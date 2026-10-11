"""JARVIS — Autonomous eXecution Intelligence & Orchestration Machine.

A single-operator, CPU-first autonomous agent for **Heoster** (Harsh Saini,
CODEEX-AI) that runs entirely on free tiers and small context windows.

The three focus areas of this build are:

* **browser control**  — a real Chrome harness (Playwright → Selenium → raw CDP)
  launched from the terminal, with DOM-level commands *and* OS-level cursor
  execution.
* **screen control**   — genuine mouse/keyboard automation with smooth cursor
  movement, OCR for reading pixels, and *no vision model anywhere*.
* **memory**           — a smart, budget-aware memory manager (working /
  episodic / semantic / procedural tiers) with hybrid retrieval, dedupe,
  decay and consolidation.
"""

from .version import __version__, __codename__

__all__ = ["__version__", "__codename__", "get_config", "get_brain"]

USER_AGENT = f"JARVIS/{__version__} (+https://github.com/Heoster/jarvis)"


def get_config(*args, **kwargs):
    from .config import load_config

    return load_config(*args, **kwargs)


def get_brain(*args, **kwargs):
    from .core.brain import Brain

    return Brain(*args, **kwargs)
