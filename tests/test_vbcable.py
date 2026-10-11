"""VB-Cable installer helper: download/unpack checks, signature check, and the window's install flow."""

from __future__ import annotations

import os
import shutil
import sys
import time
import zipfile
from pathlib import Path

import pytest

from phonemic import vbcable
from phonemic.engine import EngineStatus


def _make_pack(path: Path, with_setup: bool = True) -> str:
    with zipfile.ZipFile(path, "w") as pack:
        pack.writestr("readme.txt", "VB-CABLE")
        pack.writestr("../escape.txt", "must stay inside the folder")
        if with_setup:
            pack.writestr("VBCABLE_Setup_x64.exe", b"MZ fake")
            pack.writestr("VBCABLE_Setup.exe", b"MZ fake")
    return path.resolve().as_uri()


def test_download_pack_unpacks_and_finds_setup(tmp_path: Path) -> None:
    url = _make_pack(tmp_path / "pack.zip")
    messages: list[str] = []
    setup = vbcable.download_pack(tmp_path / "dest", messages.append, url=url)
    assert setup.name == vbcable.setup_exe_name()
    assert setup.read_bytes() == b"MZ fake"
    assert not (tmp_path / "escape.txt").exists() and not (tmp_path / "dest" / "escape.txt").exists()
    assert not (tmp_path / "dest" / "VBCABLE_Driver_Pack.zip").exists()  # archive cleaned up
    assert messages[0].startswith("Downloading VB-Cable")
    # A second run replaces the old folder instead of failing.
    assert vbcable.download_pack(tmp_path / "dest", url=url) == setup


def test_download_pack_rejects_bad_downloads(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="does not contain"):
        vbcable.download_pack(tmp_path / "a", url=_make_pack(tmp_path / "empty.zip", with_setup=False))
    (tmp_path / "page.html").write_text("<html>not a zip</html>")
    with pytest.raises(RuntimeError, match="not a valid zip"):
        vbcable.download_pack(tmp_path / "b", url=(tmp_path / "page.html").as_uri())
    with pytest.raises(RuntimeError, match="Could not download"):
        vbcable.download_pack(tmp_path / "c", url=(tmp_path / "missing.zip").as_uri())


@pytest.mark.skipif(sys.platform != "win32", reason="Authenticode is Windows-only")
def test_signature_check_refuses_other_publishers(tmp_path: Path) -> None:
    fake = tmp_path / "VBCABLE_Setup_x64.exe"
    shutil.copy(sys.executable, fake)  # signed (by the PSF) or unsigned, but not by VB-Audio
    with pytest.raises(RuntimeError, match="not signed by VB-Audio"):
        vbcable.verify_signature(fake)


@pytest.mark.skipif(
    sys.platform != "win32" or not os.environ.get("PHONEMIC_NETWORK_TESTS"),
    reason="downloads from vb-audio.com; set PHONEMIC_NETWORK_TESTS=1",
)
def test_real_pack_downloads_and_is_signed(tmp_path: Path) -> None:
    setup = vbcable.download_pack(tmp_path)
    vbcable.verify_signature(setup)


@pytest.mark.skipif(sys.platform != "win32", reason="ShellExecuteEx is Windows-only")
def test_run_elevated_waits_for_exit_code() -> None:
    where = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "where.exe"
    assert vbcable.run_elevated(where, verb="open") == 2  # "where" with no arguments exits with 2


# ------------------------------------------------------------- window flow
class FakeEngine:
    """Stands in for the real Engine: reports a virtual device once 'installed'."""

    cable_installed = False

    def __init__(self, settings: object) -> None:
        self.running = False
        self.url = "https://127.0.0.1:8443/?t=test"
        self.virtual = False

    def start(self) -> None:
        self.running = True
        self.virtual = FakeEngine.cable_installed

    def stop(self) -> None:
        self.running = False

    def status(self) -> EngineStatus:
        return EngineStatus(
            running=self.running, url=self.url, server=None, stats=None,
            output_description="CABLE Input" if self.virtual else "Speakers",
            output_latency_ms=10.0, virtual_device=self.virtual, output_warning=None,
            level=0.0, uptime_s=1.0, error=None,
        )

    def set_device(self, spec: object) -> None: ...
    def set_target_ms(self, value: float) -> None: ...
    def set_gain_db(self, value: float) -> None: ...


