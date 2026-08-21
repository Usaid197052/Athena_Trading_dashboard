"""Whitelist import runner for untrusted plugin.py files.

The plugin itself may import only numpy, pandas, and math.
Those libraries may import the rest of the standard library internally.
"""
from __future__ import annotations

import builtins
import runpy
import sys
from pathlib import Path

_REAL_IMPORT = builtins.__import__
_PLUGIN_ALLOWED = {"numpy", "pandas", "math"}


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: runner.py <plugin.py>", file=sys.stderr)
        sys.exit(2)
    target = Path(sys.argv[1]).resolve()
    job = target.parent.resolve()
    if not target.is_file():
        print("plugin not found", file=sys.stderr)
        sys.exit(2)

    plugin_file = str(target)

    def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        root = name.split(".")[0]
        filename = ""
        if isinstance(globals, dict):
            filename = str(globals.get("__file__") or "")
        is_plugin = filename.replace("\\", "/") == plugin_file.replace("\\", "/")
        if is_plugin and root not in _PLUGIN_ALLOWED:
            raise ImportError(
                f"sandbox blocked import of {name!r}. "
                "Only numpy, pandas, and math are allowed."
            )
        return _REAL_IMPORT(name, globals, locals, fromlist, level)

    _real_open = builtins.open

    def _blocked_open(file, mode="r", *args, **kwargs):
        if not str(mode).startswith("r"):
            try:
                resolved = Path(file).resolve()
            except Exception:
                raise PermissionError("sandbox blocked file write")
            if not str(resolved).startswith(str(job)):
                raise PermissionError("sandbox blocked write outside job dir")
        return _real_open(file, mode, *args, **kwargs)

    builtins.__import__ = _guarded_import
    builtins.open = _blocked_open  # type: ignore[assignment]

    ns = runpy.run_path(str(target), run_name="__sandbox__")
    fn = ns.get("main")
    if callable(fn):
        result = fn()
        if result is not None:
            print(result)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"SANDBOX_ERROR: {e}", file=sys.stderr)
        sys.exit(1)
