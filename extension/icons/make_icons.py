"""Generate the extension toolbar icons.

Draws the Doxa mark — a D built from transcript lines, one of them (the
flagged claim) in the brand purple — on a dark rounded square, and writes
16/32/48/128 px PNGs in an active and an "off" (all grey) variant. The two
small sizes use three thick lines instead of five thin ones, which would
blur together. Pure standard library: PIL is not assumed to be installed, so
the PNG encoder is hand-rolled with zlib/struct and shapes are rendered from
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

# (gradient top, gradient bottom, line color, flagged-line color) per variant.
VARIANTS: dict[str, tuple[Rgb, Rgb, Rgb, Rgb]] = {
    # active: graphite tile, ink lines, the flagged line in purple
    "": ((30, 30, 33), (15, 15, 16), (240, 240, 241), (145, 71, 255)),
    # off: grey tile, every line the same — nothing is being checked
    "_off": ((150, 150, 156), (104, 104, 110), (236, 236, 238), (236, 236, 238)),
}

# Geometry on a 128-unit square. Each line is (y, x_start, x_end): the right
# ends trace the bowl of the D. The stroke is round-capped.
Line = tuple[float, float, float]
DETAILED_LINES: tuple[Line, ...] = (
    (34.5, 40.5, 70.4),
    (49.25, 40.5, 84.1),
    (64.0, 40.5, 87.5),
    (78.75, 40.5, 84.1),
    (93.5, 40.5, 70.4),
)
DETAILED_HALF_WIDTH: float = 4.5
DETAILED_FLAGGED: int = 3
SIMPLE_LINES: tuple[Line, ...] = (
    (38.0, 44.0, 71.9),
    (64.0, 44.0, 84.0),
    (90.0, 44.0, 71.9),
)
SIMPLE_HALF_WIDTH: float = 8.0
SIMPLE_FLAGGED: int = 1
# Sizes up to this many pixels get the three-line mark.
SIMPLE_MAX_SIZE: int = 32
DESIGN_UNITS: float = 128.0

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


def lerp_color(top: Rgb, bottom: Rgb, t: float) -> Rgb:
    red, green, blue = (round(a + (b - a) * t) for a, b in zip(top, bottom))
    return (red, green, blue)


def line_hit(x: float, y: float, size: int) -> int | None:
    """Index of the transcript line covering (x, y), or None for the tile."""
    simple = size <= SIMPLE_MAX_SIZE
    lines = SIMPLE_LINES if simple else DETAILED_LINES
    half_width = SIMPLE_HALF_WIDTH if simple else DETAILED_HALF_WIDTH
    scale = size / DESIGN_UNITS
    for index, (line_y, x_start, x_end) in enumerate(lines):
        distance = segment_sdf(
            x, y, x_start * scale, line_y * scale, x_end * scale, line_y * scale
        )
        if distance <= half_width * scale:
            return index
    return None


def render_icon(
    size: int, top: Rgb, bottom: Rgb, line_color: Rgb, flagged_color: Rgb
) -> list[bytes]:
    """Render one icon as RGBA rows using supersampled SDF coverage."""
    flagged = SIMPLE_FLAGGED if size <= SIMPLE_MAX_SIZE else DETAILED_FLAGGED
    rows: list[bytes] = []
    step = 1.0 / SUBSAMPLES
    total = SUBSAMPLES * SUBSAMPLES
    for py in range(size):
        row = bytearray()
        base_color = lerp_color(top, bottom, py / max(size - 1, 1))
        for px in range(size):
            square_hits = 0
            sums = [0, 0, 0]
            for sy in range(SUBSAMPLES):
                y = py + (sy + 0.5) * step
                for sx in range(SUBSAMPLES):
                    x = px + (sx + 0.5) * step
                    if rounded_square_sdf(x, y, size) > 0.0:
                        continue
                    square_hits += 1
                    hit = line_hit(x, y, size)
                    if hit is None:
                        sample = base_color
                    else:
                        sample = flagged_color if hit == flagged else line_color
                    for channel in range(3):
                        sums[channel] += sample[channel]
            if square_hits == 0:
                row.extend((0, 0, 0, 0))
                continue
            red, green, blue = (round(total_value / square_hits) for total_value in sums)
            row.extend((red, green, blue, round(square_hits / total * 255)))
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
    for suffix, (top, bottom, line_color, flagged_color) in VARIANTS.items():
        for size in SIZES:
            rows = render_icon(size, top, bottom, line_color, flagged_color)
            target = out_dir / f"icon{size}{suffix}.png"
            write_png(target, size, rows)
            print(f"wrote {target}")


if __name__ == "__main__":
    main()
