"""Trader as an always-on Mac service (launchd): starts at login and starts again by itself if it stops.

    trader autostart install   write the service and start it now
    trader autostart status    is it running, and where are its logs
    trader autostart remove    stop it and take it out of login items

The service runs `caffeinate -i <trader> run`, so the Mac doesn't sleep while Trader is running.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .config import PROJECT_ROOT

LABEL = "com.trader.agent"
LOG = Path.home() / "Library" / "Logs" / "trader.log"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def trader_command() -> str:
    """The trader program in this project's own virtual environment (the one running this command)."""
    return str(Path(sys.executable).with_name("trader"))


def service_xml(project: Path = PROJECT_ROOT, command: str | None = None) -> bytes:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/caffeinate", "-i", command or trader_command(), "run"],
        "WorkingDirectory": str(project),
        "RunAtLoad": True,        # start at login
        "KeepAlive": True,        # start it again if it stops
        "ThrottleInterval": 30,   # wait 30 s before a restart, so a crash loop doesn't spin
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
    })


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install() -> str:
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(service_xml())
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", domain, str(path))  # replace an older copy, if any
    res = _launchctl("bootstrap", domain, str(path))
    if res.returncode != 0:
        res = _launchctl("load", "-w", str(path))
    return ("Trader's service is installed and running. It starts at login and restarts if it stops. "
            f"Logs: {LOG}" if res.returncode == 0 else f"launchctl said: {res.stderr.strip() or res.returncode}")


def status() -> str:
    res = _launchctl("print", f"gui/{os.getuid()}/{LABEL}")
    if res.returncode != 0:
        return "Trader's service is NOT installed. Run: trader autostart install"
    state = next((line.strip() for line in res.stdout.splitlines() if line.strip().startswith("state = ")), "state unknown")
    pid = next((line.strip() for line in res.stdout.splitlines() if line.strip().startswith("pid = ")), "")
    return f"Trader's service: {state} {pid}\nLogs: {LOG}\nLast lines:\n" + _tail(LOG, 8)


def remove() -> str:
    path = plist_path()
    _launchctl("bootout", f"gui/{os.getuid()}", str(path))
    if path.exists():
        path.unlink()
    return "Trader's service is stopped and removed."


def _tail(path: Path, n: int) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return "(no log yet)"
