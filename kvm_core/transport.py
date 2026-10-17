"""UDP 传输层：底层发包、广播、心跳。

只负责"把一条消息发到某个 IP"，不解析消息内容、不碰显示器。
依赖 Agent 提供 self.sock / self.name / self.token / self.cfg / self.role
以及 self.platform（发 hello 时取平台名）。
"""

from __future__ import annotations

import json

from .common import log


class TransportMixin:
    @property
    def peer_port(self) -> int:
        return int(self.cfg["peer_udp_port"] or self.cfg["udp_port"])

    def _send(self, msg: dict, ip: str) -> None:
        """带 token 包装的发送（普通消息用）。"""
        payload = json.dumps({**msg, "from": self.name, "token": self.token}).encode("utf-8")
        try:
            self.sock.sendto(payload, (ip, self.peer_port))
        except OSError as exc:
            log("发送失败: %s" % exc)

    def _send_peer(self, msg: dict) -> bool:
        if not self.peer_ip:
            return False
        self._send(msg, self.peer_ip)
        return True

    def _send_raw(self, msg: dict, ip: str) -> None:
        """不经 token 包装的原始发送（配对消息用，因为配对时双方 token 还不一致）。"""
        try:
            self.sock.sendto(json.dumps(msg).encode("utf-8"), (ip, self.peer_port))
        except OSError as exc:
            log("发送失败: %s" % exc)

    def _send_hello(self) -> None:
        msg = {"t": "hello", "role": self.role, "platform": self.platform.name}
        if self.peer_ip:
            self._send(msg, self.peer_ip)
        else:
            self._send(msg, "255.255.255.255")

    def _send_reset(self) -> bool:
        """请对端解除配对。

        只发给已知的对端地址，不广播 —— 广播 reset 等于允许局域网里任何主机
        拆散别人的配对。对端会核对报文里的 token 是否等于它当前或上一个共享
        token，两个都对不上就忽略，强度与现有的 token 校验同级别。
        """
        targets: list[str] = []
        if self.peer_ip:
            targets.append(self.peer_ip)
        for ip in self.pair_candidates:
            if ip not in targets:
                targets.append(ip)
        if not targets:
            return False
        for ip in targets:
            self._send({"t": "pairing_reset", "from": self.name}, ip)
        log("已请求对端解除配对（%s）" % ", ".join(targets))
        return True
