"""HTTPS + WebRTC server: serves the phone page and receives its audio."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import ssl
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from aiohttp import web
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

from .jitter import JitterBuffer

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
HEADER = struct.Struct("<II")  # uint32 seq, uint32 sample position (little-endian)
PING_INTERVAL_S = 1.0
SESSION_TIMEOUT_S = 6.0
MAX_SDP_BYTES = 64 * 1024


def _now_ms() -> float:
    return time.perf_counter() * 1000.0  # monotonic() ticks in 15.6 ms steps on Windows


@dataclass
class PhoneSession:
    """One connected phone (only one is active at a time)."""

    pc: RTCPeerConnection
    peer: str
    created: float = field(default_factory=time.monotonic)
    last_rx: float = field(default_factory=time.monotonic)
    ctl: Any = None
    hello: bool = False
    sample_rate: int = 0
    chunk_samples: int = 0
    user_agent: str = ""
    muted: bool = False
    rtt_ms: float | None = None
    closed: bool = False

    def send_ctl(self, message: dict[str, Any]) -> None:
        if self.ctl is not None and self.ctl.readyState == "open":
            self.ctl.send(json.dumps(message))


@dataclass(frozen=True)
class ServerStatus:
    state: str  # waiting | connecting | connected | muted
    peer: str
    device: str
    sample_rate: int
    rtt_ms: float | None
    ever_connected: bool


def describe_user_agent(ua: str) -> str:
    """Very small UA summary for log lines."""
    if "iPhone" in ua or "iPad" in ua:
        device = "iPhone" if "iPhone" in ua else "iPad"
    elif "Android" in ua:
        device = "Android"
    else:
        device = "desktop" if "Windows" in ua or "Macintosh" in ua or "X11" in ua else "phone"
    if "Edg" in ua:
        browser = "Edge"
    elif "CriOS" in ua or "Chrome" in ua:
        browser = "Chrome"
    elif "FxiOS" in ua or "Firefox" in ua:
        browser = "Firefox"
    elif "Safari" in ua:
        browser = "Safari"
    else:
        browser = "browser"
    return f"{device} {browser}"


class PhonemicServer:
    """aiohttp application with GET / (phone page) and POST /offer (signalling)."""

    def __init__(self, jitter: JitterBuffer, token: str) -> None:
        self.jitter = jitter
        self.token = token
        self.ever_connected = False
        self._session: PhoneSession | None = None
        self._offer_lock = asyncio.Lock()
        self._runner: web.AppRunner | None = None
        self._ping_task: asyncio.Task[None] | None = None

    # -------------------------------------------------------------- lifecycle
    def build_app(self) -> web.Application:
        app = web.Application(client_max_size=MAX_SDP_BYTES)
        app.router.add_get("/", self.handle_index)
        app.router.add_post("/offer", self.handle_offer)
        app.router.add_get("/favicon.ico", self.handle_favicon)
        return app

    async def start(self, bind: str, port: int, ssl_context: ssl.SSLContext, access_log: bool) -> None:
        self._runner = web.AppRunner(self.build_app(), access_log=log if access_log else None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host=bind, port=port, ssl_context=ssl_context)
        await site.start()
        self._ping_task = asyncio.ensure_future(self._ping_loop())
        log.debug("HTTPS server listening on %s:%d", bind, port)

    async def stop(self) -> None:
        if self._ping_task is not None:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
        if self._session is not None:
            self._session.send_ctl({"type": "bye"})
            await self._close_session(self._session, "server shutting down")
        if self._runner is not None:
            await self._runner.cleanup()

    # ---------------------------------------------------------------- routes
    async def handle_index(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(
            WEB_DIR / "index.html",
            headers={"Cache-Control": "no-store", "Content-Type": "text/html; charset=utf-8"},
        )

    async def handle_favicon(self, request: web.Request) -> web.Response:
        return web.Response(status=204)

    def _token_ok(self, token: object) -> bool:
        return isinstance(token, str) and hmac.compare_digest(token.encode(), self.token.encode())

    async def handle_offer(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        if not isinstance(body, dict) or not self._token_ok(body.get("token")):
            log.warning("Rejected offer with a bad token from %s", request.remote)
            return web.json_response({"error": "bad token - rescan the QR code"}, status=403)
        sdp, sdp_type = body.get("sdp"), body.get("type")
        if sdp_type != "offer" or not isinstance(sdp, str):
            return web.json_response({"error": "expected an SDP offer"}, status=400)

        async with self._offer_lock:
            answer = await self._accept_offer(sdp, request.remote or "?")
        return web.json_response({"sdp": answer.sdp, "type": answer.type})

    async def _accept_offer(self, sdp: str, peer: str) -> RTCSessionDescription:
        old = self._session
        if old is not None:
            log.info("New phone connection from %s replaces %s", peer, old.peer)
            old.send_ctl({"type": "replaced"})
            await self._close_session(old, "replaced")

        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        session = PhoneSession(pc=pc, peer=peer)
        self._session = session
        pc.on("datachannel", lambda channel: self._on_datachannel(session, channel))
        pc.on("connectionstatechange", lambda: self._on_connection_state(session))

        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
        await pc.setLocalDescription(await pc.createAnswer())
        log.info("Phone %s is connecting...", peer)
        return pc.localDescription

    # --------------------------------------------------------------- WebRTC
    def _on_connection_state(self, session: PhoneSession) -> None:
        state = session.pc.connectionState
        log.debug("Peer connection state: %s", state)
        if state in ("failed", "closed") and not session.closed:
            asyncio.ensure_future(self._close_session(session, f"connection {state}"))

    def _on_datachannel(self, session: PhoneSession, channel: Any) -> None:
        log.debug("Data channel '%s' opened", channel.label)
        if channel.label == "audio":
            channel.on("message", lambda message: self._on_audio(session, message))
        elif channel.label == "ctl":
            session.ctl = channel
            channel.on("message", lambda message: self._on_ctl(session, message))
        else:
            log.warning("Ignoring unknown data channel '%s'", channel.label)

    def _on_audio(self, session: PhoneSession, message: object) -> None:
        if session is not self._session or not session.hello:
            return
        if not isinstance(message, bytes) or len(message) <= HEADER.size or len(message) % 2:
            return
        session.last_rx = time.monotonic()
        seq, pos = HEADER.unpack_from(message)
        pcm = np.frombuffer(message, dtype="<i2", offset=HEADER.size)
        self.jitter.push(seq, pos, pcm)

    def _on_ctl(self, session: PhoneSession, message: object) -> None:
        if session is not self._session or not isinstance(message, str):
            return
        session.last_rx = time.monotonic()
        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            log.debug("Bad ctl message: %r", message[:200])
            return
        if not isinstance(msg, dict):
            return
        handler = {
            "hello": self._ctl_hello,
            "ping": self._ctl_ping,
            "pong": self._ctl_pong,
            "mute": self._ctl_mute,
            "stop": self._ctl_stop,
        }.get(msg.get("type"))
        if handler is not None:
            handler(session, msg)

    def _ctl_hello(self, session: PhoneSession, msg: dict[str, Any]) -> None:
        try:
            rate = int(msg["sampleRate"])
            self.jitter.configure(rate)
        except (KeyError, TypeError, ValueError) as exc:
            log.error("Invalid hello from phone: %s", exc)
            return
        session.hello = True
        session.sample_rate = rate
        session.chunk_samples = int(msg.get("chunkSamples") or 0)
        session.user_agent = str(msg.get("userAgent") or "")[:300]
        self.ever_connected = True
        log.info(
            "Phone connected: %s, %d Hz, %d-sample chunks",
            describe_user_agent(session.user_agent), rate, session.chunk_samples,
        )

    def _ctl_ping(self, session: PhoneSession, msg: dict[str, Any]) -> None:
        session.send_ctl({"type": "pong", "t": msg.get("t")})

    def _ctl_pong(self, session: PhoneSession, msg: dict[str, Any]) -> None:
        sent = msg.get("t")
        if not isinstance(sent, (int, float)):
            return
        rtt = max(0.0, _now_ms() - float(sent))
        session.rtt_ms = rtt if session.rtt_ms is None else 0.7 * session.rtt_ms + 0.3 * rtt

    def _ctl_mute(self, session: PhoneSession, msg: dict[str, Any]) -> None:
        session.muted = bool(msg.get("muted", True))
        self.jitter.set_muted(session.muted)
        log.info("Phone %s", "muted" if session.muted else "unmuted")

    def _ctl_stop(self, session: PhoneSession, msg: dict[str, Any]) -> None:
        asyncio.ensure_future(self._close_session(session, "stopped on the phone"))

    async def _close_session(self, session: PhoneSession, reason: str) -> None:
        if session.closed:
            return
        session.closed = True
        if self._session is session:
            self._session = None
            self.jitter.clear()
        if session.hello:
            log.info("Phone disconnected (%s)", reason)
        else:
            log.debug("Phone session closed before hello (%s)", reason)
        await session.pc.close()

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL_S)
            session = self._session
            if session is None:
                continue
            if time.monotonic() - session.last_rx > SESSION_TIMEOUT_S:
                await self._close_session(session, "timed out")
                continue
            session.send_ctl({"type": "ping", "t": _now_ms()})

    # ----------------------------------------------------------------- status
    def status(self) -> ServerStatus:
        session = self._session
        if session is None:
            state = "waiting"
        elif not session.hello:
            state = "connecting"
        else:
            state = "muted" if session.muted else "connected"
        return ServerStatus(
            state=state,
            peer=session.peer if session else "",
            device=describe_user_agent(session.user_agent) if session and session.hello else "",
            sample_rate=session.sample_rate if session else 0,
            rtt_ms=session.rtt_ms if session else None,
            ever_connected=self.ever_connected,
        )
