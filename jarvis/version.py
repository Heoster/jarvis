"""Version metadata for JARVIS."""

from __future__ import annotations

APP_NAME = "JARVIS"
__version__ = "1.0.0"
__codename__ = "Just A Rather Very Intelligent System"
__operator__ = "Heoster"
__company__ = "CODEEX-AI"

BANNER = r"""
     ██╗ █████╗ ██████╗ ██╗   ██╗██╗███████╗
     ██║██╔══██╗██╔══██╗██║   ██║██║██╔════╝
     ██║███████║██████╔╝██║   ██║██║███████╗
██   ██║██╔══██║██╔══██╗╚██╗ ██╔╝██║╚════██║
╚█████╔╝██║  ██║██║  ██║ ╚████╔╝ ██║███████║
 ╚════╝ ╚═╝  ╚═╝╚═╝  ╚═╝  ╚═══╝  ╚═╝╚══════╝
"""

TAGLINE = "Just A Rather Very Intelligent System"


def banner(compact: bool = False) -> str:
    """Return the startup banner (ASCII art + version line)."""
    if compact:
        return f"{APP_NAME} {__version__} — {__codename__}"
    return (f"{BANNER}\n"
            f"        {APP_NAME} v{__version__} · {__codename__}\n"
            f"        built for {__operator__} ({__company__}) · free-tier only")


def version_line() -> str:
    return f"{APP_NAME} {__version__} ({__codename__}) for {__operator__}"
