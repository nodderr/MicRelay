"""Simulated phone: connects to a running phonemic server exactly like the
browser page (HTTPS POST /offer + two data channels) and streams a sine tone.

Usage:
    python tests/fake_phone.py "https://192.168.1.20:8443/?t=TOKEN" --seconds 10
    python tests/fake_phone.py URL --loss 0.05 --jitter-ms 20
    python tests/fake_phone.py --check out.wav          # analyse a --record file

End-to-end check:
    python -m phonemic --null-output --record out.wav --run-seconds 12
    python tests/fake_phone.py URL --seconds 8
    python tests/fake_phone.py --check out.wav
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import struct
import sys
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

import aiohttp
import numpy as np
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # allow running as a script
from phonemic.audio_out import high_resolution_timers  # noqa: E402

log = logging.getLogger("fake_phone")
HEADER = struct.Struct("<II")


@dataclass
class PhoneResult:
    chunks: int
    sent: int
    dropped: int
    rtt_ms: float | None


@dataclass
class ToneAnalysis:
    duration_s: float
    peak_hz: float
    snr_db: float
    rms: float
    dropout_fraction: float


def _offer_url_and_token(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    token = parse_qs(parts.query).get("t", [""])[0]
    if not token:
        raise ValueError("URL must contain the ?t=TOKEN printed by phonemic")
    return f"{parts.scheme}://{parts.netloc}/offer", token


def _sine_chunk(pos: int, n: int, rate: int, freq: float, amplitude: float) -> bytes:
    t = (pos + np.arange(n)) / rate
    pcm = np.round(amplitude * 32767 * np.sin(2 * np.pi * freq * t)).astype("<i2")
    return pcm.tobytes()


def _send_schedule(total: int, loss: float, jitter_ms: float, rng: random.Random) -> list[tuple[float, int]]:
    """(send time in s, seq) for every packet that is not lost, in send order.

    Chunk `seq` is complete at (seq + 1) * 10 ms; jitter delays it randomly,
    which also reorders packets.
    """
    schedule = []
    for seq in range(total):
        if rng.random() < loss:
            continue
        schedule.append(((seq + 1) * 0.010 + rng.uniform(0.0, jitter_ms) / 1000.0, seq))
    schedule.sort()
    return schedule


async def _paced(
    loop: asyncio.AbstractEventLoop, schedule: list[tuple[float, int]], send: Callable[[int], None]
) -> None:
    """Run `send(seq)` on the event loop at the scheduled times.

    Pacing happens in a thread with time.sleep because asyncio timers on
    Windows can fire 15+ ms late, which would make the fake phone far burstier
    than a real AudioWorklet.
    """
    done = asyncio.Event()

    def pace() -> None:
        with high_resolution_timers():
            start = time.perf_counter()
            for when, seq in schedule:
                delay = start + when - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                loop.call_soon_threadsafe(send, seq)
        loop.call_soon_threadsafe(done.set)

    threading.Thread(target=pace, name="fake-phone-pacer", daemon=True).start()
    await done.wait()


async def _wait_open(channel: object, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while channel.readyState != "open":  # type: ignore[attr-defined]
        if time.monotonic() > deadline:
            raise TimeoutError(f"data channel {channel.label} did not open")  # type: ignore[attr-defined]
        await asyncio.sleep(0.02)


async def run_phone(
    url: str,
    seconds: float = 5.0,
    freq: float = 440.0,
    rate: int = 48000,
    loss: float = 0.0,
    jitter_ms: float = 0.0,
    amplitude: float = 0.5,
    seed: int | None = None,
) -> PhoneResult:
    """Connect, stream `seconds` of a sine wave, then send stop and disconnect."""
    offer_url, token = _offer_url_and_token(url)
    rng = random.Random(seed)
    loop = asyncio.get_running_loop()
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    audio = pc.createDataChannel("audio", ordered=False, maxRetransmits=0)
    ctl = pc.createDataChannel("ctl")
    rtts: list[float] = []

    def on_ctl(message: object) -> None:
        msg = json.loads(message) if isinstance(message, str) else {}
        if msg.get("type") == "ping":
            ctl.send(json.dumps({"type": "pong", "t": msg.get("t")}))
        elif msg.get("type") == "pong":
            rtts.append(time.perf_counter() * 1000 - msg["t"])

    ctl.on("message", on_ctl)
    try:
        await pc.setLocalDescription(await pc.createOffer())
        payload = {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type, "token": token}
        async with aiohttp.ClientSession() as http:
            async with http.post(offer_url, json=payload, ssl=False) as resp:
                resp.raise_for_status()
                answer = await resp.json()
        await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
        await _wait_open(ctl, 15.0)
        await _wait_open(audio, 5.0)

        chunk = rate // 100
        ctl.send(json.dumps({"type": "hello", "sampleRate": rate, "chunkSamples": chunk,
                             "userAgent": "fake_phone.py (aiortc)"}))
        total = int(seconds * 100)
        schedule = _send_schedule(total, loss, jitter_ms, rng)
        sent = len(schedule)
        dropped = total - sent

        def send(seq: int) -> None:
            pos = seq * chunk
            header = HEADER.pack(seq & 0xFFFFFFFF, pos & 0xFFFFFFFF)
            if audio.readyState == "open":
                audio.send(header + _sine_chunk(pos, chunk, rate, freq, amplitude))
            if seq % 100 == 0:
                ctl.send(json.dumps({"type": "ping", "t": time.perf_counter() * 1000}))

        await _paced(loop, schedule, send)
        await asyncio.sleep(jitter_ms / 1000.0 + 0.2)
        ctl.send(json.dumps({"type": "stop"}))
        await asyncio.sleep(0.2)
    finally:
        await pc.close()
    rtt = float(np.median(rtts)) if rtts else None
    return PhoneResult(chunks=total, sent=sent, dropped=dropped, rtt_ms=rtt)


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError("expected a 16-bit WAV")
        rate = wav.getframerate()
        channels = wav.getnchannels()
        data = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
    data = data.reshape(-1, channels)[:, 0].astype(np.float64) / 32768.0
    return data, rate


def analyze_tone(path: Path, freq: float = 440.0, trim_s: float = 0.3) -> ToneAnalysis:
    """Find the active part of a recording and measure how clean the tone is."""
    signal, rate = read_wav(path)
    active = np.flatnonzero(np.abs(signal) > 0.02)
    if active.size == 0:
        return ToneAnalysis(0.0, 0.0, -np.inf, 0.0, 1.0)
    trim = int(trim_s * rate)
    segment = signal[active[0] + trim : active[-1] - trim]
    if segment.size < rate // 2:
        return ToneAnalysis(segment.size / rate, 0.0, -np.inf, 0.0, 1.0)

    window = np.hanning(segment.size)
    spectrum = np.abs(np.fft.rfft(segment * window)) ** 2
    freqs = np.fft.rfftfreq(segment.size, 1.0 / rate)
    spectrum[freqs < 20] = 0.0
    peak_hz = float(freqs[np.argmax(spectrum)])
    band = np.abs(freqs - freq) <= 10.0
    snr_db = 10 * np.log10(spectrum[band].sum() / max(spectrum[~band].sum(), 1e-20))

    block = rate // 100
    blocks = segment[: segment.size // block * block].reshape(-1, block)
    block_rms = np.sqrt(np.mean(blocks**2, axis=1))
    dropout_fraction = float(np.mean(block_rms < 0.25 * np.median(block_rms)))
    return ToneAnalysis(
        duration_s=segment.size / rate,
        peak_hz=peak_hz,
        snr_db=float(snr_db),
        rms=float(np.sqrt(np.mean(segment**2))),
        dropout_fraction=dropout_fraction,
    )


def is_clean_tone(result: ToneAnalysis, freq: float = 440.0) -> bool:
    return (
        abs(result.peak_hz - freq) <= 2.0
        and result.snr_db >= 30.0
        and result.dropout_fraction == 0.0
        and result.duration_s >= 1.0
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Simulated phonemic phone")
    parser.add_argument("url", nargs="?", help="URL printed by phonemic (with ?t=TOKEN)")
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--freq", type=float, default=440.0)
    parser.add_argument("--rate", type=int, default=48000, help="simulated phone sample rate")
    parser.add_argument("--loss", type=float, default=0.0, help="packet loss probability (0-1)")
    parser.add_argument("--jitter-ms", type=float, default=0.0, help="random extra send delay (reorders packets)")
    parser.add_argument("--check", type=Path, metavar="WAV", help="analyse a recording instead of streaming")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)

    if args.check:
        result = analyze_tone(args.check, args.freq)
        print(result)
        clean = is_clean_tone(result, args.freq)
        print("CLEAN TONE" if clean else "NOT CLEAN")
        return 0 if clean else 1
    if not args.url:
        parser.error("url is required unless --check is used")
    result = asyncio.run(run_phone(args.url, args.seconds, args.freq, args.rate, args.loss, args.jitter_ms))
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
