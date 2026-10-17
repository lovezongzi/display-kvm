"""命令行入口：参数解析、单实例检查、托盘/headless 分派。"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
from pathlib import Path

# 先导入 .paths 完成 sys.path 设置，再导入仓库根目录下的平铺模块。
from .paths import APP_NAME, APP_TITLE, CONFIG_PATH

import hotkeys  # noqa: E402

from .autostart import disable_autostart, enable_autostart  # noqa: E402
from .config_store import ensure_config, warn_if_placeholder  # noqa: E402
from .dialogs import make_ask_user, popup  # noqa: E402
from .logsetup import setup_logging  # noqa: E402
from .tray import run_tray  # noqa: E402


def already_running(port: int) -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", int(port)), timeout=0.6)
        s.close()
        return True
    except OSError:
        return False


def build_agent_namespace(args) -> argparse.Namespace:
    return argparse.Namespace(
        config=str(args.config) if args.config else None,
        status=False,
        dry_run=bool(args.dry_run),
        platform=None,
        name=None,
        peer_ip=None,
        port=None,
        peer_port=None,
        control_port=None,
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    setup_logging()

    ap = argparse.ArgumentParser(
        prog=APP_NAME,
        description="display-kvm —— 两台主机共用一台显示器时的输入源自动切换",
    )
    ap.add_argument("--config", "-c", help="配置文件路径（默认 %%APPDATA%%\\display-kvm\\kvm.json）")
    ap.add_argument("--headless", action="store_true", help="不开托盘，纯后台运行")
    ap.add_argument("--dry-run", action="store_true", help="只记录、不真的切换")
    ap.add_argument("--status", action="store_true", help="打印一次状态后退出")
    ap.add_argument("--print-config-path", action="store_true", help="打印配置文件路径后退出")
    ap.add_argument("--show-config", action="store_true", help="打印当前配置后退出")
    ap.add_argument("--install-autostart", action="store_true", help="注册开机自启")
    ap.add_argument("--remove-autostart", action="store_true", help="取消开机自启")
    args, extra = ap.parse_known_args(argv)

    if args.print_config_path:
        print(CONFIG_PATH)
        return 0

    if args.install_autostart:
        ok = enable_autostart()
        print("开机自启：%s" % ("已开启" if ok else "开启失败"))
        return 0 if ok else 1

    if args.remove_autostart:
        ok = disable_autostart()
        print("开机自启：%s" % ("已关闭" if ok else "关闭失败"))
        return 0 if ok else 1

    cfg_path = Path(args.config).expanduser().resolve() if args.config else CONFIG_PATH
    cfg = ensure_config(cfg_path)

    if args.show_config:
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
        return 0

    import agent as agent_mod

    if args.status:
        return agent_mod.main(["--config", str(cfg_path), "--status"])

    warn_if_placeholder(cfg)

    port = int(cfg.get("control_port", 47822))
    if already_running(port):
        msg = "%s 已经在运行了（控制口 127.0.0.1:%d 已被占用）。" % (APP_TITLE, port)
        print(msg)
        if not args.headless:
            popup(msg)
        return 0

    ns = build_agent_namespace(args)
    ns.config = str(cfg_path)
    ag = agent_mod.Agent(cfg, ns)
    # 让 agent 能把自动探测的结果写回配置，并在判断不了的时候弹窗问一句
    ag.cfg_path = str(cfg_path)
    ag.ask_user = make_ask_user()

    if args.headless:
        # Carbon 事件循环必须占主线程，所以把 agent 挪到后台线程跑。
        headless_run(ag, cfg)
        return 0

    return run_tray(ag, cfg)


def headless_run(ag, cfg: dict) -> None:
    """headless 模式：agent 在后台线程，主线程留给 Carbon 事件循环。"""
    threading.Thread(target=ag.run, name="agent", daemon=True).start()
    hk = headless_hotkeys_boot(ag, cfg)
    if hk is not None:
        hk.run_event_loop_if_needed()  # 阻塞，直到进程退出
        return
    while True:
        time.sleep(3600)


def headless_hotkeys_boot(ag, cfg: dict):
    for _ in range(40):
        if getattr(ag, "running", False):
            break
        time.sleep(0.25)
    if not cfg.get("hotkeys_enabled", True):
        return None
    hk = hotkeys.HotkeyManager()

    def fire(to_peer: bool):
        def go():
            try:
                if to_peer:
                    ag.api_switch_peer("快捷键触发")
                else:
                    ag.api_switch("快捷键触发")
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=go, name="hk-fire", daemon=True).start()

    hk.bind(cfg.get("hotkey_to_me"), lambda: fire(False))
    hk.bind(cfg.get("hotkey_to_peer"), lambda: fire(True))
    if hk.count():
        if hk.start():
            print(
                "[快捷键] 已启用: %s"
                % "、".join(str(c) for c, _ in hk.bindings()),
                flush=True,
            )
            return hk
        if hk.error:
            print("[快捷键] %s" % hk.error, flush=True)
    return None


# 兼容旧名（原 app.py 里带下划线）
_headless_run = headless_run
_headless_hotkeys_boot = headless_hotkeys_boot
