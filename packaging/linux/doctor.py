"""Check the installed Linux runtime without modifying the user's data."""
from importlib import import_module
import shutil
import sys

from osia.version import PRODUCT_VERSION

failures = []
for name in ("fastapi", "aiortc", "av", "cryptography", "mcp", "PIL.Image",
             "PySide6.QtGui", "playwright.async_api", "onnxruntime", "faster_whisper", "torch"):
    try:
        import_module(name)
    except (ImportError, OSError) as error:
        failures.append(f"{name}: {error}")

missing_tools = [name for name in ("ffmpeg", "xdotool", "xclip") if shutil.which(name) is None]
print(f"OSIA {PRODUCT_VERSION} / Python {sys.version.split()[0]}")
if missing_tools:
    print("Optional system tools missing: " + ", ".join(missing_tools))
for error in failures:
    print(error, file=sys.stderr)
raise SystemExit(bool(failures))
