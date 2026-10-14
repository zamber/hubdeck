# Linux: AppImage build and alpha channel

hubdeck now ships a Linux AppImage alongside the macOS app and the Windows MSI/zip, all built on
Linux CI (no dedicated runner). The AppImage runs the tray app with no arguments, or the command
line with arguments (`hubdeck.AppImage send page media`).

## Build it

    python packaging/linux/build.py          # -> dist/hubdeck-<version>-linux-x86_64.AppImage

Needs Python 3.13 with pip, Pillow and packaging (the CI image `python:3.13.7-slim`); appimagetool
is downloaded automatically. On a Python 3.11 dev machine the tar extraction falls back to the
unfiltered path, so the build still works locally.

What goes in:

- **python-build-standalone** (`opt/python/`, the `x86_64-unknown-linux-gnu` install_only build,
  pinned by sha256 in `packaging/linux/build.py`) — a self-contained CPython 3.13.
- **hubdeck and its dependencies** (`opt/app/`), installed as `manylinux` wheels for CPython 3.13.
- **AppRun** + **hubdeck.desktop** + the icon. AppRun sets `PYTHONHOME`/`PYTHONPATH` to the bundled
  tree and execs `opt/python/bin/python3 -B -m hubdeck._bundle`, so no argument = tray, arguments =
  CLI (the same entry point the macOS bundle uses).

The image is built with `appimagetool --appimage-extract-and-run --no-appstream` (no FUSE needed).

## Release channels

`packaging/common.py:build_version` now accepts prerelease tags. A tag must start with
`hubdeck.__version__`:

- `v0.3.1`        → version `0.3.1` (stable)
- `v0.3.1-alpha.1` → version `0.3.1-alpha.1` (alpha)

`packaging/release.py` marks a GitHub release as a **prerelease** when the version has a hyphen,
and `.gitlab-ci.yml` runs the `release` job for both shapes. So the alpha channel is just a tag:
`git tag v0.3.1-alpha.1 && git push origin v0.3.1-alpha.1`.

## Linux platform status

`hubdeck.platforms.current()` returns a `LinuxPlatform` (in `platforms/linux/`) that subclasses
`PosixPlatform`. Files, single-instance locking, launching and IPC work today. Audio, synthetic
keystrokes, focus-based page switching and media reading raise `Unsupported` until a Linux backend
lands; that work goes into `platforms/linux/`:

- audio: PipeWire/PulseAudio (volume, mute, default device)
- keyboard: uinput or XTEST (synthetic keystrokes)
- focus / media: X11 (and later Wayland), MPRIS over D-Bus

Until then the device itself (HID) and every plugin that doesn't need those — including the
`huemux` plugin for lights — work on Linux.
