#!/usr/bin/env python3
"""display-kvm —— 跨机显示器输入源自动切换 Agent。

本文件只做三件事：
  1. Agent.__init__：建 socket、集中放所有共享字段、创建平台实现；
  2. 对外 facade：api_status / api_switch / api_switch_peer / update_cfg / run；
  3. 命令行入口：main / load_config。

真正的领域逻辑按职责拆在 kvm_core/ 下（transport / pairing / sync /
discovery / switch / control / probe），由 Agent 通过多继承组合。这样每个
文件只装一类关注点，改一处不必在一千多行的巨石里翻。

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
import secrets
import socket
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kvm_platform import create  # noqa: E402
from kvm_core.common import log  # noqa: E402
from kvm_core.config import (  # noqa: E402
    DEFAULT_CONFIG,
    FLOAT_KEYS,
    INT_KEYS,
    _src,
    _to_float,
    _to_int,
    load_config,
    normalize_config,
)
from kvm_core.control import ControlMixin  # noqa: E402
from kvm_core.discovery import DiscoveryMixin  # noqa: E402
from kvm_core.pairing import PairingMixin  # noqa: E402
from kvm_core.probe import ProbeMixin  # noqa: E402
from kvm_core.switch import SwitchMixin  # noqa: E402
from kvm_core.sync import SyncMixin  # noqa: E402
from kvm_core.transport import TransportMixin  # noqa: E402

# 这几个名字原来定义在本文件、被 selftest/test_config.py 直接 import 使用；
# 这里重新导出，保持 `from agent import DEFAULT_CONFIG` 这类旧写法继续可用。
__all__ = [
    "Agent",
    "main",
    "load_config",
    "log",
    "DEFAULT_CONFIG",
    "FLOAT_KEYS",
    "INT_KEYS",
    "normalize_config",
    "_to_float",
    "_to_int",
    "_src",
]


class Agent(
    TransportMixin,
    PairingMixin,
    SyncMixin,
    DiscoveryMixin,
    SwitchMixin,
    ControlMixin,
    ProbeMixin,
):
    def __init__(self, raw_cfg: dict, args: argparse.Namespace):
        cfg = {**DEFAULT_CONFIG, **raw_cfg}
        # 修掉历史版本写坏的类型（数值被存成字符串），否则自动切换会持续抛错。
        normalize_config(cfg)
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
        self.paired = bool(cfg.get("paired", False))
        self._offered_nonce = None  # 发起配对时本机的临时 nonce（收到对应 accept 才清）
        self._offered_token = None  # 发起配对时生成的共享 token
        self._session_nonce = secrets.token_hex(4)  # 本次运行的 announce 标识
        self.pair_candidates = {}  # ip -> 对端名（未配对候选，供托盘显示）
        # token 不一致的观测状态：已配对却持续收到校验不过的报文，说明两端
        # token 已经错乱。这类状态过去只能靠用户去「出错的那一端」手动解除。
        self._mismatch_window_start = 0.0
        self._mismatch_count: dict[str, int] = {}
        self._mismatch_log_at: dict[str, float] = {}
        self.token_mismatch_with = ""  # 最近检测到不一致的对端 IP，"" = 无
        # 最近一次配对的结果说明（失败原因）。Windows 上系统通知经常被静默抑制，
        # 托盘菜单状态行是唯一一定看得见的反馈，所以失败原因要放在这里。
        self.pair_error = None
        self.on_paired = None       # (ok: bool, peer: str, msg: str) -> None
        self.on_discovered = None   # (name: str, ip: str) -> None
        self.on_auto_sync = None    # (on: bool) -> None，对端把自动切换同步过来时回调
        self.my_src = _src(cfg["my_input_source"])
        self.peer_src = _src(cfg["peer_input_source"])

        self.platform = create(cfg, force=args.platform, strict=args.platform is None)

        self.role = "unknown"  # active | idle
        self.armed = False
        self.peer_ip = cfg.get("peer_ip") or ""
        self.peer_role = "unknown"
        self.peer_last_seen = 0.0  # 收到对端消息的时间戳，0 = 从没收到过
        self.peer_online = False
        self.last_switch_ts = 0.0

        # 由外壳（app.py）注入：配置落盘路径 + 需要人工确认时的提问回调
        self.cfg_path: str | None = None
        self.ask_user = None
        # 探测输入源期间置 True：此时画面会来回切，必须让自动切换闭嘴，
        # 否则它看到"角色变了 + 本机有活动"就会把画面抢回去，和探测打架。
        self.probing = False
        self.auto_on = bool(cfg["auto_switch"])
        self.running = True

        # 平台状态缓存：只在主循环里更新，其他线程只读
        self.cached_detail = ""
        self.cached_idle: float | None = None
        # 上一拍的空闲值。空闲计时器只会被输入事件清零，所以「这一拍比上一拍
        # 明显更小」= 这期间发生过键鼠活动 —— 用它做触发判据，比要求采样点
        # 恰好落在活动后的 0.45 秒窗口里可靠得多。
        self._last_idle: float | None = None
        self._next_idle_at = 0.0

        # 任务队列：UDP / 控制口线程把活丢进来，主循环执行
        self.ops: queue.Queue = queue.Queue()
        self._plat_lock = threading.Lock()

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.bind(("", int(cfg["udp_port"])))

    # ------------------------------------------------- 对外接口（托盘/外壳用）

    def api_status(self) -> dict:
        """读当前状态缓存。线程安全：只读主循环维护的字段。"""
        return self._status()

    def update_cfg(self, pairs: dict) -> None:
        """从设置面板改配置。

        必须走这里而不是直接改托盘那份 dict：Agent.__init__ 里做的是
        cfg = {**DEFAULT_CONFIG, **raw_cfg}，跟外壳拿到的不是同一个对象，
        改外壳的不会影响主循环（主循环每 tick 读的是 self.cfg）。
        """
        self.cfg.update(pairs)
        if "auto_switch" in pairs:
            # set_auto_switch 负责写内存 + 落盘 + 广播给对端（避免本端改了、
            # 对端还按旧值抢屏）。其余字段保持原处理逻辑。
            self.set_auto_switch(bool(pairs["auto_switch"]))
        if "my_input_source" in pairs:
            self.my_src = _src(pairs["my_input_source"])
            if hasattr(self.platform, "my_src"):
                self.platform.my_src = self.my_src
        if "peer_input_source" in pairs:
            self.peer_src = _src(pairs["peer_input_source"])
            if hasattr(self.platform, "peer_src"):
                self.platform.peer_src = self.peer_src
        log("配置已更新：%s" % "、".join("%s=%s" % (k, v) for k, v in pairs.items()))

    def api_switch(self, reason: str = "手动触发") -> str:
        """请求切换，线程安全：走任务队列交给主循环执行。"""
        ev = threading.Event()
        op = {"kind": "manual_switch", "reason": reason, "event": ev}
        self.ops.put(op)
        ev.wait(6.0)
        return op.get("result", "pending")

    def api_switch_peer(self, reason: str = "手动触发") -> str:
        """请求把显示器切到对端，线程安全：走任务队列交给主循环执行。"""
        ev = threading.Event()
        op = {"kind": "manual_switch_peer", "reason": reason, "event": ev}
        self.ops.put(op)
        ev.wait(6.0)
        return op.get("result", "pending")

    def _save_cfg(self) -> None:
        """把改动后的配置写回磁盘（只有外壳给了 cfg_path 才写得成）。"""
        if not self.cfg_path:
            return
        try:
            p = Path(self.cfg_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log("配置写回失败: %s" % exc)

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
        plat = create(cfg, force=args.platform, strict=args.platform is None)
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
