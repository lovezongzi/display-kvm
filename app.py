#!/usr/bin/env python3
"""display-kvm 托盘外壳（打包入口）。

把底层 Agent（agent.py）包装成「装完即用」的常驻程序：
  * 自动判断 Windows / macOS，不需要手动指定 platform
  * 配置与日志放在用户目录（打包后的程序目录是只读的）
  * Windows 托盘 / macOS 菜单栏常驻：
      看状态、立即切换、自动开关、打开配置/日志、开机自启、退出
  * 完整保留原命令行能力，其余参数原样透传给 agent.py

用法
----
    display-kvm                      # 托盘常驻（默认）
    display-kvm --headless           # 不开托盘，纯后台跑（日志写文件）
    display-kvm --status             # 打印一次状态后退出
    display-kvm --dry-run            # 只记录、不真的切
    display-kvm --print-config-path  # 打印配置文件路径
    display-kvm --install-autostart / --remove-autostart
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

APP_NAME = "display-kvm"
APP_TITLE = "Display KVM"
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_FROZEN = bool(getattr(sys, "frozen", False))

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import hotkeys  # noqa: E402  放在 sys.path 之后，保证打包后也能找到
import settings  # noqa: E402  同上


# ---------------------------------------------------------------------- 路径


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
    return HERE / rel


# ---------------------------------------------------------------------- 日志


class _Tee:
    """把多个流合成一个。任何一路失败都不影响其他路。"""

    def __init__(self, *streams):
        self._streams = [s for s in streams if s is not None]

    def write(self, data):
        n = 0
        for s in self._streams:
            try:
                n = s.write(data) or 0
            except Exception:  # noqa: BLE001
                pass
        return n if n else len(data)

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:  # noqa: BLE001
                pass

    def isatty(self):
        return False


class _RotatingFile:
    """按大小滚动的日志文件：超过 max_bytes 就改名为 .log.1 重新开。

    避免长期运行的托盘程序日志无限增长、占满磁盘（常被误当成内存泄漏）。
    """

    def __init__(self, path: Path, max_bytes: int = 2 * 1024 * 1024):
        self._path = path
        self._max = max_bytes
        self._f = open(path, "a", encoding="utf-8", buffering=1)

    def write(self, data):
        try:
            self._f.write(data)
            if self._f.tell() > self._max:
                self._rotate()
        except Exception:  # noqa: BLE001
            pass
        return len(data)

    def flush(self):
        try:
            self._f.flush()
        except Exception:  # noqa: BLE001
            pass

    def _rotate(self):
        try:
            self._f.close()
            backup = self._path.with_suffix(".log.1")
            try:
                if backup.exists():
                    backup.unlink()
            except OSError:
                pass
            try:
                self._path.rename(backup)
            except OSError:
                pass
            self._f = open(self._path, "a", encoding="utf-8", buffering=1)
            self._f.write("\n===== 日志已轮转（旧内容见 .log.1）=====\n")
        except Exception:  # noqa: BLE001
            self._f = open(self._path, "a", encoding="utf-8", buffering=1)


def setup_logging() -> None:
    """windowed 模式下 sys.stdout 是 None，print 会静默丢失——重定向到文件。"""
    try:
        MAX = 2 * 1024 * 1024
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX:
            backup = LOG_PATH.with_suffix(".log.1")
            try:
                if backup.exists():
                    backup.unlink()
                LOG_PATH.rename(backup)
            except OSError:
                pass
        fh = _RotatingFile(LOG_PATH, MAX)
        fh.write("\n===== %s 启动 =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        sys.stdout = _Tee(sys.__stdout__, fh)
        sys.stderr = _Tee(sys.__stderr__, fh)
    except Exception as exc:  # noqa: BLE001
        print("日志初始化失败: %s" % exc)


# ---------------------------------------------------------------------- 配置


def build_default_config() -> dict:
    cfg = {
        "name": "windows" if IS_WIN else "mac",
        "platform": "win" if IS_WIN else ("mac" if IS_MAC else "win"),
        "peer_name": "mac" if IS_WIN else "windows",
        "peer_ip": "",
        "token": secrets.token_hex(8),
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


# ---------------------------------------------------------------------- 开机自启

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


# ---------------------------------------------------------------------- 小工具


def open_in_system(path: Path) -> None:
    try:
        if IS_WIN:
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif IS_MAC:
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:  # noqa: BLE001
        print("打开失败 %s: %s" % (path, exc))


def _popup(msg: str) -> None:
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


def _ask_yes_no(msg: str, yes: str = "是", no: str = "不是") -> bool:
    """弹一个二选一的对话框，返回用户是不是点了「是」。

    _popup 只会弹通知、拿不到结果；自动探测在无法自己判断"哪个口是本机"
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


