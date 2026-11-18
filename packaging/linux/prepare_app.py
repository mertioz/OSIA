"""Build the Linux Python payload from the shared, tracked product."""
import compileall
from pathlib import Path
import sys

application = Path(sys.argv[1]).resolve()
product = application / "osia"
if not (product / "version.py").is_file():
    raise SystemExit("Expected a staging copy of the OSIA product.")

# Compile the same source on both platforms. QML and its supporting files
# remain alongside the compiled Python modules.
if not compileall.compile_dir(application, legacy=True, optimize=1, quiet=1,
                              stripdir=application, prependdir="OSIA/app"):
    raise SystemExit("OSIA bytecode build failed.")
for source in product.rglob("*.py"):
    if not source.with_suffix(".pyc").is_file():
        raise SystemExit(f"Compiled module missing: {source.name}")
    source.unlink()
