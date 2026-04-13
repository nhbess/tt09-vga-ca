#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Reconstruct a frame PNG from test/tb.vcd (run from repo root: python visuals/vcd2vga.py)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import PIL.Image
import vcdvcd

ROOT = Path(__file__).resolve().parents[1]
VCD = ROOT / "test" / "tb.vcd"
OUT = Path(__file__).resolve().parent / "screen.png"


def main() -> None:
    vcd = vcdvcd.VCDVCD(str(VCD))
    output = vcd["tb.uo_out[7:0]"]
    pixel_x, pixel_y = 0, 0
    pad = 48 * 2
    w, h = 640 + pad, 480 + pad
    screen = np.zeros([h, w], np.uint8)
    x, y = 0, 0
    prev = 0
    for time, clk in vcd["tb.clk"].tv:
        if clk != "0":
            continue
        val = int(output[time].replace("x", "1"), 2)
        if x < w and y < h:
            screen[y, x] = val
        x += 1
        if prev & 0x80 and not val & 0x80:
            x, y = 0, y + 1
        if prev & 0x08 and not val & 0x08:
            x, y = 0, 0
        prev = val
    bits = np.unpackbits(screen, bitorder="little").reshape(h, w, 8)
    rgb = bits[..., :3] * 170 + bits[..., 4:7] * 85
    PIL.Image.fromarray(rgb).save(OUT)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
