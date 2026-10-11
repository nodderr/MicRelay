"""The phonemic engine: server + jitter buffer + audio output in a background thread.

Both front-ends drive it: the command line (``python -m phonemic``) and the
desktop window (``python -m phonemic.gui``). All public methods are called
from the front-end's thread; the asyncio loop runs in the engine thread.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from . import audio_out, netutil
from .audio_out import NullOutput, Output, SoundDeviceOutput, WavRecorder
from .jitter import JitterBuffer, JitterStats

if TYPE_CHECKING:  # the WebRTC stack is imported lazily: it takes seconds to load
    from .server import PhonemicServer, ServerStatus

log = logging.getLogger(__name__)

NULL_OUTPUT_RATE = 48000
START_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class EngineSettings:
    port: int = 8443
    host: str | None = None  # advertised IP/hostname; None = auto-detect
    bind: str = "0.0.0.0"
    device: str | None = None  # index or name substring; None = auto (virtual cable)
    target_ms: float = 30.0
    gain_db: float = 0.0
    null_output: bool = False
    record: Path | None = None
    config_dir: Path | None = None
    new_token: bool = False
    verbose: bool = False


@dataclass(frozen=True)
class EngineStatus:
    running: bool
    url: str
    server: ServerStatus | None
    stats: JitterStats | None
    output_description: str
    output_latency_ms: float
    virtual_device: bool  # True if audio goes to a virtual cable (or null output)
    output_warning: str | None  # set when the preferred output failed and another is used
    level: float  # peak output level since the previous status() call, 0..1
    uptime_s: float
    error: str | None

    @property
    def latency_ms(self) -> float | None:
        """Estimated latency: RTT/2 + buffer + output stream latency."""
        if self.server is None or self.stats is None or not self.stats.playing:
            return None
        return (self.server.rtt_ms or 0.0) / 2 + self.stats.buffer_ms + self.output_latency_ms


def _quiet_ssl_errors(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """Phones that haven't accepted the certificate yet cause harmless TLS errors."""
    exc = context.get("exception")
    if isinstance(exc, OSError) and "ssl" in repr(context).lower():
        log.debug("TLS error ignored: %s", exc)
        return
    loop.default_exception_handler(context)


