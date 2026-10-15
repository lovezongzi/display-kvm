#!/usr/bin/env python3
"""display-kvm —— 跨机显示器输入源自动切换 Agent。

设计要点
--------
DDC 通道挂在活跃的视频链路上，所以**只有「当前正被显示器选中」的那台机器
才能命令显示器切换输入源**。这带来一个反直觉的结论：

    想切过去的那台机器，往往正好是发不出命令的那台。

于是本 Agent 的核心机制是「委托」：
  * 我检测到本机键鼠有输入活动，但我当前不在显示器上
      -> 通过网络请对端（它正被显示）替我把显示器切到我的输入源
  * 我当前正在显示器上，需要切走
      -> 本地直接发 DDC 命令，无需对端参与

这样两台各有独立键鼠的场景下，你碰哪台的键鼠，显示器就会跳到哪台。

线程模型（重要）
----------------
所有对显示器/系统的操作（DDC、EnumDisplayMonitors、空闲查询）**只在主循环
线程里执行**。UDP 线程与控制口线程只做两件事：收发消息、往任务队列里塞命令。
原因是 DDC 与 EnumDisplayMonitors 都不是线程安全的，多线程并发调用会卡死。

用法
----
    python agent.py --config kvm.json
    python agent.py --config kvm.json --status      # 只打印一次状态后退出
    python agent.py --config kvm.json --dry-run     # 只记录、不真的切
"""

from __future__ import annotations

import argparse
import json
import queue
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import platforms  # noqa: E402

DEFAULT_CONFIG: dict = {
    # 身份
    "name": "windows",
    "platform": "win",  # win | mac
    "peer_name": "mac",
    "peer_ip": "",  # 留空则靠 UDP 广播自动发现
    "token": "change-me",
    # 端口（peer_udp_port 留 0 表示与 udp_port 相同；同机双实例自测时可分开设）
    "udp_port": 47821,
    "peer_udp_port": 0,
    "control_port": 47822,
    # 显示器：本机这块屏的输入源，以及对端的输入源
    "my_input_source": "0x10",
    "peer_input_source": "0x12",
    # Windows 端用分辨率定位共用显示器
    "match_width": 2560,
    "match_height": 1440,
    # macOS 端用索引 + 命令模板
    "display_index": 1,
    "ddc_get_cmd": "m1ddc display {index} get input",
    "ddc_set_cmd": "m1ddc display {index} set input {value}",
    # 行为
    "auto_switch": True,
    "dry_run": False,
    "tick_ms": 250,
    "role_poll_seconds": 2.0,
    "arm_after_idle_seconds": 3.0,
    "fire_below_idle_seconds": 0.45,
    "cooldown_seconds": 6.0,
    "hello_interval_seconds": 3.0,
}


def _src(v) -> int:
    if isinstance(v, int):
        return v
    return int(str(v), 0)


def log(msg: str) -> None:
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


