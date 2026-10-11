"""Network helpers: LAN IP detection, URL building and terminal QR rendering."""

from __future__ import annotations

import logging
import socket
import sys

log = logging.getLogger(__name__)

# Addresses used only to ask the OS which interface it would route through.
# A UDP connect() sends no packets.
_PROBE_ADDRESSES = ("10.255.255.255", "192.168.255.255", "8.8.8.8")


def get_lan_ip() -> str:
    """Return the IPv4 address of the interface that carries the default route."""
    for probe in _PROBE_ADDRESSES:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((probe, 1))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith(("0.", "127.")):
                return ip
        except OSError:
            continue
        finally:
            sock.close()
    log.warning("Could not detect a LAN IP address; falling back to 127.0.0.1")
    return "127.0.0.1"


def get_hostname() -> str:
    """Return this machine's short hostname."""
    return socket.gethostname().split(".")[0] or "localhost"


def build_url(host: str, port: int, token: str) -> str:
    """Build the phone URL (token travels in the query string)."""
    shown = f"[{host}]" if ":" in host else host
    return f"https://{shown}:{port}/?t={token}"


def _can_encode(text: str) -> bool:
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def qr_matrix(text: str, border: int = 2) -> list[list[bool]]:
    """QR code modules for `text` (True = dark), including a quiet-zone border."""
    import qrcode  # imported lazily: only needed at start-up

    qr = qrcode.QRCode(border=border, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(text)
    qr.make(fit=True)
    return qr.get_matrix()


def render_qr(text: str) -> str:
    """Render `text` as a QR code for a dark-background terminal.

    Light modules are drawn as blocks and dark modules as spaces, so the code
    reads correctly (dark on light) on a typical dark terminal.
    """
    matrix = qr_matrix(text)
    if _can_encode("\u2580\u2584\u2588"):
        return _render_half_blocks(matrix)
    return _render_ascii(matrix)


def _render_half_blocks(matrix: list[list[bool]]) -> str:
    rows = list(matrix)
    if len(rows) % 2:
        rows.append([False] * len(rows[0]))
    lines = []
    for top, bottom in zip(rows[0::2], rows[1::2]):
        chars = []
        for dark_top, dark_bottom in zip(top, bottom):
            light_top, light_bottom = not dark_top, not dark_bottom
            if light_top and light_bottom:
                chars.append("\u2588")
            elif light_top:
                chars.append("\u2580")
            elif light_bottom:
                chars.append("\u2584")
            else:
                chars.append(" ")
        lines.append("".join(chars))
    return "\n".join(lines)


def _render_ascii(matrix: list[list[bool]]) -> str:
    return "\n".join("".join("  " if dark else "##" for dark in row) for row in matrix)
