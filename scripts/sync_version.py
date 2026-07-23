from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "version.py"


def _read_version() -> str:
    ns: dict[str, str] = {}
    exec(VERSION_FILE.read_text(), ns)
    ver = ns.get("__version__")
    if not ver:
        print("ERROR: __version__ not found in version.py", file=sys.stderr)
        sys.exit(1)
    return ver


def _patch_file(path: Path, pattern: str, replacement: str, count: int = 0) -> int:
    text = path.read_text(encoding="utf-8")
    new_text, n = re.subn(pattern, replacement, text, count=count)
    if n:
        path.write_text(new_text, encoding="utf-8")
    return n


def main() -> None:
    version = _read_version()
    patched = 0

    pkg_json = ROOT / "web_ui" / "package.json"
    if pkg_json.exists():
        n = _patch_file(
            pkg_json,
            r'"version"\s*:\s*"[^"]*"',
            f'"version": "{version}"',
        )
        patched += n
        print(f"  {pkg_json.relative_to(ROOT)}: {n} replacement(s)")

    lock_json = ROOT / "web_ui" / "package-lock.json"
    if lock_json.exists():
        n = _patch_file(
            lock_json,
            r'"version"\s*:\s*"[^"]*"',
            f'"version": "{version}"',
            count=2,
        )
        patched += n
        print(f"  {lock_json.relative_to(ROOT)}: {n} replacement(s)")

    for js_file in [
        ROOT / "installer_web" / "app.js",
        ROOT / "uninstaller_web" / "app.js",
    ]:
        if js_file.exists():
            n = _patch_file(
                js_file,
                r'version:\s*"[^"]*"',
                f'version: "{version}"',
            )
            patched += n
            print(f"  {js_file.relative_to(ROOT)}: {n} replacement(s)")

    print(f"sync_version: done ({patched} total replacements, version={version})")


if __name__ == "__main__":
    main()
