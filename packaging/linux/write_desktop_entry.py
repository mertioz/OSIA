"""Install the user launcher generated from the verified OSIA release."""
from pathlib import Path
import sys


def desktop_exec_argument(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%") + '"'


command, root, directory = map(Path, sys.argv[1:])
icon = root / "app/assets/osia_icon.png"
entry = Path(directory) / "space.osia.desktop.desktop"
content = (
    "[Desktop Entry]\n"
    "Type=Application\n"
    "Name=OSIA\n"
    "Comment=Personal AI assistant\n"
    f"Exec={desktop_exec_argument(str(command))}\n"
    f"Icon={icon}\n"
    "Terminal=false\n"
    "Categories=Utility;\n"
)
temporary = entry.with_suffix(".desktop.tmp")
temporary.write_text(content, encoding="utf-8")
temporary.replace(entry)
