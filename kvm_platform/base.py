"""平台抽象基类 PlatformBase。

定义三件事的接口，每端各自实现：
  1. target_display_present()  —— 那块共用显示器现在是不是在显示我
  2. switch_to(source)         —— 让显示器切到指定输入源
  3. idle_seconds()            —— 本机键鼠已经多久没有输入了
"""

from __future__ import annotations

from .constants import _parse_source


class PlatformBase:
    name = "base"

    # 空闲查询是不是"便宜"的：纯 API 调用、可以每 200ms 采一次。
    # False 表示要起子进程之类的慢操作，只能跟着角色轮询一起做。
    idle_is_cheap = False

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
