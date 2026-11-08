"""Build the Linux Python payload from the shared, tracked product."""
import compileall
from pathlib import Path
import re
import sys

application = Path(sys.argv[1]).resolve()
product = application / "osia"
if not (product / "version.py").is_file():
    raise SystemExit("Expected a staging copy of the OSIA product.")

# PySide uses the same Qt API. Translate imports at packaging time so the
# Linux installer uses Qt for Python under the LGPL and the Windows build
# keeps its established binding.
for source in product.rglob("*.py"):
    code = source.read_text(encoding="utf-8")
    code = code.replace("from PyQt6 import sip", "from osia.linux_qt import sip")
    code = re.sub(r"\bPyQt6\.(QtCore|QtGui|QtWidgets)\b", r"PySide6.\1", code)
    code = re.sub(r"\bpyqtSignal\b", "Signal", code)
    source.write_text(code, encoding="utf-8")
(product / "linux_qt.py").write_text(
    "from types import SimpleNamespace\n"
    "from shiboken6 import isValid\n\n"
    "def is_deleted(widget):\n"
    "    return not isValid(widget)\n\n"
    "sip = SimpleNamespace(isdeleted=is_deleted)\n",
    encoding="utf-8",
)
if not compileall.compile_dir(application, legacy=True, optimize=1, quiet=1,
                              stripdir=application, prependdir="OSIA/app"):
    raise SystemExit("OSIA bytecode build failed.")
for source in product.rglob("*.py"):
    if not source.with_suffix(".pyc").is_file():
        raise SystemExit(f"Compiled module missing: {source.name}")
    source.unlink()
