"""End-to-end test: real server (--null-output --record) + simulated phone over WebRTC."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from fake_phone import analyze_tone, is_clean_tone, run_phone

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServerProcess:
    """Runs `python -m phonemic` and captures its output in the background."""

    def __init__(self, tmp_path: Path, run_seconds: float) -> None:
        self.port = _free_port()
        self.wav = tmp_path / "out.wav"
        self.lines: list[str] = []
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(
            [
                sys.executable, "-m", "phonemic", "--null-output", "--record", str(self.wav),
                "--port", str(self.port), "--bind", "127.0.0.1", "--host", "127.0.0.1",
                "--config-dir", str(tmp_path / "config"), "--run-seconds", str(run_seconds),
            ],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.append(line.rstrip())

    def wait_for_url(self, timeout: float = 30.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for line in self.lines:
                if "Open on your phone:" in line:
                    return line.split("Open on your phone:")[1].strip()
            if self.proc.poll() is not None:
                break
            time.sleep(0.05)
        raise AssertionError("server did not start:\n" + "\n".join(self.lines))

    def finish(self, timeout: float = 40.0) -> str:
        try:
            self.proc.wait(timeout=timeout)
        finally:
            if self.proc.poll() is None:
                self.proc.kill()
        self._reader.join(timeout=5)
        return "\n".join(self.lines)


def test_clean_440hz_tone_end_to_end(tmp_path: Path) -> None:
    server = ServerProcess(tmp_path, run_seconds=10)
    url = server.wait_for_url()
    result = asyncio.run(run_phone(url, seconds=5.0, freq=440.0))
    log = server.finish()
    assert result.sent == result.chunks
    assert "Phone connected" in log, log
    analysis = analyze_tone(server.wav, 440.0)
    assert is_clean_tone(analysis, 440.0), f"{analysis}\n{log}"
    assert analysis.duration_s > 3.5


def test_tone_survives_loss_and_jitter(tmp_path: Path) -> None:
    server = ServerProcess(tmp_path, run_seconds=10)
    url = server.wait_for_url()
    result = asyncio.run(run_phone(url, seconds=5.0, freq=440.0, loss=0.03, jitter_ms=15.0, seed=5))
    log = server.finish()
    assert result.dropped > 0
    analysis = analyze_tone(server.wav, 440.0)
    assert analysis.peak_hz == pytest.approx(440.0, abs=2.0), f"{analysis}\n{log}"
    assert analysis.snr_db > 10.0, f"{analysis}\n{log}"
    # Lost packets become 10 ms gaps; there must be about as many as were dropped.
    assert analysis.dropout_fraction < 0.10, f"{analysis}\n{log}"
