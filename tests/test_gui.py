"""Smoke test of the desktop window: start, phone connects, settings persist, close."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from pathlib import Path

import pytest

tk = pytest.importorskip("tkinter")

from fake_phone import run_phone  # noqa: E402
from phonemic import gui  # noqa: E402


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _pump(root: tk.Tk, seconds: float, until: object = None) -> bool:
    """Run the Tk event loop for up to `seconds`, or until `until()` is true."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        root.update()
        if until is not None and until():  # type: ignore[operator]
            return True
        time.sleep(0.02)
    return False


def test_window_shows_connection_and_saves_settings(tmp_path: Path) -> None:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display: {exc}")
    root.withdraw()
    config_dir = tmp_path / "config"
    args = gui.parse_args(["--port", str(_free_port()), "--null-output", "--config-dir", str(config_dir)])
    app = gui.PhonemicApp(root, gui.build_settings(args, config_dir), config_dir)
    try:
        assert _pump(root, 20, lambda: app.engine is not None and app.engine.running)
        assert "Waiting" in app.pill.cget("text") or _pump(root, 2, lambda: "Waiting" in app.pill.cget("text"))
        url = app.engine.url.replace(app.engine.url.split("//")[1].split(":")[0], "127.0.0.1")
        phone = threading.Thread(target=lambda: asyncio.run(run_phone(url, seconds=4.0)), daemon=True)
        phone.start()
        assert _pump(root, 15, lambda: "Connected" in app.pill.cget("text"))
        assert "Latency" in app.stats_line1.cget("text")
        app.buffer_scale.set(55)  # snaps to the 5 ms grid and applies live
        app.gain_scale.set(3.4)
        _pump(root, 0.5)
        assert app.engine.jitter.target_ms == 55.0  # type: ignore[union-attr]
        phone.join(timeout=20)
        app.toggle()  # stop
        assert _pump(root, 5, lambda: app.pill.cget("text") == "Stopped")
        assert "disabled" in app.copy_button.state()
    finally:
        app.close()

    saved = gui.load_saved_settings(config_dir)
    assert saved["target_ms"] == 55.0
    assert saved["gain_db"] == 3.0
    assert saved["device"] is None


def test_saved_settings_are_validated(tmp_path: Path) -> None:
    (tmp_path / gui.SETTINGS_FILE).write_text('{"target_ms": 9999, "gain_db": "loud", "device": 5}')
    saved = gui.load_saved_settings(tmp_path)
    assert saved == {"target_ms": float(gui.BUFFER_RANGE[1])}
    (tmp_path / gui.SETTINGS_FILE).write_text("not json")
    assert gui.load_saved_settings(tmp_path) == {}
