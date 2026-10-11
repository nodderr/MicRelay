"""A device that won't open must not stop phonemic: the next candidate is used instead."""

from __future__ import annotations

import socket
from dataclasses import replace
from pathlib import Path

import pytest

from phonemic import audio_out, engine as engine_mod
from phonemic.audio_out import DeviceChoice
from phonemic.engine import Engine, EngineSettings

HDMI_WASAPI = DeviceChoice(30, "HA240Y GO (NVIDIA High Definition Audio)", "Windows WASAPI", 2, 48000, False)
HDMI_DS = DeviceChoice(20, "HA240Y GO (NVIDIA High Definition Audio)", "Windows DirectSound", 2, 48000, False)
HDMI_MME = DeviceChoice(5, "HA240Y GO (NVIDIA High Definiti", "MME", 2, 44100, False)
HDMI_KS = DeviceChoice(40, "HA240Y GO (NVIDIA High Definition Audio)", "Windows WDM-KS", 2, 48000, False)
SPEAKERS = DeviceChoice(31, "Speakers (Realtek(R) Audio)", "Windows WASAPI", 2, 48000, False)
CABLE = DeviceChoice(32, "CABLE Input (VB-Audio Virtual Cable)", "Windows WASAPI", 2, 48000, True)
DEVICES = [HDMI_MME, HDMI_DS, HDMI_WASAPI, SPEAKERS, CABLE, HDMI_KS]


@pytest.fixture
def devices(monkeypatch: pytest.MonkeyPatch):
    """A fake Windows device list; the HDMI monitor is the default output."""
    state = {"devices": list(DEVICES)}
    monkeypatch.setattr(audio_out, "list_output_devices", lambda: list(state["devices"]))
    monkeypatch.setattr(audio_out, "_default_output_devices", lambda devs: [d for d in (HDMI_WASAPI, HDMI_DS, HDMI_MME, HDMI_KS) if d in devs])
    return state


class FakeOutput:
    """SoundDeviceOutput stand-in that refuses to start on 'broken' devices."""

    broken: set[int] = set()
    started: list[DeviceChoice] = []

    def __init__(self, device: DeviceChoice, gain_db: float = 0.0, recorder: object = None) -> None:
        self.device = device
        self.samplerate = device.samplerate
        self.description = f"{device.name} ({device.hostapi})"
        self.latency_s = 0.01
        self.xruns = 0

    def start(self, source: object) -> None:
        if self.device.index in FakeOutput.broken:
            raise RuntimeError(f"Cannot open output device '{self.device.name}' ({self.device.hostapi}): host error")
        FakeOutput.started.append(self.device)

    def stop(self) -> None: ...
    def set_gain_db(self, gain_db: float) -> None: ...
    def take_peak(self) -> float: return 0.0


@pytest.fixture
def fake_output(monkeypatch: pytest.MonkeyPatch, devices):
    FakeOutput.broken, FakeOutput.started = set(), []
    monkeypatch.setattr(engine_mod, "SoundDeviceOutput", FakeOutput)
    return FakeOutput


def _settings(tmp_path: Path, device: str | None = None) -> EngineSettings:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return EngineSettings(port=port, host="127.0.0.1", bind="127.0.0.1", device=device, config_dir=tmp_path / "cfg")


def test_candidates_auto_prefers_cable_then_defaults(devices) -> None:
    names = [(d.name, d.hostapi) for d in audio_out.output_candidates(None)]
    assert names[0] == (CABLE.name, "Windows WASAPI")
    assert (HDMI_WASAPI.name, "Windows WASAPI") in names
    assert all(api != "Windows WDM-KS" for _, api in names)  # never a fallback


def test_candidates_for_a_named_device_try_its_other_host_apis_first(devices) -> None:
    order = audio_out.output_candidates("HA240Y")
    assert [d.index for d in order[:3]] == [HDMI_WASAPI.index, HDMI_DS.index, HDMI_MME.index]
    assert CABLE in order and HDMI_KS not in order
    assert audio_out.output_candidates("HA240Y", fallback=False) == [HDMI_WASAPI, HDMI_DS, HDMI_MME]


def test_engine_falls_back_when_the_default_output_will_not_open(tmp_path: Path, fake_output, devices) -> None:
    devices["devices"] = [d for d in DEVICES if d is not CABLE]  # a PC without VB-Cable
    fake_output.broken = {HDMI_WASAPI.index, HDMI_DS.index, HDMI_MME.index}  # monitor audio unavailable
    engine = Engine(_settings(tmp_path))
    try:
        engine.start()
        status = engine.status()
        assert status.running
        assert fake_output.started == [SPEAKERS]
        assert "HA240Y GO" in (status.output_warning or "") and "Speakers" in status.output_warning
        assert not status.virtual_device
    finally:
        engine.stop()


def test_engine_uses_another_host_api_for_the_same_device(tmp_path: Path, fake_output) -> None:
    fake_output.broken = {HDMI_WASAPI.index}
    engine = Engine(_settings(tmp_path, device="HA240Y GO"))
    try:
        engine.start()
        assert fake_output.started == [HDMI_DS]
        assert engine.jitter is not None and engine.jitter.out_rate == HDMI_DS.samplerate
    finally:
        engine.stop()


def test_engine_reports_when_nothing_opens(tmp_path: Path, fake_output) -> None:
    fake_output.broken = {d.index for d in DEVICES}
    engine = Engine(_settings(tmp_path))
    with pytest.raises(RuntimeError, match="Cannot open output device 'CABLE Input.*other outputs also failed"):
        engine.start()
    assert not engine.running


def test_choosing_a_broken_device_keeps_the_current_one(tmp_path: Path, fake_output) -> None:
    engine = Engine(_settings(tmp_path))
    try:
        engine.start()
        assert engine.status().virtual_device  # the cable
        fake_output.broken = {HDMI_WASAPI.index, HDMI_DS.index, HDMI_MME.index}
        with pytest.raises(RuntimeError, match="HA240Y"):
            engine.set_device("HA240Y GO")
        assert fake_output.started[-1] == CABLE  # restarted, not some other device
        assert engine.settings.device is None
        assert engine.status().output_warning is None
    finally:
        engine.stop()


def test_mme_names_are_matched_by_their_truncated_prefix() -> None:
    assert HDMI_WASAPI.name.lower()[: audio_out.MME_NAME_CHARS] == HDMI_MME.name.lower()
    assert replace(HDMI_MME, name="HA240Y GO") != HDMI_MME
