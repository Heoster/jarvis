"""Desktop notifications (cross-platform, no extra dependency)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Optional

from ...utils.logger import get_logger
from ..base import tool

log = get_logger("comms.notify")

__all__ = ["notify", "NOTIFICATION_TOOLS"]


def notify(title: str, message: str = "", *, urgency: str = "normal",
           timeout: int = 6) -> tuple[bool, str]:
    """Send a desktop notification. Returns ``(ok, how)``."""
    if sys.platform == "darwin":
        script = (f'display notification "{message}" with title "{title}"')
        if _run(["osascript", "-e", script]):
            return True, "osascript"
    elif sys.platform.startswith("win"):
        script = (
            "[reflection.assembly]::loadwithpartialname('System.Windows.Forms');"
            "[reflection.assembly]::loadwithpartialname('System.Drawing');"
            "$n=New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon=[System.Drawing.SystemIcons]::Information;"
            "$n.Visible=$true;"
            f"$n.ShowBalloonTip({max(1, timeout) * 1000},"
            f"'{_ps_escape(title)}','{_ps_escape(message)}',"
            "[System.Windows.Forms.ToolTipIcon]::Info)"
        )
        if _run(["powershell", "-NoProfile", "-Command", script]):
            return True, "windows balloon"
    else:
        if shutil.which("notify-send"):
            urgency = urgency if urgency in ("low", "normal", "critical") else "normal"
            if _run(["notify-send", "-u", urgency, "-t", str(timeout * 1000),
                     title, message]):
                return True, "notify-send"
        if shutil.which("zenity"):
            if _run(["zenity", "--notification", "--text", f"{title}\n{message}"]):
                return True, "zenity"
    return False, "no notification backend (install libnotify-bin on Linux)"


def _run(argv: list[str]) -> bool:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=8)
        return result.returncode == 0
    except Exception:
        return False


def _ps_escape(text: str) -> str:
    return str(text).replace("'", "''")[:200]


@tool(category="communication", timeout=20)
def send_notification(title: str, message: str = "", urgency: str = "normal") -> str:
    """Show a desktop notification to the operator.

    Use for: "task finished", long-running job completed, something needs
    attention. Keep messages short.

    Args:
        title: Notification title
        message: Body text
        urgency: "low", "normal" or "critical" (Linux)
    """
    ok, how = notify(title, message, urgency=urgency)
    if ok:
        return f"🔔 Notified via {how}: {title}"
    return (f"Could not show a desktop notification ({how}). "
            f"Printing instead: **{title}** — {message}")


@tool(category="communication", timeout=20)
def speak(text: str) -> str:
    """Say something out loud through the system text-to-speech engine.

    Nice for hands-free use. Uses `say` (macOS), PowerShell SAPI (Windows) or
    `espeak`/`spd-say` (Linux). No network, no API.

    Args:
        text: What to say (keep it under ~300 characters)
    """
    if sys.platform == "darwin" and shutil.which("say"):
        ok = _run(["say", text[:500]])
        engine = "say"
    elif sys.platform.startswith("win"):
        ok = _run(["powershell", "-NoProfile", "-Command",
                   "Add-Type -AssemblyName System.Speech;"
                   "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                   f"$s.Speak('{_ps_escape(text)}')"])
        engine = "SAPI"
    else:
        ok, engine = False, ""
        for command in (["espeak-ng"], ["espeak"], ["spd-say"]):
            if shutil.which(command[0]):
                ok = _run(command + [text[:500]])
                engine = command[0]
                break
    if ok:
        return f"🔊 Spoken via {engine}: {text[:80]}"
    return ("No speech engine available. Linux: apt-get install espeak-ng. "
            f"Text was: {text[:200]}")


NOTIFICATION_TOOLS = [send_notification, speak]