def _ask_choice(msg: str, buttons: list[str]) -> str:
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


def _ask_text(prompt: str, default: str = "") -> str | None:
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
            import platforms

            label = platforms.input_source_name(v)
        except Exception:  # noqa: BLE001
            label = "0x%02X" % v
        buttons = ["本机 Mac", "另一台机器", "无信号"]
        ans = _ask_choice("显示器现在显示的是什么？\n\n当前输入源：%s" % label, buttons)
        return {"本机 Mac": "mac", "另一台机器": "peer"}.get(ans, "none")

    return _ask


_hk_popup_shown = False


def _popup_once(msg: str) -> None:
    """同一条提示只弹一次（快捷键没权限这种，每次启动都弹会很烦）。"""
    global _hk_popup_shown
    if _hk_popup_shown:
        return
    _hk_popup_shown = True
    threading.Thread(target=_popup, args=(msg,), name="popup", daemon=True).start()


def _save_cfg_fields(pairs: dict) -> bool:
    """把若干字段写回 kvm.json（保留其它字段原样）。"""
    try:
        c = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
        c.update(pairs)
        CONFIG_PATH.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception as exc:  # noqa: BLE001
        print("[配置] 写回失败: %s" % exc, flush=True)
        return False


def already_running(port: int) -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", int(port)), timeout=0.6)
        s.close()
        return True
    except OSError:
        return False


def _warn_if_placeholder(cfg: dict) -> None:
    token = str(cfg.get("token", ""))
    if not token or "改" in token or "保持" in token or len(token) < 6:
        print("!! token 还是占位符，两端将互相忽略消息。请编辑配置文件设置一致的随机串。")


# ---------------------------------------------------------------------- 托盘


# 角色 -> 状态色。绿=显示器正在显示本机，红=在对端，灰=还没探明。
ROLE_COLOR = {
    "active": (46, 204, 113, 255),    # 绿
    "idle": (231, 76, 60, 255),       # 红
}
ROLE_NEUTRAL = (140, 145, 155, 255)   # 灰


