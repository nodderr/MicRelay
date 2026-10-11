"""Generate phonemic/assets/phonemic.ico and phonemic.png (no Pillow needed).

The icon is a dark microphone on the app's green, drawn with anti-aliased
numpy masks and written as PNG (and PNG-in-ICO, supported since Windows Vista).

    python scripts/make_icon.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np

ASSETS = Path(__file__).resolve().parent.parent / "phonemic" / "assets"
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
SUPERSAMPLE = 4
GREEN = (0x3D, 0xDC, 0x97)
DARK = (0x0F, 0x11, 0x15)


def _rounded_rect(x: np.ndarray, y: np.ndarray, x0: float, y0: float, x1: float, y1: float, r: float) -> np.ndarray:
    cx = np.clip(x, x0 + r, x1 - r)
    cy = np.clip(y, y0 + r, y1 - r)
    return (x >= x0) & (x <= x1) & (y >= y0) & (y <= y1) & ((x - cx) ** 2 + (y - cy) ** 2 <= r * r)


def _masks(size: int) -> tuple[np.ndarray, np.ndarray]:
    """Coverage (0..1) of the background tile and of the microphone glyph."""
    n = size * SUPERSAMPLE
    coords = (np.arange(n) + 0.5) / n
    x, y = np.meshgrid(coords, coords)
    tile = _rounded_rect(x, y, 0.03, 0.03, 0.97, 0.97, 0.22)
    capsule = _rounded_rect(x, y, 0.37, 0.15, 0.63, 0.58, 0.13)
    ring_r = np.hypot(x - 0.5, y - 0.44)
    holder = (ring_r >= 0.19) & (ring_r <= 0.25) & (y >= 0.44)
    stem = (x >= 0.47) & (x <= 0.53) & (y >= 0.68) & (y <= 0.80)
    base = _rounded_rect(x, y, 0.34, 0.78, 0.66, 0.85, 0.035)
    glyph = tile & (capsule | holder | stem | base)

    def down(mask: np.ndarray) -> np.ndarray:
        return mask.reshape(size, SUPERSAMPLE, size, SUPERSAMPLE).mean(axis=(1, 3))

    return down(tile), down(glyph)


def render_rgba(size: int) -> np.ndarray:
    tile, glyph = _masks(size)
    green = np.array(GREEN, dtype=np.float64)
    dark = np.array(DARK, dtype=np.float64)
    # Colour: green blended towards dark by glyph coverage (relative to the tile).
    mix = np.divide(glyph, tile, out=np.zeros_like(glyph), where=tile > 0)[..., None]
    rgb = green * (1 - mix) + dark * mix
    alpha = tile[..., None] * 255.0
    return np.concatenate([rgb, alpha], axis=2).round().astype(np.uint8)


def png_bytes(rgba: np.ndarray) -> bytes:
    height, width, _ = rgba.shape
    raw = b"".join(b"\x00" + rgba[row].tobytes() for row in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def ico_bytes(images: list[tuple[int, bytes]]) -> bytes:
    directory = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, payload = b"", b""
    for size, data in images:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset + len(payload))
        payload += data
    return directory + entries + payload


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    images = [(size, png_bytes(render_rgba(size))) for size in ICO_SIZES]
    (ASSETS / "phonemic.ico").write_bytes(ico_bytes(images))
    (ASSETS / "phonemic.png").write_bytes(png_bytes(render_rgba(64)))
    (ASSETS / "phonemic-256.png").write_bytes(images[-1][1])
    print(f"Wrote icons to {ASSETS}")


if __name__ == "__main__":
    main()
