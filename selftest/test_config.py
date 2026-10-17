#!/usr/bin/env python3
"""配置模型纯函数单测（无网络、无显示器、秒级完成）。

阶段 0 测试安全网的一部分：把 _to_float / _to_int / normalize_config /
DEFAULT_CONFIG 这些「一旦写错就会让自动切换彻底失效」的底层逻辑圈进回归。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import (  # noqa: E402
    DEFAULT_CONFIG,
    _to_float,
    _to_int,
    normalize_config,
)


def test_to_float_returns_float_or_default():
    assert _to_float("3.5", 1.0) == 3.5
    assert _to_float(3, 1.0) == 3.0
    assert _to_float("garbage", 2.0) == 2.0
    assert _to_float(None, 2.0) == 2.0
    assert _to_float("", 2.0) == 2.0


def test_to_int_returns_int_or_default():
    assert _to_int("5", 0) == 5
    assert _to_int(5.0, 0) == 5
    assert _to_int("x", 9) == 9
    assert _to_int(None, 9) == 9


def test_normalize_config_fixes_string_numbers():
    dirty = {
        "arm_after_idle_seconds": "3.0",
        "fire_below_idle_seconds": "0.45",
        "role_poll_seconds": "2",      # FLOAT_KEYS -> float
        "tick_ms": "250",             # INT_KEYS -> int
        "auto_switch": True,
    }
    out = normalize_config(dict(dirty))
    assert isinstance(out["arm_after_idle_seconds"], float)
    assert isinstance(out["fire_below_idle_seconds"], float)
    assert isinstance(out["role_poll_seconds"], float)
    assert out["arm_after_idle_seconds"] == 3.0
    assert out["role_poll_seconds"] == 2.0
    # INT_KEYS 必须归一成 int，否则 sleep/range 等场景会出 TypeError
    assert isinstance(out["tick_ms"], int)
    assert out["tick_ms"] == 250


def test_normalize_config_keeps_valid_types():
    clean = {
        "arm_after_idle_seconds": 3.0,
        "role_poll_seconds": 2,
        "auto_switch": False,
    }
    out = normalize_config(dict(clean))
    assert out["arm_after_idle_seconds"] == 3.0
    assert out["role_poll_seconds"] == 2
    assert out["auto_switch"] is False


def test_default_config_has_required_keys():
    for k in (
        "udp_port", "token", "my_input_source", "peer_input_source",
        "auto_switch", "arm_after_idle_seconds", "fire_below_idle_seconds",
    ):
        assert k in DEFAULT_CONFIG, "DEFAULT_CONFIG 缺少 %s" % k
