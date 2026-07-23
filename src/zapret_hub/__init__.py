from __future__ import annotations

import pathlib as _pl
import sys as _sys

_ns: dict[str, str] = {}

_candidates = [
    _pl.Path(__file__).resolve().parents[2] / "version.py",
]
if getattr(_sys, "frozen", False):
    _candidates.append(_pl.Path(_sys.executable).parent / "version.py")
if hasattr(_sys, "_MEIPASS"):
    _candidates.append(_pl.Path(_sys._MEIPASS) / "version.py")

for _p in _candidates:
    if _p.is_file():
        exec(_p.read_text(), _ns)
        break

__version__: str = _ns.get("__version__", "0.0.0")

__all__ = ["__version__"]
