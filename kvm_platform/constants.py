"""platform 包共享常量与工具函数。

DDC/CI 的 VCP 0x60（Input Select）取值含义，以及把取值翻译成人能看懂的
输入口名字、把各种写法的输入源统一成 int。
"""

from __future__ import annotations

import sys
from pathlib import Path

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

# DDC/CI 的 VCP 0x60（Input Select）取值含义。
# 0x01~0x12 是 MCCS 标准；0x1B 是 m1ddc 文档里点名的 USB-C；
# 0x90/0x91/0xD0/0xD1/0xD2 是 LG 一类显示器用的 alternate addressing
# （m1ddc 的 input-alt 子命令走的那一套）。
VCP_INPUT_NAMES = {
    0x01: "VGA-1", 0x02: "VGA-2",
    0x03: "DVI-1", 0x04: "DVI-2",
    0x05: "Composite-1", 0x06: "Composite-2",
    0x07: "S-Video-1", 0x08: "S-Video-2",
    0x09: "Tuner-1", 0x0A: "Tuner-2", 0x0B: "Tuner-3",
    0x0C: "Component-1", 0x0D: "Component-2", 0x0E: "Component-3",
    0x0F: "DisplayPort-1", 0x10: "DisplayPort-2",
    0x11: "HDMI-1", 0x12: "HDMI-2",
    0x1B: "USB-C",
    0x90: "HDMI-1(alt)", 0x91: "HDMI-2(alt)",
    0xD0: "DisplayPort-1(alt)", 0xD1: "DisplayPort-2(alt)", 0xD2: "USB-C/DP-3(alt)",
}


def input_source_name(v: int | None) -> str:
    """把 VCP 0x60 的取值翻译成人能看懂的输入口名字。

    托盘和日志都用它，免得满屏 0x0F 这种要查表才知道是哪个口的东西。
    """
    if v is None:
        return "未知"
    return "%s（0x%02X）" % (VCP_INPUT_NAMES.get(v, "未知口"), v)


def _parse_source(value) -> int | None:
    """把 '0x10' / '16' / 16 这类写法统一成 int；解析失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip(), 0)
    except ValueError:
        return None


# 把项目根目录（platform/ 的父目录）加进 sys.path，保证同目录下的
# ddc_win 等模块在冻结（PyInstaller one-file）场景下也能 import 到。
_PARENT = Path(__file__).resolve().parent.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))
