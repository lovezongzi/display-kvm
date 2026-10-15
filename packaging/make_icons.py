#!/usr/bin/env python3
"""生成 display-kvm 的应用图标（.png / .ico / .icns），不依赖任何外部素材。

    python packaging/make_icons.py

产物写入 packaging/assets/。.icns 由脚本按 ICNS 容器格式手写，
因此**在 Windows 上也能生成 macOS 图标**，不需要 iconutil。
"""

from __future__ import annotations

import io
import struct
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "assets"

# 配色：深色底 + 蓝绿两块屏 + 浅色双向箭头（暗示「两台机器之间切换」）
BG = (22, 26, 35, 255)
PANEL_A = (77, 155, 255, 255)
PANEL_A_IN = (16, 34, 58, 255)
PANEL_B = (54, 200, 96, 255)
PANEL_B_IN = (12, 52, 28, 255)
ARROW = (232, 238, 248, 255)


def render(size: int) -> Image.Image:
    """按 256 基准坐标绘制，再按 size 缩放。"""
    k = size / 256.0
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def box(x0, y0, x1, y1):
        return [x0 * k, y0 * k, x1 * k, y1 * k]

    # 圆角底
    d.rounded_rectangle(box(6, 6, 250, 250), radius=56 * k, fill=BG)

    # 左屏（蓝）
    d.rounded_rectangle(box(24, 78, 104, 166), radius=9 * k, fill=PANEL_A)
    d.rounded_rectangle(box(35, 89, 93, 153), radius=4 * k, fill=PANEL_A_IN)
    d.rounded_rectangle(box(50, 168, 78, 176), radius=4 * k, fill=PANEL_A)

    # 右屏（绿）
    d.rounded_rectangle(box(152, 78, 232, 166), radius=9 * k, fill=PANEL_B)
    d.rounded_rectangle(box(163, 89, 221, 153), radius=4 * k, fill=PANEL_B_IN)
    d.rounded_rectangle(box(178, 168, 206, 176), radius=4 * k, fill=PANEL_B)

    # 中间双向箭头：上排向右、下排向左
    w = max(1, int(7 * k))
    d.line([(116 * k, 108 * k), (140 * k, 108 * k)], fill=ARROW, width=w)
    d.polygon([(148 * k, 108 * k), (132 * k, 97 * k), (132 * k, 119 * k)], fill=ARROW)
    d.line([(140 * k, 140 * k), (116 * k, 140 * k)], fill=ARROW, width=w)
    d.polygon([(108 * k, 140 * k), (124 * k, 129 * k), (124 * k, 151 * k)], fill=ARROW)

    return img


# ICNS 容器里的 PNG 类型码：ic07=128, ic08=256, ic09=512, ic10=1024,
# ic11=32, ic12=64（这些都是「内嵌 PNG」类型，现代 macOS 全部支持）
_ICNS_TYPES = [
    (b"ic11", 32),
    (b"ic12", 64),
    (b"ic07", 128),
    (b"ic08", 256),
    (b"ic09", 512),
    (b"ic10", 1024),
]


def write_png() -> None:
    render(512).save(OUT / "icon.png", format="PNG")


def write_ico() -> None:
    render(256).save(
        OUT / "icon.ico",
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )


def write_icns() -> None:
    chunks = []
    seen = set()
    for type_code, size in _ICNS_TYPES:
        if size in seen:
            continue
        seen.add(size)
        buf = io.BytesIO()
        render(size).save(buf, format="PNG")
        data = buf.getvalue()
        chunks.append(type_code + struct.pack(">I", len(data) + 8) + data)

    body = b"".join(chunks)
    blob = b"icns" + struct.pack(">I", len(body) + 8) + body
    (OUT / "icon.icns").write_bytes(blob)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    write_png()
    write_ico()
    write_icns()
    for f in ("icon.png", "icon.ico", "icon.icns"):
        p = OUT / f
        print("%-12s %8d bytes" % (f, p.stat().st_size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