def _tray_image(role: str = ""):
    """托盘图标。右下角叠一个状态圆点，颜色跟着角色走。

    注意：macOS 菜单栏可能把图标当 template 渲染成纯黑白，所以颜色不能只靠
    图标 —— 菜单文本里也带了彩色圆点 emoji（见 role_label），两边都上色。
    """
    from PIL import Image, ImageDraw

    bundled = resource_path("assets/icon.png")
    img = None
    if bundled.exists():
        try:
            img = Image.open(bundled).convert("RGBA")
        except Exception:  # noqa: BLE001
            img = None

    if img is None:
        # 兜底：现场画一个
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([1, 1, size - 2, size - 2], radius=14, fill=(22, 26, 35, 255))
        d.rounded_rectangle([7, 18, 26, 42], radius=3, fill=(77, 155, 255, 255))
        d.rounded_rectangle([38, 18, 57, 42], radius=3, fill=(54, 200, 96, 255))
        d.line([(28, 26), (36, 26)], fill=(232, 238, 248, 255), width=2)
        d.line([(36, 36), (28, 36)], fill=(232, 238, 248, 255), width=2)
    else:
        img = img.copy()
        if img.size[0] < 32:
            img = img.resize((64, 64), Image.LANCZOS)

    w, h = img.size
    r = max(8, w // 4)
    d = ImageDraw.Draw(img)
    # 先描一圈深色边，让圆点在浅色/深色菜单栏上都分得清
    d.ellipse([w - r - 3, h - r - 3, w - 1, h - 1], fill=(18, 20, 26, 255))
    d.ellipse([w - r, h - r, w - 4, h - 4], fill=ROLE_COLOR.get(role, ROLE_NEUTRAL))
    return img


def run_tray(agent, cfg: dict) -> int:
    import pystray
    from pystray import Menu, MenuItem

    # 注意：Menu(...) 构造时就会调用 line_* 这些回调取文本，此时 refresh() 还没跑过。
    # 所以这里必须把回调会读到的键全部初始化，否则第一次取文本就 KeyError
    # —— 在打包后的 app 里这会表现为启动即 SIGABRT（刷新 stdout 失败 -> 致命错误）。
    state = {
        "role": "",
        "detail": "",
        "peer": "",
        "peer_online": False,
        "peer_role": "unknown",
        "peer_age": None,
        "auto": agent.auto_on,
    }

    def refresh() -> None:
        try:
            r = agent.api_status()
            state["role"] = r.get("role", "")
            state["detail"] = r.get("detail", "")
            state["peer"] = r.get("peer_ip") or "未发现"
            state["peer_online"] = bool(r.get("peer_online"))
            state["peer_role"] = r.get("peer_role") or "unknown"
            state["peer_age"] = r.get("peer_age_seconds")
            state["auto"] = bool(r.get("auto"))
        except Exception as exc:  # noqa: BLE001
            state["detail"] = "状态读取失败: %s" % exc

    def role_label(role: str) -> str:
        # 彩色圆点 emoji。菜单栏图标可能被系统当 template 渲染成黑白
        # （见 _tray_image 的注释），但菜单文本里的 emoji 一定是彩色的，
        # 所以颜色靠这里保证，图标那边只是锦上添花。
        return {
            "active": "🟢 正在显示本机",
            "idle": "🔴 不在本机（对端占用）",
        }.get(role, "⚪ %s" % (role or "…"))

    def line_status(item=None) -> str:
        return "%s · %s" % (agent.name, role_label(state["role"]))

    def line_detail(item=None) -> str:
        return (state["detail"] or "…")[:64]

    def line_peer(item=None) -> str:
        # 只报在线 / 离线 / 未发现。
        # 以前这里会显示对端 IP 和"xx 秒前心跳"：那两个数字对使用者没有
        # 任何 actionable 的信息，却让菜单每两秒变一次宽度、读起来也费劲。
        # 真要排查连接问题，日志里都有（对端下线也照样记）。
        who = cfg.get("peer_name") or "对端"
        if state.get("peer_online"):
            return "%s · 在线" % who
        if state.get("peer_age") is None:
            return "%s · 未发现" % who
        return "%s · 离线" % who

    def do_switch(icon, item):
        r = agent.api_switch("托盘手动切换")
        msg = {
            "local": "本机直接切换已下发",
            "delegated": "已委托对端切换",
            "no-peer": "还没发现对端，无法切换",
            "cooldown": "冷却中，请稍候",
            "pending": "已提交，稍后生效",
        }.get(r, r)
        _notify(icon, APP_TITLE, msg)
        refresh()
        icon.update_menu()

    def do_switch_peer(icon, item):
        r = agent.api_switch_peer("托盘手动切换")
        msg = {
            "local": "已把画面交给对端",
            "delegated": "已委托对端切换",
            "no-peer": "还没发现对端，无法切换",
            "cooldown": "冷却中，请稍候",
            "pending": "已提交，稍后生效",
        }.get(r, r)
        _notify(icon, APP_TITLE, msg)
        refresh()
        icon.update_menu()

    def do_autodetect(icon, item):
        _notify(icon, APP_TITLE, "开始探测输入口，画面会切换几次")
        agent.autodetect_async()

    # ---------------------------------------------------------- 全局快捷键
    HK = hotkeys.HotkeyManager()
    HK_FIELD = {"to_me": "hotkey_to_me", "to_peer": "hotkey_to_peer"}
    HK_TITLE = {
        "to_me": "切到本机",
        "to_peer": "切到对端（%s）" % (cfg.get("peer_name") or "对端"),
    }

    def hk_label(kind: str) -> str:
        raw = (cfg.get(HK_FIELD[kind]) or "").strip()
        if not raw:
            return "未设置"
        c = hotkeys.parse_combo(raw)
        if c.is_empty():
            return "%s ?" % raw
        if not c.mods:
            return "%s（缺修饰键）" % raw
        return str(c)

    def _hk_fire(kind: str) -> None:
        """热键回调。必须挪到独立线程：api_switch 最多会等 6 秒，而 CGEventTap
        的回调卡太久会被系统判定超时、把 tap 直接禁用掉。"""

        def go():
            try:
                if kind == "to_me":
                    agent.api_switch("快捷键触发")
                else:
                    agent.api_switch_peer("快捷键触发")
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=go, name="hk-fire", daemon=True).start()

    def rebind_hotkeys() -> None:
        # 同一个 HotkeyManager 的 bindings 是后台线程每帧重新读的，所以换绑定
        # 只要 clear + bind 就生效，不用（也没法）重启那个线程。
        HK.clear()
        if cfg.get("hotkeys_enabled", True):
            HK.bind(cfg.get("hotkey_to_me"), lambda: _hk_fire("to_me"))
            HK.bind(cfg.get("hotkey_to_peer"), lambda: _hk_fire("to_peer"))
        if HK.count() and not HK.active():
            if HK.start():
                print(
                    "[快捷键] 已启用: %s"
                    % "、".join(str(c) for c, _ in HK.bindings()),
                    flush=True,
                )
            else:
                print("[快捷键] 启用失败：%s" % (HK.error or "未知原因"), flush=True)
                # 打包后看不到 stdout，权限缺失必须弹出来，否则用户只会觉得"没反应"
                _popup_once(HK.error)
        else:
            print(
                "[快捷键] 未启用（enabled=%s me=%r peer=%r 绑定=%d 线程存活=%s）"
                % (
                    cfg.get("hotkeys_enabled", True),
                    cfg.get("hotkey_to_me"),
                    cfg.get("hotkey_to_peer"),
                    HK.count(),
                    HK.active(),
                ),
                flush=True,
            )

    def set_hotkey(icon, kind) -> None:
        cur = (cfg.get(HK_FIELD[kind]) or "").strip()
        ans = _ask_text(
            "给「%s」设一个全局快捷键。\n\n"
            "写法：修饰键 + 主键，例如 ctrl+alt+1\n"
            "修饰键：ctrl（Mac 上是 ⌃）/ alt（⌥）/ shift（⇧）/ cmd（⌘）\n"
            "主键：字母、数字、f1~f20、方向键、space 等\n\n"
            "留空 = 取消这个快捷键。" % HK_TITLE[kind],
            cur,
        )
        if ans is None:
            return  # 点了取消
        ans = ans.strip()
        if ans:
            ok, note = hotkeys.validate_combo(ans)
            if not ok:
                _notify(icon, APP_TITLE, note)
                return
            ans = hotkeys.parse_combo(ans).to_config()
        cfg[HK_FIELD[kind]] = ans
        _save_cfg_fields({HK_FIELD[kind]: ans})
        rebind_hotkeys()
        _notify(icon, APP_TITLE, "「%s」：%s" % (HK_TITLE[kind], hk_label(kind)))
        refresh()
        icon.update_menu()

    def toggle_hotkeys(icon, item) -> None:
        cfg["hotkeys_enabled"] = not cfg.get("hotkeys_enabled", True)
        _save_cfg_fields({"hotkeys_enabled": cfg["hotkeys_enabled"]})
        rebind_hotkeys()
        _notify(icon, APP_TITLE, "全局快捷键已%s" % ("开启" if cfg["hotkeys_enabled"] else "关闭"))
        refresh()
        icon.update_menu()

    def toggle_auto(icon, item):
        agent.auto_on = not agent.auto_on
        _notify(icon, APP_TITLE, "自动切换已%s" % ("开启" if agent.auto_on else "关闭"))
        refresh()
        icon.update_menu()

    def do_settings(icon, item) -> None:
        """打开参数设置窗口（一个集中面板，填完点保存即时生效）。

        macOS 走无模态原生窗口：窗口弹出即返回，用户点「保存」时由窗口内部直接回写；
        Windows 走 PowerShell WinForms，是同步的，这里拿到改动后回写。
        两种情况下原生窗口都弹不出来时，才打开配置文件兜底。
        """

        def apply(changed):
            cfg.update(changed)
            _save_cfg_fields(changed)
            # 主循环读的是 agent 自己那份 cfg（构造时拷贝过的），必须同步过去，
            # 否则要重启才生效。
            agent.update_cfg(changed)
            _notify(icon, APP_TITLE, "已保存：%s" % settings.summarize(changed))
            refresh()
            icon.update_menu()

        res = settings.open_settings(
            cfg, on_save=apply, fallback=lambda: open_in_system(CONFIG_PATH)
        )
        if res == settings.FALLBACK:
            _notify(
                icon,
                APP_TITLE,
                "设置窗口打不开，已打开配置文件，改完保存后重启程序生效",
            )
            return
        if res == settings.OPENED:
            return  # 窗口已弹出，保存时由窗口回调 apply 写回
        if res is None:
            return  # 用户取消 / 没改动
        apply(res)  # Windows 同步路径

    def open_cfg(icon, item):
        open_in_system(CONFIG_PATH)

    def open_log(icon, item):
        open_in_system(LOG_PATH)

    def toggle_startup(icon, item):
        if is_autostart_enabled():
            disable_autostart()
            _notify(icon, APP_TITLE, "已关闭开机自启")
        else:
            enable_autostart()
            _notify(icon, APP_TITLE, "已开启开机自启")
        icon.update_menu()

    def do_quit(icon, item):
        try:
            agent.running = False
        finally:
            icon.stop()

    menu = Menu(
        MenuItem(line_status, None, enabled=False),
        MenuItem(line_detail, None, enabled=False),
        MenuItem(line_peer, None, enabled=False),
        Menu.SEPARATOR,
        MenuItem("立即切到本机", do_switch),
        MenuItem("立即切到对端（%s）" % (cfg.get("peer_name") or "对端"), do_switch_peer),
        MenuItem("自动探测本机输入口", do_autodetect),
        MenuItem("自动切换", toggle_auto, checked=lambda item: state["auto"]),
        MenuItem("参数设置 …", do_settings),
        Menu.SEPARATOR,
        MenuItem(
            lambda item: "快捷键 · %s：%s" % (HK_TITLE["to_me"], hk_label("to_me")),
            lambda icon, item: set_hotkey(icon, "to_me"),
        ),
        MenuItem(
            lambda item: "快捷键 · %s：%s" % (HK_TITLE["to_peer"], hk_label("to_peer")),
            lambda icon, item: set_hotkey(icon, "to_peer"),
        ),
        MenuItem("启用快捷键", toggle_hotkeys, checked=lambda item: cfg.get("hotkeys_enabled", True)),
        Menu.SEPARATOR,
        MenuItem("打开配置文件", open_cfg),
        MenuItem("打开日志", open_log),
        MenuItem("开机自启", toggle_startup, checked=lambda item: is_autostart_enabled()),
        Menu.SEPARATOR,
        MenuItem("退出", do_quit),
    )

    icon = pystray.Icon(APP_NAME, _tray_image(), APP_TITLE, menu)

    def _ui_call(fn, *a) -> None:
        """把 AppKit 调用投递回主线程。

        macOS 上 NSStatusItem 只能在主线程改（pystray 的 _darwin 后端不做任何
        线程调度，_update_menu/_update_title 直接碰 NSStatusItem）。从后台线程
        调用会撞上 -[BSServiceMainRunLoopQueue assertBarrierOnQueue]，
        进程以 EXC_BREAKPOINT/SIGTRAP 直接崩掉。
        """
        if IS_MAC:
            try:
                from PyObjCTools import AppHelper

                AppHelper.callAfter(fn, *a)
                return
            except Exception:  # noqa: BLE001
                return
        fn(*a)

    def on_setup(ic):
        ic.visible = True
        threading.Thread(target=agent.run, name="agent", daemon=True).start()
        rebind_hotkeys()

        def apply_state():
            try:
                st = agent.api_status()
                role = st.get("role", "")
                ic.title = "%s · %s" % (APP_TITLE, role_label(role))
                # 图标（右下角状态圆点）只在角色变化时才重绘，避免每 2 秒都新建
                # 一个 PIL/NSImage 对象，长期运行累积内存压力。
                if getattr(ic, "_dk_role", None) != role:
                    ic._dk_role = role
                    ic.icon = _tray_image(role)
            except Exception:  # noqa: BLE001
                pass
            try:
                ic.update_menu()
            except Exception:  # noqa: BLE001
                pass

        def refresher():
            while True:
                time.sleep(2.0)
                refresh()  # 只写普通 dict，任意线程安全
                _ui_call(apply_state)

        threading.Thread(target=refresher, name="tray-refresh", daemon=True).start()

    icon.run(setup=on_setup)
    return 0


