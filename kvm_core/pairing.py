"""配对状态机：announce / offer / accept / reset，以及发起与解除配对。

安全性：配对只在「双方都未配对」时发生。已配对的一方会直接忽略任何新的
announce / offer，因此没法用伪造的 offer 劫持一个已经配对好的对子。
协商出的 token 在局域网里明文跑，家用无妨；公司/公共网络请勿用。
"""

from __future__ import annotations

import secrets
import threading
import time

from .common import log


class PairingMixin:
    def _fire_paired(self, ok: bool, peer: str, msg: str) -> None:
        if callable(self.on_paired):
            try:
                self.on_paired(ok, peer, msg)
            except Exception:  # noqa: BLE001
                pass

    def _on_pairing(self, msg: dict, addr) -> None:
        """处理 announce / offer / accept / reset。"""
        kind = msg.get("t")
        who = msg.get("from")
        if who == self.name:
            return

        if kind == "pairing_announce":
            if self.paired:
                return
            self.pair_candidates[addr[0]] = who
            if callable(self.on_discovered):
                try:
                    self.on_discovered(who, addr[0])
                except Exception:  # noqa: BLE001
                    pass
            return

        if kind == "pairing_reset":
            # 带外恢复通道。只认「知道当前或上一个共享 token」的对端，
            # 不知道 token 的第三方拆不散已配对的两端，强度与普通校验同级。
            # 这是解开死锁的关键：卡住的那端会拒绝一切 offer，
            # 没有它就只能靠用户去那台机器上点托盘。
            incoming = str(msg.get("token") or "")
            known = [self.token]
            last = str(self.cfg.get("last_token") or "")
            if last:
                known.append(last)
            if not incoming or incoming not in known:
                return
            if not self.paired:
                return
            self.paired = False
            self.cfg["paired"] = False
            self._offered_nonce = None
            self._offered_token = None
            self.pair_error = None
            self.pair_candidates[addr[0]] = who
            # 不换 token：换了新的反而让两端差距更大，等下次配对协商即可。
            self._save_cfg()
            log("对端 %s 请求解除配对，已恢复未配对状态" % who)
            return

        if kind == "pairing_offer":
            if self.paired:
                return  # 已配对：不接受新配对，防劫持
            incoming = str(msg.get("token") or "")
            if len(incoming) < 6:
                return
            # 双方同时点「配对」时按 from 名字大小定主导：名字大的一方维持自己的
            # offer，名字小的一方采纳对方的 token（mac/windows 不会同名，必有胜负）。
            if self._offered_nonce and who < self.name:
                return
            # 采纳对端发来的 token，写盘并回确认
            self.token = incoming
            self.cfg["token"] = incoming
            self.paired = True
            self.cfg["paired"] = True
            self._offered_nonce = None
            self.pair_error = None
            self._save_cfg()
            self._send_raw(
                {"t": "pairing_accept", "from": self.name, "nonce": msg.get("nonce")},
                addr[0],
            )
            log("已通过对端 %s 的配对，写入共享 token" % who)
            self._fire_paired(True, who, "已与 %s 配对" % who)
            return

        if kind == "pairing_accept":
            if not self._offered_nonce:
                return
            if str(msg.get("nonce")) != self._offered_nonce:
                return
            # 对端确认采纳了本机发出的 token
            self.paired = True
            self.cfg["paired"] = True
            self._save_cfg()
            self._offered_nonce = None
            self.pair_error = None
            who = msg.get("from")
            log("配对成功：%s 已采纳本机共享 token" % who)
            self._fire_paired(True, who, "已与 %s 配对" % who)
            return

    def start_pairing(self) -> None:
        """发起局域网配对：生成共享 token 并广播，等未配对的对端采纳。"""

        def blast() -> None:
            # 目标：全子网广播 + 已发现的候选 IP + 已配置的 peer_ip。
            # 不单独依赖广播（某些环境下 255.255.255.255 不可达，但点对点可达）。
            targets = ["255.255.255.255"]
            targets += [ip for ip in self.pair_candidates if ip]
            if self.cfg.get("peer_ip"):
                targets.append(self.cfg["peer_ip"])
            offer = {
                "t": "pairing_offer",
                "from": self.name,
                "token": self._offered_token,
                "nonce": self._offered_nonce,
            }
            for _ in range(4):
                if self.paired and self._offered_nonce is None:
                    break
                for ip in targets:
                    self._send_raw(offer, ip)
                time.sleep(0.8)
            # 超时仍未被采纳：**绝不能假装成功**。
            # 一旦这里置 paired=True 并保留新 token，而对端根本没收到 offer（防火墙 /
            # 广播不可达 / 对端已配对而忽略），两端 token 就永久不一致，之后所有消息
            # 都会被对端判为「token 不匹配」；而且本机 paired=True 后会忽略新的 offer，
            # 再点「配对两端」也没用 —— 死局。所以必须回滚到发起前的 token，
            # 保持未配对，让两端回到一致状态，用户可以修好网络后再试。
            if self._offered_nonce is not None and not self.paired:
                self.token = prev_token
                self.cfg["token"] = prev_token
                self.paired = False
                self.cfg["paired"] = False
                self._offered_nonce = None
                self._offered_token = None
                self.pair_error = "配对失败：未收到对端应答（已回滚）"
                self._save_cfg()
                port = self.cfg.get("udp_port")
                log(
                    "配对失败：%.1f 秒内未收到对端应答，已回滚原 token。"
                    "请确认对端已运行、两端同网段、防火墙放行 UDP %s" % (4 * 0.8, port)
                )
                self._fire_paired(
                    False,
                    "",
                    "配对失败：未收到对端应答，已回滚原 token。"
                    "请确认对端已运行、两端在同一网段，且防火墙放行 UDP %s" % port,
                )

        # 必须在任何改动之前记下原 token：超时失败时要回滚到它，
        # 否则两端 token 不一致，会一直报「token 不匹配」。
        prev_token = self.token
        self.pair_error = None
        # 先请对端一并解除：它若卡在 paired=true 会忽略一切 offer，
        # 不先重置就永远配不上。必须在换 token 之前发 —— 对端是按「当前或
        # 上一个共享 token」认人的，换完再发它就认不出来了。
        self._send_reset()
        if self.paired:
            self.unpair()  # 已配对先解除，再重新协商
        self.cfg["last_token"] = prev_token
        new_token = secrets.token_hex(8)
        nonce = secrets.token_hex(4)
        self.token = new_token
        self.cfg["token"] = new_token
        self._offered_token = new_token
        self._offered_nonce = nonce
        self.paired = False  # 等 accept 确认
        self.cfg["paired"] = False  # 等 accept 确认后才置 true
        self.pair_candidates.clear()
        threading.Thread(target=blast, name="pair", daemon=True).start()
        log("已开始局域网配对，等待对端采纳共享 token …")

    def unpair(self, reset_peer: bool = False) -> None:
        """解除配对：换一个新随机 token 并恢复可被发现，便于重新配对。

        reset_peer=True 时先请对端一并解除，避免「本端解了、对端还卡着」的
        半边状态。必须先发再换 token，理由同 start_pairing。
        """
        if reset_peer:
            self._send_reset()
        # 旧 token 留一份：对端若带着它来请求解除，才认得出来。
        self.cfg["last_token"] = self.token
        self.token = secrets.token_hex(8)
        self.cfg["token"] = self.token
        self.paired = False
        self.cfg["paired"] = False
        self._offered_nonce = None
        self.pair_error = None
        self.pair_candidates.clear()
        self._save_cfg()
        log("已解除配对，恢复可被发现状态")
