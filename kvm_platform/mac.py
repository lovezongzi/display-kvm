"""macOS（Apple Silicon）平台实现。

Apple Silicon 上 m1ddc 走的是私有 CoreDisplay 代理，**显示器切到对端之后
依然能读能写**，所以"我在不在显示器上"只能靠 current_input 与 my_input_source
比较来判断，而不能靠"能不能读 DDC"。这是 v1.0.0 一开始判断错的地方。
"""

from __future__ import annotations

import ctypes
import re
import subprocess
import time

from .base import PlatformBase
from .constants import _parse_source, input_source_name


class MacPlatform(PlatformBase):
    name = "mac"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.display_index = cfg.get("display_index", 1)
        self.my_src = _parse_source(cfg.get("my_input_source"))
        self.get_cmd = cfg.get("ddc_get_cmd", "m1ddc display {index} get input")
        self.set_cmd = cfg.get("ddc_set_cmd", "m1ddc display {index} set input {value}")
        # 懒加载并缓存 Quartz 动态库句柄：idle_seconds() 每轮轮询都调，
        # 原来每次都 ctypes.CDLL 重新加载 ApplicationServices 框架，既慢又
        # 会累积 dyld 引用（轻微泄漏）。
        self._quartz_lib = None
        # 走 Quartz 时空闲查询是纯内存调用，可以高频采样；一旦退化到
        # ioreg（要起子进程）就自动降频，见 idle_seconds()。
        self.idle_is_cheap = True

    def _run(self, cmd: str, timeout: float = 6.0) -> tuple[int, str, str]:
        try:
            p = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
        except subprocess.TimeoutExpired:
            return 124, "", "timeout"
        except Exception as exc:  # noqa: BLE001
            return 1, "", str(exc)

    def current_input(self) -> int | None:
        # 读输入源(轮询)给短超时：m1ddc 正常 <1s，个别显示器休眠时会卡住。
        # 卡 6s 等于主循环被冻 6s；降到 4s 把最坏卡顿限制住（切换仍用 6s）。
        rc, out, err = self._run(self.get_cmd.format(index=self.display_index), timeout=4.0)
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
        if v is not None:
            self.idle_is_cheap = True
            return v
        # 只剩 ioreg 子进程时不能高频采样，否则每 200ms 起一次进程
        self.idle_is_cheap = False
        return self._idle_ioreg()

    def _idle_quartz(self) -> float | None:
        try:
            if self._quartz_lib is None:
                self._quartz_lib = ctypes.CDLL(
                    "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
                )
            lib = self._quartz_lib
            lib.CGEventSourceSecondsSinceLastEventType.restype = ctypes.c_double
            lib.CGEventSourceSecondsSinceLastEventType.argtypes = [ctypes.c_int32, ctypes.c_int32]
            # kCGEventSourceStateCombinedSessionState = 0, kCGAnyInputEventType = 0xFFFFFFFF
            return float(lib.CGEventSourceSecondsSinceLastEventType(0, -1))
        except Exception:  # noqa: BLE001
            return None

    def _idle_ioreg(self) -> float | None:
        # 必须带 -r：IOHIDSystem 挂在注册表深处，不加 -r 时 -d 1/-d 2 只打印
        # 到 Root，永远匹配不到 HIDIdleTime（这条回退路径等于一直是坏的）。
        rc, out, err = self._run("ioreg -c IOHIDSystem -r -d 2")
        if rc != 0:
            return None
        m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
        return int(m.group(1)) / 1e9 if m else None

    def detail(self) -> str:
        cur = self.current_input()
        if cur is None:
            return "共用显示器当前不在本机（DDC 命令无返回）"
        return "共用显示器在本机，当前输入源 0x%02X" % cur
