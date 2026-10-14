"""Linux-specific platform code.

Files, locking, launching and IPC come from PosixPlatform. Audio, keystrokes, focus and media
raise `Unsupported` from base.Platform until a Linux backend lands (PipeWire/PulseAudio, uinput or
XTEST, X11/Wayland focus, MPRIS). That work belongs here, subclassing PosixPlatform.
"""

from __future__ import annotations

from ..posix import PosixPlatform


class LinuxPlatform(PosixPlatform):
    name = "linux"
