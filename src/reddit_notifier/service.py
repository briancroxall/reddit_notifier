"""Run the poller and web UI in the background on macOS, using launchd.

`reddit-notifier service start` writes two small "LaunchAgent" files to
~/Library/LaunchAgents/ and asks launchd (macOS's built-in service manager)
to run them. launchd then starts them again at every login and restarts them
if they crash. `service stop` stops them and removes those files, so nothing
starts again until the next `service start`.

`reddit-notifier tunnel start` does the same for an SSH tunnel to a server
running the notifier, so its web UI is always at http://127.0.0.1:5051.

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
import urllib.request
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
    name: str  # "poller", "web", or "tunnel"
    command: str = ""  # the reddit-notifier subcommand it runs, if any

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
TUNNEL = Job("tunnel")
SSH = "/usr/bin/ssh"


def launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def launch_agent(job: Job, program_args: list[str], working_dir: Path, path_env: str) -> dict:
    """LaunchAgent settings shared by every job."""
    return {
        "Label": job.label,
        "ProgramArguments": program_args,
        "WorkingDirectory": str(working_dir),
        # launchd starts programs with a bare-bones PATH.
        "EnvironmentVariables": {"PATH": path_env, "PYTHONUNBUFFERED": "1"},
        "RunAtLoad": True,  # start at login
        "KeepAlive": True,  # restart if it stops
        "ThrottleInterval": 60,  # but at most once a minute, if it keeps failing
        "StandardOutPath": str(job.log_path),
        "StandardErrorPath": str(job.log_path),
    }


def plist_for(job: Job, uv: str, project_dir: Path, config_path: Path) -> dict:
    """The LaunchAgent settings for the poller or web UI."""
    args = [
        uv, "run", "--project", str(project_dir),
        "reddit-notifier", "--config", str(config_path), job.command,
    ]
    return launch_agent(job, args, project_dir, f"{Path(uv).parent}:/usr/bin:/bin:/usr/sbin:/sbin")


def tunnel_command(cfg: Config, background: bool = False) -> list[str]:
    """ssh command that makes the server's web UI appear on this computer."""
    cmd = [
        SSH, "-N",  # -N: forward the port only; don't open a shell
        "-L", f"{cfg.tunnel_port}:127.0.0.1:{cfg.web_port}",
        "-o", "ExitOnForwardFailure=yes",  # fail loudly if the local port is taken
        # Check the connection every 30s; give up after 3 misses (e.g. after
        # sleep or a Wi-Fi change) so a fresh connection can be made.
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
    ]
    if background:
        cmd += ["-o", "BatchMode=yes"]  # never wait for a password prompt nobody will see
    return cmd + [cfg.server_ssh]


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


def install(job: Job, plist: dict) -> None:
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with job.plist_path.open("wb") as f:
        plistlib.dump(plist, f)
    result = launchctl("bootstrap", launchd_domain(), str(job.plist_path))
    if result.returncode != 0:
        raise ServiceError(f"launchd wouldn't start the {job.name}: {result.stderr.strip()}")


def remove(job: Job) -> str:
    """Stop a job and delete its LaunchAgent, so it doesn't start at login."""
    was_loaded = is_loaded(job)
    unload(job)
    job.plist_path.unlink(missing_ok=True)
    return f"Stopped the {job.name}." if was_loaded else f"The {job.name} wasn't running."


def start(cfg: Config, config_path: Path, here: bool = False) -> list[str]:
    """Install and start both jobs (restarting them if already running).
    Returns lines to show the user."""
    require_macos()
    if cfg.server_ssh and not here:
        raise ServiceError(
            f"config.toml says the notifier runs on a server ({cfg.server_ssh}).\n"
            "Running it here too would check Reddit twice and could send duplicate alerts.\n"
            "If you've stopped it on the server, start it here with:\n"
            "  reddit-notifier service start --here"
        )
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

    lines = []
    for job in JOBS:
        install(job, plist_for(job, uv, project_dir, config_path))
        lines.append(f"Started the {job.name}.")
    lines.append(f"Web UI: http://127.0.0.1:{cfg.web_port}  ·  Logs: {LOG_DIR}")
    lines.append("They'll start again at every login until you run: reddit-notifier service stop")
    return lines


def stop() -> list[str]:
    require_macos()
    lines = [remove(job) for job in JOBS]
    lines.append("They won't start again until you run: reddit-notifier service start")
    return lines


def job_state(job: Job) -> str:
    result = launchctl("print", f"{launchd_domain()}/{job.label}")
    if result.returncode != 0:
        return "not running (stopped)"
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


def logs(n: int = 20, jobs: list[Job] = JOBS) -> list[str]:
    lines = []
    for job in jobs:
        lines.append(f"==> {job.log_path} <==")
        if job.log_path.exists():
            lines.extend(job.log_path.read_text(errors="replace").splitlines()[-n:])
        else:
            lines.append("(no log yet)")
    return lines


# --- tunnel to a server --------------------------------------------------------


def require_server(cfg: Config) -> None:
    if not cfg.server_ssh:
        raise ServiceError(
            "Set the server in config.toml first, e.g.:\n"
            "  [server]\n"
            '  ssh = "root@your-server.example.com"'
        )


def tunnel_start(cfg: Config, config_path: Path) -> list[str]:
    require_macos()
    require_server(cfg)
    unload(TUNNEL)  # restart if already running
    if port_in_use(cfg.tunnel_port):
        raise ServiceError(
            f"Port {cfg.tunnel_port} is in use, maybe by a `reddit-notifier tunnel`"
            " open in a terminal. Close that (Ctrl-C) and try again."
        )
    install(TUNNEL, launch_agent(
        TUNNEL, tunnel_command(cfg, background=True), config_path.resolve().parent,
        "/usr/bin:/bin:/usr/sbin:/sbin",
    ))
    return [
        f"Started the tunnel to {cfg.server_ssh}.",
        f"The server's web UI: http://127.0.0.1:{cfg.tunnel_port}"
        " (allow a few seconds to connect).",
        "It reconnects by itself after sleep or network changes, and starts again at"
        " every login until you run: reddit-notifier tunnel stop",
    ]


def tunnel_stop() -> list[str]:
    require_macos()
    return [remove(TUNNEL), "It won't start again until you run: reddit-notifier tunnel start"]


def web_ui_reachable(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            return response.status == 200
    except OSError:
        return False


def tunnel_status(cfg: Config) -> list[str]:
    require_macos()
    lines = [f"tunnel  {job_state(TUNNEL)}"]
    if web_ui_reachable(cfg.tunnel_port):
        lines.append(f"The server's web UI is reachable at http://127.0.0.1:{cfg.tunnel_port}")
    else:
        lines.append(
            f"The server's web UI isn't reachable at http://127.0.0.1:{cfg.tunnel_port} right now."
            " If the tunnel is running, it may be reconnecting; see: reddit-notifier tunnel logs"
        )
    return lines