class Engine:
    """Start/stop the whole pipeline and query its status from any thread."""

    def __init__(self, settings: EngineSettings) -> None:
        self.settings = settings
        self.url = ""
        self.jitter: JitterBuffer | None = None
        self.output: Output | None = None
        self.virtual_device = False
        self.output_warning: str | None = None
        self._server: PhonemicServer | None = None
        self._recorder: WavRecorder | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._ready = threading.Event()
        self._output_lock = threading.Lock()
        self._error: BaseException | None = None
        self._started_at = 0.0

    # ------------------------------------------------------------- lifecycle
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._ready.is_set()

    def start(self) -> None:
        """Start everything; raises if the server or audio output cannot start."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(target=self._thread_main, name="phonemic-engine", daemon=True)
        self._thread.start()
        if not self._ready.wait(START_TIMEOUT_S):
            raise RuntimeError("phonemic did not start in time")
        if self._error is not None:
            self._thread.join(timeout=5)
            raise self._error

    def stop(self) -> None:
        """Stop everything and wait for the engine thread to finish."""
        loop, event, thread = self._loop, self._stop_event, self._thread
        if loop is not None and event is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:  # loop already closed
                pass
        if thread is not None:
            thread.join(timeout=15)
        self._thread = None

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._main())
        except BaseException as exc:  # reported to start() or via status()
            if not isinstance(exc, (RuntimeError, ValueError, OSError)):
                log.exception("Engine crashed")
            self._error = exc
        finally:
            self._ready.set()
            self._loop = None

    async def _main(self) -> None:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(_quiet_ssl_errors)
        self._loop = loop
        self._stop_event = asyncio.Event()
        s = self.settings
        from . import certs
        from .server import PhonemicServer

        host = s.host or netutil.get_lan_ip()
        config_dir = s.config_dir or certs.default_config_dir()
        cert_path, key_path = certs.ensure_certificate(config_dir, host, netutil.get_hostname())
        token = certs.load_or_create_token(config_dir, regenerate=s.new_token)
        ssl_context = certs.make_ssl_context(cert_path, key_path)

        self.jitter = JitterBuffer(NULL_OUTPUT_RATE, s.target_ms)  # rate set by the output
        self._server = PhonemicServer(self.jitter, token)
        try:
            await self._server.start(s.bind, s.port, ssl_context, access_log=s.verbose)
        except OSError as exc:
            raise RuntimeError(
                f"Cannot listen on port {s.port} ({exc.strerror or exc}). "
                "Is phonemic already running? Otherwise choose another port."
            ) from exc
        try:
            with self._output_lock:
                self.output, self.virtual_device, self.output_warning = self._open_output(s.device)
            self.url = netutil.build_url(host, s.port, token)
            self._started_at = time.monotonic()
            self._ready.set()
            await self._stop_event.wait()
        finally:
            await self._server.stop()
            with self._output_lock:
                if self.output is not None:
                    self.output.stop()
            self._close_recorder()
            log.info("Stopped")

    # ---------------------------------------------------------------- output
    def _open_output(self, device_spec: str | None, fallback: bool = True) -> tuple[Output, bool, str | None]:
        """Start the first output that works: (output, is_virtual, fallback warning).

        `device_spec` None means auto (virtual cable, else the default output).
        With `fallback`, a device that won't open is skipped for the next
        candidate instead of stopping phonemic.
        """
        s = self.settings
        assert self.jitter is not None
        if s.null_output:
            output: Output = NullOutput(NULL_OUTPUT_RATE, s.gain_db, self._recorder_for(NULL_OUTPUT_RATE))
            self.jitter.set_out_rate(output.samplerate)
            output.start(self.jitter.pull)
            return output, True, None
        audio_out.prepare_linux_routing()
        candidates = audio_out.output_candidates(device_spec, fallback)
        if not candidates:
            raise RuntimeError("No audio output device found. Connect speakers or headphones, or install a virtual cable.")
        errors: list[str] = []
        for device in candidates:
            output = SoundDeviceOutput(device, s.gain_db, self._recorder_for(device.samplerate))
            self.jitter.set_out_rate(device.samplerate)
            try:
                output.start(self.jitter.pull)
            except RuntimeError as exc:
                log.warning("%s", exc)
                errors.append(str(exc))
                continue
            warning = None
            if errors:
                warning = f"Couldn't open {candidates[0].name}, so phonemic is using {device.name} instead."
                log.warning("%s", warning)
            return output, device.is_virtual, warning
        more = f" ({len(errors) - 1} other outputs also failed; see the log)" if len(errors) > 1 else ""
        raise RuntimeError(errors[0] + more)

    def _recorder_for(self, samplerate: int) -> WavRecorder | None:
        s = self.settings
        if s.record and self._recorder is None:
            self._recorder = WavRecorder(s.record, samplerate)
        if self._recorder is not None and self._recorder.samplerate != samplerate:
            log.warning("Recording stopped: new device runs at a different sample rate")
            self._close_recorder()
        return self._recorder

    def _close_recorder(self) -> None:
        if self._recorder is not None:
            self._recorder.close()
            self._recorder = None

    def set_device(self, device_spec: str | None) -> None:
        """Switch the output device while running (the phone stays connected)."""
        if self.settings.null_output:
            raise RuntimeError("Cannot change the device in null-output mode")
        if self.jitter is None:
            raise RuntimeError("phonemic is not running")
        with self._output_lock:
            old = self.output
            if old is not None:
                old.stop()
            try:
                # Only the chosen device (through any host API): picking a device
                # should not silently land somewhere else.
                new, is_virtual, warning = self._open_output(device_spec, fallback=device_spec is None)
            except Exception:
                if old is not None:
                    self.jitter.set_out_rate(old.samplerate)
                    old.start(self.jitter.pull)
                raise
            self.output = new
            self.virtual_device = is_virtual
            self.output_warning = warning
            self.settings = replace(self.settings, device=device_spec)
        log.info("Output switched to %s", new.description)

    def set_target_ms(self, target_ms: float) -> None:
        self.settings = replace(self.settings, target_ms=target_ms)
        if self.jitter is not None:
            self.jitter.set_target_ms(target_ms)

    def set_gain_db(self, gain_db: float) -> None:
        self.settings = replace(self.settings, gain_db=gain_db)
        output = self.output
        if output is not None:
            output.set_gain_db(gain_db)

    # ---------------------------------------------------------------- status
    def status(self) -> EngineStatus:
        running = self.running
        output = self.output
        error = self._error
        return EngineStatus(
            running=running,
            url=self.url,
            server=self._server.status() if self._server is not None and running else None,
            stats=self.jitter.stats() if self.jitter is not None and running else None,
            output_description=output.description if output is not None else "",
            output_latency_ms=output.latency_s * 1000.0 if output is not None and running else 0.0,
            virtual_device=self.virtual_device,
            output_warning=self.output_warning,
            level=output.take_peak() if output is not None and running else 0.0,
            uptime_s=time.monotonic() - self._started_at if running else 0.0,
            error=str(error) if error is not None else None,
        )
