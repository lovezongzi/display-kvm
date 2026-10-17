#!/usr/bin/env python3
"""自动切换网络同步回归测试（v1.0.12 新增 config_sync），pytest 化。

验证：
  1. A 端改自动切换 -> B 端镜像，且 B 立刻落盘
  2. B 端改自动切换 -> A 端镜像（双向）
  3. 接收端只镜像、不回播（靠「值相同即跳过」截断，不会无限互发）
  4. 未配对（token 不一致）的报文被丢弃，不会误同步

全程只跑 UDP 线程，不碰真实显示器（platform=mac、dry_run=True）。
手动跑：python selftest/test_auto_sync.py
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
import pytest  # noqa: E402

PORT_A = 47931
PORT_B = 47933


def _make_agent(name: str, udp_port: int, peer_udp_port: int, token: str) -> "agent_mod.Agent":
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


@pytest.fixture(scope="module")
def pair():
    a = _make_agent("win-a", PORT_A, PORT_B, "shared-token-0001")
    b = _make_agent("mac-b", PORT_B, PORT_A, "shared-token-0001")
    time.sleep(0.3)
    yield a, b
    a.running = b.running = False


def test_a_on_mirrors_to_b_and_persists(pair):
    a, b = pair
    a.set_auto_switch(True)
    time.sleep(0.6)
    assert b.auto_on is True, "B 应镜像为开"
    assert '"auto_switch": true' in open(b.cfg_path).read(), "B 应落盘为 true"


def test_b_off_mirrors_to_a(pair):
    a, b = pair
    b.set_auto_switch(False)
    time.sleep(0.6)
    assert a.auto_on is False, "A 应镜像为关"
    assert '"auto_switch": false' in open(a.cfg_path).read(), "A 应落盘为 false"


def test_same_value_no_echo(pair):
    a, b = pair
    a.set_auto_switch(True)
    time.sleep(0.6)
    b.set_auto_switch(True)  # 值相同，_on_config_sync 应直接 return
    time.sleep(0.6)
    assert a.auto_on is True and b.auto_on is True, "两端应一致为开"


def test_wrong_token_ignored(pair):
    a, b = pair
    a.token = "wrong-token"
    a.cfg["token"] = "wrong-token"
    before = b.auto_on
    a.set_auto_switch(False)
    time.sleep(0.6)
    assert b.auto_on == before, "错误 token 的报文不应改变 B"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
