"""本地 TCP 控制口：给 kvmctl.py 和外壳用的命令接口。

协议是「一行命令 → 一个 JSON 响应」，命令集与 v1.0.x 完全一致：
status / switch / switch-peer / pair / unpair / reset-peer / autodetect /
auto on / auto off / quit。改协议会影响已发布的 kvmctl 与 Windows 外壳，
所以这里只做代码搬家，不动任何命令字符串与响应字段。
"""

from __future__ import annotations

import json
import socket
import threading
import time

from kvm_platform import input_source_name

from .common import log


class ControlMixin:
    def _status(self) -> dict:
        return {
            "name": self.name,
            "platform": self.platform.name,
            "role": self.role,
            "detail": self.cached_detail,
            "idle_seconds": self.cached_idle,
            "auto": self.auto_on,
            "armed": self.armed,
            "dry_run": bool(self.cfg["dry_run"]),
            "peer_ip": self.peer_ip,
            "peer_role": self.peer_role,
            "peer_online": self.peer_online,
            "paired": self.paired,
            # 已配对却持续收到校验不过的报文 —— 两端 token 错乱的信号。
            # 过去这个状态只在日志里刷屏，状态查询里看不见。
            "token_mismatch": bool(self.token_mismatch_with),
            "token_mismatch_with": self.token_mismatch_with,
            "peer_age_seconds": (time.time() - self.peer_last_seen) if self.peer_last_seen else None,
            "my_input_source": "0x%02X" % self.my_src,
            "peer_input_source": "0x%02X" % self.peer_src,
            "my_input_source_name": input_source_name(self.my_src),
            "peer_input_source_name": input_source_name(self.peer_src),
        }

    def _handle_command(self, line: str) -> dict:
        line = line.strip().lower()
        if line in ("status", ""):
            return {"ok": True, "status": self._status()}

        if line == "switch":
            ev = threading.Event()
            op = {"kind": "manual_switch", "reason": "手动触发", "event": ev}
            self.ops.put(op)
            ev.wait(3.0)
            return {"ok": True, "action": op.get("result", "pending")}

        if line in ("switch-peer", "switch peer", "switchpeer"):
            ev = threading.Event()
            op = {"kind": "manual_switch_peer", "reason": "手动触发（切到对端）", "event": ev}
            self.ops.put(op)
            ev.wait(3.0)
            return {"ok": True, "action": op.get("result", "pending")}

        if line == "pair":
            self.start_pairing()
            return {"ok": True, "action": "pairing_started"}

        if line == "unpair":
            self.unpair(reset_peer=True)
            return {"ok": True, "action": "unpaired"}

        if line in ("reset-peer", "reset peer", "resetpeer"):
            ok = self._send_reset()
            return {"ok": ok, "action": "reset_sent" if ok else "no_known_peer"}

        if line == "autodetect":
            self.autodetect_async()
            return {"ok": True, "action": "started"}

        if line == "auto on":
            self.set_auto_switch(True)
            log("自动切换已开启")
            return {"ok": True, "auto": True}

        if line == "auto off":
            self.set_auto_switch(False)
            log("自动切换已关闭")
            return {"ok": True, "auto": False}

        if line == "quit":
            self.running = False
            return {"ok": True, "action": "quit"}

        return {"ok": False, "error": "未知命令: %s" % line}

    def _control_loop(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", int(self.cfg["control_port"])))
        srv.listen(8)
        srv.settimeout(0.5)
        while self.running:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                conn.settimeout(2)
                try:
                    data = conn.recv(1024).decode("utf-8", "replace")
                except OSError:
                    continue
                try:
                    resp = self._handle_command(
                        data.splitlines()[0] if data.strip() else "status"
                    )
                except Exception as exc:  # noqa: BLE001
                    resp = {"ok": False, "error": str(exc)}
                try:
                    conn.sendall(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
                except OSError:
                    pass
        srv.close()
