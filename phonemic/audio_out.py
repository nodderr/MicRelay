"""Audio output: device discovery, sounddevice playback, null output and WAV recording."""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable, Iterator, Protocol

import numpy as np

log = logging.getLogger(__name__)

BLOCK_SECONDS = 0.010
VIRTUAL_PATTERNS = ("cable input", "blackhole", "loopback", "phonemic")
# Lower index = preferred when the same device is exposed by several host APIs.
HOSTAPI_PREFERENCE = (
    "windows wasapi",
    "core audio",
    "alsa",
    "windows directsound",
    "mme",
    "windows wdm-ks",
)
LINUX_SINK = "phonemic"
LINUX_SOURCE = "phonemic_mic"
LINUX_PULSE_DEVICES = ("pulse", "pipewire", "default")

Source = Callable[[np.ndarray], None]


class Output(Protocol):
    samplerate: int
    description: str

    def start(self, source: Source) -> None: ...
    def stop(self) -> None: ...
    def set_gain_db(self, gain_db: float) -> None: ...
    def take_peak(self) -> float: ...
    @property
    def latency_s(self) -> float: ...
    @property
    def xruns(self) -> int: ...


@dataclass(frozen=True)
class DeviceChoice:
    index: int
    name: str
    hostapi: str
    channels: int
    samplerate: int
    is_virtual: bool


@contextlib.contextmanager
def high_resolution_timers() -> Iterator[None]:
    """Ask Windows for 1 ms timer resolution so time.sleep() paces accurately."""
    winmm = getattr(ctypes, "windll", None) and ctypes.windll.winmm
    if winmm:
        winmm.timeBeginPeriod(1)
    try:
        yield
    finally:
        if winmm:
            winmm.timeEndPeriod(1)


# --------------------------------------------------------------------- devices
def import_sounddevice() -> ModuleType:
    """Import sounddevice with a helpful error if PortAudio is missing."""
    try:
        import sounddevice
    except OSError as exc:  # PortAudio shared library not found
        raise RuntimeError(
            "sounddevice could not load PortAudio "
            f"({exc}). On Linux install it with e.g. 'sudo apt install libportaudio2'."
        ) from exc
    return sounddevice


def hostapi_rank(hostapi: str) -> int:
    name = hostapi.lower()
    for rank, preferred in enumerate(HOSTAPI_PREFERENCE):
        if name == preferred:
            return rank
    return len(HOSTAPI_PREFERENCE)


def list_output_devices() -> list[DeviceChoice]:
    sd = import_sounddevice()
    hostapis = sd.query_hostapis()
    devices = []
    for index, info in enumerate(sd.query_devices()):
        if info["max_output_channels"] < 1:
            continue
        name = str(info["name"])
        devices.append(
            DeviceChoice(
                index=index,
                name=name,
                hostapi=str(hostapis[info["hostapi"]]["name"]),
                channels=int(info["max_output_channels"]),
                samplerate=int(info["default_samplerate"]),
                is_virtual=any(p in name.lower() for p in VIRTUAL_PATTERNS),
            )
        )
    return devices


def print_devices() -> None:
    devices = list_output_devices()
    print("Output devices (* = looks like a virtual cable):")
    for dev in devices:
        mark = "*" if dev.is_virtual else " "
        print(
            f" {mark} [{dev.index:3d}] {dev.name}  ({dev.hostapi}, "
            f"{dev.channels} ch, {dev.samplerate} Hz)"
        )
    if sys.platform.startswith("linux") and linux_sink_exists():
        print(f"   PulseAudio/PipeWire sink '{LINUX_SINK}' exists and will be used via 'pulse'.")


def _best_by_hostapi(candidates: list[DeviceChoice]) -> DeviceChoice | None:
    if not candidates:
        return None
    return min(candidates, key=lambda d: (hostapi_rank(d.hostapi), d.index))


def find_device(spec: str) -> DeviceChoice:
    """Resolve --device: a numeric index or a case-insensitive name substring."""
    devices = list_output_devices()
    if spec.strip().isdigit():
        index = int(spec)
        for dev in devices:
            if dev.index == index:
                return dev
        raise ValueError(f"No output device with index {index} (see --list-devices)")
    needle = spec.lower()
    best = _best_by_hostapi([d for d in devices if needle in d.name.lower()])
    if best is None:
        raise ValueError(f"No output device matching '{spec}' (see --list-devices)")
    return best


def auto_select_device() -> DeviceChoice | None:
    """Pick a virtual cable if one is installed."""
    devices = list_output_devices()
    if sys.platform.startswith("linux") and linux_sink_exists():
        for wanted in LINUX_PULSE_DEVICES:
            for dev in devices:
                if dev.name.lower() == wanted:
                    return DeviceChoice(
                        dev.index, f"{dev.name} -> sink '{LINUX_SINK}'", dev.hostapi,
                        dev.channels, dev.samplerate, True,
                    )
    for pattern in VIRTUAL_PATTERNS:
        best = _best_by_hostapi([d for d in devices if pattern in d.name.lower()])
        if best is not None:
            return best
    return None


