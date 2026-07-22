from __future__ import annotations

import sys


def platform() -> str:
    return "windows" if sys.platform.startswith("win") else "linux" if sys.platform.startswith("linux") else sys.platform


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_unix() -> bool:
    return not is_windows()
