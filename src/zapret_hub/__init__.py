from __future__ import annotations

import pathlib as _pl
import sys as _sys

_ns: dict[str, str] = {}

# Development: version.py at project root (../../ from src/zapret_hub/__init__.py)
# PyInstaller onedir: version.py may be next to the executable or in _internal/
# Nuitka: version.py next to the executable
_this = _pl.Path(__file__).resolve()
_candidates = [
    _this.parents[2] / "version.py",
    _this.parent / "version.py",
    _this.parent.parent / "version.py",
]
if getattr(_sys, "frozen", False):
    _exe_dir = _pl.Path(_sys.executable).parent
    _candidates += [
        _exe_dir / "version.py",
        _exe_dir / "_internal" / "version.py",
    ]
if hasattr(_sys, "_MEIPASS"):
    _candidates.append(_pl.Path(_sys._MEIPASS) / "version.py")

for _p in _candidates:
    if _p.is_file():
        exec(_p.read_text(), _ns)
        break

__version__: str = _ns.get("__version__", "0.0.0")

__all__ = ["__version__"]
