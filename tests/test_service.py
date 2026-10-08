"""Service tests use a fake launchctl and temporary folders: they never touch
real launchd jobs or ~/Library."""

import plistlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from reddit_notifier import db, service
from reddit_notifier.config import Config


@pytest.fixture
def cfg(tmp_path):
    return Config("fragranceswap", 10, "ua", tmp_path / "test.db", "https://ntfy.example", "topic")


@pytest.fixture
def fake_mac(tmp_path, monkeypatch):
    """Fake launchd: records launchctl calls and tracks which jobs are loaded."""
    state = SimpleNamespace(calls=[], loaded={}, others=[], port_busy=False)

    def launchctl(*args):
        state.calls.append(args)
        if args[0] == "print":
            label = args[1].split("/")[-1]
            if label in state.loaded:
                return subprocess.CompletedProcess(args, 0, state.loaded[label], "")
            return subprocess.CompletedProcess(args, 113, "", "not found")
        if args[0] == "bootstrap":
            label = plistlib.loads(Path(args[2]).read_bytes())["Label"]
            state.loaded[label] = "state = running\n\tpid = 4242\n"
        if args[0] == "bootout":
            state.loaded.pop(args[1].split("/")[-1], None)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(service, "launchctl", launchctl)
    monkeypatch.setattr(service, "LAUNCH_AGENTS", tmp_path / "LaunchAgents")
    monkeypatch.setattr(service, "LOG_DIR", tmp_path / "Logs")
    monkeypatch.setattr(service.shutil, "which", lambda name: "/opt/homebrew/bin/uv")
    monkeypatch.setattr(service, "other_copies_running", lambda: state.others)
    monkeypatch.setattr(service, "port_in_use", lambda port: state.port_busy)
    return state


def commands(state):
    return [args[0] for args in state.calls if args[0] != "print"]


def test_plist_contents(tmp_path):
    job = service.Job("poller", "run")
    plist = service.plist_for(job, "/opt/homebrew/bin/uv", Path("/proj"), Path("/proj/config.toml"))
    assert plist["Label"] == "local.reddit-notifier.poller"
    assert plist["ProgramArguments"] == [
        "/opt/homebrew/bin/uv", "run", "--project", "/proj",
        "reddit-notifier", "--config", "/proj/config.toml", "run",
    ]
    assert plist["WorkingDirectory"] == "/proj"
    assert plist["EnvironmentVariables"]["PATH"].startswith("/opt/homebrew/bin:")
    assert plist["RunAtLoad"] is True and plist["KeepAlive"] is True
    assert plist["StandardOutPath"].endswith("reddit-notifier/poller.log")


def test_start_installs_and_starts_both(cfg, fake_mac, tmp_path):
    lines = service.start(cfg, tmp_path / "config.toml")
    assert commands(fake_mac) == ["bootstrap", "bootstrap"]
    for name, command in [("poller", "run"), ("web", "web")]:
        plist = plistlib.loads((tmp_path / "LaunchAgents" / f"local.reddit-notifier.{name}.plist").read_bytes())
        assert plist["ProgramArguments"][-1] == command
    assert (tmp_path / "Logs").is_dir()
    assert "Started the poller." in lines and "Started the web." in lines


def test_start_again_restarts(cfg, fake_mac, tmp_path):
    service.start(cfg, tmp_path / "config.toml")
    fake_mac.calls.clear()
    service.start(cfg, tmp_path / "config.toml")
    assert commands(fake_mac) == ["bootout", "bootout", "bootstrap", "bootstrap"]


def test_start_refuses_when_running_in_a_terminal(cfg, fake_mac, tmp_path):
    fake_mac.others = ["poller (process 123)"]
    with pytest.raises(service.ServiceError, match="poller \\(process 123\\)"):
        service.start(cfg, tmp_path / "config.toml")
    assert "bootstrap" not in commands(fake_mac)


def test_start_refuses_when_port_busy(cfg, fake_mac, tmp_path):
    fake_mac.port_busy = True
    with pytest.raises(service.ServiceError, match="Port 5050 is in use"):
        service.start(cfg, tmp_path / "config.toml")


def test_stop_removes_jobs_so_they_stay_stopped(cfg, fake_mac, tmp_path):
    service.start(cfg, tmp_path / "config.toml")
    lines = service.stop()
    assert fake_mac.loaded == {}
    assert list((tmp_path / "LaunchAgents").iterdir()) == []  # nothing left to start at login
    assert "Stopped the poller." in lines

    assert "The poller wasn't running." in service.stop()  # stopping twice is fine


def test_status(cfg, fake_mac, tmp_path):
    conn = db.connect(cfg.db_path)
    assert service.status(cfg, conn)[:3] == [
        "poller  not running (stopped)",
        "web     not running (stopped)",
        "Last poll: none yet",
    ]

    service.start(cfg, tmp_path / "config.toml")
    db.log_poll(conn, "error", error="Rate limited by Reddit (HTTP 429)")
    lines = service.status(cfg, conn)
    assert lines[0] == "poller  running (process 4242)"
    assert lines[2] == "Last poll: just now (error: Rate limited by Reddit (HTTP 429))"


