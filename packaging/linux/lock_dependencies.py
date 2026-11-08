"""Turn pip install reports into a hash-checked Linux dependency list."""
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit, urlunsplit

allowed_hosts = {"files.pythonhosted.org", "download.pytorch.org", "github.com"}
packages = {}
for report_path in sys.argv[1:]:
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    for entry in report["install"]:
        name = entry["metadata"]["name"]
        identifier = re.sub(r"[-_.]+", "-", name).lower()
        download = entry["download_info"]
        address = urlsplit(download["url"])
        digest = download["archive_info"]["hashes"]["sha256"]
        if (address.scheme != "https" or address.hostname not in allowed_hosts
                or address.username or address.password or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise SystemExit(f"Unverified dependency source: {name}")
        url = urlunsplit(address._replace(fragment=""))
        packages[identifier] = f"{name} @ {url} --hash=sha256:{digest}"
for name in sorted(packages):
    print(packages[name])
