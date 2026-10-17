#!/usr/bin/env python3
"""配对死锁回归测试（v1.0.10），pytest 化。

复现 v1.0.7 遗留的死局：两端 token 不一致，且都卡在 paired=True。
此时任何 pairing_offer 都会被「已配对则忽略」拒掉，唯一出路是跑到
「出错的那一端」手动点托盘 —— 而那一端往往正是看不见问题的那台机器。

验证 v1.0.10 的两条出路：
    1. pairing_reset（带外恢复）：一端主动解除时，能请对端一并松口
    2. token 不匹配自愈：持续校验失败后自动降级，不需要人介入
最后验证解除之后能重新配对成功，不会停在「两端都未配对」的半途。

手动跑：python selftest/test_pairing.py
全程只跑 UDP 线程，不碰真实显示器（强制 platform=mac，且不做任何 DDC 调用）。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent as agent_mod  # noqa: E402
import pytest  # noqa: E402

PORT_A = 47921
PORT_B = 47923


def _make_agent(name: str, udp_port: int, peer_udp_port: int, token: str) -> "agent_mod.Agent":
    cfg = {
        "name": name,
        "platform": "mac",
        "peer_name": "peer",
        "peer_ip": "127.0.0.1",
        "token": token,
        "paired": False,
        "udp_port": udp_port,
        "peer_udp_port": peer_udp_port,
        "my_input_source": "0x10",
        "peer_input_source": "0x0F",
        "auto_switch": False,
        "dry_run": True,
        "tick_ms": 250,
    }
    args = argparse.Namespace(
        dry_run=True, port=None, peer_port=None,
        control_port=None, name=None, peer_ip=None, platform="mac",
    )
    a = agent_mod.Agent(cfg, args)
    threading.Thread(target=a._udp_loop, name="udp-" + name, daemon=True).start()
    return a


def _setup_deadlock(a: "agent_mod.Agent", b: "agent_mod.Agent") -> None:
    """把两端摆成 v1.0.7 的死局：token 不同，却都认为自己已配对。"""
    a.token, a.cfg["token"] = "tokA" + "0" * 12, "tokA" + "0" * 12
    b.token, b.cfg["token"] = "tokB" + "0" * 12, "tokB" + "0" * 12
    a.cfg["last_token"] = b.token
    b.cfg["last_token"] = a.token
    a.paired = True
    b.paired = True


@pytest.fixture(scope="module")
def pair():
    a = _make_agent("win-a", PORT_A, PORT_B, "tokA")
    b = _make_agent("mac-b", PORT_B, PORT_A, "tokB")
    time.sleep(0.3)
    yield a, b
    a.running = b.running = False


def test_deadlock_reproducible(pair):
    a, b = pair
    _setup_deadlock(a, b)
    b.start_pairing()
    time.sleep(1.2)
    assert a.paired is True, "对端已配对时应拒绝新 offer（死锁可复现）"


def test_out_of_band_reset(pair):
    a, b = pair
    _setup_deadlock(a, b)
    b.unpair(reset_peer=True)
    time.sleep(0.8)
    assert b.paired is False, "发起方自己解除"
    assert a.paired is False, "对端收到 reset 后也解除（不再需要去那台机器上点托盘）"


def test_fake_token_reset_ignored(pair):
    a, b = pair
    _setup_deadlock(a, b)
    a._send_raw({"t": "pairing_reset", "from": "attacker", "token": "deadbeefdeadbeef"}, "127.0.0.1")
    b._send_raw({"t": "pairing_reset", "from": "attacker", "token": "deadbeefdeadbeef"}, "127.0.0.1")
    time.sleep(0.5)
    assert a.paired is True and b.paired is True, "伪造 token 的 reset 被忽略"


def test_self_heal_on_mismatch(pair):
    a, b = pair
    _setup_deadlock(a, b)
    n = agent_mod.Agent.MISMATCH_TRIGGER_COUNT
    for _ in range(n + 2):
        b._send({"t": "hello", "role": "idle", "platform": "mac"}, "127.0.0.1")
        time.sleep(0.03)
    time.sleep(0.8)
    assert a.paired is False, "本端自动降级为未配对（阈值 %d 次）" % n
    assert b.paired is False, "并已请对端一并降级"


def test_no_false_unpair_when_peer_offline(pair):
    a, b = pair
    _setup_deadlock(a, b)
    a._mismatch_count.clear()
    time.sleep(1.5)  # 什么报文都不发
    assert a.paired is True, "无报文时保持已配对"


def test_repair_after_unpair(pair):
    a, b = pair
    a.paired = b.paired = False
    a._offered_nonce = b._offered_nonce = None
    b.start_pairing()
    time.sleep(2.0)
    assert a.token == b.token, "两端 token 一致"
    assert a.paired and b.paired, "两端都标记为已配对"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
