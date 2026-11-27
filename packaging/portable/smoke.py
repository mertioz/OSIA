"""Exercise the extracted OSIA desktop, companion server and login integration."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen
from uuid import uuid4


def wait_ready(executable: Path, child=None, log: Path | None = None) -> None:
    started = time.monotonic()
    package = executable.parent.parent if sys.platform == "darwin" else executable.parent
    while time.monotonic() - started < 120:
        if child is not None and child.poll() is not None:
            raise RuntimeError(f"Desktop exited with status {child.returncode}")
        if log is not None and ("entering app.exec()" not in log.read_text("utf-8", errors="replace")):
            time.sleep(0.25)
            continue
        try:
            with urlopen("http://127.0.0.1:8765/health", timeout=1) as response:
                state = json.load(response)
        except OSError:
            time.sleep(0.25)
            continue
        if state.get("status") == "healthy":
            assert Path(state["runtime"]["checkout_root"]).resolve().is_relative_to(package), state["status"]
            print(f"{executable.name} desktop and local server ready in {time.monotonic()-started:.2f}s", flush=True)
            return
        time.sleep(0.25)
    tail = log.read_text("utf-8", errors="replace")[-2000:] if log is not None else ""
    raise RuntimeError(f"Desktop or server did not become ready\n{tail}")


def check_mac_login(home: Path, executable: Path) -> None:
    entry = home / "Library/LaunchAgents/space.osia.desktop.login.plist"
    subprocess.run(["/usr/bin/plutil", "-lint", str(entry)], check=True)
    job = plistlib.loads(entry.read_bytes())
    assert job["ProgramArguments"] == [str(executable)] and job["RunAtLoad"]
    assert job["LimitLoadToSessionType"] == "Aqua" and "KeepAlive" not in job
    # Exercise the generated executable and session conditions with a unique
    # job label and profile. Never register or unload a real user's OSIA job.
    job["Label"] += ".qa." + uuid4().hex
    job["EnvironmentVariables"] = {"HOME": str(home), "PERF_DEBUG": "1", "OSIA_REDUCED_MOTION": "1"}
    log = home / "launchd.log"
    log.touch()
    job["StandardOutPath"] = str(log)
    job["StandardErrorPath"] = str(log)
    fixture = home / "login-probe.plist"
    fixture.write_bytes(plistlib.dumps(job))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["/bin/launchctl", "bootstrap", domain, str(fixture)], check=True)
    try:
        wait_ready(executable, log=log)
        print("macOS launchd started the extracted desktop and server at login", flush=True)
    finally:
        subprocess.run(["/bin/launchctl", "bootout", f'{domain}/{job["Label"]}'], check=True)


def smoke(executable: Path, check_login: bool = False) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 8765))
    version = subprocess.check_output([str(executable), "--version"], text=True).strip()
    with tempfile.TemporaryDirectory(prefix="osia-portable-qa-") as scratch:
        home = Path(scratch)
        env = {**os.environ, "HOME": str(home), "PERF_DEBUG": "1",
               "XDG_DATA_HOME": str(home / "data"), "XDG_CONFIG_HOME": str(home / "config"),
               "XDG_CACHE_HOME": str(home / "cache"), "OSIA_REDUCED_MOTION": "1"}
        log = home / "launch.log"
        with log.open("wb") as stream:
            child = subprocess.Popen([str(executable)], cwd=home, env=env,
                                     stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            wait_ready(executable, child, log)
            if check_login and sys.platform.startswith("linux"):
                entry = (home / "config/autostart/osia.desktop").read_text("utf-8")
                if str(executable) not in entry or "X-GNOME-Autostart-enabled=true" not in entry:
                    raise RuntimeError("Default Linux login entry does not target this archive")
        finally:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            child.wait(timeout=15)
        if check_login and sys.platform == "darwin":
            check_mac_login(home, executable)
        print(f"OSIA {version} portable verification passed", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("--check-login", action="store_true")
    args = parser.parse_args()
    smoke(args.executable.resolve(strict=True), args.check_login)
