"""UDP 消息分发：hello / switch_request / ack / config_sync，以及 token 校验
与「两端 token 错乱」的观测自愈。

只做"收到什么就分派给谁"，真正碰显示器/配对的动作一律交给别的 mixin 或
任务队列（主循环线程执行）。
"""

from __future__ import annotations

import json
import time

from .common import log
from .config import _src


class DiscoveryMixin:
    # 判定「两端 token 已经错乱」的阈值：窗口内累计这么多次校验失败。
    MISMATCH_WINDOW_SECONDS = 30.0
    MISMATCH_TRIGGER_COUNT = 10
    MISMATCH_LOG_EVERY = 30.0  # 同一 IP 的日志节流间隔

    def _on_message(self, msg: dict, addr) -> None:
        kind = msg.get("t")
        # 配对消息先于 token 校验处理：未配对的两端要靠它协商出共享 token，
        # 此时双方 token 还不一致，走普通校验会被直接丢弃。
        if kind in ("pairing_announce", "pairing_offer", "pairing_accept", "pairing_reset"):
            self._on_pairing(msg, addr)
            return
        if msg.get("from") == self.name:
            return
        if str(msg.get("token")) != self.token:
            self._on_token_mismatch(addr[0])
            return
        # 走到这里说明 token 对上了，清掉此前累积的不一致观测。
        if self.token_mismatch_with == addr[0]:
            self.token_mismatch_with = ""
            self._mismatch_count.pop(addr[0], None)

        # 任何一条合法消息都算一次心跳（hello / switch_request / ack 都算）
        self.peer_last_seen = time.time()
        if not self.peer_online or self.peer_ip != addr[0]:
            log("发现对端 %s（%s）" % (msg.get("from"), addr[0]))
        self.peer_online = True

        kind = msg.get("t")

        if kind == "hello":
            self.peer_ip = addr[0]
            self.peer_role = msg.get("role", "unknown")
            if not self.cfg.get("peer_ip"):
                self._send({"t": "hello", "role": self.role, "platform": self.platform.name}, addr[0])

        elif kind == "switch_request":
            source = msg.get("source")
            if source is None:
                return
            # 不在这里碰平台，交给主循环
            self.ops.put(
                {
                    "kind": "remote_switch",
                    "source": _src(source),
                    "who": msg.get("from"),
                    "addr": addr[0],
                }
            )

        elif kind == "ack":
            log("对端确认：切换%s" % ("成功" if msg.get("ok") else "失败"))

        elif kind == "config_sync":
            # 对端改了可同步的配置（目前只有 auto_switch）。这条消息已经过 token
            # 校验，只有已配对的对端能发得进来；接收端只镜像、不回播，避免环路。
            self._on_config_sync(msg)

    def _on_token_mismatch(self, ip: str) -> None:
        """收到校验不过的报文。

        未配对时两端 token 本来就不同，这是正常的，直接静默（否则会一直刷日志）。
        已配对却持续校验失败，说明 token 错乱 —— 节流记日志，达到阈值就自愈。

        判据必须是「收到了对端报文但校验失败」，不能是「没收到心跳」：
        对端只是关机时，不该把配对解掉。
        """
        if not self.paired:
            return

        now = time.time()
        if now - self._mismatch_window_start > self.MISMATCH_WINDOW_SECONDS:
            self._mismatch_window_start = now
            self._mismatch_count.clear()

        n = self._mismatch_count.get(ip, 0) + 1
        self._mismatch_count[ip] = n
        self.token_mismatch_with = ip

        if now - self._mismatch_log_at.get(ip, 0.0) >= self.MISMATCH_LOG_EVERY:
            self._mismatch_log_at[ip] = now
            log(
                "忽略来自 %s 的消息：token 不匹配（%.0f 秒内 %d 次）"
                % (ip, self.MISMATCH_WINDOW_SECONDS, n)
            )

        if n < self.MISMATCH_TRIGGER_COUNT:
            return

        # 自愈：本端降级，并请对端一并降级。过去这一步只能由用户去「出错的
        # 那一端」点托盘，而那一端往往正是看不见问题的那台机器。
        self._mismatch_count.clear()
        self._mismatch_log_at.pop(ip, None)
        self.token_mismatch_with = ""
        log(
            "与 %s 的 token 持续不一致（%.0f 秒内 %d 次），判定为两端错乱，"
            "自动解除配对以便重新协商" % (ip, self.MISMATCH_WINDOW_SECONDS, n)
        )
        self.unpair(reset_peer=True)
        self._fire_paired(
            False,
            ip,
            "检测到与 %s 的 token 不一致，已自动解除配对。请在任意一端重新点「配对两端」。" % ip,
        )

    def _udp_loop(self) -> None:
        while self.running:
            try:
                data, addr = self.sock.recvfrom(4096)
            except OSError:
                if self.running:
                    time.sleep(0.2)
                continue
            try:
                msg = json.loads(data.decode("utf-8"))
            except Exception:  # noqa: BLE001
                continue
            try:
                self._on_message(msg, addr)
            except Exception as exc:  # noqa: BLE001
                log("处理消息出错: %s" % exc)

