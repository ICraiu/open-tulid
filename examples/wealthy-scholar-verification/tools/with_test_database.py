"""Run verification with a private, disposable MongoDB inside the project image."""
from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time


def main(argv=None):
    command = list(sys.argv[1:] if argv is None else argv)
    if not command:
        raise SystemExit("usage: with_test_database.py COMMAND [ARG ...]")
    # Never inherit a connection string that could address a real project DB.
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="wealthy-scholar-test-db-") as directory:
        log_path = Path(directory) / "mongo.log"
        with log_path.open("w") as log:
            process = subprocess.Popen([
                "mongod", "--dbpath", directory, "--bind_ip", "127.0.0.1",
                "--port", str(port), "--nounixsocket", "--quiet",
            ], stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("MongoDB exited before becoming ready")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError("MongoDB did not become ready in 30 seconds")
                env = {**os.environ, "MONGO_URI": f"mongodb://127.0.0.1:{port}/wealthy_scholar_test"}
                return subprocess.run(command, env=env, timeout=3600).returncode
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                print(f"Private database verification failed: {exc}", file=sys.stderr)
                print(log_path.read_text()[-4000:], file=sys.stderr)
                return 1
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
