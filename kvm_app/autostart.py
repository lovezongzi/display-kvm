"""开机自启：Windows 走注册表 Run 键，macOS 走 LaunchAgent plist。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .paths import APP_NAME, HERE, IS_FROZEN, IS_MAC, IS_WIN, LOG_PATH

_WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_MAC_PLIST = Path.home() / "Library" / "LaunchAgents" / "cn.displaykvm.agent.plist"

_MAC_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>cn.displaykvm.agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>{exe}</string>
        <string>--headless</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>{log}</string>
    <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


def _autostart_command() -> str:
    if IS_FROZEN:
        return '"%s"' % sys.executable
    return '"%s" "%s"' % (sys.executable, (HERE / "app.py").resolve())


def is_autostart_enabled() -> bool:
    if IS_WIN:
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY) as k:
                winreg.QueryValueEx(k, APP_NAME)
                return True
        except OSError:
            return False
    if IS_MAC:
        return _MAC_PLIST.exists()
    return False


def enable_autostart() -> bool:
    try:
        if IS_WIN:
            import winreg

            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE
            ) as k:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _autostart_command())
            return True
        if IS_MAC:
            exe = sys.executable if IS_FROZEN else str((HERE / "app.py").resolve())
            _MAC_PLIST.parent.mkdir(parents=True, exist_ok=True)
            _MAC_PLIST.write_text(
                _MAC_PLIST_TEMPLATE.format(exe=exe, log=LOG_PATH), encoding="utf-8"
            )
            subprocess.run(["launchctl", "load", str(_MAC_PLIST)], capture_output=True)
            return True
    except Exception as exc:  # noqa: BLE001
        print("开启开机自启失败: %s" % exc)
    return False


def disable_autostart() -> bool:
    try:
        if IS_WIN:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE
            ) as k:
                winreg.DeleteValue(k, APP_NAME)
            return True
        if IS_MAC:
            if _MAC_PLIST.exists():
                subprocess.run(["launchctl", "unload", str(_MAC_PLIST)], capture_output=True)
                _MAC_PLIST.unlink()
            return True
    except FileNotFoundError:
        return True
    except Exception as exc:  # noqa: BLE001
        print("关闭开机自启失败: %s" % exc)
    return False
