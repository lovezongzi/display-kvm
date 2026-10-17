"""display-kvm 应用层（托盘外壳）。

这里只放「怎么把 Agent 装进一个常驻 GUI 程序」相关的东西，不碰切换协议与
DDC 细节（那些在 kvm_core/ 与 kvm_platform/）。包名用 kvm_app 而不是 app：
仓库根目录还留着 app.py 作为打包入口，同名包会把它遮住。
"""

from . import paths  # noqa: F401  必须最先导入：它负责把项目根塞进 sys.path
from .autostart import (
    disable_autostart,
    enable_autostart,
    is_autostart_enabled,
)
from .config_store import (
    README_LINES,
    autodetect,
    build_default_config,
    ensure_config,
    save_cfg_fields,
    warn_if_placeholder,
)
from .dialogs import (
    ask_choice,
    ask_text,
    ask_yes_no,
    make_ask_user,
    open_in_system,
    popup,
    popup_once,
)
from .entry import (
    already_running,
    build_agent_namespace,
    headless_hotkeys_boot,
    headless_run,
    main,
)
from .logsetup import setup_logging
from .paths import (
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
from .tray import ROLE_COLOR, ROLE_NEUTRAL, TrayApp, run_tray, tray_image

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