class Agent:
    def __init__(self, raw_cfg: dict, args: argparse.Namespace):
        cfg = {**DEFAULT_CONFIG, **raw_cfg}
        if args.dry_run:
            cfg["dry_run"] = True
        if args.port:
            cfg["udp_port"] = int(args.port)
        if getattr(args, "peer_port", None):
            cfg["peer_udp_port"] = int(args.peer_port)
        if args.control_port:
            cfg["control_port"] = int(args.control_port)
        if args.name:
            cfg["name"] = args.name
        if args.peer_ip:
            cfg["peer_ip"] = args.peer_ip
        self.cfg = cfg

        self.name = cfg["name"]
        self.token = str(cfg["token"])
        self.my_src = _src(cfg["my_input_source"])
        self.peer_src = _src(cfg["peer_input_source"])

        self.platform = platforms.create(cfg, force=args.platform, strict=args.platform is None)

        self.role = "unknown"  # active | idle
        self.armed = False
        self.peer_ip = cfg.get("peer_ip") or ""
        self.peer_role = "unknown"
        self.last_switch_ts = 0.0
        self.auto_on = bool(cfg["auto_switch"])
        self.running = True

        # 平台状态缓存：只在主循环里更新，其他线程只读
        self.cached_detail = ""
        self.cached_idle: float | None = None

        # 任务队列：UDP / 控制口线程把活丢进来，主循环执行
        self.ops: queue.Queue = queue.Queue()
        self._plat_lock = threading.Lock()

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.bind(("", int(cfg["udp_port"])))

    # ------------------------------------------------------------------ 网络

    @property
    def peer_port(self) -> int:
        return int(self.cfg["peer_udp_port"] or self.cfg["udp_port"])

    def _send(self, msg: dict, ip: str) -> None:
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

    def _send_hello(self) -> None:
        msg = {"t": "hello", "role": self.role, "platform": self.platform.name}
        if self.peer_ip:
            self._send(msg, self.peer_ip)
        else:
            self._send(msg, "255.255.255.255")

    def _on_message(self, msg: dict, addr) -> None:
        if msg.get("from") == self.name:
            return
        if str(msg.get("token")) != self.token:
            log("忽略来自 %s 的消息：token 不匹配" % addr[0])
            return

        kind = msg.get("t")

        if kind == "hello":
            if self.peer_ip != addr[0]:
                log("发现对端 %s（%s）" % (msg.get("from"), addr[0]))
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

    # ------------------------------------------------------------------ 切换（仅主循环调用）

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
            self._perform_switch(self.peer_src, "%s｜本机正在显示器上，直接切" % reason)
            return "local"

        if not self.peer_ip:
            log("%s｜但还没发现对端，无法委托" % reason)
            return "no-peer"

        self._send_peer({"t": "switch_request", "source": self.my_src})
        log("%s｜已委托 %s 把显示器切到 0x%02X" % (reason, self.cfg["peer_name"], self.my_src))
        self.last_switch_ts = now
        return "delegated"

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

    # ------------------------------------------------------------------ 主循环

    def _tick_auto(self) -> None:
        idle = self.cached_idle
        if idle is None:
            return

        if self.role != "idle":
            self.armed = False
            return

        if not self.armed:
            if idle >= self.cfg["arm_after_idle_seconds"]:
                self.armed = True
            return

        if idle <= self.cfg["fire_below_idle_seconds"]:
            self.armed = False
            self.request_switch("检测到本机键鼠活动")

    def _refresh_platform_cache(self) -> None:
        t0 = time.time()
        with self._plat_lock:
            present, detail, idle = self.platform.probe()
        cost = time.time() - t0
        if cost > 1.0:
            log("警告：探测显示器耗时 %.1f 秒，偏慢" % cost)

        new_role = "active" if present else "idle"
        if new_role != self.role:
            log("角色变更：%s -> %s" % (self.role, new_role))
        self.role = new_role
        if self.role == "active":
            self.armed = False
        self.cached_detail = detail
        self.cached_idle = idle

    def _main_loop(self) -> None:
        next_role = 0.0
        next_hello = 0.0
        while self.running:
            now = time.time()

            self._drain_ops()

            if now >= next_role:
                try:
                    self._refresh_platform_cache()
                except Exception as exc:  # noqa: BLE001
                    log("探测显示器状态出错: %s" % exc)
                next_role = now + float(self.cfg["role_poll_seconds"])

            if now >= next_hello:
                self._send_hello()
                next_hello = now + float(self.cfg["hello_interval_seconds"])

            if self.auto_on:
                try:
                    self._tick_auto()
                except Exception as exc:  # noqa: BLE001
                    log("自动检测出错: %s" % exc)

            time.sleep(float(self.cfg["tick_ms"]) / 1000.0)

    # ------------------------------------------------------------------ 控制口

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
            "my_input_source": "0x%02X" % self.my_src,
            "peer_input_source": "0x%02X" % self.peer_src,
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

        if line == "auto on":
            self.auto_on = True
            log("自动切换已开启")
            return {"ok": True, "auto": True}

        if line == "auto off":
            self.auto_on = False
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

    # ------------------------------------------------- 对外接口（托盘/外壳用）

    def api_status(self) -> dict:
        """读当前状态缓存。线程安全：只读主循环维护的字段。"""
        return self._status()

    def api_switch(self, reason: str = "手动触发") -> str:
        """请求切换，线程安全：走任务队列交给主循环执行。"""
        ev = threading.Event()
        op = {"kind": "manual_switch", "reason": reason, "event": ev}
        self.ops.put(op)
        ev.wait(6.0)
        return op.get("result", "pending")

    # ------------------------------------------------------------------ 启动

    def run(self) -> None:
        log("display-kvm agent 启动")
        log(
            "本机身份 %s（%s），本机输入源 0x%02X，对端输入源 0x%02X"
            % (self.name, self.platform.name, self.my_src, self.peer_src)
        )
        if self.cfg["dry_run"]:
            log("** DRY-RUN 模式：只记录，不会真的切换 **")
        log("UDP 端口 %d，本地控制口 127.0.0.1:%d" % (self.cfg["udp_port"], self.cfg["control_port"]))

        # 先起线程再探测：探测可能耗时（DDC 走 I2C 慢通道），
        # 先把控制口和 UDP 拉起来，避免启动期间完全没有响应。
        for t in (
            threading.Thread(target=self._udp_loop, name="udp", daemon=True),
            threading.Thread(target=self._control_loop, name="ctl", daemon=True),
        ):
            t.start()

        self._refresh_platform_cache()
        log("初始角色：%s（%s）" % (self.role, self.cached_detail))

        try:
            self._main_loop()
        except KeyboardInterrupt:
            log("收到中断，退出")
        finally:
            self.running = False
            self.sock.close()


# --------------------------------------------------------------------------


def load_config(path: str | None) -> dict:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        raise SystemExit("配置文件不存在: %s" % p)
    return json.loads(p.read_text(encoding="utf-8"))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="display-kvm agent")
    ap.add_argument("--config", "-c", help="JSON 配置文件路径")
    ap.add_argument("--status", action="store_true", help="打印一次状态后退出")
    ap.add_argument("--dry-run", action="store_true", help="只记录不执行")
    ap.add_argument("--platform", choices=["win", "mac"], help="强制平台")
    ap.add_argument("--name", help="覆盖本机身份名")
    ap.add_argument("--peer-ip", help="覆盖对端 IP")
    ap.add_argument("--port", help="覆盖本机 UDP 监听端口")
    ap.add_argument("--peer-port", help="覆盖对端 UDP 端口（同机自测用）")
    ap.add_argument("--control-port", help="覆盖本地控制端口")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)

    if args.status:
        plat = platforms.create(cfg, force=args.platform, strict=args.platform is None)
        present = plat.target_display_present()
        print(
            json.dumps(
                {
                    "platform": plat.name,
                    "role": "active" if present else "idle",
                    "detail": plat.detail(),
                    "idle_seconds": plat.idle_seconds(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    Agent(cfg, args).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
