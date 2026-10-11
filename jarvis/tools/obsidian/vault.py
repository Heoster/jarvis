"""Obsidian vault access — the file layer behind every Obsidian tool.

An Obsidian vault is just a folder of markdown files plus a ``.obsidian``
settings directory, so everything here is plain, dependency-light filesystem
work: no Obsidian process, no plugin, no sync service, no account.  That makes
it perfect for JARVIS's free/offline-first rules — the notes stay yours, on
disk, readable by any editor.

Concepts implemented here:

* **vault discovery** — ``OBSIDIAN_VAULT`` → ``obsidian.vault_path`` → common
  locations, with a clear error that explains how to point JARVIS at a vault.
* **frontmatter** — YAML between ``---`` fences, preserved on write.
* **wikilinks & tags** — ``[[Note]]``, ``[[Note|alias]]``, ``#tag/subtag``.
* **tasks** — ``- [ ]`` items with ``📅 due``, priorities, tags.
* **daily notes** — folder + date format, template expansion.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

from ...config import dump_yaml, parse_yaml_text
from ...config import get as cfg_get
from ...config import load_config
from ...utils.logger import get_logger
from ...utils.sanitizer import truncate

log = get_logger("tools.obsidian")

#: folders that are never part of the note set
IGNORED_DIRS = {".obsidian", ".trash", ".git", ".smart-env", ".space",
                "node_modules", ".venv", "__pycache__", "attachments_cache"}

WIKILINK_RE = re.compile(r"\[\[([^\]\|#]+)(?:#[^\]\|]+)?(?:\|([^\]]+))?\]\]")
INLINE_TAG_RE = re.compile(r"(?<![\w/])#([A-Za-z][\w/\-]{0,60})")
TASK_RE = re.compile(r"^\s*[-*+]\s+\[([ xX/\-])]\s+(.*)$")
DUE_RE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})|@due\((\d{4}-\d{2}-\d{2})\)|due::\s*(\d{4}-\d{2}-\d{2})")
PRIORITY_RE = re.compile(r"([⏫🔼🔽⏬]|priority::\s*(high|medium|low))")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.M)


class ObsidianError(RuntimeError):
    """Raised when the vault cannot be located or a note cannot be resolved."""


# ── vault discovery ─────────────────────────────────────────────────────────
def _candidate_vaults() -> List[Path]:
    home = Path.home()
    docs = home / "Documents"
    return [
        home / "Obsidian",
        home / "ObsidianVault",
        docs / "Obsidian",
        docs / "ObsidianVault",
        docs / "Obsidian Vault",
        docs / "vault",
        home / "vault",
        home / "notes" / "obsidian",
    ]


def resolve_vault(config: Optional[Mapping[str, Any]] = None, *,
                  explicit: str = "", required: bool = True) -> Optional[Path]:
    """Find the vault: explicit → ``OBSIDIAN_VAULT`` → config → common places."""
    if config is None:
        config = load_config()
    raw = (explicit or os.environ.get("OBSIDIAN_VAULT", "")
           or str(cfg_get(config, "obsidian.vault_path", "") or "")).strip()
    candidates: List[Path] = []
    if raw:
        candidates.append(Path(raw).expanduser())
    else:
        candidates.extend(_candidate_vaults())
    for path in candidates:
        if path.is_dir():
            return path.resolve()
    if not required:
        return None
    raise ObsidianError(
        "No Obsidian vault configured. Point JARVIS at one with either:\n"
        "  • export OBSIDIAN_VAULT=\"/path/to/MyVault\"\n"
        "  • or set obsidian.vault_path in config.yaml\n"
        f"(looked at: {', '.join(str(c) for c in candidates) or 'nothing'})"
    )


# ── note model ──────────────────────────────────────────────────────────────
@dataclass
class Task:
    note: str
    line: int
    text: str
    done: bool = False
    due: str = ""
    priority: str = ""
    tags: List[str] = field(default_factory=list)

    def line_text(self) -> str:
        box = "x" if self.done else " "
        due = f" 📅 {self.due}" if self.due else ""
        return f"- [{box}] {self.text}{due}"


@dataclass
class Note:
    path: Path
    rel: str
    title: str
    raw: str
    frontmatter: Dict[str, Any] = field(default_factory=dict)
    body: str = ""
    links: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    tasks: List[Task] = field(default_factory=list)
    mtime: float = 0.0
    size: int = 0

    @property
    def word_count(self) -> int:
        return len(self.body.split())

    def headings(self) -> List[str]:
        return [match.group(2).strip() for match in HEADING_RE.finditer(self.body)]

    def field(self, name: str, default: Any = "") -> Any:
        return self.frontmatter.get(name, default)

    def preview(self, max_chars: int = 220) -> str:
        text = " ".join(self.body.split())
        return truncate(text or "(empty note)", max_chars)

    def metadata_line(self) -> str:
        bits = [f"{self.word_count} words"]
        if self.tags:
            bits.append("tags: " + ", ".join(f"#{t}" for t in self.tags[:5]))
        if self.tasks:
            open_tasks = sum(1 for task in self.tasks if not task.done)
            bits.append(f"{open_tasks} open task(s)")
        if self.links:
            bits.append(f"{len(self.links)} link(s)")
        return " · ".join(bits)


def split_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Return ``(frontmatter, body)``; tolerant of missing/odd YAML."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            end = index
            break
    if end is None:
        return {}, text
    block = "\n".join(lines[1:end])
    try:
        data = parse_yaml_text(block) or {}
    except Exception as exc:  # a broken frontmatter must not hide the note
        log.debug(f"frontmatter parse failed ({exc}); treating as text")
        return {}, text
    if not isinstance(data, Mapping):
        return {}, text
    return dict(data), "\n".join(lines[end + 1:]).lstrip("\n")


def render_frontmatter(data: Mapping[str, Any]) -> str:
    if not data:
        return ""
    dumped = dump_yaml(data)
    return f"---\n{dumped}\n---\n"


def normalise_tags(values: Iterable[Any]) -> List[str]:
    out: List[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip().lstrip("#")
        for tag in re.split(r"[,\s]+", text):
            tag = tag.strip()
            if tag and tag not in out:
                out.append(tag)
    return out


def parse_note(path: Path, vault: Path) -> Note:
    raw = path.read_text(encoding="utf-8", errors="replace")
    frontmatter, body = split_frontmatter(raw)
    tags = normalise_tags(list(frontmatter.get("tags") or [])
                          + list(frontmatter.get("tag") or []))
    for match in INLINE_TAG_RE.finditer(body):
        tag = match.group(1)
        if not tag.isdigit() and tag not in tags:
            tags.append(tag)
    links = []
    for match in WIKILINK_RE.finditer(raw):
        target = match.group(1).strip()
        if target and target not in links:
            links.append(target)
    for match in re.finditer(r"\[[^\]]+\]\(([^)]+\.md)\)", body):
        target = Path(match.group(1)).stem
        if target and target not in links:
            links.append(target)
    title = str(frontmatter.get("title") or "").strip()
    if not title:
        first_heading = HEADING_RE.search(body)
        title = (first_heading.group(2).strip() if first_heading else path.stem)
    stat = path.stat()
    return Note(path=path, rel=path.relative_to(vault).as_posix(), title=title, raw=raw,
                frontmatter=frontmatter, body=body, links=links, tags=tags,
                tasks=parse_tasks(body, path.relative_to(vault).as_posix()),
                mtime=stat.st_mtime, size=stat.st_size)


def parse_tasks(body: str, note_rel: str) -> List[Task]:
    tasks: List[Task] = []
    for number, line in enumerate(body.splitlines(), 1):
        match = TASK_RE.match(line)
        if not match:
            continue
        marker, rest = match.group(1), match.group(2).strip()
        due = ""
        due_match = DUE_RE.search(rest)
        if due_match:
            due = next(group for group in due_match.groups() if group) or ""
            rest = DUE_RE.sub("", rest).strip()
        priority = ""
        priority_match = PRIORITY_RE.search(rest)
        if priority_match:
            priority = priority_match.group(1) or priority_match.group(2) or ""
            rest = PRIORITY_RE.sub("", rest).strip()
        tags = [m.group(1) for m in INLINE_TAG_RE.finditer(rest)]
        cleaned = INLINE_TAG_RE.sub("", rest).strip() or rest
        tasks.append(Task(note=note_rel, line=number, text=cleaned, done=marker in "xX",
                          due=due, priority=priority, tags=tags))
    return tasks


# ── walking the vault ───────────────────────────────────────────────────────
def iter_notes(vault: Path, *, folder: str = "", limit: int = 0) -> Iterator[Path]:
    root = (vault / folder).resolve() if folder else vault
    if not str(root).startswith(str(vault)):
        raise ObsidianError(f"folder '{folder}' escapes the vault")
    count = 0
    for path in sorted(root.rglob("*.md")):
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        if path.name.startswith("."):
            continue
        yield path
        count += 1
        if limit and count >= limit:
            return


def load_notes(vault: Path, *, folder: str = "", limit: int = 0) -> List[Note]:
    notes: List[Note] = []
    for path in iter_notes(vault, folder=folder, limit=limit):
        try:
            notes.append(parse_note(path, vault))
        except Exception as exc:  # one bad file must not break the vault
            log.debug(f"skipping unreadable note {path}: {exc}")
    return notes


def vault_index(vault: Path) -> Dict[str, Path]:
    """Map every reasonable *name* for a note to its path (for resolution)."""
    index: Dict[str, Path] = {}
    for path in iter_notes(vault):
        rel = path.relative_to(vault).as_posix()
        for key in (rel, rel[:-3], path.stem):
            index.setdefault(key.lower(), path)
    return index


def resolve_note(vault: Path, ref: str, *, create: bool = False,
                 folder: str = "") -> Path:
    """Turn a note reference into a path.

    Accepts ``[[Note]]``, ``Note.md``, ``Folder/Note.md``, ``Folder/Note`` or a
    bare title.  With ``create=True`` a missing note is placed in ``folder``
    (or the vault root).
    """
    if not ref or not ref.strip():
        raise ObsidianError("no note reference given")
    cleaned = ref.strip()
    if cleaned.startswith("[[") and cleaned.endswith("]]"):
        cleaned = cleaned[2:-2]
    cleaned = cleaned.split("|")[0].split("#")[0].strip()
    if not cleaned:
        raise ObsidianError(f"cannot resolve note reference {ref!r}")

    candidate = (vault / cleaned).resolve()
    if str(candidate).startswith(str(vault)):
        if candidate.is_file():
            return candidate
        if candidate.suffix != ".md" and candidate.with_suffix(".md").is_file():
            return candidate.with_suffix(".md")

    target = cleaned if cleaned.lower().endswith(".md") else f"{cleaned}.md"
    found = vault_index(vault).get(target.lower())
    if found:
        return found
    found = vault_index(vault).get(Path(target).stem.lower())
    if found:
        return found

    if create:
        # an explicit path ("Subjects/Physics/Note") already carries its folder —
        # only a bare note name is placed inside `folder`
        use_folder = folder and "/" not in target and "\\" not in target
        destination = (vault / folder / target) if use_folder else (vault / target)
        destination = destination.resolve()
        if not str(destination).startswith(str(vault)):
            raise ObsidianError("refusing to write outside the vault")
        destination.parent.mkdir(parents=True, exist_ok=True)
        return destination
    raise ObsidianError(
        f"note not found: {ref!r}. Use obsidian_search or obsidian_list_notes "
        "to find the right name, or pass create=True when writing.")


# ── reading / writing ───────────────────────────────────────────────────────
def read_note(vault: Path, ref: str) -> Note:
    return parse_note(resolve_note(vault, ref), vault)


def write_note(vault: Path, ref: str, content: str = "", *,
               mode: str = "write", frontmatter: Optional[Mapping[str, Any]] = None,
               tags: Sequence[Any] = (), title: str = "", folder: str = "") -> Note:
    """Create/update a note; ``mode`` is ``write`` | ``append`` | ``prepend``."""
    path = resolve_note(vault, ref, create=True, folder=folder)
    mode = (mode or "write").lower()
    new_tags = normalise_tags(tags)
    exists = path.exists()
    if not exists and mode in ("append", "prepend"):
        mode = "write"

    if exists:
        current = path.read_text(encoding="utf-8", errors="replace")
        meta, body = split_frontmatter(current)
    else:
        meta, body = {}, ""

    if frontmatter:
        meta.update(dict(frontmatter))
    if title:
        meta["title"] = title
    if new_tags:
        meta["tags"] = normalise_tags(list(meta.get("tags") or []) + new_tags)
    if not exists and not meta:
        meta = {"created": datetime.now().isoformat(timespec="seconds"),
                "tags": new_tags or []}
        if not meta["tags"]:
            meta.pop("tags")
    meta.setdefault("updated", datetime.now().isoformat(timespec="seconds"))

    if mode == "append":
        body = f"{body.rstrip()}\n\n{content}".strip() + "\n"
    elif mode == "prepend":
        body = f"{content}\n\n{body.lstrip()}".strip() + "\n"
    else:
        body = content if content.endswith("\n") else content + "\n"

    path.write_text(render_frontmatter(meta) + body, encoding="utf-8")
    return parse_note(path, vault)


def append_under_heading(vault: Path, ref: str, heading: str, content: str,
                         *, create_heading: bool = True) -> Note:
    """Append text under an existing ``## heading`` (used by study tooling)."""
    note = read_note(vault, ref)
    meta, body = split_frontmatter(note.raw)
    lines = body.splitlines()
    target_index = None
    level = 2
    for index, line in enumerate(lines):
        match = HEADING_RE.match(line)
        if match and match.group(2).strip().lower() == heading.strip().lower():
            target_index = index
            level = len(match.group(1))
            break
    block = content.strip()
    if target_index is None:
        if not create_heading:
            raise ObsidianError(f"heading '{heading}' not found in {note.rel}")
        body = f"{body.rstrip()}\n\n{'#' * level} {heading}\n{block}\n"
    else:
        insert_at = len(lines)
        for index in range(target_index + 1, len(lines)):
            match = HEADING_RE.match(lines[index])
            if match and len(match.group(1)) <= level:
                insert_at = index
                break
        lines[insert_at:insert_at] = ["", block]
        body = "\n".join(lines).rstrip() + "\n"
    note.path.write_text(render_frontmatter(meta) + body, encoding="utf-8")
    return parse_note(note.path, vault)


