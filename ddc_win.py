"""Windows 端 DDC/CI 显示器控制核心。

通过 VESA MCCS 协议（VCP 0x60 = Input Source）读写显示器的输入源，
效果等同于按显示器实体按钮。

依赖：仅 ctypes + 系统自带的 dxva2.dll / user32.dll，无需 pip 安装任何东西。

命令行：
    python ddc_win.py list
    python ddc_win.py get   <序号>
    python ddc_win.py set   <序号> <输入源十六进制> [--restore-after 秒数] [--yes]

安全网：--restore-after 会在切换后等待 N 秒，然后把输入源切回原值。
      用于在自己机器上做"切换是否真的生效"的验证，避免切过去回不来。
"""

from __future__ import annotations

import ctypes
import re
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# --------------------------------------------------------------------------
# MCCS 输入源编码表（VCP 0x60）
# --------------------------------------------------------------------------

INPUT_NAMES: dict[int, str] = {
    0x01: "VGA-1",
    0x02: "VGA-2",
    0x03: "DVI-1",
    0x04: "DVI-2",
    0x05: "Composite-1",
    0x06: "Composite-2",
    0x07: "S-Video-1",
    0x08: "S-Video-2",
    0x09: "Tuner-1",
    0x0A: "Tuner-2",
    0x0B: "Tuner-3",
    0x0C: "Component-1",
    0x0D: "Component-2",
    0x0E: "Component-3",
    0x0F: "DisplayPort-1",
    0x10: "DisplayPort-2",
    0x11: "HDMI-1",
    0x12: "HDMI-2",
    0x13: "HDMI-3",
    0x14: "HDMI-4",
    0x1B: "USB-C",
}

VCP_INPUT_SOURCE = 0x60
VCP_BRIGHTNESS = 0x10


def input_name(code: int) -> str:
    return INPUT_NAMES.get(code, "Unknown-0x%02X" % code)


# --------------------------------------------------------------------------
# Win32 绑定
# --------------------------------------------------------------------------

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)


class PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [
        ("hPhysicalMonitor", wintypes.HANDLE),
        ("szPhysicalMonitorDescription", ctypes.c_wchar * 128),
    ]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", ctypes.c_wchar * 32),
    ]


_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HANDLE, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
)

_user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p, _MONITORENUMPROC, wintypes.LPARAM]
_user32.EnumDisplayMonitors.restype = wintypes.BOOL
_user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFOEXW)]
_user32.GetMonitorInfoW.restype = wintypes.BOOL

_dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL
_dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(PHYSICAL_MONITOR)
]
_dxva2.GetPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL
_dxva2.DestroyPhysicalMonitors.argtypes = [wintypes.DWORD, ctypes.POINTER(PHYSICAL_MONITOR)]
_dxva2.DestroyPhysicalMonitors.restype = wintypes.BOOL
_dxva2.GetVCPFeatureAndVCPFeatureReply.argtypes = [
    wintypes.HANDLE,
    ctypes.c_ubyte,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
]
_dxva2.GetVCPFeatureAndVCPFeatureReply.restype = wintypes.BOOL
_dxva2.SetVCPFeature.argtypes = [wintypes.HANDLE, ctypes.c_ubyte, wintypes.DWORD]
_dxva2.SetVCPFeature.restype = wintypes.BOOL
_dxva2.GetCapabilitiesStringLength.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_dxva2.GetCapabilitiesStringLength.restype = wintypes.BOOL
_dxva2.CapabilitiesRequestAndCapabilitiesReply.argtypes = [wintypes.HANDLE, ctypes.c_char_p, wintypes.DWORD]
_dxva2.CapabilitiesRequestAndCapabilitiesReply.restype = wintypes.BOOL


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass
class Display:
    index: int
    device: str  # 形如 \\.\DISPLAY1
    width: int
    height: int
    x: int
    y: int
    desc: str
    current_input: int | None = None
    supported_inputs: list[int] = field(default_factory=list)

    @property
    def is_primary(self) -> bool:
        return self.x == 0 and self.y == 0

    def describe(self) -> str:
        cur = input_name(self.current_input) if self.current_input is not None else "?"
        opts = ", ".join("%s(0x%02X)" % (input_name(c), c) for c in self.supported_inputs) or "?"
        return (
            "[%d] %s  %dx%d @ (%d,%d)%s\n"
            "    当前输入源 : %s\n"
            "    可选输入源 : %s"
            % (
                self.index,
                self.device,
                self.width,
                self.height,
                self.x,
                self.y,
                "  (主屏)" if self.is_primary else "",
                cur,
                opts,
            )
        )


# --------------------------------------------------------------------------
# 底层读写
# --------------------------------------------------------------------------


def _read_vcp(handle, code: int) -> int | None:
    vt = wintypes.DWORD(0)
    cur = wintypes.DWORD(0)
    mx = wintypes.DWORD(0)
    ok = _dxva2.GetVCPFeatureAndVCPFeatureReply(
        handle, code, ctypes.byref(vt), ctypes.byref(cur), ctypes.byref(mx)
    )
    return cur.value if ok else None


def _write_vcp(handle, code: int, value: int) -> bool:
    return bool(_dxva2.SetVCPFeature(handle, code, value))


def _capability_string(handle) -> str | None:
    length = wintypes.DWORD(0)
    if not _dxva2.GetCapabilitiesStringLength(handle, ctypes.byref(length)) or length.value == 0:
        return None
    buf = ctypes.create_string_buffer(length.value + 1)
    if not _dxva2.CapabilitiesRequestAndCapabilitiesReply(handle, buf, length.value):
        return None
    return buf.value.decode("ascii", "replace")