def default_output_device() -> DeviceChoice:
    """The system default output, via the lowest-latency host API that has one."""
    sd = import_sounddevice()
    by_index = {dev.index: dev for dev in list_output_devices()}
    hostapis = sorted(sd.query_hostapis(), key=lambda api: hostapi_rank(api["name"]))
    for api in hostapis:
        dev = by_index.get(api.get("default_output_device", -1))
        if dev is not None:
            return dev
    raise RuntimeError("No default output device found; use --device or --null-output")


def virtual_mic_hint() -> str:
    """How to install a virtual microphone on this OS."""
    if sys.platform == "win32":
        return (
            "No virtual cable found. Install VB-Cable (https://vb-audio.com/Cable/), reboot,\n"
            "then rerun: phonemic plays into 'CABLE Input' and Zoom/Discord/OBS select\n"
            "'CABLE Output' as their microphone."
        )
    if sys.platform == "darwin":
        return (
            "No virtual cable found. Install BlackHole 2ch "
            "(https://existential.audio/blackhole/ or 'brew install blackhole-2ch'),\n"
            "then rerun: phonemic plays into 'BlackHole 2ch' and apps select "
            "'BlackHole 2ch' as their microphone."
        )
    return (
        "No virtual sink found. Run 'python -m phonemic --setup-linux' once per login;\n"
        f"apps then select '{LINUX_SOURCE}' as their microphone."
    )


# ----------------------------------------------------------------------- Linux
def _pactl(*args: str) -> str:
    result = subprocess.run(
        ["pactl", *args], capture_output=True, text=True, check=True, timeout=10
    )
    return result.stdout


def _pactl_names(kind: str) -> set[str]:
    names = set()
    for line in _pactl("list", "short", kind).splitlines():
        parts = line.split("\t")
        if len(parts) > 1:
            names.add(parts[1])
    return names


def linux_sink_exists() -> bool:
    if not shutil.which("pactl"):
        return False
    try:
        return LINUX_SINK in _pactl_names("sinks")
    except (subprocess.SubprocessError, OSError):
        return False


def prepare_linux_routing() -> None:
    """Route PortAudio's 'pulse' device into the phonemic sink, if it exists.

    Must run before the first stream is opened.
    """
    if sys.platform.startswith("linux") and linux_sink_exists():
        os.environ.setdefault("PULSE_SINK", LINUX_SINK)
        os.environ.setdefault("PIPEWIRE_NODE", LINUX_SINK)
        log.debug("PULSE_SINK=%s", os.environ["PULSE_SINK"])


def setup_linux() -> int:
    """Create a null sink plus a remapped source named phonemic_mic using pactl."""
    if not shutil.which("pactl"):
        print("pactl not found. Install pulseaudio-utils (works with PipeWire too).")
        return 1
    try:
        modules = []
        if LINUX_SINK not in _pactl_names("sinks"):
            modules.append(
                _pactl(
                    "load-module", "module-null-sink", f"sink_name={LINUX_SINK}",
                    f"sink_properties=device.description={LINUX_SINK}_sink",
                ).strip()
            )
            print(f"Created null sink '{LINUX_SINK}'.")
        else:
            print(f"Null sink '{LINUX_SINK}' already exists.")
        if LINUX_SOURCE not in _pactl_names("sources"):
            modules.append(
                _pactl(
                    "load-module", "module-remap-source", f"master={LINUX_SINK}.monitor",
                    f"source_name={LINUX_SOURCE}",
                    f"source_properties=device.description={LINUX_SOURCE}",
                ).strip()
            )
            print(f"Created virtual microphone '{LINUX_SOURCE}'.")
        else:
            print(f"Virtual microphone '{LINUX_SOURCE}' already exists.")
    except subprocess.CalledProcessError as exc:
        print(f"pactl failed: {exc.stderr or exc}")
        return 1
    print(
        f"\nDone. Start phonemic normally; in your apps pick '{LINUX_SOURCE}' as the microphone.\n"
        "These modules last until logout. To remove them now: "
        + (" ; ".join(f"pactl unload-module {m}" for m in modules) or "pactl unload-module <id>")
        + "\nTo make them permanent, add the two load-module lines to ~/.config/pulse/default.pa\n"
        "(PulseAudio) or run --setup-linux from your session autostart (PipeWire)."
    )
    return 0


