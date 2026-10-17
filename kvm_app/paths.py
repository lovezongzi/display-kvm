"""路径与平台常量。

原先这些散落在 app.py 顶部，导致别的模块想用 CONFIG_PATH 只能反向
`import app`（形成循环依赖）。现在统一在这里算一次，谁需要谁来拿。

路径语义（历史坑，别改坏）：
  * 打包后（frozen）资源在 sys._MEIPASS，app.py 本体不可见；
  * 开发态下项目根目录才是 assets/、bin/、app.py 所在处 —— 也就是本包
    目录的上一级，不是本包目录自己。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "display-kvm"
APP_TITLE = "Display KVM"
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_FROZEN = bool(getattr(sys, "frozen", False))

_PKG_DIR = Path(__file__).resolve().parent
if IS_FROZEN:
    ROOT = Path(getattr(sys, "_MEIPASS", _PKG_DIR))
else:
    ROOT = _PKG_DIR.parent

# 兼容旧名字：原 app.py 里 HERE 指的是项目根目录
HERE = ROOT

# 让同目录的 hotkeys.py / settings.py / agent.py 在开发态可导入。
# （打包后它们都在 PYZ 里，这段 insert 无害。）
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def app_dir() -> Path:
    if IS_WIN:
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif IS_MAC:
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


CONFIG_PATH = app_dir() / "kvm.json"
LOG_PATH = app_dir() / "display-kvm.log"


def resource_path(rel: str) -> Path:
    """兼容 PyInstaller onefile：资源被解压到 sys._MEIPASS。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        p = Path(base) / rel
        if p.exists():
            return p
    return ROOT / rel
