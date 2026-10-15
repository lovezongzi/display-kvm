"""display-kvm 跨平台适配层。

三个能力，每端各自实现：
  1. target_display_present()  —— 那块共用显示器现在是不是在显示我
  2. switch_to(source)         —— 让显示器切到指定输入源
  3. idle_seconds()            —— 本机键鼠已经多久没有输入了

macOS / Apple Silicon 的特殊性（很重要，v1.0.0 在这里判断错了）：
Windows 上 DDC 只走"当前激活的那条链路"，显示器切到对端后本机就读写不到，
于是"能读 DDC" ≈ "显示器正在显示我"，两个概念可以合并。
但 Apple Silicon 上 m1ddc 走的是私有 CoreDisplay 代理，**显示器切到对端之后
依然能读能写**。所以：
  - "我在不在显示器上" 只能靠 current_input 与 my_input_source 比较来判断；
  - "能不能直接切" 恒为真 —— 本机自己就能把显示器切回来，不必委托对端。
"""

from __future__ import annotations

import ctypes
import re
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

# DDC/CI 的 VCP 0x60（Input Select）取值含义。
# 0x01~0x12 是 MCCS 标准；0x1B 是 m1ddc 文档里点名的 USB-C；
# 0x90/0x91/0xD0/0xD1/0xD2 是 LG 一类显示器用的 alternate addressing
#（m1ddc 的 input-alt 子命令走的那一套）。
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

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


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


class PlatformBase:
    name = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        # 最近一次 DDC 读是否成功。读失败时上层不应据此改角色（见 agent._refresh_platform_cache）
        self.last_read_ok = True
        # 本机/对端各自接在哪个输入口，两端都要用（角色判定就是拿它和当前输入源比）
        self.my_src = _parse_source(cfg.get("my_input_source"))
        self.peer_src = _parse_source(cfg.get("peer_input_source"))

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

    def can_switch_directly(self) -> bool:
        """本机现在能否直接操作显示器的输入源（不必委托对端）。

        默认用"显示器当前在显示我"近似 —— 这适用于 Windows：DDC 只对活跃
        链路可用，切换到对端后本机就写不动了，只能委托对端。
        macOS 上 DDC 通道始终可用，会覆写为恒 True。
        """
        return self.target_display_present()

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


# ==========================================================================
# macOS
# ==========================================================================


class MacPlatform(PlatformBase):
    name = "mac"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.display_index = cfg.get("display_index", 1)
        self.my_src = _parse_source(cfg.get("my_input_source"))
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
            self.last_read_ok = False
            return None
        # m1ddc 用**负数**表示失败（刚切完输入源的那几秒会返回 -7）。
        # 原来的 \b(\d+)\b 会把 "-7" 里的 7 抠出来当成一个合法的输入源编号，
        # 于是状态里会出现"当前输入源 0x07"这种根本不存在的值。
        m = re.search(r"(-?\d+)", out)
        if not m:
            self.last_read_ok = False
            return None
        v = int(m.group(1))
        self.last_read_ok = v >= 0
        return v if v >= 0 else None

    def _on_this_machine(self, cur: int | None) -> bool:
        """只有当前输入源恰好是本机那个口时，画面才真的在本机。

        Apple Silicon 上 m1ddc 的 DDC 代理在显示器切到对端后仍然可读，
        所以"读得到"完全不能当作"在显示我"。v1.0.0 就是错在这里：
        结果 role 恒为 active —— 自动切换永不触发、点击按钮又切错方向。
        """
        return cur is not None and self.my_src is not None and cur == self.my_src

    def target_display_present(self) -> bool:
        return self._on_this_machine(self.current_input())

    def probe(self) -> tuple[bool, str, float | None]:
        cur = self.current_input()
        present = self._on_this_machine(cur)
        if cur is None:
            detail = "共用显示器当前不在本机（DDC 命令无返回）"
        elif present:
            detail = "共用显示器在本机，当前输入源 %s" % input_source_name(cur)
        else:
            detail = "共用显示器在对端，当前输入源 %s（本机是 %s）" % (
                input_source_name(cur),
                input_source_name(self.my_src),
            )
        return present, detail, self.idle_seconds()

    def probe_live_inputs(self, candidates, settle_seconds: float = 14.0) -> list[int]:
        """逐个把显示器切到候选输入源，找出哪些口上真的接着一台开着的机器。

        原理：写到一个空口上，显示器会自己跳回有信号的口。所以
        「下发 X，过一会儿读回来还是 X」= X 上有活信号。

        坑：这个"跳回"很慢。H27T22C3 实测切到空口后要 **12 秒**才跳回，
        3 秒就去读的话会把空口全判成有信号（实测四个口全中）。所以
        settle_seconds 默认给 14 秒，宁可慢也不能判错。

        注意：这个过程会真的切换画面，所以只在用户主动触发时跑，
        结束时会把显示器切回探测前的那个口。
        """
        original = self.current_input()
        live: list[int] = []
        for v in candidates:
            if not self.switch_to(v):
                continue
            time.sleep(settle_seconds)
            cur = self.current_input()
            # 刚切完那几秒 DDC 读经常返回 -7（读失败），直接判定会把有信号的
            # 口误判成空口。多读几次，读到有效值再下结论。
            for _ in range(4):
                if cur is not None:
                    break
                time.sleep(1.0)
                cur = self.current_input()
            if cur == v:
                live.append(v)
        if original is not None:
            self.switch_to(original)
        return live

    def can_switch_directly(self) -> bool:
        # Apple Silicon 的 DDC 代理：显示器显示对端时本机照样能下发 set input。
        # （已在 HKC H27T22C3 + MacBook Air 上实测：显示器正在显示 Windows 时，
        #  Mac 下发 set input 15 能直接把画面切回来，rc=0。）
        #
        # 注意这里**不能**用"读没读到"来判断：刚切完输入源的那几秒 DDC 读必然
        # 失败（返回 -7），但写照样能成功。用读的结果会把这期间的自动切换
        # 误判成"通道断了"，进而退回去委托一个并不存在的对端。
        return True

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
