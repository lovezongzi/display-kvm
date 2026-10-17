"""配置模型：默认值、数值类型归一化、加载。

配置里的数值字段历史上被设置窗口写成字符串（"3"、"0.45"），而主循环要做
`idle >= cfg[...]` 这种比较，字符串会抛 TypeError 把自动切换拖死。这里在加载
配置时统一归一化，让被污染的配置文件自愈。
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_CONFIG: dict = {
    # 身份
    "name": "windows",
    "platform": "win",  # win | mac
    "peer_name": "mac",
    "peer_ip": "",  # 留空则靠 UDP 广播自动发现
    "token": "change-me",
    "paired": False,  # 是否已通过局域网配对（配对后不再接受新配对，防被劫持）
    "last_token": "",  # 上一次的共享 token。对端请求解除配对时拿它认人：
    # 两端 token 已经错乱时，发起方手里只有旧 token，
    # 只认「当前 token」的话 reset 永远受理不了，死锁就解不开。
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
    # 超过这么久没收到对端的任何消息，就判定对端离线并清掉它的地址。
    # 否则对端下线后托盘会一直显示它最后一次的 IP 和角色（v1.0.0 的行为）。
    "peer_timeout_seconds": 12.0,
    # 自动探测时逐个试的输入源（十进制）。15/16/17/18 是 m1ddc 文档里的
    # DisplayPort 1/2、HDMI 1/2，27 是 USB-C。
    "input_candidates": [15, 16, 17, 18, 27],
    # 靠对端判断"现在显示的是谁"时，给它多久上报角色
    "peer_settle_seconds": 6.0,
    # 全局快捷键。"" = 不绑；写成 "ctrl+alt+1" 这种（见 hotkeys.py）。
    # Mac 上首次使用需要在 系统设置 → 隐私与安全性 → 辅助功能 里勾本程序。
    "hotkeys_enabled": True,
    "hotkey_to_me": "ctrl+alt+1",
    "hotkey_to_peer": "ctrl+alt+2",
}

# 会被拿去做数值比较/运算的字段。v1.0.8 之前，设置窗口保存时把**所有**
# 非布尔字段一律 str() 写回配置（"3"、"0.45"），于是这些值变成字符串；
# 主循环每轮执行 `idle >= cfg["arm_after_idle_seconds"]` 时就会抛
#   TypeError: '>=' not supported between instances of 'float' and 'str'
# 被 _main_loop 的 except 吞掉后只留一行日志 —— 结果是**自动切换完全失效**，
# 且日志每秒被刷两行（实测 30 分钟刷了 7000+ 行）。
# 这里在加载配置时就统一归一化，让已被污染的配置文件自愈。
FLOAT_KEYS = (
    "role_poll_seconds",
    "arm_after_idle_seconds",
    "fire_below_idle_seconds",
    "cooldown_seconds",
    "hello_interval_seconds",
    "peer_timeout_seconds",
    "peer_settle_seconds",
)

INT_KEYS = (
    "tick_ms",
    "udp_port",
    "peer_udp_port",
    "control_port",
    "display_index",
    "match_width",
    "match_height",
)


def _to_float(v, default: float) -> float:
    """把配置值转成 float。转换失败（脏数据/空串）时退回默认值。"""
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


def _to_int(v, default: int) -> int:
    if isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return default


def normalize_config(cfg: dict) -> dict:
    """就地修复配置里的数值字段类型，返回同一 dict。"""
    for k in FLOAT_KEYS:
        if k in cfg and not isinstance(cfg[k], float):
            cfg[k] = _to_float(cfg[k], float(DEFAULT_CONFIG.get(k, 0.0)))
    for k in INT_KEYS:
        if k in cfg and not isinstance(cfg[k], int):
            cfg[k] = _to_int(cfg[k], int(DEFAULT_CONFIG.get(k, 0)))
    return cfg


def _src(v) -> int:
    if isinstance(v, int):
        return v
    return int(str(v), 0)


def load_config(path: str | None) -> dict:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        raise SystemExit("配置文件不存在: %s" % p)
    return json.loads(p.read_text(encoding="utf-8"))