@pytest.fixture
def window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tk = pytest.importorskip("tkinter")
    from phonemic import gui

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display: {exc}")
    root.withdraw()
    FakeEngine.cable_installed = False
    shown: list[str] = []
    monkeypatch.setattr(gui, "Engine", FakeEngine)
    monkeypatch.setattr(gui.vbcable, "supported", lambda: True)
    monkeypatch.setattr(gui.audio_out, "refresh_devices", lambda: None)
    monkeypatch.setattr(gui, "device_options", lambda: [(gui.AUTO_DEVICE, None)])
    monkeypatch.setattr(gui.messagebox, "askokcancel", lambda *a, **k: True)
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda title, message: shown.append(message))
    config_dir = tmp_path / "config"
    app = gui.PhonemicApp(root, gui.build_settings(gui.parse_args([]), config_dir), config_dir)
    app.shown = shown  # type: ignore[attr-defined]
    try:
        yield app
    finally:
        app.close()


def _pump(app: object, seconds: float, until: object) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.root.update()  # type: ignore[attr-defined]
        if until():  # type: ignore[operator]
            return True
        time.sleep(0.02)
    return False


def _banner(app: object) -> tuple[bool, str, str]:
    banner = app.cable_banner  # type: ignore[attr-defined]
    button = banner.button.cget("text") if banner.button.winfo_manager() else ""
    return banner.visible, banner.message.cget("text"), button


def test_window_installs_cable_and_switches_to_it(window, monkeypatch: pytest.MonkeyPatch) -> None:
    app = window
    assert _pump(app, 5, lambda: _banner(app)[2] == "Install virtual mic")

    def fake_install(dest: Path, progress, cancel) -> None:
        progress("Downloading VB-Cable… 50%")
        time.sleep(0.5)
        FakeEngine.cable_installed = True

    monkeypatch.setattr(vbcable, "install", fake_install)
    app.install_cable()
    assert _pump(app, 5, lambda: "Downloading" in _banner(app)[1])
    assert _banner(app)[2] == ""  # no button while busy
    assert _pump(app, 10, lambda: bool(app.shown))
    assert "CABLE Output" in app.shown[0]
    assert _pump(app, 5, lambda: not _banner(app)[0])  # banner gone: the cable is in use
    assert app.engine.status().virtual_device


def test_window_reports_declined_prompt(window, monkeypatch: pytest.MonkeyPatch) -> None:
    app = window
    assert _pump(app, 5, lambda: app.engine is not None)

    def declined(dest: Path, progress, cancel) -> None:
        raise vbcable.InstallCancelled("declined")

    monkeypatch.setattr(vbcable, "install", declined)
    app.install_cable()
    assert _pump(app, 5, lambda: _banner(app)[2] == "Try again")
    assert "declined" in _banner(app)[1]


def test_window_asks_for_restart_when_device_is_missing(window, monkeypatch: pytest.MonkeyPatch) -> None:
    app = window
    assert _pump(app, 5, lambda: app.engine is not None)
    monkeypatch.setattr(vbcable, "install", lambda dest, progress, cancel: None)
    app.install_cable()
    assert _pump(app, 10, lambda: _banner(app)[2] == "Check again")
    assert "Restart Windows" in _banner(app)[1]
    FakeEngine.cable_installed = True  # e.g. after Windows finished setting it up
    app.recheck_cable()
    assert _pump(app, 10, lambda: bool(app.shown))
    assert _pump(app, 5, lambda: not _banner(app)[0])
