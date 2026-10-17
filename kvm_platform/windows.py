"""Windows 平台实现（DDC/CI + GetLastInputInfo）。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from .base import PlatformBase
from .constants import input_source_name


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class WindowsPlatform(PlatformBase):
    name = "win"
    idle_is_cheap = True  # GetLastInputInfo 就是一次内核调用，随便高频

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        import ddc_win  # 同目录（见 constants 里的 sys.path 注入）

        self._ddc = ddc_win
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
        self._kernel32.GetTickCount.restype = wintypes.DWORD

        self.match_w = cfg.get("match_width")
        self.match_h = cfg.get("match_height")

    # ---- 找出那块共用显示器 ----------------------------------------------

    def _find_target(self):
        # with_capabilities=False 是关键：拉取 MCCS 能力串走 I2C 慢通道，
        # 单次约 1.2 秒/显示器，而角色判定只用得到 VCP 0x60。
        for d in self._ddc.list_displays(with_capabilities=False):
            if self.match_w and d.width != self.match_w:
                continue
            if self.match_h and d.height != self.match_h:
                continue
            if d.current_input is None:
                continue  # 列出了但读不到，视为不可用
            return d
        return None

    def target_display_present(self) -> bool:
        # 和 macOS 一样：光"显示器在列表里、DDC 读得到"不能说明它在显示我，
        # 必须拿当前输入源和本机那个口比。否则 Windows 端角色永远是 active，
        # 自动切换永远不会触发（v1.0.0 到现在的 Windows 侧行为）。
        d = self._find_target()
        return d is not None and d.current_input == self.my_src

    def probe(self) -> tuple[bool, str, float | None]:
        d = self._find_target()
        if d is None:
            return False, "共用显示器当前不在本机（DDC 通道不可用）", self.idle_seconds()
        cur = d.current_input
        if cur == self.my_src:
            detail = "共用显示器在本机，当前输入源 %s" % input_source_name(cur)
            return True, detail, self.idle_seconds()
        detail = "共用显示器在对端，当前输入源 %s（本机是 %s）" % (
            input_source_name(cur),
            input_source_name(self.my_src),
        )
        return False, detail, self.idle_seconds()

    def current_input(self) -> int | None:
        d = self._find_target()
        return d.current_input if d else None

    def switch_to(self, source: int) -> bool:
        d = self._find_target()
        if d is None:
            return False
        return self._ddc.set_input(d.index, source)

    # ---- 输入空闲时长 -----------------------------------------------------

    def idle_seconds(self) -> float | None:
        lii = _LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not self._user32.GetLastInputInfo(ctypes.byref(lii)):
            return None
        tick = self._kernel32.GetTickCount()
        return ((tick - lii.dwTime) & 0xFFFFFFFF) / 1000.0

    def detail(self) -> str:
        d = self._find_target()
        if d is None:
            return "共用显示器当前不在本机（DDC 通道不可用）"
        cur = d.current_input
        return "共用显示器在本机，当前输入源 0x%02X" % cur