def _parse_supported_inputs(cap: str | None) -> list[int]:
    """从 MCCS 能力串里抠出 60(...) 中声明的输入源编码。"""
    if not cap:
        return []
    m = re.search(r"\b60\s*\(([^)]*)\)", cap)
    if not m:
        return []
    out = []
    for tok in m.group(1).split():
        try:
            out.append(int(tok, 16))
        except ValueError:
            pass
    return out


# --------------------------------------------------------------------------
# 枚举
# --------------------------------------------------------------------------


def _iter_hmonitors():
    """逐个回调 EnumDisplayMonitors 给出 (hmonitor, device_name, rect)。"""
    collected: list[tuple[int, str, tuple[int, int, int, int]]] = []

    def _cb(hmon, hdc, lprc, data):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if _user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            rect = (
                mi.rcMonitor.right - mi.rcMonitor.left,
                mi.rcMonitor.bottom - mi.rcMonitor.top,
                mi.rcMonitor.left,
                mi.rcMonitor.top,
            )
            collected.append((int(hmon), mi.szDevice, rect))
        return True

    cb = _MONITORENUMPROC(_cb)
    _user32.EnumDisplayMonitors(None, None, cb, 0)
    return collected


def list_displays(with_capabilities: bool = True) -> list[Display]:
    """列出所有物理显示器及其输入源信息。只读，不会改变任何状态。

    性能提示：只要判断「这台显示器现在在不在我这儿」，把 with_capabilities
    设为 False。拉取 MCCS 能力串（GetCapabilitiesString）走的是 I2C 慢通道，
    在部分显示器上单次就要好几秒，而角色判定根本用不到它。
    """
    displays: list[Display] = []

    for hmon, device, rect in _iter_hmonitors():
        n = wintypes.DWORD(0)
        if not _dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmon, ctypes.byref(n)) or n.value == 0:
            continue
        arr = (PHYSICAL_MONITOR * n.value)()
        if not _dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, n.value, arr):
            continue
        try:
            for i in range(n.value):
                pm = arr[i]
                displays.append(
                    Display(
                        index=len(displays),
                        device=device,
                        width=rect[0],
                        height=rect[1],
                        x=rect[2],
                        y=rect[3],
                        desc=pm.szPhysicalMonitorDescription,
                        current_input=_read_vcp(pm.hPhysicalMonitor, VCP_INPUT_SOURCE),
                        supported_inputs=(
                            _parse_supported_inputs(_capability_string(pm.hPhysicalMonitor))
                            if with_capabilities
                            else []
                        ),
                    )
                )
        finally:
            _dxva2.DestroyPhysicalMonitors(n.value, arr)

    return displays


def find_display(index: int) -> Display | None:
    for d in list_displays():
        if d.index == index:
            return d
    return None


# --------------------------------------------------------------------------
# 切换
# --------------------------------------------------------------------------


def set_input(index: int, source: int) -> bool:
    """把第 index 台显示器的输入源切到 source（如 0x12 = HDMI-2）。

    返回 True 表示 dxva2 接受并下发成功。注意：命令发出后显示器会立刻开始
    重新锁定信号，期间本函数所在的进程通常仍能正常返回。
    """
    done: list[bool] = []

    for hmon, device, rect in _iter_hmonitors():
        n = wintypes.DWORD(0)
        if not _dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmon, ctypes.byref(n)) or n.value == 0:
            continue
        arr = (PHYSICAL_MONITOR * n.value)()
        if not _dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, n.value, arr):
            continue
        try:
            for i in range(n.value):
                idx = len(done)
                if idx == index:
                    ok = _write_vcp(arr[i].hPhysicalMonitor, VCP_INPUT_SOURCE, source)
                    done.append(ok)
                    return ok
                done.append(False)
        finally:
            _dxva2.DestroyPhysicalMonitors(n.value, arr)

    return False


def get_input(index: int) -> int | None:
    d = find_display(index)
    return d.current_input if d else None


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------


def _usage() -> None:
    print(__doc__)


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        _usage()
        return 0

    cmd = argv[0]

    if cmd == "list":
        displays = list_displays()
        if not displays:
            print("没有检测到任何物理显示器。")
            return 1
        for d in displays:
            print(d.describe())
            print()
        print("共 %d 台显示器。" % len(displays))
        return 0

    if cmd == "get":
        if len(argv) < 2:
            print("用法: python ddc_win.py get <序号>")
            return 2
        idx = int(argv[1])
        cur = get_input(idx)
        print("输入源 = %s" % (input_name(cur) if cur is not None else "读取失败"))
        return 0

    if cmd == "set":
        if len(argv) < 3:
            print("用法: python ddc_win.py set <序号> <输入源十六进制>")
            return 2
        idx = int(argv[1])
        source = int(argv[2], 16)
        restore_after = None
        if "--restore-after" in argv:
            restore_after = float(argv[argv.index("--restore-after") + 1])
        assume_yes = "--yes" in argv

        before = get_input(idx)
        print("目标显示器 [%d]，当前输入源 %s" % (idx, input_name(before) if before is not None else "?"))
        print("即将切换到 %s (0x%02X)" % (input_name(source), source))
        if not assume_yes:
            try:
                ans = input("确认执行？(y/N) ").strip().lower()
            except EOFError:
                ans = "n"
            if ans != "y":
                print("已取消。")
                return 0

        ok = set_input(idx, source)
        print("命令下发：%s" % ("成功" if ok else "失败"))

        if ok and restore_after is not None and before is not None:
            print("%.0f 秒后自动切回 %s ..." % (restore_after, input_name(before)))
            time.sleep(restore_after)
            back = set_input(idx, before)
            print("回切：%s" % ("成功" if back else "失败"))

        return 0 if ok else 1

    _usage()
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
