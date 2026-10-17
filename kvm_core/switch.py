"""切换执行：本地 DDC 下发、委托对端、对端心跳超时、任务队列执行。

设计要点
--------
DDC 通道挂在活跃的视频链路上，所以**只有「当前正被显示器选中」的那台机器
才能命令显示器切换输入源**。于是核心机制是「委托」：
  * 我检测到本机键鼠有活动，但我当前不在显示器上
      -> 通过网络请对端（它正被显示）替我把显示器切到我的输入源
  * 我当前正在显示器上，需要切走
      -> 本地直接发 DDC 命令，无需对端参与

所有对显示器/系统的操作只在主循环线程里执行（UDP / 控制口线程只往 ops
队列塞命令），因为 DDC 与 EnumDisplayMonitors 都不是线程安全的。
"""

from __future__ import annotations

import queue
import time

from .common import log


class SwitchMixin:
    def _perform_switch(self, source: int, reason: str) -> bool:
        if self.cfg["dry_run"]:
            log("[dry-run] %s -> 本应把输入源切到 0x%02X" % (reason, source))
            self.last_switch_ts = time.time()
            return True
        log("%s -> 正在把输入源切到 0x%02X ..." % (reason, source))
        with self._plat_lock:
            ok = self.platform.switch_to(source)
        log("DDC 命令%s" % ("已下发" if ok else "下发失败"))
        self.last_switch_ts = time.time()
        return ok

    def request_switch(self, reason: str, force: bool = False) -> str:
        """请求把显示器切到本机。自动选择「本地执行」或「委托对端」。"""
        now = time.time()
        if not force and now - self.last_switch_ts < self.cfg["cooldown_seconds"]:
            return "cooldown"

        if self.role == "active":
            # 「切到本机」= 把显示器输入源设成本机那个口。这里原来写的是
            # self.peer_src，语义相反：本机已经在显示器上时点这个按钮，
            # 会反而把画面切到对端去。
            self._perform_switch(self.my_src, "%s｜本机正在显示器上，直接切" % reason)
            return "local"

        # 显示器在对端，但本机是否还能自己写 DDC？
        # Windows：不能（DDC 只对活跃链路可用）-> 必须委托对端。
        # macOS/Apple Silicon：能，m1ddc 走私有代理，显示器显示对端时照样可写
        #   -> 自己切，比委托对端更快，也让 Mac 端不依赖 Windows 端在线。
        with self._plat_lock:
            direct = self.platform.can_switch_directly()
        if direct:
            self._perform_switch(self.my_src, "%s｜本机可直接操作 DDC，自己切" % reason)
            return "local"

        if not self.peer_ip:
            log("%s｜但还没发现对端，无法委托" % reason)
            return "no-peer"

        self._send_peer({"t": "switch_request", "source": self.my_src})
        log("%s｜已委托 %s 把显示器切到 0x%02X" % (reason, self.cfg["peer_name"], self.my_src))
        self.last_switch_ts = now
        return "delegated"

    def request_switch_peer(self, reason: str, force: bool = False) -> str:
        """请求把显示器切到**对端**（把画面交出去）。

        v1.0.0 只实现了"切到本机"：所有切换路径写的都是 my_src，peer_src
        从头到尾只出现在状态显示里。结果是任何一端都只能把画面拉给自己，
        没有任何办法主动把画面交给对方 —— 想切过去只能去碰对方那台机器的键鼠。

        执行者只能是"当前正在显示"或"能直接写 DDC"的那一端：
          - 本机正在显示                    -> 本机写 peer_src，画面直接交出去
          - 本机不在显示、但能直写（Apple Silicon 的 Mac）-> 同样本地写
          - 两条都不满足（如 Windows 且不在显示）-> 委托对端，让它切到我的 peer_src
        """
        now = time.time()
        if not force and now - self.last_switch_ts < self.cfg["cooldown_seconds"]:
            return "cooldown"

        with self._plat_lock:
            direct = self.role == "active" or self.platform.can_switch_directly()

        if direct:
            self._perform_switch(self.peer_src, "%s｜把画面交给对端" % reason)
            return "local"

        if not self.peer_ip:
            log("%s｜但还没发现对端，无法委托" % reason)
            return "no-peer"

        # 委托时带的是"目标输入源"，也就是对端的口（= 我的 peer_src）
        self._send_peer({"t": "switch_request", "source": self.peer_src})
        log("%s｜已委托 %s 把显示器切到 0x%02X" % (reason, self.cfg["peer_name"], self.peer_src))
        self.last_switch_ts = now
        return "delegated"

    def _expire_peer(self, now: float) -> None:
        """对端心跳超时就判定离线并清掉地址。

        v1.0.0 里 peer_ip / peer_role 一旦被赋值就永不失效：对端下线后，
        托盘仍显示它最后一次的 IP 和角色，看着像"还连着"，其实早就断了。
        """
        if not self.peer_last_seen:
            return
        if now - self.peer_last_seen <= float(self.cfg["peer_timeout_seconds"]):
            return
        if self.peer_online:
            log(
                "对端 %s 已 %.0f 秒没有心跳，判定离线"
                % (self.peer_ip or "?", now - self.peer_last_seen)
            )
        self.peer_online = False
        if not self.cfg.get("peer_ip"):
            # 只有自动发现出来的地址才清掉；配置里写死的要保留
            self.peer_ip = ""
        self.peer_role = "unknown"

    def _execute(self, op: dict) -> None:
        kind = op.get("kind")
        try:
            if kind == "remote_switch":
                if self.role != "active":
                    log("收到 %s 的委托，但我现在也不在显示器上，无法执行" % op["who"])
                    self._send({"t": "ack", "ok": False}, op["addr"])
                    return
                log("收到 %s 的委托：把显示器切到 0x%02X" % (op["who"], op["source"]))
                ok = self._perform_switch(op["source"], "受 %s 委托" % op["who"])
                self._send({"t": "ack", "ok": ok}, op["addr"])

            elif kind == "manual_switch":
                op["result"] = self.request_switch(op.get("reason") or "手动触发", force=True)

            elif kind == "manual_switch_peer":
                op["result"] = self.request_switch_peer(op.get("reason") or "手动触发", force=True)
        except Exception as exc:  # noqa: BLE001
            log("执行任务出错: %s" % exc)
            op["result"] = "error"
        finally:
            ev = op.get("event")
            if ev:
                ev.set()

    def _drain_ops(self) -> None:
        while True:
            try:
                op = self.ops.get_nowait()
            except queue.Empty:
                return
            self._execute(op)