def unique_note_path(vault: Path, folder: str, stem: str) -> Path:
    """``Note.md``, ``Note 2.md``, ``Note 3.md`` … never overwrite by accident."""
    base = (vault / folder / stem).resolve() if folder else (vault / stem).resolve()
    candidate = base.with_suffix(".md")
    counter = 1
    while candidate.exists():
        counter += 1
        candidate = base.with_name(f"{stem} {counter}").with_suffix(".md")
    return candidate


# ── searching ───────────────────────────────────────────────────────────────
@dataclass
class SearchHit:
    note: str
    title: str
    line: int
    text: str
    score: float = 1.0

    def line_text(self) -> str:
        return f"{self.note}:{self.line} — {truncate(self.text.strip(), 160)}"


def search_vault(vault: Path, query: str, *, limit: int = 20, folder: str = "",
                 regex: bool = False, context: int = 1,
                 case_sensitive: bool = False) -> List[SearchHit]:
    """Full-text search.  ``#tag`` queries match tags, plain text matches body."""
    query = (query or "").strip()
    if not query:
        return []
    hits: List[SearchHit] = []

    if query.startswith("#"):
        wanted = query.lstrip("#").lower()
        for note in load_notes(vault, folder=folder):
            if any(tag.lower() == wanted or tag.lower().startswith(wanted + "/")
                   for tag in note.tags):
                hits.append(SearchHit(note=note.rel, title=note.title,
                                      line=1, text=note.preview(160)))
            if len(hits) >= limit:
                break
        return hits

    pattern: Optional[re.Pattern[str]] = None
    needle = query
    if regex:
        try:
            pattern = re.compile(query, 0 if case_sensitive else re.IGNORECASE)
        except re.error as exc:
            raise ObsidianError(f"invalid regex {query!r}: {exc}") from exc
    elif not case_sensitive:
        needle = query.lower()

    for note in load_notes(vault, folder=folder):
        lines = note.body.splitlines()
        for index, line in enumerate(lines):
            matched = bool(pattern.search(line)) if pattern else (needle in line.lower()
                                                                   if not case_sensitive
                                                                   else needle in line)
            if not matched:
                continue
            snippet_lines = lines[max(0, index - context): index + context + 1]
            hits.append(SearchHit(note=note.rel, title=note.title, line=index + 1,
                                  text="\n".join(snippet_lines)))
            break  # one hit per note keeps the result list readable
        if len(hits) >= limit:
            break
    return hits