def test_status_when_job_keeps_failing(cfg, fake_mac):
    fake_mac.loaded["local.reddit-notifier.web"] = "state = not running\n\tlast exit code = 1\n"
    lines = service.status(cfg, db.connect(cfg.db_path))
    assert lines[1] == "web     installed but not running right now, last exit: 1; see the log"


def test_logs(fake_mac, tmp_path):
    (tmp_path / "Logs").mkdir()
    (tmp_path / "Logs" / "poller.log").write_text("\n".join(f"line {i}" for i in range(30)))
    lines = service.logs(n=2)
    assert lines[:3] == [f"==> {tmp_path / 'Logs' / 'poller.log'} <==", "line 28", "line 29"]
    assert lines[-1] == "(no log yet)"  # no web log


def test_other_copies_running(monkeypatch):
    ps_output = (
        "  101 -zsh\n"
        "  102 uv run reddit-notifier run\n"
        "  103 /opt/homebrew/.../Python /proj/.venv/bin/reddit-notifier run\n"
        "  104 /opt/homebrew/.../Python /proj/.venv/bin/reddit-notifier --config /proj/config.toml web\n"
        "  105 /opt/homebrew/.../Python /proj/.venv/bin/reddit-notifier service start\n"
        "  106 vim run\n"
    )
    monkeypatch.setattr(
        service.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, ps_output, "")
    )
    assert service.other_copies_running() == ["poller (process 103)", "web UI (process 104)"]


def test_requires_macos(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "linux")
    with pytest.raises(service.ServiceError, match="systemd"):
        service.stop()



# --- guard against running on the Mac and a server at once ---------------------


def test_start_refuses_when_config_names_a_server(cfg, fake_mac, tmp_path):
    cfg.server_ssh = "root@example.com"
    with pytest.raises(service.ServiceError, match="service start --here"):
        service.start(cfg, tmp_path / "config.toml")
    assert "bootstrap" not in commands(fake_mac)

    service.start(cfg, tmp_path / "config.toml", here=True)  # deliberate override
    assert commands(fake_mac) == ["bootstrap", "bootstrap"]


# --- tunnel ------------------------------------------------------------------


def test_tunnel_command():
    cfg = Config("x", 10, "ua", Path("db"), "s", "t", server_ssh="root@example.com")
    cmd = service.tunnel_command(cfg)
    assert cmd[:4] == ["/usr/bin/ssh", "-N", "-L", "5051:127.0.0.1:5050"]
    assert cmd[-1] == "root@example.com"
    assert "BatchMode=yes" not in cmd
    assert "BatchMode=yes" in service.tunnel_command(cfg, background=True)


def test_tunnel_start_installs_ssh_job(cfg, fake_mac, tmp_path):
    cfg.server_ssh = "root@example.com"
    lines = service.tunnel_start(cfg, tmp_path / "config.toml")
    plist = plistlib.loads((tmp_path / "LaunchAgents" / "local.reddit-notifier.tunnel.plist").read_bytes())
    assert plist["ProgramArguments"] == service.tunnel_command(cfg, background=True)
    assert plist["KeepAlive"] is True and plist["RunAtLoad"] is True
    assert plist["StandardOutPath"].endswith("/tunnel.log")
    assert "http://127.0.0.1:5051" in lines[1]
    # Only the tunnel: starting it must not start a local poller.
    assert list(fake_mac.loaded) == ["local.reddit-notifier.tunnel"]


def test_tunnel_start_needs_server(cfg, fake_mac, tmp_path):
    with pytest.raises(service.ServiceError, match="Set the server in config.toml"):
        service.tunnel_start(cfg, tmp_path / "config.toml")


def test_tunnel_start_refuses_when_port_busy(cfg, fake_mac, tmp_path):
    cfg.server_ssh = "root@example.com"
    fake_mac.port_busy = True
    with pytest.raises(service.ServiceError, match="Port 5051 is in use"):
        service.tunnel_start(cfg, tmp_path / "config.toml")


def test_tunnel_stop(cfg, fake_mac, tmp_path):
    cfg.server_ssh = "root@example.com"
    service.tunnel_start(cfg, tmp_path / "config.toml")
    assert service.tunnel_stop()[0] == "Stopped the tunnel."
    assert fake_mac.loaded == {}
    assert not (tmp_path / "LaunchAgents" / "local.reddit-notifier.tunnel.plist").exists()


def test_tunnel_status(cfg, fake_mac, tmp_path, monkeypatch):
    cfg.server_ssh = "root@example.com"
    service.tunnel_start(cfg, tmp_path / "config.toml")
    monkeypatch.setattr(service, "web_ui_reachable", lambda port: True)
    assert service.tunnel_status(cfg) == [
        "tunnel  running (process 4242)",
        "The server's web UI is reachable at http://127.0.0.1:5051",
    ]
    monkeypatch.setattr(service, "web_ui_reachable", lambda port: False)
    assert "isn't reachable" in service.tunnel_status(cfg)[1]
