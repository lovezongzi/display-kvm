#!/usr/bin/env python3
"""自动切换网络同步回归测试（v1.0.12 新增 config_sync）。

验证：
  1. A 端改自动切换 -> B 端镜像，且 B 立刻落盘
  2. B 端改自动切换 -> A 端镜像（双向）
  3. 接收端只镜像、不回播（靠「值相同即跳过」截断，不会无限互发）
  4. 未配对（token 不一致）的报文被丢弃，不会误同步

全程只跑 UDP 线程，不碰真实显示器（platform=mac、dry_run=True）。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent as agent_mod  # noqa: E402

PORT_A = 47931
PORT_B = 47933

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print("  %s %s%s" % ("PASS" if ok else "FAIL", name, ("  —— " + detail) if detail else ""))


def make_agent(name: str, udp_port: int, peer_udp_port: int, token: str) -> "agent_mod.Agent":
    cfg = {
        "name": name,
        "platform": "mac",
        "peer_name": "peer",
        "peer_ip": "127.0.0.1",
        "token": token,
        "paired": True,
        "udp_port": udp_port,
        "peer_udp_port": peer_udp_port,
        "my_input_source": "0x10",
        "peer_input_source": "0x0F",
        "auto_switch": False,
        "dry_run": True,
        "tick_ms": 250,
    }
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    tmp.write('{"token": "%s", "auto_switch": false}' % token)
    tmp.close()
    args = argparse.Namespace(
        dry_run=True, port=None, peer_port=None, control_port=None,
        name=None, peer_ip=None, platform="mac",
    )
    a = agent_mod.Agent(cfg, args)
    a.cfg_path = tmp.name  # 让 _save_cfg 能落盘，验证同步值被写回
    threading.Thread(target=a._udp_loop, name="udp-" + name, daemon=True).start()
    return a


def main() -> int:
    print("=" * 60)
    print("自动切换网络同步测试（v1.0.12）")
    print("=" * 60)

    a = make_agent("win-a", PORT_A, PORT_B, "shared-token-0001")
    b = make_agent("mac-b", PORT_B, PORT_A, "shared-token-0001")
    time.sleep(0.3)

    # ---------------------------------------------------------------- 1
    print("\n[1] A 开 -> B 应镜像为开，并落盘")
    a.set_auto_switch(True)
    time.sleep(0.6)
    check("B.auto_on 变为 True", b.auto_on is True)
    check("B 配置写回磁盘为 true", '"auto_switch": true' in open(b.cfg_path).read())

    # ---------------------------------------------------------------- 2
    print("\n[2] B 关 -> A 应镜像为关（双向）")
    b.set_auto_switch(False)
    time.sleep(0.6)
    check("A.auto_on 变为 False", a.auto_on is False)
    check("A 配置写回磁盘为 false", '"auto_switch": false' in open(a.cfg_path).read())

    # ---------------------------------------------------------------- 3
    print("\n[3] 值相同不触发回声：重复下发同一值不应有副作用/应快速稳定")
    a.set_auto_switch(True)        # 广播 True
    time.sleep(0.6)
    b.set_auto_switch(True)        # B 本地也置 True（值相同，_on_config_sync 应直接 return）
    time.sleep(0.6)
    check("两端最终一致=开且无异常", a.auto_on is True and b.auto_on is True)

    # ---------------------------------------------------------------- 4
    print("\n[4] 未配对（token 不一致）的同步报文被丢弃")
    a.token = "wrong-token"
    a.cfg["token"] = "wrong-token"
    safe_b = b.auto_on
    a.set_auto_switch(False)       # 用错误 token 广播
    time.sleep(0.6)
    check("B 未被错误 token 的报文改变", b.auto_on == safe_b,
          "B.auto_on=%s (期望维持 %s)" % (b.auto_on, safe_b))

    a.running = b.running = False

    print("\n" + "=" * 60)
    bad = [r for r in results if not r[1]]
    print("共 %d 项，通过 %d，失败 %d" % (len(results), len(results) - len(bad), len(bad)))
    for name, _, detail in bad:
        print("  失败：%s  %s" % (name, detail))
    print("=" * 60)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
