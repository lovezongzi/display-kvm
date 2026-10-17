"""配置文件：默认值、首次探测、读写。

这里曾是「两份真相源」之一（另一份是 kvm_core/config.py 的 DEFAULT_CONFIG）。
现在两边仍各自持有默认值，但职责已分开：
  * kvm_core/config.py   运行期模型（类型归一化、load_config）
  * kvm_app/config_store.py  首次生成的模板（含平台相关字段、_readme 注释）
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from pathlib import Path

from .paths import CONFIG_PATH, IS_MAC, IS_WIN, resource_path

README_LINES = [
    "display-kvm 配置（JSON 不支持注释，故用 _readme 字段说明）",
    "1) token 必须与对端完全一致，否则两端互相忽略消息。",
    "2) my_input_source  = 本机接在这台显示器上的输入口。",
    "3) peer_input_source = 对端接在这台显示器上的输入口。",
    "4) dry_run=true 时只写日志、不真的切换，确认逻辑无误后改 false。",
    "5) 改完配置要重启程序（托盘菜单 -> 退出，再打开）。",
    "6) macOS：m1ddc 读到的『当前输入源』只表示显示器此刻在显示谁。要在"
    "显示器正显示本机时读，得到的才是本机的口；否则会读到对端的口。",
    "7) macOS：Mac 端可以自己直接把显示器切回来（DDC 通道随时可用），"
    "     不依赖 Windows 端在线。",
]


def autodetect(cfg: dict) -> dict:
    """首次生成配置时探测本机，把共用屏的分辨率和当前输入源预填进去。"""
    if IS_WIN:
        try:
            import ddc_win

            ds = ddc_win.list_displays(with_capabilities=False)
            if ds:
                best = max(ds, key=lambda d: (d.width or 0) * (d.height or 0))
                if best.width and best.height:
                    cfg["match_width"] = best.width
                    cfg["match_height"] = best.height
                if best.current_input is not None:
                    cfg["my_input_source"] = "0x%02X" % best.current_input
        except Exception as exc:  # noqa: BLE001
            print("自动探测显示器失败（保持默认值）: %s" % exc)
    elif IS_MAC:
        # Apple Silicon 上用 m1ddc 读到的"当前输入源"= 显示器此刻正在显示谁的口。
        # 如果首次启动时显示器正显示对端，读到的就是对端的口；直接写进
        # my_input_source 会把方向整个写反（v1.0.0 踩的就是这个坑：
        # 面板上点"切到本机"反而切到了 Windows）。
        # 所以这里只探测、只提示，不覆盖模板/用户的值。
        try:
            import re

            idx = cfg.get("display_index", 1)
            p = subprocess.run(
                ["m1ddc", "display", str(idx), "get", "input"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            if p.returncode == 0:
                m = re.search(r"\b(\d+)\b", p.stdout or "")
                if m:
                    print(
                        "检测到共享显示器当前输入源 0x%02X —— 注意这只表示"
                        "「此刻显示器在显示谁」，不一定是本机的口。"
                        "请对照 my_input_source 自行核对（已保持模板值不变）。"
                        % int(m.group(1))
                    )
        except Exception:  # noqa: BLE001
            pass
    return cfg


def build_default_config() -> dict:
    cfg = {
        "name": "windows" if IS_WIN else "mac",
        "platform": "win" if IS_WIN else ("mac" if IS_MAC else "win"),
        "peer_name": "mac" if IS_WIN else "windows",
        "peer_ip": "",
        "token": secrets.token_hex(8),
        "paired": False,
        "udp_port": 47821,
        "peer_udp_port": 0,
        "control_port": 47822,
        "my_input_source": "0x10",
        "peer_input_source": "0x12",
        "auto_switch": True,
        "dry_run": True,
        "tick_ms": 250,
        "role_poll_seconds": 2.0,
        "arm_after_idle_seconds": 3.0,
        "fire_below_idle_seconds": 0.45,
        "cooldown_seconds": 6.0,
        "hello_interval_seconds": 3.0,
    }
    if IS_WIN:
        cfg.update({"match_width": 2560, "match_height": 1440})
    else:
        # 优先用打包进 app 的 m1ddc（launchd 启动时 PATH 里没有 /opt/homebrew/bin），
        # 没有打包的就回退到 PATH 里的 m1ddc（Apple Silicon）。
        m1 = resource_path("bin/m1ddc")
        ddc = ('"%s"' % m1) if m1.exists() and os.access(str(m1), os.X_OK) else "m1ddc"
        cfg.update(
            {
                "display_index": 1,
                "ddc_get_cmd": ddc + " display {index} get input",
                "ddc_set_cmd": ddc + " display {index} set input {value}",
            }
        )
    return autodetect(cfg)


def ensure_config(path: Path) -> dict:
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(cfg, dict):
                return cfg
            print("配置文件不是 JSON 对象，忽略: %s" % path)
        except Exception as exc:  # noqa: BLE001
            print("配置文件解析失败：%s（%s）" % (path, exc))
        return {}

    cfg = build_default_config()
    cfg["_readme"] = README_LINES
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已生成默认配置：%s" % path)
    return cfg


def save_cfg_fields(pairs: dict) -> bool:
    """把若干字段写回 kvm.json（保留其它字段原样）。"""
    try:
        c = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
        c.update(pairs)
        CONFIG_PATH.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception as exc:  # noqa: BLE001
        print("[配置] 写回失败: %s" % exc, flush=True)
        return False


# 兼容旧名（原 app.py 里是私有的 _save_cfg_fields）
_save_cfg_fields = save_cfg_fields


def warn_if_placeholder(cfg: dict) -> None:
    token = str(cfg.get("token", ""))
    if not token or "改" in token or "保持" in token or len(token) < 6:
        print("!! token 还是占位符，两端将互相忽略消息。请编辑配置文件设置一致的随机串。")


_warn_if_placeholder = warn_if_placeholder