# ── links ───────────────────────────────────────────────────────────────────
def links_for(vault: Path, ref: str) -> Dict[str, Any]:
    note = read_note(vault, ref)
    index = vault_index(vault)
    known = {Path(path).stem.lower() for path in index.values()}
    backlinks: List[str] = []
    for other in load_notes(vault):
        if other.rel == note.rel:
            continue
        if any(link.lower() == note.path.stem.lower()
               or Path(link).stem.lower() == note.path.stem.lower()
               for link in other.links):
            backlinks.append(other.rel)
    broken = [link for link in note.links if link.lower() not in known
              and Path(link).stem.lower() not in known]
    return {"note": note.rel, "outgoing": note.links, "backlinks": backlinks,
            "broken": broken}


def orphan_notes(vault: Path, *, limit: int = 40) -> List[str]:
    notes = load_notes(vault)
    if not notes:
        return []
    incoming: Dict[str, int] = {note.path.stem.lower(): 0 for note in notes}
    for note in notes:
        for link in note.links:
            key = Path(link).stem.lower()
            if key in incoming:
                incoming[key] += 1
    return [note.rel for note in notes
            if incoming.get(note.path.stem.lower(), 0) == 0][:limit]


# ── tasks ───────────────────────────────────────────────────────────────────
def all_tasks(vault: Path, *, include_done: bool = False, due_before: str = "",
              folder: str = "") -> List[Task]:
    collected: List[Task] = []
    for note in load_notes(vault, folder=folder):
        for task in note.tasks:
            if task.done and not include_done:
                continue
            if due_before and task.due and task.due > due_before:
                continue
            collected.append(task)
    collected.sort(key=lambda task: (task.due or "9999-99-99", task.note))
    return collected


