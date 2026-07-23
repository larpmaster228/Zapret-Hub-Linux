from __future__ import annotations

import pathlib as _pl

_ns: dict[str, str] = {}
exec((_pl.Path(__file__).resolve().parents[2] / "version.py").read_text(), _ns)
__version__: str = _ns["__version__"]

__all__ = ["__version__"]
