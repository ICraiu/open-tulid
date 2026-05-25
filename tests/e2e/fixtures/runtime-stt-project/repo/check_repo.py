from __future__ import annotations

import pathlib
import py_compile
import sys


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    app_path = pathlib.Path("app.py")
    if not app_path.is_file():
        print("app.py missing", file=sys.stderr)
        return 1
    content = app_path.read_text(encoding="utf-8")
    if mode == "tests":
        return 0 if "def healthz()" in content and "return 'ok'" in content else 1
    if mode == "build":
        try:
            py_compile.compile(str(app_path), doraise=True)
        except py_compile.PyCompileError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    print(f"unknown mode: {mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
