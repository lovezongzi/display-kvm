"""display-kvm 跨平台适配层。

三个能力，每端各自实现：
  1. target_display_present()  —— 那块共用显示器现在是不是在显示我（等价于 DDC 通道是否可用）
  2. switch_to(source)         —— 让显示器切到指定输入源（只能由"正在被显示"的一端调用）
  3. idle_seconds()            —— 本机键鼠已经多久没有输入了
"""

from __future__ import annotations

import ctypes
import re
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


class PlatformBase:
    name = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def target_display_present(self) -> bool:
        raise NotImplementedError

    def probe(self) -> tuple[bool, str, float | None]:
        """一次性返回 (是否在显示器上, 描述, 键鼠空闲秒数)。

        子类应覆写此方法，避免重复枚举显示器。默认实现直接拼三件事。
        """
        present = self.target_display_present()
        return present, self.detail(), self.idle_seconds()

    def switch_to(self, source: int) -> bool:
        raise NotImplementedError

    def idle_seconds(self) -> float | None:
        raise NotImplementedError

    def detail(self) -> str:
        return ""


# ==========================================================================
# Windows
# ==========================================================================


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class WindowsPlatform(PlatformBase):
    name = "win"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        import ddc_win  # 同目录

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
        return self._find_target() is not None

    def probe(self) -> tuple[bool, str, float | None]:
        d = self._find_target()
        if d is None:
            detail = "共用显示器当前不在本机（DDC 通道不可用）"
        else:
            detail = "共用显示器在本机，当前输入源 0x%02X" % d.current_input
        return d is not None, detail, self.idle_seconds()

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


# ==========================================================================
# macOS
# ==========================================================================


class MacPlatform(PlatformBase):
    name = "mac"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.display_index = cfg.get("display_index", 1)
        self.get_cmd = cfg.get("ddc_get_cmd", "m1ddc display {index} get input")
        self.set_cmd = cfg.get("ddc_set_cmd", "m1ddc display {index} set input {value}")

    def _run(self, cmd: str) -> tuple[int, str, str]:
        try:
            p = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=6,
            )
            return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
        except subprocess.TimeoutExpired:
            return 124, "", "timeout"
        except Exception as exc:  # noqa: BLE001
            return 1, "", str(exc)

    def current_input(self) -> int | None:
        rc, out, err = self._run(self.get_cmd.format(index=self.display_index))
        if rc != 0:
            return None
        m = re.search(r"\b(\d+)\b", out)
        return int(m.group(1)) if m else None

    def target_display_present(self) -> bool:
        return self.current_input() is not None

    def probe(self) -> tuple[bool, str, float | None]:
        cur = self.current_input()
        if cur is None:
            detail = "共用显示器当前不在本机（DDC 命令无返回）"
        else:
            detail = "共用显示器在本机，当前输入源 0x%02X" % cur
        return cur is not None, detail, self.idle_seconds()

    def switch_to(self, source: int) -> bool:
        rc, out, err = self._run(
            self.set_cmd.format(index=self.display_index, value=source, value_hex="0x%02X" % source)
        )
        return rc == 0

    def idle_seconds(self) -> float | None:
        # 优先用 Quartz（无需起子进程）；失败则回退到 ioreg
        v = self._idle_quartz()
        return v if v is not None else self._idle_ioreg()

    def _idle_quartz(self) -> float | None:
        try:
            lib = ctypes.CDLL(
                "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
            )
            lib.CGEventSourceSecondsSinceLastEventType.restype = ctypes.c_double
            lib.CGEventSourceSecondsSinceLastEventType.argtypes = [ctypes.c_int32, ctypes.c_int32]
            # kCGEventSourceStateCombinedSessionState = 0, kCGAnyInputEventType = 0xFFFFFFFF
            return float(lib.CGEventSourceSecondsSinceLastEventType(0, -1))
        except Exception:  # noqa: BLE001
            return None

    def _idle_ioreg(self) -> float | None:
        rc, out, err = self._run("ioreg -c IOHIDSystem -d 1")
        if rc != 0:
            return None
        m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
        return int(m.group(1)) / 1e9 if m else None

    def detail(self) -> str:
        cur = self.current_input()
        if cur is None:
            return "共用显示器当前不在本机（DDC 命令无返回）"
        return "共用显示器在本机，当前输入源 0x%02X" % cur


# ==========================================================================


def create(cfg: dict, force: str | None = None, strict: bool = True) -> PlatformBase:
    """创建平台实现。

    force 由命令行显式指定时传 strict=False：允许在错误系统上强行加载
    （DDC 命令会失败、角色恒为 idle），仅用于本机联调网络与委托逻辑。
    """
    from_config = force is None
    want = force or cfg.get("platform") or ("win" if IS_WIN else "mac" if IS_MAC else None)

    def _check(want_is: str, native: bool) -> None:
        if native:
            return
        msg = "平台要求 %s，但当前系统不是它" % want_is
        if strict and from_config:
            raise RuntimeError(msg + "。若确实要强行加载请用命令行 --platform 覆盖。")
        print("[warn] %s —— 强行加载，DDC 命令会失败，角色将恒为 idle" % msg)

    if want == "win":
        _check("Windows", IS_WIN)
        return WindowsPlatform(cfg)
    if want == "mac":
        _check("macOS", IS_MAC)
        return MacPlatform(cfg)
    raise RuntimeError("无法判断平台，请在配置里显式指定 platform 为 win 或 mac")
