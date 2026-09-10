#!/usr/bin/env python3
"""Scripted project toolchain: no installs, providers, or services."""
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
if name == "mongod":
    # Lifecycle stand-in for the private DB wrapper. Component behavior remains
    # scripted here; real persistence is checked separately in the project image.
    import socket
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", int(args[args.index("--port") + 1])))
        listener.listen()
        while True:
            connection, _ = listener.accept()
            connection.close()
elif name == "node":
    path = Path(args[-1])
    behavior = path.read_text().strip()
    if behavior == "broken":
        sys.exit(1)
    title = str(path) if behavior == "empty" else "fixture assertion"
    skipped = 1 if behavior == "skipped" else 0
    print(f"TAP version 13\n# Subtest: {title}\nok 1 - {title}\n1..1")
    print(f"# tests 1\n# pass {1-skipped}\n# fail 0\n# cancelled 0\n# skipped {skipped}\n# todo 0")
elif name == "uv" and "pytest" in args:
    directory = Path(args[args.index("pytest") + 1])
    paths = [directory] if directory.is_file() else sorted(directory.rglob("test_*.py"))
    if any(p.read_text().strip() == "broken" for p in paths):
        sys.exit(1)
    report = Path(next(arg.split("=", 1)[1] for arg in args if arg.startswith("--junitxml=")))
    cases = ''.join('<testcase name="fixture">' + ('<skipped/>' if p.read_text().strip() == 'skipped' else '') + '</testcase>' for p in paths if p.read_text().strip() != 'empty')
    report.write_text('<testsuites><testsuite>' + cases + '</testsuite></testsuites>')
elif name == "npm" and "build" in args:
    behavior = json.loads(Path("package.json").read_text()).get("build", "passed")
    if behavior == "broken":
        sys.exit(1)
    output = Path(args[args.index("--outDir") + 1])
    output.mkdir(parents=True, exist_ok=True)
    if behavior != "empty":
        (output / "index.html").write_text('<script src="app.js"></script>')
        (output / "app.js").write_text('built frontend')
elif name not in ("npm", "uv"):
    raise SystemExit("unexpected tool")