def toggle_task(vault: Path, note_ref: str, line: int, *, done: Optional[bool] = None) -> Task:
    note = read_note(vault, note_ref)
    meta, body = split_frontmatter(note.raw)
    lines = body.splitlines()
    if line < 1 or line > len(lines):
        raise ObsidianError(f"{note.rel} has no line {line}")
    match = TASK_RE.match(lines[line - 1])
    if not match:
        raise ObsidianError(f"line {line} of {note.rel} is not a task")
    marker = match.group(1)
    new_state = (marker not in "xX") if done is None else bool(done)
    lines[line - 1] = TASK_RE.sub(
        lambda m: f"{m.group(0)[:m.start(1) - m.start(0)]}{'x' if new_state else ' '}"
                  f"{m.group(0)[m.end(1) - m.start(0):]}",
        lines[line - 1])
    note.path.write_text(render_frontmatter(meta) + "\n".join(lines) + "\n",
                         encoding="utf-8")
    updated = parse_note(note.path, vault)
    return next((task for task in updated.tasks if task.line == line),
                Task(note=updated.rel, line=line, text=match.group(2), done=new_state))


# ── daily notes ─────────────────────────────────────────────────────────────
def daily_settings(config: Optional[Mapping[str, Any]] = None) -> Tuple[str, str]:
    config = config or load_config()
    folder = str(cfg_get(config, "obsidian.daily_notes.folder", "Daily Notes") or "")
    fmt = str(cfg_get(config, "obsidian.daily_notes.format", "%Y-%m-%d") or "%Y-%m-%d")
    return folder, fmt


