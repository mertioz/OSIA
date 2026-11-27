"""Exercise the extracted launcher, server and desktop in an isolated user profile."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen


def smoke(executable: Path, check_login: bool = False) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 8765))
    version = subprocess.check_output([str(executable), "--version"], text=True).strip()
    with tempfile.TemporaryDirectory(prefix="osia-portable-qa-") as scratch:
        home = Path(scratch)
        env = {**os.environ, "HOME": str(home), "PERF_DEBUG": "1",
               "XDG_DATA_HOME": str(home / "data"), "XDG_CONFIG_HOME": str(home / "config"),
               "XDG_CACHE_HOME": str(home / "cache"), "OSIA_REDUCED_MOTION": "1"}
        mac_login = [str(executable), "--osia-login-item"]
        if sys.platform == "darwin":
            if check_login:
                # This explicit check is intended for a disposable macOS user.
                if subprocess.run([*mac_login, "state"], check=False).returncode != 1:
                    raise RuntimeError("The test user already has an OSIA login item")
            else:
                # ServiceManagement belongs to the real login session, not HOME.
                (home / ".osia").mkdir()
                (home / ".osia/settings.json").write_text("{}", encoding="utf-8")
        log = home / "launch.log"
        with log.open("wb") as stream:
            child = subprocess.Popen([str(executable)], cwd=home, env=env,
                                     stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            started = time.monotonic()
            while time.monotonic() - started < 120:
                output = log.read_text("utf-8", errors="replace")
                if child.poll() is not None:
                    raise RuntimeError(f"Portable launch exited {child.returncode}\n{output[-2400:]}")
                if "entering app.exec()" in output:
                    try:
                        with urlopen("http://127.0.0.1:8765/health", timeout=1) as response:
                            health = json.load(response)
                    except OSError:
                        time.sleep(0.25)
                        continue
                    if health.get("status") == "healthy":
                        checkout = Path(health["runtime"]["checkout_root"]).resolve()
                        package = executable.parent.parent if sys.platform == "darwin" else executable.parent
                        assert checkout.is_relative_to(package), checkout
                        if check_login and sys.platform == "darwin":
                            state = subprocess.run([*mac_login, "state"], check=False).returncode
                            if state not in (0, 3):
                                raise RuntimeError(f"Default macOS login item missing (state {state})")
                            print("macOS login item:", "enabled" if state == 0 else "awaiting system approval")
                        elif check_login and sys.platform.startswith("linux"):
                            entry = (home / "config/autostart/osia.desktop").read_text("utf-8")
                            if str(executable) not in entry or "X-GNOME-Autostart-enabled=true" not in entry:
                                raise RuntimeError("Default Linux login entry does not target this archive")
                        print(f"OSIA {version} desktop and local server ready in {time.monotonic()-started:.2f}s")
                        return
                time.sleep(0.25)
            raise RuntimeError(f"Desktop or server did not become ready\n{output[-2400:]}")
        finally:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            child.wait(timeout=15)
            if check_login and sys.platform == "darwin":
                subprocess.run([*mac_login, "disable"], check=True)
                if subprocess.run([*mac_login, "state"], check=False).returncode != 1:
                    raise RuntimeError("The test login item was not removed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("--check-login", action="store_true")
    args = parser.parse_args()
    smoke(args.executable.resolve(strict=True), args.check_login)
