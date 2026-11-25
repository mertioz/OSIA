"""Start the extracted Unix desktop and its companion server."""
import fcntl
import os
from pathlib import Path
import subprocess
import sys


def run():
    root = Path(__file__).resolve().parent
    application = root / "app"
    sys.path.insert(0, str(application))
    os.environ["PYTHONPATH"] = str(application)
    os.environ["PYTHONNOUSERSITE"] = "1"
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(root / "browsers"))
    os.environ["PATH"] = os.pathsep.join((
        str(root / "browsers" / "bin"), str(application / "runtimes" / "codex" / "bin"),
        os.environ.get("PATH", os.defpath),
    ))
    if sys.argv[1:] == ["--version"]:
        from osia.version import PRODUCT_VERSION
        print(PRODUCT_VERSION)
        return 0
    if sys.argv[1:] == ["--doctor"]:
        import runpy
        runpy.run_path(str(root / "doctor.py"), run_name="__main__")
        return 0
    if sys.argv[1:] == ["--server"]:
        from osia.server.run_server import main
        return main()

    from osia.config import CONFIG_DIR, ensure_config_files
    from osia.main import main

    Path(CONFIG_DIR).mkdir(parents=True, exist_ok=True)
    with (Path(CONFIG_DIR) / "portable-desktop.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        ensure_config_files(create_token=True)
        # The server's own port guard exits before starting transports when a
        # pre-existing installation already serves this user.
        child = subprocess.Popen([sys.executable, "-I", "-B", __file__, "--server"])
        try:
            return main()
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


if __name__ == "__main__":
    raise SystemExit(run())
