from __future__ import annotations

import shutil
from pathlib import Path

from scripts.prepare_nuitka_release import main as prepare_nuitka_release_main


ROOT = Path(__file__).resolve().parent

_ns: dict[str, str] = {}
exec((ROOT / "version.py").read_text(), _ns)
_version: str = _ns["__version__"]

INSTALLER_SRC = ROOT / "dist_installer" / f"install_zaprethub_{_version}_universal.exe"
RELEASE_DIR = ROOT / f"release_{_version}"


def main() -> None:
    prepare_nuitka_release_main()
    if INSTALLER_SRC.exists():
        RELEASE_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(INSTALLER_SRC, RELEASE_DIR / INSTALLER_SRC.name)
    print("ok")


if __name__ == "__main__":
    main()