def _notify(icon, title: str, msg: str) -> None:
    try:
        icon.notify(msg, title)
    except Exception:  # noqa: BLE001
        print("[notify] %s: %s" % (title, msg))


# ---------------------------------------------------------------------- 入口


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

    _warn_if_placeholder(cfg)

    port = int(cfg.get("control_port", 47822))
    if already_running(port):
        msg = "%s 已经在运行了（控制口 127.0.0.1:%d 已被占用）。" % (APP_TITLE, port)
        print(msg)
        if not args.headless:
            _popup(msg)
        return 0

    ns = build_agent_namespace(args)
    ns.config = str(cfg_path)
    ag = agent_mod.Agent(cfg, ns)
    # 让 agent 能把自动探测的结果写回配置，并在判断不了的时候弹窗问一句
    ag.cfg_path = str(cfg_path)
    ag.ask_user = make_ask_user()

    if args.headless:
        # Carbon 事件循环必须占主线程，所以把 agent 挪到后台线程跑。
        _headless_run(ag, cfg)
        return 0

    return run_tray(ag, cfg)


def _headless_run(ag, cfg: dict) -> None:
    """headless 模式：agent 在后台线程，主线程留给 Carbon 事件循环。"""
    threading.Thread(target=ag.run, name="agent", daemon=True).start()
    hk = _headless_hotkeys_boot(ag, cfg)
    if hk is not None:
        hk.run_event_loop_if_needed()  # 阻塞，直到进程退出
        return
    while True:
        time.sleep(3600)


def _headless_hotkeys_boot(ag, cfg: dict):
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


if __name__ == "__main__":
    raise SystemExit(main())
