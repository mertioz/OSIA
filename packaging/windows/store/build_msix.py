"""Pack a checked OSIA Windows onedir build for Microsoft Partner Center."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from PIL import Image

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
NS = {"p": "http://schemas.microsoft.com/appx/manifest/foundation/windows10"}


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def sdk_files():
    kits = Path(os.environ["ProgramFiles(x86)"]) / "Windows Kits/10"
    versions = sorted((p for p in (kits / "bin").iterdir() if p.is_dir() and p.name.startswith("10.")),
                      key=lambda p: tuple(map(int, p.name.split("."))), reverse=True)
    for version in versions:
        makeappx = version / "x64/makeappx.exe"
        metadata = kits / "UnionMetadata" / version.name / "Windows.winmd"
        if makeappx.is_file() and metadata.is_file():
            return makeappx, metadata
    raise FileNotFoundError("Install the x64 Windows 10/11 SDK packaging tools.")


def build_helper(directory: Path, metadata: Path) -> Path:
    framework = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319"
    references = Path(os.environ["ProgramFiles(x86)"]) / "Reference Assemblies/Microsoft/Framework/.NETFramework"
    facade = next(p for p in sorted(references.glob("v4.*/Facades/System.Runtime.dll"), reverse=True)
                  if p.parent.parent.name != "v4.X")
    helper = directory / "OSIA.StoreStartup.exe"
    subprocess.run([str(framework / "csc.exe"), "/nologo", "/target:winexe", "/optimize+", "/platform:x64",
                    f"/reference:{metadata}", f"/reference:{framework / 'System.Runtime.WindowsRuntime.dll'}",
                    f"/reference:{facade}", f"/out:{helper}", str(HERE / "StartupTask.cs")], check=True)
    return helper


def verified_payload(bundle: Path):
    inventory = bundle / "_internal/legal/BUNDLE_FILES.json"
    files = {p.relative_to(bundle).as_posix(): p for p in bundle.rglob("*") if p.is_file()}
    records = json.loads(inventory.read_bytes())
    names = {record["path"] for record in records}
    if len(names) != len(records) or set(files) != names | {inventory.relative_to(bundle).as_posix()}:
        raise ValueError("Frozen bundle inventory does not match the input files")
    for entry in records:
        path = files[entry["path"]]
        if not path.resolve().is_relative_to(bundle) or path.stat().st_size != entry["bytes"] or sha256(path) != entry["sha256"]:
            raise ValueError(f"Frozen file differs from inventory: {entry['path']}")
    return [(path, str(PureWindowsPath("app", name))) for name, path in files.items()]


def build(bundle: Path, out: Path):
    bundle = bundle.resolve(strict=True)
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    target = out / "OSIA.msix"
    if target.exists():
        raise FileExistsError(target)
    meta = json.loads((bundle / "_internal/app_metadata.json").read_bytes())
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, cwd=REPO).strip()
    subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=REPO, check=True)
    if meta["product"] != "OSIA" or meta["architecture"] != "x86_64" or meta["commitSha"] != source:
        raise ValueError("Release identity does not match the checked-out source")
    major, minor, patch = map(int, meta["version"].split("."))
    version = f"{major + 1}.{minor}.{patch}.0"
    files = verified_payload(bundle)
    makeappx, metadata = sdk_files()
    with tempfile.TemporaryDirectory(dir=out, prefix="msix-") as tmp:
        work = Path(tmp)
        manifest = ET.parse(HERE / "AppxManifest.xml")
        identity = manifest.find("p:Identity", NS)
        if identity is None:
            raise ValueError("Missing MSIX identity")
        # Preserve the namespace prefixes named by IgnorableNamespaces.
        text = (HERE / "AppxManifest.xml").read_text("utf-8")
        text = text.replace(f'Version="{identity.attrib["Version"]}"', f'Version="{version}"', 1)
        rendered = work / "AppxManifest.xml"
        rendered.write_text(text, encoding="utf-8")
        files += [(rendered, "AppxManifest.xml"), (build_helper(work, metadata), r"app\OSIA.StoreStartup.exe")]
        with Image.open(REPO / "assets/osia_icon.png") as icon:
            for name, size in (("StoreLogo", 50), ("Square150", 150), ("Square44", 44)):
                path = work / (name + ".png")
                icon.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS).save(path)
                files.append((path, str(PureWindowsPath("StoreAssets", path.name))))
        mapping = work / "files.map"
        mapping.write_text("[Files]\n" + "".join(f'"{path}" "{name}"\n' for path, name in files), encoding="utf-8-sig")
        candidate = work / target.name
        subprocess.run([str(makeappx), "pack", "/f", str(mapping), "/p", str(candidate), "/h", "SHA256"], check=True)
        candidate.replace(target)
    receipt = {"file": target.name, "productVersion": meta["version"], "packageVersion": version,
               "sourceCommit": source, "sizeBytes": target.stat().st_size, "sha256": sha256(target)}
    (out / "store-build.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps(receipt))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.bundle, args.output)
