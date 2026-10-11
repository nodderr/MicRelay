"""Engine tests: start/stop, live settings and status, driven by the fake phone."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from pathlib import Path

import pytest

from fake_phone import run_phone
from phonemic.engine import Engine, EngineSettings, EngineStatus


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _settings(tmp_path: Path, port: int | None = None) -> EngineSettings:
    return EngineSettings(
        port=port or _free_port(), host="127.0.0.1", bind="127.0.0.1",
        null_output=True, config_dir=tmp_path / "config",
    )


def test_engine_streams_and_reports_status(tmp_path: Path) -> None:
    engine = Engine(_settings(tmp_path))
    engine.start()
    samples: list[EngineStatus] = []
    stop_sampling = threading.Event()

    def sample() -> None:
        while not stop_sampling.is_set():
            samples.append(engine.status())
            time.sleep(0.1)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        assert engine.running
        assert engine.url.startswith("https://127.0.0.1:")
        engine.set_target_ms(40.0)
        engine.set_gain_db(-6.0)
        asyncio.run(run_phone(engine.url, seconds=4.0))
    finally:
        stop_sampling.set()
        sampler.join()
        engine.stop()

    assert not engine.running
    connected = [s for s in samples if s.server is not None and s.server.state == "connected"]
    assert len(connected) > 10
    assert connected[-1].server.device  # type: ignore[union-attr]
    # A 0.5-amplitude tone at -6 dB peaks around 0.25.
    playing = [s for s in connected if s.stats is not None and s.stats.playing]
    assert max(s.level for s in playing) == pytest.approx(0.25, abs=0.03)
    assert playing[-1].stats.target_ms == 40.0  # type: ignore[union-attr]
    assert playing[-1].latency_ms is not None
    assert all(s.stats.underruns == 0 for s in playing)  # type: ignore[union-attr]


def test_engine_reports_busy_port(tmp_path: Path) -> None:
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        engine = Engine(_settings(tmp_path, port))
        with pytest.raises(RuntimeError, match="already running"):
            engine.start()
    assert not engine.running


def test_engine_can_restart(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    for _ in range(2):
        engine = Engine(settings)
        engine.start()
        assert engine.status().running
        engine.stop()
        assert not engine.status().running