def expand_template(text: str, *, title: str, when: date) -> str:
    expanded = text.replace("{{title}}", title)
    expanded = re.sub(r"\{\{date(?::([^}]+))?\}\}",
                      lambda m: when.strftime(m.group(1) or "%Y-%m-%d"), expanded)
    expanded = expanded.replace("{{time}}", datetime.now().strftime("%H:%M"))
    return expanded


def upsert_section(vault: Path, ref: str, heading: str, block: str, *,
                   level: int = 3, after: str = "") -> Note:
    """Write ``block`` under ``heading``, *replacing* any previous version.

    ``append_under_heading`` always inserts, so calling it twice duplicates the
    section.  Reports (the study brief, weekly reviews) are regenerated often,
    so they replace their section instead: the old block runs from the heading
    up to the next heading of the same or higher level.
    """
    note = read_note(vault, ref)
    meta, body = split_frontmatter(note.raw)
    lines = body.splitlines()
    wanted = heading.strip().lower()
    new_block = block.strip().splitlines()

    start = next((index for index, line in enumerate(lines)
                  if (match := HEADING_RE.match(line))
                  and match.group(2).strip().lower() == wanted), None)
    if start is not None:
        target_level = len(HEADING_RE.match(lines[start]).group(1))
        end = len(lines)
        for index in range(start + 1, len(lines)):
            match = HEADING_RE.match(lines[index])
            if match and len(match.group(1)) <= target_level:
                end = index
                break
        lines[start:end] = new_block + [""]
    else:
        insert_at = len(lines)
        if after:
            wanted_anchor = after.strip().lower()
            section = next((index for index, line in enumerate(lines)
                            if (match := HEADING_RE.match(line))
                            and match.group(2).strip().lower() == wanted_anchor), None)
            if section is not None:
                anchor_level = len(HEADING_RE.match(lines[section]).group(1))
                insert_at = len(lines)
                for index in range(section + 1, len(lines)):
                    match = HEADING_RE.match(lines[index])
                    if match and len(match.group(1)) <= anchor_level:
                        insert_at = index
                        break
        lines[insert_at:insert_at] = ["", ""] + new_block + [""]

    note.path.write_text(render_frontmatter(meta) + "\n".join(lines).rstrip("\n") + "\n",
                         encoding="utf-8")
    return parse_note(note.path, vault)


def daily_note(vault: Path, *, day: Optional[date] = None, create: bool = True,
               config: Optional[Mapping[str, Any]] = None,
               extra: str = "") -> Note:
    config = config or load_config()
    day = day or date.today()
    folder, fmt = daily_settings(config)
    name = day.strftime(fmt)
    path = (vault / folder / f"{name}.md") if folder else (vault / f"{name}.md")
    if not path.exists():
        if not create:
            raise ObsidianError(f"no daily note for {name} ({path})")
        template_path = str(cfg_get(config, "obsidian.daily_notes.template", "") or "")
        body = ""
        if template_path:
            candidate = (vault / template_path)
            if candidate.is_file():
                body = expand_template(candidate.read_text(encoding="utf-8"),
                                       title=name, when=day)
        if not body:
            body = (f"# {name}\n\n"
                    f"## Focus\n\n- [ ] \n\n"
                    f"## Classes / lectures\n\n\n"
                    f"## Study log\n\n"
                    f"## Revision\n\n"
                    f"## Notes & doubts\n")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    note = parse_note(path, vault)
    if extra:
        note = append_under_heading(vault, note.rel, "Study log", extra)
    return note


VaultNotConfigured = ObsidianError
