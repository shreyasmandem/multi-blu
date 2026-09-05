"""Generate assets/multiblu.ico -- a mint tile with a broadcast glyph.

Drawn straight into a numpy array and written as a classic BMP-backed .ico, so
the build needs no image library. Run it only when the icon should change:

    python tools/make_icon.py
"""

from __future__ import annotations

import pathlib
import struct

import numpy as np

ACCENT = (0x3E, 0xD5, 0x98)      # mint, matching the app's accent
INK = (0x0B, 0x0C, 0x0F)         # near-black, matching the app background
SIZES = (256, 128, 64, 48, 32, 16)
OUT = pathlib.Path(__file__).resolve().parent.parent / "assets" / "multiblu.ico"


def _coverage(distance: np.ndarray, edge: float, softness: float = 1.2) -> np.ndarray:
    """Antialiased 0..1 mask for `distance <= edge`."""
    return np.clip((edge - distance) / softness + 0.5, 0.0, 1.0)


def render(size: int) -> np.ndarray:
    """Return an (size, size, 4) RGBA float array in 0..255."""
    scale = size / 256.0
    y, x = np.mgrid[0:size, 0:size].astype(np.float64)
    cx = cy = (size - 1) / 2.0

    # Rounded-square tile.
    radius = 56 * scale
    half = (size / 2.0) - 1.0 * scale
    dx = np.abs(x - cx) - (half - radius)
    dy = np.abs(y - cy) - (half - radius)
    outside = np.hypot(np.maximum(dx, 0), np.maximum(dy, 0))
    inside = np.minimum(np.maximum(dx, dy), 0)
    tile = _coverage(outside + inside, radius, max(1.0, 1.2 * scale))

    img = np.zeros((size, size, 4), dtype=np.float64)
    img[..., 0] = ACCENT[0]
    img[..., 1] = ACCENT[1]
    img[..., 2] = ACCENT[2]
    img[..., 3] = tile * 255.0

    # Broadcast glyph: a source dot with arcs radiating from it.
    ox = cx - 74 * scale
    dist = np.hypot(x - ox, y - cy)
    glyph = _coverage(dist, 26 * scale, max(1.0, 1.2 * scale))

    angle = np.abs(np.arctan2(y - cy, x - ox))
    wedge = _coverage(angle, np.deg2rad(52), 0.10)
    for arc_radius in (70, 112, 154):
        band = np.abs(dist - arc_radius * scale)
        glyph = np.maximum(glyph, _coverage(band, 9 * scale,
                                            max(1.0, 1.2 * scale)) * wedge)

    alpha = (glyph * tile)[..., None]
    ink = np.array(INK, dtype=np.float64)
    img[..., :3] = img[..., :3] * (1 - alpha) + ink * alpha
    return img


def _bmp_entry(img: np.ndarray) -> bytes:
    """One .ico image in BMP form: BITMAPINFOHEADER, BGRA rows, then AND mask."""
    size = img.shape[0]
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                         size * size * 4, 0, 0, 0, 0)
    rgba = np.clip(img, 0, 255).astype(np.uint8)[::-1]          # bottom-up
    bgra = rgba[..., [2, 1, 0, 3]].tobytes()
    mask_stride = ((size + 31) // 32) * 4                       # padded to 4 bytes
    return header + bgra + b"\x00" * (mask_stride * size)


def main() -> None:
    images = [(s, _bmp_entry(render(s))) for s in SIZES]
    OUT.parent.mkdir(parents=True, exist_ok=True)

    offset = 6 + 16 * len(images)
    directory, payload = b"", b""
    for size, blob in images:
        directory += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0,
                                 1, 32, len(blob), offset)
        payload += blob
        offset += len(blob)

    OUT.write_bytes(struct.pack("<HHH", 0, 1, len(images)) + directory + payload)
    print("wrote %s (%d bytes, sizes %s)" % (OUT, OUT.stat().st_size,
                                             ", ".join(map(str, SIZES))))


if __name__ == "__main__":
    main()
