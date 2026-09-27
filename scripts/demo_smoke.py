#!/usr/bin/env python3
"""Bounded clean-environment smoke test for the credential-free demo server."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENDPOINTS = ("/health", "/api/public-status", "/api/public-pnl-history")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    port = _free_port()
    env = {
        key: value for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in ("KEY", "TOKEN", "SECRET", "WALLET", "ADDRESS"))
    }
    env.update({
        "DEMO_MODE": "true",
        "API_HOST": "127.0.0.1",
        "API_PORT": str(port),
        "BOT_DATA_DIR": str(ROOT / ".demo-smoke-data"),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    launcher = (
        "import hyperliquid_bot as app; "
        "forbidden=lambda *a, **k: (_ for _ in ()).throw(AssertionError('exchange client created')); "
        "app.Exchange=forbidden; app.Info=forbidden; raise SystemExit(app.main([]))"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", launcher], cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    deadline = time.monotonic() + 15
    responses: dict[str, dict] = {}
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=1)
                raise RuntimeError(f"demo server exited early ({process.returncode}): {stdout[-300:]} {stderr[-300:]}")
            try:
                for endpoint in ENDPOINTS:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}{endpoint}", timeout=1) as response:
                        responses[endpoint] = json.load(response)
                break
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(0.1)
        else:
            raise RuntimeError("demo server did not become healthy within 15 seconds")

        assert responses["/health"]["status"] == "ok"
        status = responses["/api/public-status"]
        assert status["demo_data"] is True and status["execution_mode"] == "DEMO"
        assert status["position"] == "none" and status["perps_positions"] == []
        pnl = responses["/api/public-pnl-history"]
        assert pnl == {"demo_data": True, "data": [], "total_pnl": 0.0}
        print("Demo smoke passed: three public endpoints are synthetic and exchange clients were forbidden.")
        return 0
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        smoke_dir = ROOT / ".demo-smoke-data"
        try:
            smoke_dir.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