# ------------------------------------------------------------------- recording
class WavRecorder:
    """Writes mono float blocks to a 16-bit WAV from a background thread."""

    def __init__(self, path: Path, samplerate: int) -> None:
        self.path = path
        self.samplerate = samplerate
        self._queue: queue.SimpleQueue[np.ndarray | None] = queue.SimpleQueue()
        self._file = open(path, "wb")
        self._wav = wave.open(self._file, "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(samplerate)
        self._thread = threading.Thread(target=self._run, name="wav-writer", daemon=True)
        self._thread.start()

    def write(self, block: np.ndarray) -> None:
        """Called from the audio thread: just hands a copy to the writer."""
        self._queue.put(block.copy())

    def _run(self) -> None:
        while True:
            block = self._queue.get()
            if block is None:
                break
            pcm = np.clip(block * 32767.0, -32768, 32767).astype("<i2")
            self._wav.writeframes(pcm.tobytes())  # also patches the header
            self._file.flush()  # keep the file valid even if the process is killed

    def close(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=5)
        self._wav.close()
        self._file.close()
        log.info("Recording saved to %s", self.path)


# --------------------------------------------------------------------- outputs
class _BlockRenderer:
    """Shared per-block processing: pull, gain, clip, record, level metering."""

    def __init__(self, gain_db: float, recorder: WavRecorder | None) -> None:
        self._gain = np.float32(1.0)
        self.set_gain_db(gain_db)
        self._recorder = recorder
        self._mono = np.zeros(4096, dtype=np.float32)
        self._source: Source | None = None
        self._peak = 0.0

    def set_gain_db(self, gain_db: float) -> None:
        """Change the output gain; safe to call while the stream is running."""
        self._gain = np.float32(10.0 ** (gain_db / 20.0))

    def take_peak(self) -> float:
        """Peak level (0..1) of what was played since the previous call."""
        peak, self._peak = self._peak, 0.0
        return peak

    def render(self, frames: int) -> np.ndarray:
        if frames > len(self._mono):
            self._mono = np.zeros(frames, dtype=np.float32)
        mono = self._mono[:frames]
        if self._source is None:
            mono.fill(0.0)
        else:
            self._source(mono)
        if self._gain != 1.0:
            mono *= self._gain
        np.clip(mono, -1.0, 1.0, out=mono)
        self._peak = max(self._peak, float(mono.max()), -float(mono.min()))
        if self._recorder is not None:
            self._recorder.write(mono)
        return mono


class SoundDeviceOutput(_BlockRenderer):
    """Low-latency PortAudio output stream (mono duplicated to every channel)."""

    def __init__(
        self, device: DeviceChoice, gain_db: float = 0.0, recorder: WavRecorder | None = None
    ) -> None:
        super().__init__(gain_db, recorder)
        self.device = device
        self.samplerate = device.samplerate
        self.description = f"{device.name} ({device.hostapi}, {device.samplerate} Hz)"
        self._stream = None
        self._xruns = 0

    def start(self, source: Source) -> None:
        sd = import_sounddevice()
        self._source = source
        try:
            self._stream = sd.OutputStream(
                device=self.device.index,
                samplerate=self.samplerate,
                channels=min(self.device.channels, 8),
                dtype="float32",
                blocksize=round(self.samplerate * BLOCK_SECONDS),
                latency="low",
                callback=self._callback,
            )
            self._stream.start()
        except sd.PortAudioError as exc:
            raise RuntimeError(
                f"Cannot open output device '{self.device.name}': {exc}. "
                "Pick another one with --device (see --list-devices)."
            ) from exc
        log.info(
            "Audio output started: %s, %d ch, stream latency %.1f ms",
            self.description, self._stream.channels, self.latency_s * 1000,
        )

    def _callback(self, outdata: np.ndarray, frames: int, time_info: object, status: object) -> None:
        if status:
            self._xruns += 1
        outdata[:] = self.render(frames)[:, None]

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    @property
    def latency_s(self) -> float:
        return float(self._stream.latency) if self._stream is not None else 0.0

    @property
    def xruns(self) -> int:
        return self._xruns


class NullOutput(_BlockRenderer):
    """Consumes audio in real time without a sound device (for testing)."""

    def __init__(
        self, samplerate: int = 48000, gain_db: float = 0.0, recorder: WavRecorder | None = None
    ) -> None:
        super().__init__(gain_db, recorder)
        self.samplerate = samplerate
        self.description = f"null output ({samplerate} Hz)"
        self._block = round(samplerate * BLOCK_SECONDS)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._xruns = 0

    def start(self, source: Source) -> None:
        self._source = source
        self._thread = threading.Thread(target=self._run, name="null-output", daemon=True)
        self._thread.start()
        log.info("Audio output started: %s", self.description)

    def _run(self) -> None:
        with high_resolution_timers():
            self._pace()

    def _pace(self) -> None:
        period = self._block / self.samplerate
        deadline = time.perf_counter()
        while not self._stop.is_set():
            self.render(self._block)
            deadline += period
            delay = deadline - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.2:  # fell far behind (e.g. machine suspended): resync
                self._xruns += 1
                deadline = time.perf_counter()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    @property
    def latency_s(self) -> float:
        return BLOCK_SECONDS

    @property
    def xruns(self) -> int:
        return self._xruns
