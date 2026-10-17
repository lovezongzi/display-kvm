"""系统对话框与外部打开。

Windows 走 ctypes/MessageBox 与 PowerShell，macOS 走 osascript。
这些调用都可能阻塞几秒（等用户点按钮），所以只允许从主线程/UI 线程调用；
agent 那边如果要从后台线程提问，由调用方自己调度。
"""

from __future__ import annotations

import os
import subprocess
import threading

from .paths import APP_TITLE, IS_MAC, IS_WIN


def open_in_system(path) -> None:
    try:
        if IS_WIN:
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif IS_MAC:
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:  # noqa: BLE001
        print("打开失败 %s: %s" % (path, exc))


def popup(msg: str) -> None:
    try:
        if IS_WIN:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, msg, APP_TITLE, 0x40)
        elif IS_MAC:
            safe = msg.replace('"', "'")
            subprocess.Popen(
                ["osascript", "-e", 'display dialog "%s" buttons {"好"} default button 1' % safe]
            )
    except Exception:  # noqa: BLE001
        pass


def ask_yes_no(msg: str, yes: str = "是", no: str = "不是") -> bool:
    """弹一个二选一的对话框，返回用户是不是点了「是」。

    popup 只会弹通知、拿不到结果；自动探测在无法自己判断"哪个口是本机"
    的时候，需要真的问一句。
    """
    try:
        if IS_WIN:
            import ctypes

            # MB_YESNO = 0x04，IDYES = 6
            return ctypes.windll.user32.MessageBoxW(None, msg, APP_TITLE, 0x04) == 6
        if IS_MAC:
            safe = msg.replace('"', "'").replace("\n", " ")
            p = subprocess.run(
                [
                    "osascript",
                    "-e",
                    'display dialog "%s" buttons {"%s","%s"} default button 1' % (safe, yes, no),
                ],
                capture_output=True,
                text=True,
                timeout=180,
            )
            return yes in (p.stdout or "")
    except Exception:  # noqa: BLE001
        pass
    return False


def ask_choice(msg: str, buttons: list[str]) -> str:
    """弹一个多选一的对话框，返回被点的那个按钮文字。"""
    try:
        if IS_WIN:
            import ctypes

            # 三个选项够用：MB_YESNOCANCEL
            r = ctypes.windll.user32.MessageBoxW(None, msg, APP_TITLE, 0x03)
            return {6: buttons[0], 7: buttons[1]}.get(r, buttons[2] if len(buttons) > 2 else "")
        if IS_MAC:
            safe = msg.replace('"', "'").replace("\n", " ")
            joined = ",".join('"%s"' % b for b in buttons)
            p = subprocess.run(
                [
                    "osascript",
                    "-e",
                    'display dialog "%s" buttons {%s} default button 1' % (safe, joined),
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
            out = p.stdout or ""
            for b in buttons:
                if ("button returned:%s" % b) in out:
                    return b
    except Exception:  # noqa: BLE001
        pass
    return ""


def ask_text(prompt: str, default: str = "") -> str | None:
    """弹一个输入框。返回用户输入的文字；取消或失败返回 None。

    Windows 没有原生 InputBox API，走 PowerShell 的 VisualBasic.Interaction。
    """
    try:
        if IS_WIN:
            ps = (
                "Add-Type -AssemblyName Microsoft.VisualBasic; "
                "[Microsoft.VisualBasic.Interaction]::InputBox(\"%s\", \"%s\", \"%s\")"
                % (prompt.replace('"', "'").replace("\n", " "), APP_TITLE, default)
            )
            p = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            return (p.stdout or "").strip() or None
        if IS_MAC:
            import re

            safe = prompt.replace('"', "'").replace("\n", " ")
            dflt = default.replace('"', "'")
            p = subprocess.run(
                [
                    "osascript",
                    "-e",
                    'display dialog "%s" default answer "%s" '
                    'buttons {"取消","确定"} default button 2' % (safe, dflt),
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
            out = p.stdout or ""
            if "button returned:确定" not in out:
                return None
            m = re.search(r"text returned:(.*)$", out.strip())
            return (m.group(1) if m else "").strip()
    except Exception:  # noqa: BLE001
        pass
    return None


def make_ask_user():
    """给 agent 用的提问回调：这个输入口上现在显示的是什么？

    返回 "mac" / "peer" / "none"。三选一而不是"是不是本机"，是因为空口会
    显示"无信号" —— 只给两个选项的话，"无信号"会被误记成对端的口。
    """

    def _ask(v: int) -> str:
        try:
            from kvm_platform import input_source_name

            label = input_source_name(v)
        except Exception:  # noqa: BLE001
            label = "0x%02X" % v
        buttons = ["本机 Mac", "另一台机器", "无信号"]
        ans = ask_choice("显示器现在显示的是什么？\n\n当前输入源：%s" % label, buttons)
        return {"本机 Mac": "mac", "另一台机器": "peer"}.get(ans, "none")

    return _ask


_popup_shown = False


def popup_once(msg: str) -> None:
    """同一条提示只弹一次（快捷键没权限这种，每次启动都弹会很烦）。"""
    global _popup_shown
    if _popup_shown:
        return
    _popup_shown = True
    threading.Thread(target=popup, args=(msg,), name="popup", daemon=True).start()
