"""Simulation tests for the jitter buffer, drift controller and resampler."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pytest

from phonemic.jitter import JitterBuffer, _unwrap32

SignalFn = Callable[[int, int], np.ndarray]  # (start_pos, n) -> int16 samples
T0 = 0.0  # fixed virtual time for the step-by-step unit tests


def sine_signal(freq: float, rate: int, amplitude: float = 0.5) -> SignalFn:
    def make(pos: int, n: int) -> np.ndarray:
        t = (pos + np.arange(n)) / rate
        return np.round(amplitude * 32767 * np.sin(2 * np.pi * freq * t)).astype(np.int16)

    return make


def ramp_signal(pos: int, n: int) -> np.ndarray:
    """Every position maps to a unique, non-zero int16 value (period 20000)."""
    return ((pos + np.arange(n)) % 20000 + 1).astype(np.int16)


@dataclass
class SimResult:
    output: np.ndarray
    fills_ms: list[float] = field(default_factory=list)
    buffer_ms: list[float] = field(default_factory=list)
    times: list[float] = field(default_factory=list)
    drift_ppm: list[float] = field(default_factory=list)
    dropped_seqs: set[int] = field(default_factory=set)


def simulate(
    buf: JitterBuffer,
    in_rate: int,
    seconds: float,
    *,
    ppm: float = 0.0,
    loss: float = 0.0,
    jitter_ms: float = 0.0,
    reorder: float = 0.0,
    signal: SignalFn | None = None,
    seed: int = 1,
) -> SimResult:
    """Discrete-event simulation of a phone feeding the buffer and a sound card pulling.

    The phone clock runs `ppm` parts-per-million fast relative to the sound card.
    Each 10 ms chunk is sent when it is complete, plus a random network delay;
    with probability `reorder` a packet is held back by 1.2 chunks so it
    arrives after its successor (adjacent swap).
    """
    rng = random.Random(seed)
    signal = signal or sine_signal(440.0, in_rate)
    chunk = in_rate // 100
    block = buf.out_rate // 100
    chunk_period = chunk / (in_rate * (1.0 + ppm * 1e-6))
    n_chunks = int(seconds / chunk_period) + 50

    arrivals: list[tuple[float, int]] = []
    dropped: set[int] = set()
    for seq in range(n_chunks):
        if rng.random() < loss:
            dropped.add(seq)
            continue
        delay = rng.uniform(0.0, jitter_ms) / 1000.0
        if rng.random() < reorder:
            delay += 1.2 * chunk_period
        arrivals.append(((seq + 1) * chunk_period + delay, seq))
    arrivals.sort()

    result = SimResult(output=np.zeros(int(seconds * 100) * block, dtype=np.float32), dropped_seqs=dropped)
    out_block = np.zeros(block, dtype=np.float32)
    ai = 0
    for j in range(int(seconds * 100)):
        pull_time = (j + 1) * block / buf.out_rate
        while ai < len(arrivals) and arrivals[ai][0] <= pull_time:
            seq = arrivals[ai][1]
            buf.push(seq, seq * chunk, signal(seq * chunk, chunk), now=arrivals[ai][0])
            ai += 1
        stats = buf.stats()
        result.fills_ms.append(stats.fill_ms)
        result.buffer_ms.append(stats.buffer_ms)
        result.times.append(pull_time)
        result.drift_ppm.append(stats.drift_ppm)
        buf.pull(out_block, now=pull_time)
        result.output[j * block : (j + 1) * block] = out_block
    return result


# --------------------------------------------------------------------- tests
def test_steady_input_has_no_underruns() -> None:
    buf = JitterBuffer(48000, target_ms=30)
    buf.configure(48000)
    sim = simulate(buf, 48000, seconds=60, jitter_ms=3)
    stats = buf.stats()
    assert stats.underruns == 0
    assert stats.overruns == 0
    assert stats.lost == 0 and stats.late == 0
    buffer_ms = np.array(sim.buffer_ms)
    assert np.all(np.abs(buffer_ms[100:] - 30.0) < 3.0)  # from the first second on


@pytest.mark.parametrize("ppm", [300.0, -300.0])
def test_clock_drift_stays_within_10ms_over_10_minutes(ppm: float) -> None:
    buf = JitterBuffer(48000, target_ms=30)
    buf.configure(48000)
    sim = simulate(buf, 48000, seconds=600, ppm=ppm, jitter_ms=3, seed=7)
    stats = buf.stats()
    times = np.array(sim.times)
    buffer_ms = np.array(sim.buffer_ms)
    raw = np.array(sim.fills_ms)
    playing = times > 1.0  # skip the initial fill-up of the buffer
    assert stats.underruns == 0
    assert stats.overruns == 0
    assert np.max(np.abs(buffer_ms[playing] - 30.0)) <= 10.0
    # The raw fill also contains the not-yet-due part of the newest packet
    # (0..10 ms on top of the delay), so it stays within target-10 .. target+20.
    assert np.all(raw[playing] >= 30.0 - 10.0)
    assert np.all(raw[playing] <= 30.0 + 20.0)
    # On average the resampling correction cancels the clock offset exactly.
    late_half = np.array(sim.drift_ppm)[times > 300.0]
    assert float(np.mean(late_half)) == pytest.approx(ppm, abs=30.0)


def test_loss_and_reordering_keep_output_aligned() -> None:
    rate = 48000
    chunk = rate // 100
    buf = JitterBuffer(rate, target_ms=30, drift_correction=False)
    buf.configure(rate)
    sim = simulate(
        buf, rate, seconds=30, loss=0.05, jitter_ms=2, reorder=0.05, signal=ramp_signal, seed=3
    )
    stats = buf.stats()
    assert stats.underruns == 0
    assert stats.late == 0  # reordered packets were all within the buffer depth

    values = np.round(sim.output * 32768).astype(np.int64)
    first = int(np.flatnonzero(values)[0])
    start_pos = int(values[first]) - 1  # value = pos % 20000 + 1; output starts at pos 0 period
    # Reconstruct the absolute position of output sample `first`.
    played = values[first:]
    positions = np.arange(played.size) + start_pos
    expected = (positions % 20000 + 1).astype(np.int64)
    seqs = positions // chunk
    lost_mask = np.isin(seqs, list(sim.dropped_seqs))
    # Every sample is either the right value or silence for a lost packet: no
    # samples were skipped, duplicated or shifted, so the output length is exact.
    assert np.array_equal(played[~lost_mask], expected[~lost_mask])
    assert np.all(played[lost_mask] == 0)
    assert np.mean(lost_mask) == pytest.approx(0.05, abs=0.02)
    assert abs(stats.lost - len([s for s in sim.dropped_seqs if s < seqs[-1]])) <= 2


def test_loss_with_drift_correction_preserves_timeline() -> None:
    rate = 48000
    buf = JitterBuffer(rate, target_ms=30)
    buf.configure(rate)
    seconds = 60
    sim = simulate(buf, rate, seconds=seconds, loss=0.05, jitter_ms=2, reorder=0.05, seed=11)
    stats = buf.stats()
    block = rate // 100
    assert stats.underruns == 0
    assert sim.output.size == seconds * 100 * block
    # Loss does not make the controller speed up or slow down playback: the
    # buffer stays at its target and the average correction stays near zero.
    assert np.all(np.abs(np.array(sim.buffer_ms)[3000:] - 30.0) < 5.0)
    assert abs(float(np.mean(sim.drift_ppm[3000:]))) < 50.0


def test_resampler_44k1_to_48k_preserves_1khz_tone() -> None:
    buf = JitterBuffer(48000, target_ms=30)
    buf.configure(44100)
    sim = simulate(buf, 44100, seconds=4, signal=sine_signal(1000.0, 44100))
    out = sim.output[48000:]  # skip start-up, analyse 3 s
    spectrum = np.abs(np.fft.rfft(out * np.hanning(out.size))) ** 2
    freqs = np.fft.rfftfreq(out.size, 1 / 48000)
    peak = freqs[np.argmax(spectrum)]
    assert peak == pytest.approx(1000.0, abs=1.0)
    band = np.abs(freqs - 1000.0) <= 10.0
    spurious_db = 10 * np.log10(spectrum[~band].sum() / spectrum[band].sum())
    assert spurious_db < -50.0
    rms = np.sqrt(np.mean(out**2))
    assert rms == pytest.approx(0.5 / np.sqrt(2), rel=0.02)
    assert buf.stats().underruns == 0


def test_late_packet_is_dropped_and_counted() -> None:
    buf = JitterBuffer(48000, target_ms=20, drift_correction=False)
    buf.configure(48000)
    for seq in range(1, 5):
        buf.push(seq, seq * 480, np.ones(480, dtype=np.int16), now=T0)
    out = np.zeros(480, dtype=np.float32)
    for _ in range(3):
        buf.pull(out, now=T0)
    buf.push(0, 0, np.ones(480, dtype=np.int16))  # long after its slot played
    assert buf.stats().late == 1


def test_overrun_drops_oldest_audio_down_to_target() -> None:
    buf = JitterBuffer(48000, target_ms=30, drift_correction=False)
    buf.configure(48000)
    for seq in range(30):  # a 300 ms burst
        buf.push(seq, seq * 480, np.ones(480, dtype=np.int16), now=T0)
    stats = buf.stats()
    assert stats.overruns >= 1
    assert stats.fill_ms <= 30.0 + 40.0 + 10.0


def test_short_starvation_is_concealed_without_shifting_timeline() -> None:
    buf = JitterBuffer(48000, target_ms=30, drift_correction=False)
    buf.configure(48000)
    for seq in range(4):
        buf.push(seq, seq * 480, np.full(480, seq + 1, dtype=np.int16), now=T0)
    out = np.zeros(480, dtype=np.float32)
    for seq in range(4):
        buf.pull(out, now=T0)  # newest packet starts 30 ms ahead: chunks 0..3 play
        assert np.allclose(out, (seq + 1) / 32768)
    buf.pull(out, now=T0)  # chunk 4 never arrives (lost): silence
    assert np.all(out == 0)
    buf.push(5, 5 * 480, np.full(480, 6, dtype=np.int16), now=T0)
    buf.pull(out, now=T0)  # timeline kept: chunk 5 plays right after the gap
    assert np.allclose(out, 6 / 32768)
    stats = buf.stats()
    assert stats.concealed == 1
    assert stats.underruns == 0


def test_underrun_outputs_silence_then_rebuffers() -> None:
    buf = JitterBuffer(48000, target_ms=30, drift_correction=False)
    buf.configure(48000)
    for seq in range(4):
        buf.push(seq, seq * 480, np.full(480, 1000, dtype=np.int16), now=T0)
    out = np.zeros(480, dtype=np.float32)
    for _ in range(4 + 4):  # 4 blocks of audio, then a stall longer than the target
        buf.pull(out, now=T0)
    assert buf.stats().underruns == 1
    assert np.all(out == 0)
    buf.pull(out, now=T0)
    assert np.all(out == 0)  # still re-buffering
    for seq in range(7, 11):
        buf.push(seq, seq * 480, np.full(480, 1000, dtype=np.int16), now=T0)
    buf.pull(out, now=T0)
    assert np.allclose(out, 1000 / 32768)
    assert buf.stats().fill_ms == pytest.approx(30.0)


def test_unwrap32_handles_counter_wraparound() -> None:
    assert _unwrap32(5, 2**32 - 10) == 2**32 + 5
    assert _unwrap32(2**32 - 5, 2**32 + 3) == 2**32 - 5
    assert _unwrap32(100, 50) == 100
