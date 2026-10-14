"""Build the hubdeck Linux AppImage on Linux - no Docker or special toolchain needed.

    python packaging/linux/build.py [--out dist]

Produces dist/hubdeck-<version>-linux-x86_64.AppImage: a standalone Python
(python-build-standalone), hubdeck and its dependencies (manylinux wheels), and the AppImage
runtime's AppRun, so the image runs hubdeck's tray (no arguments) or its command line (with
arguments, e.g. `hubdeck.AppImage send page media`).

Needs: Python 3.13 with pip, Pillow and packaging; appimagetool (downloaded automatically).

Version: a tag vX.Y.Z builds X.Y.Z (it must start with hubdeck.__version__); a tag vX.Y.Z-alpha.N
builds the prerelease X.Y.Z-alpha.N; anything else is a development build named after its branch.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from common import (PBS_URL, ROOT, build_version, compile_bytecode, draw_icon, fetch,  # noqa: E402
                    install_packages, log, remove, requirements, run)

PBS = ("cpython-3.13.16+20261009-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
       "24801ea31c7d406d559a17e2226325c723d50c56900b9c0da7b62adaae2affc9")
MARKERS = {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix", "platform_machine": "x86_64"}
WHEEL_PLATFORMS = ["manylinux_2_17_x86_64", "manylinux2014_x86_64", "manylinux_2_28_x86_64"]
APPIMAGETOOL = ("https://github.com/AppImage/appimagetool/releases/download/continuous/"
                "appimagetool-x86_64.AppImage")

# parts of the standalone Python hubdeck never uses (bin/ stays: the AppRun runs bin/python3)
PRUNE = ["include", "share", "lib/pkgconfig", "lib/tcl*", "lib/tk*", "lib/libtcl*", "lib/libtk*",
         "lib/python3.13/test", "lib/python3.13/idlelib", "lib/python3.13/tkinter",
         "lib/python3.13/turtledemo", "lib/python3.13/ensurepip", "lib/python3.13/lib-dynload/_tkinter*",
         "lib/python3.13/site-packages/*"]

# from the installed packages: console scripts and test suites
APP_PRUNE = ["bin", "**/tests", "**/test"]

APPRUN = """#!/bin/sh
# hubdeck's AppRun: run the bundled Python with hubdeck's packages. No arguments = the tray app,
# with arguments = the command line (e.g. 'hubdeck.AppImage send page media').
HERE="${APPDIR:-$(dirname "$(readlink -f "$0")")}"
export PYTHONHOME="$HERE/opt/python"
export PYTHONPATH="$HERE/opt/app"
export PYTHONNOUSERSITE=1
exec "$HERE/opt/python/bin/python3" -B -m hubdeck._bundle "$@"
"""

DESKTOP = """[Desktop Entry]
Type=Application
Name=hubdeck
Comment=Stream Deck-style controller for Cooler Master MasterHub
Exec=hubdeck
Icon=hubdeck
Categories=Utility;
Terminal=false
"""


def install_python(appdir: Path, cache: Path) -> Path:
    name, sha = PBS
    archive = fetch(PBS_URL + name, sha, cache)
    log("unpacking Python")
    with tarfile.open(archive) as tar:
        try:
            tar.extractall(appdir / "opt", filter="tar")        # Python 3.12+: reject path traversal
        except TypeError:                                         # Python < 3.12 has no filter keyword
            tar.extractall(appdir / "opt")
    remove(appdir / "opt" / "python", PRUNE)
    return appdir / "opt" / "python"


def appimagetool(cache: Path) -> Path:
    tool = cache / "appimagetool-x86_64.AppImage"
    if not tool.exists():
        log("downloading appimagetool")
        cache.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(APPIMAGETOOL, timeout=120) as r:
            tool.write_bytes(r.read())
        tool.chmod(0o755)
    return tool


def write_runtime(appdir: Path):
    (appdir / "AppRun").write_text(APPRUN)
    (appdir / "AppRun").chmod(0o755)
    (appdir / "hubdeck.desktop").write_text(DESKTOP)
    log("drawing the icon")
    draw_icon(256, 232, 42).save(appdir / "hubdeck.png")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="dist")
    args = ap.parse_args()
    version = build_version()
    out = ROOT / args.out / f"hubdeck-{version}-linux-x86_64.AppImage"
    cache = ROOT / "build" / "linux-cache"
    work = ROOT / "build" / "linux-appimage"
    shutil.rmtree(work, ignore_errors=True)
    appdir = work / "hubdeck.AppDir"
    appdir.mkdir(parents=True)
    tmp = Path(tempfile.mkdtemp(prefix="hubdeck-linux-"))

    log(f"building hubdeck {version} AppImage")
    install_python(appdir, cache)
    install_packages(appdir / "opt" / "app", WHEEL_PLATFORMS, requirements(MARKERS), tmp, APP_PRUNE)
    compile_bytecode(appdir / "opt" / "app")
    write_runtime(appdir)

    log("building the AppImage")
    out.parent.mkdir(parents=True, exist_ok=True)
    run([appimagetool(cache), "--appimage-extract-and-run", "--no-appstream", appdir, out])
    print(out)


if __name__ == "__main__":
    main()
