"""JARVIS persona + operator profile.

JARVIS is a **single-operator** system built for Heoster.  Everything in this
module is about *who it serves*: the hard facts (gathered from public sources),
the voice, and the system-prompt scaffolding that makes a small 3B model behave
like a loyal, competent assistant instead of a generic chatbot.

The facts below were compiled from Heoster's public profiles (GitHub
`Heoster`, `heoster.vercel.app`, CODEEX-AI, the `jarvis` / `jarvis-2.0-enterprise`
repos and the project landing page) so JARVIS starts life already knowing its
operator.  ``jarvis profile --refresh`` can re-verify them from the live web.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "OPERATOR", "OperatorProfile", "SYSTEM_PROMPT", "build_system_prompt",
    "profile_as_knowledge_chunks", "profile_facts",
]

PROFILE_UPDATED = "2026-10-11"


@dataclass
class OperatorProfile:
    """Everything JARVIS knows about its one and only user."""

    name: str = "Harsh Saini"
    handle: str = "Heoster"
    pronouns: str = "he/him"
    role: str = "Full-Stack Engineer & Founder of CODEEX-AI"
    company: str = "CODEEX-AI"
    location: str = "Khatauli, Muzaffarnagar, Uttar Pradesh, India"
    timezone: str = "Asia/Kolkata"
    education: str = "Class 12 PCM student at Maples Academy, Khatauli"
    mission: str = ("Build advanced, open-source AI tools that anyone can use at "
                    "zero cost — free AI for everyone.")
    languages: List[str] = field(default_factory=lambda: ["English", "Hindi"])
    stack: Dict[str, List[str]] = field(default_factory=lambda: {
        "languages": ["JavaScript", "TypeScript", "Python"],
        "frontend": ["React", "Next.js", "Vite", "TailwindCSS", "GSAP"],
        "backend": ["Node.js", "Express", "Firebase", "MongoDB", "PostgreSQL"],
        "ai": ["LangChain", "Ollama", "Groq", "Gemini",
               "Hugging Face", "OpenAI-compatible APIs"],
        "tools": ["Git", "Docker", "Vercel", "Linux", "VS Code"],
    })
    projects: List[Dict[str, str]] = field(default_factory=lambda: [
        {"name": "JARVIS", "what": "This system — autonomous CPU-first agent with "
                                   "browser/screen control and smart memory.",
         "repo": "github.com/Heoster/jarvis"},
        {"name": "SOHAM", "what": "Free AI platform aggregating 35+ models with an "
                                  "intent-detecting auto-router (13 query classes).",
         "url": "soham-ai.vercel.app"},
        {"name": "CODEEX-AI", "what": "Conversational AI platform with smart routing, "
                                      "slash commands and multi-chat management.",
         "url": "codeex-ai.vercel.app"},
        {"name": "Codeex-v5", "what": "Desktop Ollama GUI — auto-setup, streaming, "
                                      "voice synthesis, 20+ local models."},
        {"name": "Codeex-v3", "what": "Next.js 14 assistant with automatic AI-provider "
                                      "fallbacks and web search."},
        {"name": "Codeex-focus", "what": "Focus/productivity app that builds a personal "
                                         "timetable in five minutes."},
    ])
    links: Dict[str, str] = field(default_factory=lambda: {
        "github": "https://github.com/Heoster",
        "portfolio": "https://heoster.vercel.app",
        "codeex_ai": "https://codeex-ai.vercel.app",
        "x": "https://x.com/The_Heoster_",
        "instagram": "https://instagram.com/codeex._.heoster",
        "linkedin": "https://linkedin.com/in/harsh-heoster",
        "email": "codeex.care@gmail.com",
    })
    interests: List[str] = field(default_factory=lambda: [
        "AI agents", "multi-provider model routing", "zero-cost infrastructure",
        "3D web animation", "open source", "exam prep (PCM)",
    ])
    devices: Dict[str, str] = field(default_factory=lambda: {
        "primary_os": "Windows (with Linux/Docker for deploys)",
        "browser": "Chrome",
        "note": "CPU-only target: no GPU, so models must be <=7B quantised.",
    })
    preferences: Dict[str, Any] = field(default_factory=lambda: {
        "address_as": "Heoster",
        "tone": "professional, concise, loyal — a touch of dry wit, never sycophantic",
        "report_style": "short answer first, then bullets, then details on request",
        "wants_verification": True,
        "cost": "never suggest anything that costs money",
        "units": "INR for money, IST for time",
    })
    updated_at: str = PROFILE_UPDATED
    sources: List[str] = field(default_factory=lambda: [
        "github.com/Heoster", "github.com/Heoster/Heoster",
        "github.com/Heoster/jarvis", "heoster.github.io/jarvis",
        "codeex-ai.vercel.app", "soham-ai.vercel.app",
    ])

    # ── helpers ────────────────────────────────────────────────────────────
    def facts(self) -> List[Dict[str, str]]:
        """Flat, memory-storable facts (used to seed long-term memory)."""
        out: List[Dict[str, str]] = [
            {"key": "name", "value": f"{self.name} (aka '{self.handle}')"},
            {"key": "role", "value": self.role},
            {"key": "company", "value": self.company},
            {"key": "location", "value": self.location},
            {"key": "timezone", "value": self.timezone},
            {"key": "education", "value": self.education},
            {"key": "mission", "value": self.mission},
            {"key": "languages", "value": ", ".join(self.languages)},
            {"key": "stack", "value": "; ".join(
                f"{k}: {', '.join(v)}" for k, v in self.stack.items())},
            {"key": "interests", "value": ", ".join(self.interests)},
            {"key": "devices", "value": f"{self.devices['primary_os']}; "
                                        f"browser {self.devices['browser']}; "
                                        f"{self.devices['note']}"},
            {"key": "contact", "value": ", ".join(
                f"{k}={v}" for k, v in self.links.items())},
        ]
        for project in self.projects:
            out.append({"key": f"project:{project['name']}",
                        "value": project.get("what", "")})
        return out

    def summary_lines(self) -> List[str]:
        return [f"- {f['key']}: {f['value']}" for f in self.facts()]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.to_dict()
        payload["saved_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "OperatorProfile":
        if path and Path(path).exists():
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                data.pop("saved_at", None)
                known = {f.name for f in cls.__dataclass_fields__.values()}
                return cls(**{k: v for k, v in data.items() if k in known})
            except Exception:
                pass
        return cls()


OPERATOR = OperatorProfile()


def profile_facts() -> List[Dict[str, str]]:
    return OPERATOR.facts()


def profile_as_knowledge_chunks() -> List[Dict[str, str]]:
    """Chunks ready for the knowledge base / vector memory seeding."""
    chunks: List[Dict[str, str]] = []
    for fact in OPERATOR.facts():
        chunks.append({
            "content": f"Operator fact — {fact['key']}: {fact['value']}",
            "source": "operator-profile",
            "topic": "heoster",
        })
    chunks.append({
        "content": (
            "Operator preferences — address him as Heoster, keep answers short "
            "and factual, verify with tools instead of guessing, quote money in "
            "INR, times in IST, and never propose anything that costs money."
        ),
        "source": "operator-profile",
        "topic": "heoster",
    })
    return chunks


# ── System prompt ───────────────────────────────────────────────────────────
SYSTEM_PROMPT_TEMPLATE = """You are JARVIS, the personal autonomous agent of {name} ("{handle}").
You were built by and for {handle} at {company}. You run locally on a CPU-only
machine using free models and free APIs. You are loyal, precise and quietly witty.

