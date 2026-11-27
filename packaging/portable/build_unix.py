"""Package the clean desktop source with a native, relocatable runtime."""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile


HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent


def link_or_copy(source, destination):
    try:
        os.link(source, destination)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        shutil.copy2(source, destination)
    return destination


def copy_runtime(source, destination):
    shutil.copytree(source.resolve(strict=True), destination, symlinks=True,
                    copy_function=link_or_copy, ignore=shutil.ignore_patterns(".links"))


def build(runtime: Path, output: Path) -> None:
    mac = sys.platform == "darwin"
    if (sys.platform, platform.machine()) not in {("linux", "x86_64"), ("darwin", "arm64")}:
        raise RuntimeError("Build on the native target architecture")
    subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=REPO, check=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    output.mkdir(parents=True, exist_ok=True)
    name = "OSIA.tar.gz" if mac else "OSIA.tar.xz"
    target = output / name
    if target.exists():
        raise FileExistsError(target)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix="osia-portable-") as tmp:
        stage = Path(tmp)
        package = stage / ("OSIA.app" if mac else "OSIA")
        root = package / "Contents" / "Resources" if mac else package
        app = root / "app"
        app.mkdir(parents=True)
        source_tar = stage / "source.tar"
        subprocess.run(["git", "archive", "--format=tar", "-o", str(source_tar), revision,
                        "osia", "assets", "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md",
                        "packaging/licenses/qt"], cwd=REPO, check=True)
        subprocess.run(["tar", "-xf", str(source_tar), "-C", str(app)], check=True)
        copy_runtime(runtime / "python", root / "python")
        copy_runtime(runtime / "runtimes", app / "runtimes")
        copy_runtime(runtime / "browsers", root / "browsers")
        subprocess.run(
            [str(root / "python/bin/python3.12"), "-I", "-B",
             str(REPO / "packaging/linux/link_browsers.py")],
            env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(root / "browsers")}, check=True,
        )
        shutil.copy2(HERE / "run.py", root / "portable.py")
        shutil.copy2(REPO / "packaging/linux/doctor.py", root / "doctor.py")
        entry = package / "Contents/MacOS/OSIA" if mac else package / "OSIA"
        entry.parent.mkdir(parents=True, exist_ok=True)
        mac_flags = ["-x", "objective-c", "-fobjc-arc", "-mmacosx-version-min=15.0", "-framework", "Foundation",
                     "-framework", "ServiceManagement"] if mac else []
        subprocess.run(["cc", "-O2", "-Wall", "-Werror", *mac_flags,
                        str(HERE / "launcher.c"), "-o", str(entry)], check=True)
        import runpy
        version = runpy.run_path(str(app / "osia/version.py"))
        metadata = {"version": version["PRODUCT_VERSION"], "releaseSequence": version["RELEASE_SEQUENCE"],
                    "platform": "macos-arm64" if mac else "linux-x64", "sourceCommit": revision}
        if not mac:
            metadata["installerId"] = "space.osia.desktop.linux.v1"
            (root / "bin").mkdir()
            (root / "bin/osia").symlink_to("../OSIA")
        (root / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        if mac:
            info = {"CFBundleExecutable": "OSIA", "CFBundleIdentifier": "space.osia.desktop",
                    "CFBundleName": "OSIA", "CFBundleDisplayName": "OSIA", "CFBundlePackageType": "APPL",
                    "CFBundleShortVersionString": metadata["version"],
                    "CFBundleVersion": metadata["version"].rsplit(".", 1)[-1], "LSMinimumSystemVersion": "15.0",
                    "NSLocalNetworkUsageDescription": "Connect OSIA Mobile to this Mac on your local network.",
                    "NSBonjourServices": ["_osia._tcp"], "NSMicrophoneUsageDescription": "Listen to your voice request.",
                    "NSAppleEventsUsageDescription": "Perform the desktop actions you request."}
            (package / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
            subprocess.run(["codesign", "--force", "--sign", "-", str(package)], check=True)
            subprocess.run(["codesign", "--verify", "--strict", str(package)], check=True)
        for item in package.rglob("*"):
            if item.is_symlink():
                destination = Path(os.readlink(item))
                if destination.is_absolute() or not item.resolve().is_relative_to(package):
                    raise ValueError(f"Non-relocatable link: {item.relative_to(package)}")
        assert subprocess.check_output([str(entry), "--version"], text=True).strip() == metadata["version"]
        partial = stage / name
        command = ["tar", "-czf" if mac else "-cJf", str(partial), "-C", str(stage), package.name]
        subprocess.run(command, env={**os.environ, "XZ_OPT": "-T2 -2"}, check=True)
        partial.rename(target)
    with target.open("rb") as archive:
        digest = hashlib.file_digest(archive, "sha256").hexdigest()
    (output / (name + ".sha256")).write_text(f"{digest}  {name}\n", encoding="ascii")
    metadata.update(filename=name, sizeBytes=target.stat().st_size, sha256=digest)
    (output / "portable-metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("runtime", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.runtime.resolve(), args.output.resolve())
