"""Turn pip install reports into a hash-checked Linux dependency list."""
import json
from pathlib import Path
import re
import sys
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import urlopen

allowed_hosts = {
    "files.pythonhosted.org", "download.pytorch.org", "download-r2.pytorch.org", "github.com",
}
packages = {}
for report_path in sys.argv[1:]:
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    for entry in report["install"]:
        name = entry["metadata"]["name"]
        identifier = re.sub(r"[-_.]+", "-", name).lower()
        download = entry["download_info"]
        address = urlsplit(download["url"])
        if address.scheme != "https" or address.hostname not in allowed_hosts or address.username or address.password:
            raise SystemExit(f"Unverified dependency source: {name}")
        url = urlunsplit(address._replace(fragment=""))
        digest = download["archive_info"].get("hashes", {}).get("sha256", "")
        if not digest and address.hostname == "files.pythonhosted.org":
            # Some indexes omit the hash for a wheel mirrored from PyPI.
            version = entry["metadata"]["version"]
            with urlopen(f"https://pypi.org/pypi/{quote(name)}/{quote(version)}/json", timeout=15) as response:
                metadata = json.load(response)
            digest = next(
                (file["digests"]["sha256"] for file in metadata["urls"] if file["url"] == url),
                "",
            )
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SystemExit(f"Unverified dependency checksum: {name}")
        packages[identifier] = f"{name} @ {url} --hash=sha256:{digest}"
for name in sorted(packages):
    print(packages[name])
