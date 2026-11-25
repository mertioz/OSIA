"""Exercise the extracted launcher, server and desktop in an isolated user profile."""
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


def smoke(executable: Path) -> None:
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


if __name__ == "__main__":
    smoke(Path(sys.argv[1]).resolve(strict=True))
