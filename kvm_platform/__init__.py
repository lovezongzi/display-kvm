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

import sys

from .base import PlatformBase
from .constants import IS_MAC, IS_WIN, VCP_INPUT_NAMES, input_source_name
from .mac import MacPlatform
from .windows import WindowsPlatform

__all__ = [
    "PlatformBase",
    "WindowsPlatform",
    "MacPlatform",
    "create",
    "input_source_name",
    "VCP_INPUT_NAMES",
    "IS_WIN",
    "IS_MAC",
]


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
