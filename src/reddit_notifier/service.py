"""Run the poller and web UI in the background on macOS, using launchd.

`reddit-notifier service start` writes two small "LaunchAgent" files to
~/Library/LaunchAgents/ and asks launchd (macOS's built-in service manager)
to run them. launchd then starts them again at every login and restarts them
if they crash. `service stop` stops them and removes those files, so nothing
starts again until the next `service start`.

On Linux (a server or a Raspberry Pi), use the systemd files in deploy/ instead.
"""

import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import db
from .config import Config

LABEL_PREFIX = "local.reddit-notifier"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
LOG_DIR = Path.home() / "Library" / "Logs" / "reddit-notifier"


class ServiceError(Exception):
    pass


@dataclass
class Job:
    name: str  # "poller" or "web"
    command: str  # the reddit-notifier subcommand it runs

    @property
    def label(self) -> str:
        return f"{LABEL_PREFIX}.{self.name}"

    @property
    def plist_path(self) -> Path:
        return LAUNCH_AGENTS / f"{self.label}.plist"

    @property
    def log_path(self) -> Path:
        return LOG_DIR / f"{self.name}.log"


JOBS = [Job("poller", "run"), Job("web", "web")]


def launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def plist_for(job: Job, uv: str, project_dir: Path, config_path: Path) -> dict:
    """The LaunchAgent settings for one job."""
    return {
        "Label": job.label,
        "ProgramArguments": [
            uv, "run", "--project", str(project_dir),
            "reddit-notifier", "--config", str(config_path), job.command,
        ],
        "WorkingDirectory": str(project_dir),
        # launchd starts programs with a bare-bones PATH; give uv its folder.
        "EnvironmentVariables": {
            "PATH": f"{Path(uv).parent}:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUNBUFFERED": "1",
        },
        "RunAtLoad": True,  # start at login
        "KeepAlive": True,  # restart if it stops
        "ThrottleInterval": 60,  # but at most once a minute, if it keeps failing
        "StandardOutPath": str(job.log_path),
        "StandardErrorPath": str(job.log_path),
    }


# --- checks before starting -------------------------------------------------


def other_copies_running() -> list[str]:
    """reddit-notifier `run` or `web` processes not started by launchd,
    e.g. still open in a terminal tab. Two pollers would double-poll Reddit."""
    ps = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True)
    found = []
    for line in ps.stdout.splitlines():
        pid, command = line.strip().split(None, 1)
        # Match the Python process, not its `uv run` wrapper, so each counts once.
        if int(pid) != os.getpid() and re.search(r"bin/reddit-notifier\b.* (run|web)$", command):
            found.append(f"{'poller' if command.endswith(' run') else 'web UI'} (process {pid})")
    return found


def port_in_use(port: int, wait_seconds: float = 5) -> bool:
    """True if something is still listening after waiting a little (a web UI
    we just stopped can take a moment to let go of the port)."""
    deadline = time.monotonic() + wait_seconds
    while True:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return False
        if time.monotonic() >= deadline:
            return True
        time.sleep(0.25)


# --- commands ------------------------------------------------------------------


def require_macos() -> None:
    if sys.platform != "darwin":
        raise ServiceError(
            "`service` uses macOS's launchd. On Linux, use the systemd files in deploy/."
        )


def is_loaded(job: Job) -> bool:
    return launchctl("print", f"{launchd_domain()}/{job.label}").returncode == 0


def unload(job: Job) -> None:
    if is_loaded(job):
        launchctl("bootout", f"{launchd_domain()}/{job.label}")


def start(cfg: Config, config_path: Path) -> list[str]:
    """Install and start both jobs (restarting them if already running).
    Returns lines to show the user."""
    require_macos()
    uv = shutil.which("uv")
    if uv is None:
        raise ServiceError("Couldn't find uv on your PATH.")
    config_path = config_path.resolve()
    project_dir = config_path.parent

    for job in JOBS:  # stop ours first, so a restart picks up code changes
        unload(job)
    if others := other_copies_running():
        raise ServiceError(
            "Already running outside the service: " + ", ".join(others) + ".\n"
            "Stop those first (Ctrl-C in their terminal tabs), then try again."
        )
    if port_in_use(cfg.web_port):
        raise ServiceError(
            f"Port {cfg.web_port} is in use by another program."
            " Stop it, or set web_port in config.toml."
        )

    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    lines = []
    for job in JOBS:
        with job.plist_path.open("wb") as f:
            plistlib.dump(plist_for(job, uv, project_dir, config_path), f)
        result = launchctl("bootstrap", launchd_domain(), str(job.plist_path))
        if result.returncode != 0:
            raise ServiceError(f"launchd wouldn't start the {job.name}: {result.stderr.strip()}")
        lines.append(f"Started the {job.name}.")
    lines.append(f"Web UI: http://127.0.0.1:{cfg.web_port}  ·  Logs: {LOG_DIR}")
    lines.append("They'll start again at every login until you run: reddit-notifier service stop")
    return lines


def stop() -> list[str]:
    require_macos()
    lines = []
    for job in JOBS:
        was_loaded = is_loaded(job)
        unload(job)
        job.plist_path.unlink(missing_ok=True)
        lines.append(f"Stopped the {job.name}." if was_loaded else f"The {job.name} wasn't running.")
    lines.append("They won't start again until you run: reddit-notifier service start")
    return lines


def job_state(job: Job) -> str:
    result = launchctl("print", f"{launchd_domain()}/{job.label}")
    if result.returncode != 0:
        return "not running (service stopped)"
    pid = re.search(r"\bpid = (\d+)", result.stdout)
    if pid:
        return f"running (process {pid.group(1)})"
    last_exit = re.search(r"last exit code = (.+)", result.stdout)
    detail = f", last exit: {last_exit.group(1).strip()}" if last_exit else ""
    return f"installed but not running right now{detail}; see the log"


def status(cfg: Config, conn) -> list[str]:
    require_macos()
    lines = [f"{job.name:7} {job_state(job)}" for job in JOBS]
    last = db.last_poll(conn)
    if last:
        from .web import timeago  # keeps one definition of "x min ago"

        error = f": {last['error']}" if last["error"] else ""
        lines.append(f"Last poll: {timeago(last['ran_at'])} ({last['status']}{error})")
    else:
        lines.append("Last poll: none yet")
    lines.append(f"Logs: {LOG_DIR}")
    return lines


def logs(n: int = 20) -> list[str]:
    lines = []
    for job in JOBS:
        lines.append(f"==> {job.log_path} <==")
        if job.log_path.exists():
            lines.extend(job.log_path.read_text(errors="replace").splitlines()[-n:])
        else:
            lines.append("(no log yet)")
    return lines
