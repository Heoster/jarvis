"""Token counting with a tiktoken fast path and a dependency-free fallback.

Why both?  The whole point of JARVIS is running on machines that have nothing
installed.  ``tiktoken`` needs to download its BPE file on first use, which
breaks the "zero cost, offline-capable" promise, so we treat it as an optional
accelerator and otherwise use a calibrated estimator.
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable, List, Optional, Sequence

__all__ = ["TokenCounter", "count_tokens", "estimate_tokens"]

_WORD_RE = re.compile(r"\w+|[^\w\s]")
_CJK_RE = re.compile(r"[\u3000-\u9fff\uff00-\uffef]")


def estimate_tokens(text: str) -> int:
    """Heuristic token estimate good to ~±10% for English/code text.

    Blends three cheap signals: characters/4, words*1.3, and punctuation count,
    then adds a premium for CJK and for long unbroken runs (base64, minified JS)
    which tokenise far worse than prose.
    """
    if not text:
        return 0
    chars = len(text)
    words = len(_WORD_RE.findall(text))
    punctuation = sum(1 for ch in text if not ch.isalnum() and not ch.isspace())
    newlines = text.count("\n")

    base = math.ceil(chars / 4.0)
    word_est = math.ceil(words * 1.3)
    est = max(base, word_est)
    est += punctuation // 6
    est += newlines // 8

    cjk = len(_CJK_RE.findall(text))
    est += cjk  # CJK is roughly 1 token per char

    longest_run = max((len(m) for m in re.findall(r"\S+", text)), default=0)
    if longest_run > 80:
        est += (longest_run - 80) // 2
    return max(1, est)


class TokenCounter:
    """Counts tokens for text and for LLM message lists."""

    MESSAGE_OVERHEAD = 4
    NAME_OVERHEAD = 2
    REPLY_PRIMER = 3

    def __init__(self, model: str = "gpt-4o-mini", encoding: Optional[str] = None):
        self.model = model
        self.encoding_name = encoding or "cl100k_base"
        self._encoder: Any = None
        self._encoder_ready = False

    # ── encoder plumbing ───────────────────────────────────────────────────
    @property
    def encoder(self) -> Any:
        if not self._encoder_ready:
            self._encoder_ready = True
            try:  # pragma: no cover - depends on optional dep / network
                import tiktoken  # type: ignore

                try:
                    self._encoder = tiktoken.encoding_for_model(self.model)
                except Exception:
                    self._encoder = tiktoken.get_encoding(self.encoding_name)
            except Exception:
                self._encoder = None
        return self._encoder

    @property
    def exact(self) -> bool:
        return self.encoder is not None

    # ── counting ───────────────────────────────────────────────────────────
    def count(self, text: str) -> int:
        if not text:
            return 0
        enc = self.encoder
        if enc is not None:
            try:
                return len(enc.encode(text))
            except Exception:
                pass
        return estimate_tokens(text)

    def count_many(self, texts: Iterable[str]) -> int:
        return sum(self.count(t) for t in texts)

    def count_message(self, message: Any) -> int:
        content = getattr(message, "content", message)
        if isinstance(content, list):
            text = " ".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        else:
            text = "" if content is None else str(content)
        total = self.count(text) + self.MESSAGE_OVERHEAD
        if getattr(message, "name", None):
            total += self.NAME_OVERHEAD
        for call in getattr(message, "tool_calls", None) or []:
            args = call.get("args", call.get("arguments", "")) if isinstance(call, dict) else ""
            total += self.count(str(call.get("name", "") if isinstance(call, dict) else "")) + 2
            if not isinstance(args, str):
                args = repr(args)
            total += self.count(args) + 6
        return total

    def count_messages(self, messages: Sequence[Any]) -> int:
        return sum(self.count_message(m) for m in messages) + self.REPLY_PRIMER

    def fits(self, messages: Sequence[Any], budget: int) -> bool:
        return self.count_messages(messages) <= budget

    def truncate(self, text: str, max_tokens: int, *, marker: str = "\n[…truncated]") -> str:
        """Trim ``text`` to at most ``max_tokens`` (approximate when inexact)."""
        if max_tokens <= 0:
            return ""
        if self.count(text) <= max_tokens:
            return text
        enc = self.encoder
        if enc is not None:
            try:
                tokens = enc.encode(text)[: max(1, max_tokens - 6)]
                return enc.decode(tokens) + marker
            except Exception:
                pass
        # char-based estimate, then verified once
        approx_chars = max(16, int(max_tokens * 3.6))
        candidate = text[:approx_chars] + marker
        while self.count(candidate) > max_tokens and approx_chars > 32:
            approx_chars = int(approx_chars * 0.85)
            candidate = text[:approx_chars] + marker
        return candidate

    def head_tail(self, text: str, max_tokens: int, *, head_ratio: float = 0.7) -> str:
        """Keep the start *and* the end of a long blob (best for logs/HTML)."""
        if self.count(text) <= max_tokens:
            return text
        head_tokens = int(max_tokens * head_ratio)
        tail_tokens = max(1, max_tokens - head_tokens - 8)
        head = self.truncate(text, head_tokens, marker="")
        tail = self._tail(text, tail_tokens)
        return f"{head}\n[…{max(0, self.count(text) - max_tokens)} tokens omitted…]\n{tail}"

    def _tail(self, text: str, max_tokens: int) -> str:
        enc = self.encoder
        if enc is not None:
            try:
                return enc.decode(enc.encode(text)[-max_tokens:])
            except Exception:
                pass
        return text[-int(max_tokens * 3.6):]


_DEFAULT = TokenCounter()


def count_tokens(text: str, model: str = "gpt-4o-mini") -> int:
    global _DEFAULT
    if model != _DEFAULT.model:
        _DEFAULT = TokenCounter(model)
    return _DEFAULT.count(text)