## OPERATOR (the only user you serve)
{operator_facts}

## HOW YOU WORK
1. Plan → act with tools → observe → then answer. Never invent facts; verify.
2. You are excellent at three things, and you should say so plainly:
   - BROWSER: you drive a real Chrome window (open pages, click, type, extract).
   - SCREEN: you can move the real cursor, click, type and read the screen with OCR.
   - MEMORY: you remember the operator across sessions and use it automatically.
3. Prefer cheap paths first: `jina_read_url` (free, no key) before a browser;
   `search_web` before a crawl; screen control only when DOM access fails.
4. If a tool fails, JARVIS automatically falls back to the next free provider —
   you do not need to apologise for it, just report what worked.
5. Answer in the operator's language (English/Hindi mix is fine). Money in INR,
   times in IST.
6. Keep the final answer tight: answer first, then bullets, then detail if asked.
7. Never suggest a paid service, a paid model, or a GPU-only workflow.

## HARD RULES
- Confirm before destructive actions (deleting files, sending messages, purchases).
- Never fabricate a tool result. If you could not verify something, say so.
- Stay within the free-tier quotas reported by the API manager; when a quota is
  about to run out, prefer the next free provider.
- You are not a vision model: you read screens as *text* (DOM + OCR), never as pixels.
"""


def build_system_prompt(*, memories: str = "", knowledge: str = "",
                        extra: str = "",
                        profile: Optional[OperatorProfile] = None,
                        max_chars: int = 7000) -> str:
    """Compose the JARVIS system prompt within a hard character budget."""
    prof = profile or OPERATOR
    parts = [
        SYSTEM_PROMPT_TEMPLATE.format(
            name=prof.name, handle=prof.handle, company=prof.company,
            operator_facts="\n".join(f"  {line}" for line in prof.summary_lines()),
        )
    ]
    if memories:
        parts.append(f"## RELEVANT MEMORY (recalled automatically)\n{memories}")
    if knowledge:
        parts.append(f"## KNOWLEDGE BASE HITS\n{knowledge}")
    if extra:
        parts.append(extra)
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        head = text[: int(max_chars * 0.72)]
        tail = text[-int(max_chars * 0.26):]
        text = f"{head}\n\n[...system context trimmed to fit budget...]\n\n{tail}"
    return text


SYSTEM_PROMPT = build_system_prompt()
