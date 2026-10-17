"""display-kvm 托盘外壳 —— 应用层门面。

真正的实现在 kvm_app/ 包里，这里只做两件事：
  1. 保持 `import app` / `python app.py` 的旧入口不变（PyInstaller spec 指向本文件）；
  2. 把历史上从 app 命名空间里拿名字的调用方（含私有名）重新导出。

模块划分：
    kvm_app/paths.py         路径与平台常量（CONFIG_PATH / LOG_PATH / resource_path）
    kvm_app/logsetup.py      日志重定向与滚动
    kvm_app/config_store.py  配置模板 / 首次探测 / 读写
    kvm_app/autostart.py     开机自启（注册表 / LaunchAgent）
    kvm_app/dialogs.py       系统对话框、外部打开
    kvm_app/tray.py          托盘界面（TrayApp）
    kvm_app/entry.py         命令行入口与进程分派
"""

from __future__ import annotations

import sys

from kvm_app import paths as _paths
from kvm_app.autostart import (
    _autostart_command,
    disable_autostart,
    enable_autostart,
    is_autostart_enabled,
)
from kvm_app.config_store import (
    README_LINES,
    _save_cfg_fields,
    _warn_if_placeholder,
    autodetect,
    build_default_config,
    ensure_config,
    save_cfg_fields,
    warn_if_placeholder,
)
from kvm_app.dialogs import (
    ask_choice,
    ask_text,
    ask_yes_no,
    make_ask_user,
    open_in_system,
    popup,
    popup_once,
)
from kvm_app.entry import (
    _headless_hotkeys_boot,
    _headless_run,
    already_running,
    build_agent_namespace,
    headless_hotkeys_boot,
    headless_run,
    main,
)
from kvm_app.logsetup import _RotatingFile, _Tee, setup_logging
from kvm_app.paths import (
    APP_NAME,
    APP_TITLE,
    CONFIG_PATH,
    HERE,
    IS_FROZEN,
    IS_MAC,
    IS_WIN,
    LOG_PATH,
    ROOT,
    app_dir,
    resource_path,
)
from kvm_app.tray import ROLE_COLOR, ROLE_NEUTRAL, TrayApp, run_tray, tray_image

# ---------------------------------------------------------------- 兼容旧名
# 原 app.py 里的私有函数被改名去掉了下划线，这里补回旧名字，
# 保证任何按旧名调用的外部脚本不会断。
_popup = popup
_ask_yes_no = ask_yes_no
_ask_choice = ask_choice
_ask_text = ask_text
_popup_once = popup_once
_tray_image = tray_image


def _notify(icon, title: str, msg: str) -> None:
    """旧签名版本的通知包装（原 app.py 的模块级 _notify）。"""
    try:
        icon.notify(msg, title)
    except Exception:  # noqa: BLE001
        print("[notify] %s: %s" % (title, msg))


__all__ = [
    "APP_NAME",
    "APP_TITLE",
    "CONFIG_PATH",
    "HERE",
    "IS_FROZEN",
    "IS_MAC",
    "IS_WIN",
    "LOG_PATH",
    "README_LINES",
    "ROLE_COLOR",
    "ROLE_NEUTRAL",
    "ROOT",
    "TrayApp",
    "already_running",
    "app_dir",
    "ask_choice",
    "ask_text",
    "ask_yes_no",
    "autodetect",
    "build_agent_namespace",
    "build_default_config",
    "disable_autostart",
    "enable_autostart",
    "ensure_config",
    "headless_hotkeys_boot",
    "headless_run",
    "is_autostart_enabled",
    "main",
    "make_ask_user",
    "open_in_system",
    "popup",
    "popup_once",
    "resource_path",
    "run_tray",
    "save_cfg_fields",
    "setup_logging",
    "tray_image",
    "warn_if_placeholder",
]


if __name__ == "__main__":
    raise SystemExit(main())
