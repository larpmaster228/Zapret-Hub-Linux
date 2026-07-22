from __future__ import annotations

import argparse
import shutil
import zipfile
from pathlib import Path


VERSION = "3.0.0"


def _should_skip_path(path: Path, source_dir: Path) -> bool:
    try:
        rel = path.relative_to(source_dir)
    except Exception:
        return False
    parts = rel.parts
    if any(part.startswith("tg-ws-proxy.bak.") for part in parts):
        return True
    lowered = tuple(part.lower() for part in parts)
    if "docs" in lowered and rel.name.lower() == "readme.md":
        return True
    return False


def _zip_with_root(source_dir: Path, zip_path: Path, root_name: str = "zapret_hub") -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in sorted(source_dir.rglob("*")):
            if item.is_dir():
                continue
            if not item.exists():
                continue
            if _should_skip_path(item, source_dir):
                continue
            rel = item.relative_to(source_dir)
            try:
                archive.write(item, Path(root_name) / rel)
            except (PermissionError, FileNotFoundError):
                continue


def _copy_uninstaller(source: Path | None, destination_dir: Path) -> None:
    if source is None or not source.exists():
        return
    destination_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination_dir / "uninstall_zaprethub.exe")


def _parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description=(
            "Prepare GitHub/mirror release assets for the slim installer model. "
            "Portable zips are published for goshkow.ru; the installer downloads them later "
            "and does not embed these archives."
        )
    )
    parser.add_argument("--x64-source", default=str(root / "dist_nuitka" / "main.dist"))
    parser.add_argument("--arm64-source", default=str(root / ".release_cache" / "win_arm64"))
    parser.add_argument("--payload-dir", default=str(root / "installer_payload"))
    parser.add_argument("--release-dir", default=str(root / f"release_{VERSION}"))
    parser.add_argument("--version", default=VERSION)
    parser.add_argument("--uninstaller-source", default="")
    parser.add_argument("--uninstaller-x64", default="")
    parser.add_argument("--uninstaller-arm64", default="")
    parser.add_argument(
        "--skip-installer-payload-zips",
        action="store_true",
        help="Do not write installer_payload/*.zip (slim installer downloads from mirror).",
    )
    return parser.parse_args()


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    args = _parse_args()
    version = str(args.version)
    x64_source = Path(args.x64_source).resolve()
    arm64_source = Path(args.arm64_source).resolve()
    payload_dir = Path(args.payload_dir).resolve()
    release_dir = Path(args.release_dir).resolve()
    shared_uninstaller = Path(args.uninstaller_source).resolve() if args.uninstaller_source else None
    uninstaller_x64 = Path(args.uninstaller_x64).resolve() if args.uninstaller_x64 else shared_uninstaller
    uninstaller_arm64 = Path(args.uninstaller_arm64).resolve() if args.uninstaller_arm64 else shared_uninstaller

    if not x64_source.exists():
        raise FileNotFoundError(f"x64 Nuitka source not found: {x64_source}")
    if not arm64_source.exists():
        raise FileNotFoundError(f"arm64 source not found: {arm64_source}")

    if not args.skip_installer_payload_zips:
        # Optional local/dev fallback only — slim installer does not embed these.
        payload_dir.mkdir(parents=True, exist_ok=True)
        _zip_with_root(x64_source, payload_dir / "win_x64.zip")
        _zip_with_root(arm64_source, payload_dir / "win_arm64.zip")
    else:
        print("Skipping installer_payload zips (slim download-from-mirror installer).")

    release_dir.mkdir(parents=True, exist_ok=True)
    portable_x64_dir = release_dir / f"zapret_hub_{version}_portable_win_x64"
    portable_arm64_dir = release_dir / f"zapret_hub_{version}_portable_win_arm64"

    if portable_x64_dir.exists():
        shutil.rmtree(portable_x64_dir, ignore_errors=True)
    if portable_arm64_dir.exists():
        shutil.rmtree(portable_arm64_dir, ignore_errors=True)

    shutil.copytree(x64_source, portable_x64_dir, dirs_exist_ok=True)
    shutil.copytree(arm64_source, portable_arm64_dir, dirs_exist_ok=True)
    _copy_uninstaller(uninstaller_x64, portable_x64_dir)
    _copy_uninstaller(uninstaller_arm64, portable_arm64_dir)
    for backup_dir in portable_x64_dir.rglob("tg-ws-proxy.bak.*"):
        if backup_dir.is_dir():
            shutil.rmtree(backup_dir, ignore_errors=True)
    for backup_dir in portable_arm64_dir.rglob("tg-ws-proxy.bak.*"):
        if backup_dir.is_dir():
            shutil.rmtree(backup_dir, ignore_errors=True)
    _zip_with_root(portable_x64_dir, release_dir / f"zapret_hub_{version}_portable_win_x64.zip")
    _zip_with_root(portable_arm64_dir, release_dir / f"zapret_hub_{version}_portable_win_arm64.zip")

    note = release_dir / "README_RELEASE.txt"
    note.write_text(
        "Slim installer release model\n"
        "============================\n"
        "1) Publish portable_win_x64.zip and portable_win_arm64.zip to the goshkow.ru mirror\n"
        "   (and/or keep them as GitHub release assets).\n"
        "2) install_zaprethub_*_universal.exe is a slim installer: at runtime it downloads the\n"
        "   matching arch build from https://goshkow.ru/zapret-hub/update — it does NOT embed\n"
        "   the portable archives.\n"
        "3) Each portable folder includes arch-matching uninstall_zaprethub.exe.\n"
        "4) Standalone uninstallers may also be published as separate release assets.\n",
        encoding="utf-8",
    )

    print(f"Prepared release folder in: {release_dir}")
    if not args.skip_installer_payload_zips:
        print(f"Prepared optional local payloads in: {payload_dir}")
    if uninstaller_x64 and uninstaller_x64.exists():
        print(f"Portable x64 includes uninstaller: {uninstaller_x64}")
    else:
        print("WARNING: portable x64 has no uninstall_zaprethub.exe")
    if uninstaller_arm64 and uninstaller_arm64.exists():
        print(f"Portable arm64 includes uninstaller: {uninstaller_arm64}")
    else:
        print("WARNING: portable arm64 has no uninstall_zaprethub.exe")


if __name__ == "__main__":
    main()