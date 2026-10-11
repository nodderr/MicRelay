"""Jitter buffer with reordering, loss concealment, resampling and drift control.

Design
------
Every audio packet carries the sample position of its first sample, so the
buffer is a ring indexed by *absolute input sample position*:

* Writing a packet copies it to ``ring[pos % N]``. Reordered packets land in
  the right place automatically; packets that arrive after their slot has
  been played are dropped as *late*.
* Reading advances a fractional read position by ``step`` input samples per
  output sample and linearly interpolates. ``step`` combines the fixed rate
  conversion (phone rate -> output rate) with a small drift correction.
* Consumed ring slots are zeroed, so a lost packet simply plays back as
  silence of exactly the right length.
* If the newest audio has not arrived when it is due (typically the latest
  packet was lost), silence is played *without* shifting the timeline for up
  to one target length. Only a longer stall counts as an underrun: the output
  goes silent and the buffer re-fills to the target before playing again.

Buffer delay and drift control
------------------------------
The quantity that matters is how long each packet waits before it is played.
``pull()`` timestamps every block, which gives a continuous playout cursor;
``push()`` measures, for each arriving packet, the time until the cursor
reaches its first sample. Phones deliver packets in bursts, so the raw fill
level is a sawtooth whose phase slowly rotates against the sound card's
blocks; the per-packet delay has no such artefact.

A PI controller steers the smoothed delay towards ``target_ms`` by nudging
``step`` by at most +/-0.5 %, which absorbs clock drift between the phone and
the sound card. Starting (and re-starting after an underrun or overrun) puts
the newest packet exactly ``target_ms`` ahead of the read position.

Threading
---------
``push()`` runs on the asyncio thread and ``pull()`` on the audio thread. Both
hold the lock only briefly (well under 1 ms) and ``pull()`` works in
preallocated scratch arrays; its only allocation is one block-sized temporary.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass

import numpy as np

log = logging.getLogger(__name__)

MAX_DRIFT = 0.005  # +/-0.5 % maximum resampling-ratio correction
_U32 = 1 << 32


@dataclass(frozen=True)
class JitterStats:
    """Snapshot of the buffer state for status display and tests."""

    configured: bool
    playing: bool
    muted: bool
    in_rate: int
    out_rate: int
    target_ms: float
    buffer_ms: float  # smoothed time packets wait before playout (regulated)
    fill_ms: float  # instantaneous buffered audio
    drift_ppm: float
    received: int
    lost: int
    late: int
    underruns: int
    overruns: int
    concealed: int


class DriftController:
    """PI controller mapping buffer-delay error (seconds) to a ratio adjustment.

    With kp=0.1/s the loop settles in ~10 s; ki adds integral action so a
    constant clock offset is cancelled with zero steady-state error
    (damping ~0.7). Measurements are low-pass filtered first because each
    packet's delay includes network jitter.
    """

    def __init__(
        self,
        kp: float = 0.1,
        ki: float = 0.005,
        limit: float = MAX_DRIFT,
        smoothing_s: float = 0.5,
    ) -> None:
        self.kp = kp
        self.ki = ki
        self.limit = limit
        self.smoothing_s = smoothing_s
        self.adjust = 0.0
        self.smoothed: float | None = None
        self._integral = 0.0

    def reset(self) -> None:
        self.adjust = 0.0
        self.smoothed = None
        self._integral = 0.0

    def resync(self, value_s: float) -> None:
        """Forget the filtered value (after a jump) but keep the learned drift."""
        self.smoothed = value_s

    def update(self, value_s: float, target_s: float, dt: float) -> float:
        """Feed one measurement taken `dt` seconds after the previous one."""
        if self.smoothed is None:
            self.smoothed = value_s
        else:
            alpha = min(1.0, dt / self.smoothing_s)
            self.smoothed += alpha * (value_s - self.smoothed)
        error = self.smoothed - target_s
        proportional = self.kp * error
        integral = self._integral + error * dt
        if abs(proportional + self.ki * integral) <= self.limit:
            self._integral = integral  # anti-windup: integrate only when unsaturated
        raw = proportional + self.ki * self._integral
        self.adjust = max(-self.limit, min(self.limit, raw))
        return self.adjust


def _unwrap32(value: int, reference: int) -> int:
    """Extend a wrapping uint32 counter to an unbounded int near `reference`."""
    diff = (value - reference) % _U32
    if diff >= _U32 // 2:
        diff -= _U32
    return reference + diff


class JitterBuffer:
    """Thread-safe playout buffer; see the module docstring.

    ``push`` and ``pull`` accept an optional ``now`` (seconds, any monotonic
    clock shared by both) so simulations can drive the buffer with virtual time.
    """

    def __init__(
        self,
        out_rate: int,
        target_ms: float = 30.0,
        *,
        drift_correction: bool = True,
        ring_seconds: float = 4.0,
        resync_seconds: float = 1.0,
        max_block: int = 4096,
    ) -> None:
        if target_ms <= 0:
            raise ValueError("target_ms must be positive")
        self.out_rate = int(out_rate)
        self.target_ms = float(target_ms)
        self.drift_correction = drift_correction
        self._ring_seconds = ring_seconds
        self._resync_seconds = resync_seconds
        self._lock = threading.Lock()
        self._controller = DriftController()
        self._alloc_scratch(max_block)
        self._configured = False
        self._muted = False
        self._in_rate = 0
        self._ring = np.zeros(1, dtype=np.float32)
        self._reset_stats()
        self._reset_position()

    # ------------------------------------------------------------------ setup
    def _alloc_scratch(self, size: int) -> None:
        self._max_block = size
        # Covers rates up to 192 kHz -> 8 kHz (step 24) with +/-0.5 % drift.
        self._k = np.arange(size * 25 + 4, dtype=np.float64)
        self._p = np.empty(size, dtype=np.float64)
        self._window = np.zeros(size * 25 + 4, dtype=np.float32)

    def _reset_stats(self) -> None:
        self._received = 0
        self._late = 0
        self._underruns = 0
        self._overruns = 0
        self._concealed = 0
        self._lost_carry = 0
        self._seq_first: int | None = None
        self._seq_max = 0
        self._seq_received = 0

    def _reset_position(self) -> None:
        """Forget the stream timeline (next packet re-anchors it)."""
        self._anchored = False
        self._playing = False
        self._starved = 0.0
        self._read_pos = 0.0
        self._consumed = 0
        self._highest_end = 0
        self._newest_start = 0
        self._newest_arrival = 0.0
        self._pull_time = 0.0
        self._pull_start = 0.0  # read position at the last pull
        self._pull_span = 0.0  # input samples consumed by the last pull
        self._ring.fill(0.0)
        # Close out sequence accounting so an intentional gap isn't "loss".
        self._lost_carry += self._current_lost()
        self._seq_first = None
        self._seq_received = 0

    def configure(self, in_rate: int) -> None:
        """Start a new session with the phone's sample rate (resets everything)."""
        if not 8000 <= in_rate <= 192000:
            raise ValueError(f"unsupported sample rate {in_rate}")
        with self._lock:
            self._in_rate = int(in_rate)
            self._ring = np.zeros(int(in_rate * self._ring_seconds), dtype=np.float32)
            self._controller.reset()
            self._reset_stats()
            self._reset_position()
            self._muted = False
            self._configured = True
        log.debug("Jitter buffer configured: %d Hz -> %d Hz", in_rate, self.out_rate)

    def clear(self) -> None:
        """End the session: output silence until configure() is called again."""
        with self._lock:
            self._configured = False
            self._reset_position()

    def reset(self) -> None:
        """Drop buffered audio and re-buffer from the next packet (keeps stats)."""
        with self._lock:
            self._reset_position()

    def set_target_ms(self, target_ms: float) -> None:
        """Change the target while running; the controller glides to it."""
        if target_ms <= 0:
            raise ValueError("target_ms must be positive")
        with self._lock:
            self.target_ms = float(target_ms)

    def set_out_rate(self, out_rate: int) -> None:
        """Switch to an output device with a different sample rate."""
        with self._lock:
            self.out_rate = int(out_rate)

    def set_muted(self, muted: bool) -> None:
        with self._lock:
            if muted != self._muted:
                self._muted = muted
                self._reset_position()

    # ---------------------------------------------------------- derived values
    @property
    def _target_samples(self) -> int:
        return max(1, round(self.target_ms * self._in_rate / 1000.0))

    @property
    def _max_fill_samples(self) -> int:
        extra_ms = max(40.0, self.target_ms)
        return round((self.target_ms + extra_ms) * self._in_rate / 1000.0)

    def _current_lost(self) -> int:
        if self._seq_first is None:
            return 0
        expected = self._seq_max - self._seq_first + 1
        return max(0, expected - self._seq_received)

    # ------------------------------------------------------------ ring helpers
    def _ring_write(self, start: int, data: np.ndarray) -> None:
        size = len(self._ring)
        offset = start % size
        first = min(len(data), size - offset)
        self._ring[offset : offset + first] = data[:first]
        if first < len(data):
            self._ring[: len(data) - first] = data[first:]

    def _ring_zero(self, start: int, end: int) -> None:
        size = len(self._ring)
        count = min(end - start, size)
        if count <= 0:
            return
        offset = start % size
        first = min(count, size - offset)
        self._ring[offset : offset + first] = 0.0
        if first < count:
            self._ring[: count - first] = 0.0

    def _advance_to(self, new_read: float) -> None:
        """Move the read position forward, silencing the consumed slots."""
        if new_read <= self._read_pos:
            return
        new_consumed = math.floor(new_read)
        self._ring_zero(self._consumed, new_consumed)
        self._consumed = new_consumed
        self._read_pos = new_read

    def _anchor(self, pos: int) -> None:
        self._anchored = True
        self._playing = False
        self._read_pos = float(pos)
        self._consumed = pos
        self._highest_end = pos
        self._newest_start = pos

    def _start_point(self, now: float) -> float:
        """Read position at which the newest packet waits exactly `target` in total.

        The time it has already spent in the buffer counts towards the target.
        Whole samples only, so an unmodified stream is copied bit-exactly.
        """
        waited = math.floor(max(0.0, now - self._newest_arrival) * self._in_rate)
        waited = min(waited, self._target_samples)
        return float(self._newest_start - self._target_samples + waited)

    def _jump_to(self, read: float, now: float) -> None:
        """Skip ahead (overrun) and restart the playout cursor from there."""
        self._advance_to(read)
        self._pull_time = now
        self._pull_start = self._read_pos
        self._pull_span = 0.0

    # -------------------------------------------------------------- producers
    def push(self, seq: int, pos: int, pcm: np.ndarray, now: float | None = None) -> None:
        """Add one packet. `pcm` is int16 (wire format) or float32 in [-1, 1]."""
        if now is None:
            now = time.perf_counter()
        if pcm.dtype == np.int16:
            samples = pcm.astype(np.float32) * np.float32(1.0 / 32768.0)
        else:
            samples = np.asarray(pcm, dtype=np.float32)
        if samples.size == 0:
            return
        with self._lock:
            if not self._configured or self._muted:
                return
            self._push_locked(int(seq), int(pos), samples, now)

    def _push_locked(self, seq32: int, pos32: int, samples: np.ndarray, now: float) -> None:
        pos = _unwrap32(pos32, self._highest_end if self._anchored else pos32)
        if not self._anchored:
            self._anchor(pos)
        elif self._is_discontinuity(pos, len(samples)):
            log.info("Stream discontinuity detected; re-buffering")
            self._reset_position()
            self._anchor(pos)
        self._count_sequence(seq32)

        end = pos + len(samples)
        if end <= self._consumed:
            self._late += 1
            return
        if end - self._consumed > len(self._ring):
            self._overruns += 1
            self._jump_to(float(end - self._target_samples), now)
        start = max(pos, self._consumed)
        self._ring_write(start, samples[start - pos :])
        if end > self._highest_end:
            self._highest_end = end
            self._newest_start = pos
            self._newest_arrival = now
        if self._highest_end - self._read_pos > self._max_fill_samples:
            self._overruns += 1
            self._jump_to(self._start_point(now), now)
            self._controller.resync(self.target_ms / 1000.0)
        elif self._playing and self.drift_correction:
            self._measure_delay(pos, len(samples), now)

    def _measure_delay(self, pos: int, length: int, now: float) -> None:
        """Feed the controller with how long this packet will wait before playout.

        Each packet is weighted by its duration (not its arrival gap), so
        packets that arrive in bursts still count fully.
        """
        rate = self._in_rate * (1.0 + self._controller.adjust)
        elapsed = min(max(0.0, now - self._pull_time) * rate, self._pull_span)
        cursor = self._pull_start + elapsed
        delay_s = (pos - cursor) / self._in_rate
        self._controller.update(delay_s, self.target_ms / 1000.0, length / self._in_rate)

    def _is_discontinuity(self, pos: int, length: int) -> bool:
        gap = int(self._resync_seconds * self._in_rate)
        return pos > self._highest_end + gap or pos + length < self._consumed - gap

    def _count_sequence(self, seq32: int) -> None:
        if self._seq_first is None:
            self._seq_first = self._seq_max = seq32
        else:
            seq = _unwrap32(seq32, self._seq_max)
            self._seq_max = max(self._seq_max, seq)
        self._seq_received += 1
        self._received += 1

    # --------------------------------------------------------------- consumer
    def pull(self, out: np.ndarray, now: float | None = None) -> None:
        """Fill `out` (float32, mono) with the next output samples. Never blocks long."""
        if now is None:
            now = time.perf_counter()
        n = len(out)
        if n > self._max_block:
            self._alloc_scratch(n)  # only if the audio API changes block size
        with self._lock:
            if not (self._configured and self._anchored) or self._muted:
                out.fill(0.0)
                return
            self._pull_locked(out, n, now)

    def _pull_locked(self, out: np.ndarray, n: int, now: float) -> None:
        step = (self._in_rate / self.out_rate) * (1.0 + self._controller.adjust)
        needed = (n - 1) * step + 1.0  # input samples spanned by this block

        if not self._playing:
            ahead = self._newest_start - self._read_pos
            if ahead < self._target_samples or self._highest_end - self._read_pos < needed:
                out.fill(0.0)
                return
            self._playing = True
            self._advance_to(self._start_point(now))
            if self._controller.smoothed is not None:
                self._controller.resync(self.target_ms / 1000.0)

        self._pull_time = now
        self._pull_start = self._read_pos
        self._pull_span = n * step
        if self._highest_end - self._read_pos < needed:
            self._starve(out, n, step)
            return
        self._starved = 0.0
        self._interpolate(out, n, step)
        self._advance_to(self._read_pos + n * step)

    def _starve(self, out: np.ndarray, n: int, step: float) -> None:
        """Handle a block for which not all audio has arrived.

        A short starvation is usually a lost packet: play what exists (missing
        slots are silent) and keep the timeline, so latency is unchanged. If
        it lasts longer than the target, it is a real underrun: output silence
        and re-buffer up to the target.
        """
        if self._starved == 0.0:
            self._concealed += 1
        if self._starved + n * step <= self._target_samples:
            self._starved += n * step
            self._interpolate(out, n, step)
            self._advance_to(self._read_pos + n * step)
            return
        self._underruns += 1
        self._playing = False
        self._starved = 0.0
        out.fill(0.0)

    def _interpolate(self, out: np.ndarray, n: int, step: float) -> None:
        """Linear interpolation of `n` output samples starting at the read position."""
        offsets = self._p[:n]
        np.multiply(self._k[:n], step, out=offsets)
        offsets += self._read_pos - self._consumed  # relative to the consumed index
        span = int(offsets[-1]) + 2
        if span > len(self._window):  # unusual rate/block combination
            self._window = np.zeros(span * 2, dtype=np.float32)
            self._k = np.arange(span * 2, dtype=np.float64)
        window = self._window[:span]
        size = len(self._ring)
        start = self._consumed % size
        first = min(span, size - start)
        window[:first] = self._ring[start : start + first]
        if first < span:
            window[first:] = self._ring[: span - first]
        out[:] = np.interp(offsets, self._k[:span], window)

    # ------------------------------------------------------------------ stats
    def stats(self) -> JitterStats:
        with self._lock:
            rate = self._in_rate or 1
            fill = (self._highest_end - self._read_pos) if self._anchored else 0.0
            smoothed = self._controller.smoothed
            buffer_ms = 1000.0 * smoothed if smoothed is not None and self._playing else 0.0
            return JitterStats(
                configured=self._configured,
                playing=self._playing,
                muted=self._muted,
                in_rate=self._in_rate,
                out_rate=self.out_rate,
                target_ms=self.target_ms,
                buffer_ms=buffer_ms,
                fill_ms=1000.0 * max(0.0, fill) / rate,
                drift_ppm=1e6 * self._controller.adjust,
                received=self._received,
                lost=self._lost_carry + self._current_lost(),
                late=self._late,
                underruns=self._underruns,
                overruns=self._overruns,
                concealed=self._concealed,
            )
