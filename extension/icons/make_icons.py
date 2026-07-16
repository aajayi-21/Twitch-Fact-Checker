"""Generate the extension toolbar icons.

Draws a bold white check mark on a rounded, vertically-gradient square and
writes 16/32/48/128 px PNGs in an active (violet) and an "off" (grey)
variant. Pure standard library: PIL is not assumed to be installed, so the
PNG encoder is hand-rolled with zlib/struct and shapes are rendered from
signed-distance functions with 4x4 supersampling for clean edges at 16 px.

Usage: python3 make_icons.py  (writes PNGs next to this file)
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

SIZES: tuple[int, ...] = (16, 32, 48, 128)
SUBSAMPLES: int = 4  # 4x4 supersampling per pixel

Rgb = tuple[int, int, int]

# (gradient top, gradient bottom, check color) per variant.
VARIANTS: dict[str, tuple[Rgb, Rgb, Rgb]] = {
    "": ((154, 92, 255), (108, 43, 217), (255, 255, 255)),  # active: violet
    "_off": ((168, 173, 184), (110, 116, 128), (236, 237, 239)),  # grey
}

# Unit-square geometry (0..1). The check is two capped strokes.
CHECK_POINTS: tuple[tuple[float, float], ...] = (
    (0.27, 0.54),
    (0.445, 0.705),
    (0.755, 0.33),
)
CHECK_HALF_WIDTH: float = 0.075
CORNER_RADIUS: float = 0.22
SQUARE_INSET: float = 0.02


def rounded_square_sdf(x: float, y: float, size: float) -> float:
    """Signed distance from (x, y) to the rounded square (negative inside)."""
    half = size * (0.5 - SQUARE_INSET)
    radius = size * CORNER_RADIUS
    center = size * 0.5
    dx = abs(x - center) - (half - radius)
    dy = abs(y - center) - (half - radius)
    outside = math.hypot(max(dx, 0.0), max(dy, 0.0))
    inside = min(max(dx, dy), 0.0)
    return outside + inside - radius


def segment_sdf(
    x: float, y: float, ax: float, ay: float, bx: float, by: float
) -> float:
    """Distance from (x, y) to the segment (a, b) — round caps come free."""
    abx, aby = bx - ax, by - ay
    apx, apy = x - ax, y - ay
    length_sq = abx * abx + aby * aby
    if length_sq == 0.0:
        t = 0.0
    else:
        t = max(0.0, min(1.0, (apx * abx + apy * aby) / length_sq))
    return math.hypot(apx - t * abx, apy - t * aby)


def check_sdf(x: float, y: float, size: float) -> float:
    """Signed distance to the check-mark stroke (negative inside)."""
    points = [(px * size, py * size) for px, py in CHECK_POINTS]
    distance = min(
        segment_sdf(x, y, *points[0], *points[1]),
        segment_sdf(x, y, *points[1], *points[2]),
    )
    return distance - CHECK_HALF_WIDTH * size


def lerp_color(top: Rgb, bottom: Rgb, t: float) -> Rgb:
    red, green, blue = (round(a + (b - a) * t) for a, b in zip(top, bottom))
    return (red, green, blue)


def render_icon(size: int, top: Rgb, bottom: Rgb, check: Rgb) -> list[bytes]:
    """Render one icon as RGBA rows using supersampled SDF coverage."""
    rows: list[bytes] = []
    step = 1.0 / SUBSAMPLES
    total = SUBSAMPLES * SUBSAMPLES
    for py in range(size):
        row = bytearray()
        base_color = lerp_color(top, bottom, py / max(size - 1, 1))
        for px in range(size):
            square_hits = 0
            check_hits = 0
            for sy in range(SUBSAMPLES):
                y = py + (sy + 0.5) * step
                for sx in range(SUBSAMPLES):
                    x = px + (sx + 0.5) * step
                    if rounded_square_sdf(x, y, size) <= 0.0:
                        square_hits += 1
                        if check_sdf(x, y, size) <= 0.0:
                            check_hits += 1
            alpha = square_hits / total
            if alpha == 0.0:
                row.extend((0, 0, 0, 0))
                continue
            check_mix = check_hits / square_hits
            red = round(base_color[0] + (check[0] - base_color[0]) * check_mix)
            green = round(base_color[1] + (check[1] - base_color[1]) * check_mix)
            blue = round(base_color[2] + (check[2] - base_color[2]) * check_mix)
            row.extend((red, green, blue, round(alpha * 255)))
        rows.append(bytes(row))
    return rows


def write_png(path: Path, size: int, rows: list[bytes]) -> None:
    """Minimal RGBA8 PNG encoder (IHDR + one IDAT + IEND, filter 0 rows)."""

    def chunk(chunk_type: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    raw = b"".join(b"\x00" + row for row in rows)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def main() -> None:
    out_dir = Path(__file__).resolve().parent
    for suffix, (top, bottom, check) in VARIANTS.items():
        for size in SIZES:
            rows = render_icon(size, top, bottom, check)
            target = out_dir / f"icon{size}{suffix}.png"
            write_png(target, size, rows)
            print(f"wrote {target}")


if __name__ == "__main__":
    main()
