"""Skill library — the procedural tier ("how to do it next time").

When JARVIS solves a multi-step task (open Chrome → search → click result →
extract), it records the recipe.  Next time a similar request arrives the recipe
is recalled, turned into a *plan hint* in the system prompt, and the agent skips
the exploratory turns.  Skills are plain JSON files under ``skills/`` so Heoster
can hand-edit or share them.

A skill can also be **promoted** to a deterministic macro: if every step is a
tool call with fixed arguments, JARVIS can replay it without the LLM.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from ..utils.logger import get_logger
from ..utils.sanitizer import sanitize_filename, truncate
from .long_term import LongTermMemory, MemoryTier

log = get_logger("skills")

#: the repo's ``skills/`` folder: ships the built-in workflows
BUILTIN_DIR = Path(__file__).resolve().parents[2] / "skills"

__all__ = ["Skill", "SkillStep", "SkillLibrary"]


@dataclass
class SkillStep:
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    note: str = ""

    @property
    def deterministic(self) -> bool:
        """A step is replayable if it has no template placeholders."""
        return not any(isinstance(v, str) and "{{" in v for v in self.args.values())


@dataclass
class Skill:
    name: str
    description: str = ""
    trigger: str = ""                       # when the operator asks for this
    steps: List[SkillStep] = field(default_factory=list)
    id: str = ""
    uses: int = 0
    successes: int = 0
    failures: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    tags: List[str] = field(default_factory=list)
    source: str = "learned"

    @property
    def success_rate(self) -> float:
        attempts = max(1, self.successes + self.failures)
        return round(self.successes / attempts, 3)

    def as_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["steps"] = [asdict(step) for step in self.steps]
        data["success_rate"] = self.success_rate
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Skill":
        steps = [SkillStep(tool=str(s.get("tool", "")),
                           args=dict(s.get("args") or {}),
                           note=str(s.get("note", "")))
                 for s in data.get("steps") or []]
        return cls(
            name=str(data.get("name", "skill")),
            description=str(data.get("description", "")),
            trigger=str(data.get("trigger", "")),
            steps=steps,
            id=str(data.get("id") or uuid.uuid4().hex[:12]),
            uses=int(data.get("uses", 0)),
            successes=int(data.get("successes", 0)),
            failures=int(data.get("failures", 0)),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
            tags=list(data.get("tags") or []),
            source=str(data.get("source", "learned")),
        )

    def outline(self, limit: int = 6) -> str:
        lines = []
        for i, step in enumerate(self.steps[:limit], 1):
            if step.tool:
                lines.append(f"{i}. {step.tool}({_short_args(step.args)})"
                             + (f" — {truncate(step.note, 90)}" if step.note else ""))
            else:  # a judgement step: no tool, just what to do with the results
                lines.append(f"{i}. (no tool) {truncate(step.note, 120)}")
        return "\n".join(lines)

    def is_macro(self) -> bool:
        return bool(self.steps) and all(step.tool and step.deterministic
                                        for step in self.steps)


def _short_args(args: Mapping[str, Any], limit: int = 60) -> str:
    try:
        text = ", ".join(f"{k}={truncate(str(v), 24)}" for k, v in args.items())
    except Exception:
        text = ""
    return truncate(text, limit)


class SkillLibrary:
    """Loads, learns and recalls procedural knowledge."""

    def __init__(self, config: Optional[Mapping[str, Any]] = None,
                 memory: Optional[LongTermMemory] = None):
        cfg = dict(config or {})
        if "memory" in cfg and isinstance(cfg["memory"], Mapping):
            cfg = dict(cfg["memory"])
        skills_cfg = dict(cfg.get("skills") or {})
        self.directory = Path(skills_cfg.get("directory")
                              or (Path(__file__).resolve().parents[2] / "skills"))
        self.auto_load = bool(skills_cfg.get("auto_load", True))
        self.directory.mkdir(parents=True, exist_ok=True)
        self.memory = memory
        self.skills: Dict[str, Skill] = {}
        if self.auto_load:
            self.load_all()

    # ── persistence ────────────────────────────────────────────────────────
    def _path_for(self, name: str) -> Path:
        return self.directory / f"{sanitize_filename(name.lower().replace(' ', '_'))}.json"

    def load_all(self) -> int:
        """Load built-in workflows (shipped in the repo) and then learned skills.

        Learned skills load last, so a learned skill with the same name overrides a
        built-in one.  The built-in folder is always read, even when the operator
        points ``skills.directory`` somewhere else.
        """
        count = 0
        folders = [BUILTIN_DIR]
        if self.directory.resolve() != BUILTIN_DIR.resolve():
            folders.append(self.directory)
        paths = [path for folder in folders for path in sorted(folder.glob("*.json"))]
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning(f"cannot load skill {path.name}: {exc}")
                continue
            # a file holds one skill (learned) or a list of them (built-in workflows)
            for item in data if isinstance(data, list) else [data]:
                try:
                    skill = Skill.from_dict(item)
                    if not skill.id:
                        skill.id = path.stem
                    self.skills[skill.name.lower()] = skill
                    count += 1
                except Exception as exc:
                    log.warning(f"cannot load skill in {path.name}: {exc}")
        if count:
            log.debug(f"loaded {count} skill(s)")
        return count

    def save(self, skill: Skill) -> Path:
        skill.updated_at = time.time()
        path = self._path_for(skill.name)
        path.write_text(json.dumps(skill.as_dict(), indent=2), encoding="utf-8")
        self.skills[skill.name.lower()] = skill
        return path

    # ── learning ───────────────────────────────────────────────────────────
    def record(self, name: str, steps: Sequence[Mapping[str, Any]], *,
               description: str = "", trigger: str = "",
               tags: Optional[Sequence[str]] = None,
               success: bool = True) -> Skill:
        """Create or update a skill from an observed tool sequence."""
        key = name.lower()
        parsed_steps = [SkillStep(tool=str(s.get("tool") or s.get("name") or ""),
                                  args=dict(s.get("args") or {}),
                                  note=str(s.get("note", "")))
                        for s in steps if (s.get("tool") or s.get("name"))]
        existing = self.skills.get(key)
        if existing:
            existing.steps = parsed_steps or existing.steps
            existing.description = description or existing.description
            existing.trigger = trigger or existing.trigger
            existing.uses += 1
            existing.successes += int(bool(success))
            existing.failures += int(not success)
            for tag in tags or []:
                if tag not in existing.tags:
                    existing.tags.append(tag)
            skill = existing
        else:
            skill = Skill(name=name, description=description, trigger=trigger,
                          steps=parsed_steps, tags=list(tags or []))
            skill.uses = 1
            skill.successes = int(bool(success))
            skill.failures = int(not success)
            skill.id = uuid.uuid4().hex[:12]
        self.save(skill)
        if self.memory is not None and len(skill.steps) >= 2:
            self.memory.store_procedure(
                skill.name, [f"{s.tool}({_short_args(s.args)})" for s in skill.steps],
                when_to_use=skill.trigger or skill.description,
                importance=min(0.95, 0.6 + 0.1 * len(skill.steps)))
        log.info(f"skill '{skill.name}' recorded ({len(skill.steps)} steps, "
                 f"rate {skill.success_rate})")
        return skill

    def learn_from_trace(self, user_request: str,
                         tool_calls: Sequence[Mapping[str, Any]], *,
                         success: bool = True) -> Optional[Skill]:
        """Auto-learn when a task needed 3+ tool calls (worth remembering)."""
        if len(tool_calls) < 3:
            return None
        name = _name_from_request(user_request)
        steps = [{"tool": call.get("name") or call.get("tool"),
                  "args": call.get("args") or {}} for call in tool_calls]
        return self.record(name, steps, description=truncate(user_request, 160),
                           trigger=truncate(user_request, 120),
                           tags=["auto-learned"], success=success)

    def feedback(self, name: str, *, success: bool) -> None:
        skill = self.skills.get(name.lower())
        if not skill:
            return
        skill.successes += int(success)
        skill.failures += int(not success)
        self.save(skill)

    # ── recall ─────────────────────────────────────────────────────────────
    def match(self, request: str, *, threshold: float = 0.34,
              limit: int = 3) -> List[Skill]:
        """Cheap lexical match (no embedding cost) over trigger + name + tags."""
        tokens = set(re.findall(r"[a-z0-9_]+", request.lower()))
        if not tokens:
            return []
        scored: List[tuple[float, Skill]] = []
        for skill in self.skills.values():
            haystack = " ".join([skill.name.replace("_", " "), skill.trigger, skill.description,
                                 " ".join(skill.tags)]).lower()
            words = set(re.findall(r"[a-z0-9_]+", haystack))
            if not words:
                continue
            overlap = len(tokens & words)
            score = overlap / max(4, len(tokens))
            if skill.success_rate > 0.7 and skill.uses > 2:
                score *= 1.15
            if score >= threshold:
                scored.append((score, skill))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [skill for _score, skill in scored[:limit]]

    def get(self, name: str) -> Optional[Skill]:
        return self.skills.get(name.lower())

    def hints_for(self, request: str, *, limit: int = 2) -> str:
        """A compact block for the system prompt."""
        matches = self.match(request, limit=limit)
        if not matches:
            return ""
        parts = ["## LEARNED SKILLS THAT FIT THIS REQUEST"]
        for skill in matches:
            parts.append(f"* {skill.name} (used {skill.uses}×, "
                         f"{int(skill.success_rate * 100)}% success) — "
                         f"{truncate(skill.description, 120)}\n"
                         f"{skill.outline(8)}")
        return "\n".join(parts)

    def list(self, limit: int = 50) -> List[Skill]:
        return sorted(self.skills.values(), key=lambda s: s.uses, reverse=True)[:limit]

    def delete(self, name: str) -> bool:
        skill = self.skills.pop(name.lower(), None)
        if not skill:
            return False
        path = self._path_for(skill.name)
        if path.exists():
            path.unlink()
        return True

    def stats(self) -> Dict[str, Any]:
        return {
            "directory": str(self.directory),
            "skills": len(self.skills),
            "macros": sum(1 for s in self.skills.values() if s.is_macro()),
            "total_uses": sum(s.uses for s in self.skills.values()),
            "top": [{"name": s.name, "uses": s.uses,
                     "success_rate": s.success_rate}
                    for s in self.list(5)],
        }


def _name_from_request(request: str) -> str:
    words = re.findall(r"[a-zA-Z0-9]+", request.lower())[:5]
    base = "_".join(words) or "task"
    return base[:60]
