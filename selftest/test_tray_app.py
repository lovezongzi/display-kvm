"""托盘界面（kvm_app.tray）的行为测试。

不需要真的 pystray / PIL：这里注入一个假的 pystray，但**刻意复刻**它最关键
的一个行为 —— Menu(...) 构造时立刻调用每个 MenuItem 的文本回调。历史上
正是这个时机导致打包后的程序启动即 SIGABRT（文本回调读了还没初始化的
state，KeyError -> 刷新 stdout 失败 -> 致命错误）。

只要这条不变量守住，TrayApp 就不可能再在启动阶段炸掉。
"""

from __future__ import annotations

import sys
import types

import pytest

from kvm_app import tray as tray_mod


# --------------------------------------------------------------------- 假 pystray


class _FakeMenuItem:
    def __init__(self, text, action=None, checked=None, enabled=True):
        self.text = text
        self.action = action
        self.checked = checked
        self.enabled = enabled


class _FakeMenu:
    SEPARATOR = "<sep>"

    def __init__(self, *items):
        self.items = items
        # 复刻真实行为：构造时立即求值一次文本回调
        self.texts = []
        for it in items:
            if isinstance(it, _FakeMenuItem) and callable(it.text):
                self.texts.append(it.text(it))


@pytest.fixture
def fake_pystray(monkeypatch):
    mod = types.ModuleType("pystray")
    mod.Menu = _FakeMenu
    mod.MenuItem = _FakeMenuItem
    monkeypatch.setitem(sys.modules, "pystray", mod)
    # _ui_call 在 macOS 上会走 PyObjCTools；测试里强制走同步分支，
    # 这样断言"动作回调是否刷新了菜单"才是确定的。
    monkeypatch.setattr(tray_mod, "IS_MAC", False)
    return mod


# --------------------------------------------------------------------- 桩对象


class _StubAgent:
    def __init__(self):
        self.name = "mac-stub"
        self.auto_on = True
        self.paired = False
        self.running = True
        self.pair_error = None
        self.pair_candidates = {}
        self.switch_result = "local"
        self.switch_peer_result = "delegated"
        self.calls = []
        self.cfg_updates = []

    def api_status(self):
        return {
            "role": "active",
            "detail": "DDC 输入源 0x0F",
            "peer_ip": "192.168.1.9",
            "peer_online": True,
            "peer_role": "idle",
            "peer_age_seconds": 1.2,
            "auto": self.auto_on,
            "paired": self.paired,
            "token_mismatch": False,
        }

    def api_switch(self, reason=""):
        self.calls.append(("switch", reason))
        return self.switch_result

    def api_switch_peer(self, reason=""):
        self.calls.append(("switch_peer", reason))
        return self.switch_peer_result

    def set_auto_switch(self, on, broadcast=True):
        self.calls.append(("set_auto", on))
        self.auto_on = bool(on)
        return True

    def start_pairing(self):
        self.calls.append(("pair", None))

    def unpair(self, reset_peer=False):
        self.calls.append(("unpair", reset_peer))

    def update_cfg(self, pairs):
        self.cfg_updates.append(dict(pairs))

    def autodetect_async(self):
        self.calls.append(("autodetect", None))


class _StubIcon:
    def __init__(self):
        self.notified = []
        self.menu_updates = 0
        self.stopped = False
        self.visible = False
        self.title = ""

    def notify(self, msg, title=None):
        self.notified.append((title, msg))

    def update_menu(self):
        self.menu_updates += 1

    def stop(self):
        self.stopped = True


def _make(**cfg_over):
    cfg = {
        "peer_name": "windows",
        "name": "mac-stub",
        "hotkeys_enabled": True,
        "hotkey_to_me": "ctrl+alt+1",
        "hotkey_to_peer": "",
        "auto_switch": True,
    }
    cfg.update(cfg_over)
    agent = _StubAgent()
    app = tray_mod.TrayApp(agent, cfg)
    icon = _StubIcon()
    app.icon = icon
    return app, agent, icon, cfg


# --------------------------------------------------------------------- 测试


def test_menu_builds_without_keyerror(fake_pystray):
    """启动阶段就调用文本回调，不许抛异常（历史上这里 SIGABRT）。"""
    app, agent, icon, cfg = _make()
    menu = app.build_menu()
    assert len(menu.items) > 10
    # 求值出的文本里应该有本机名与配对状态行
    joined = "\n".join(t for t in menu.texts if isinstance(t, str))
    assert "mac-stub" in joined
    assert "配对状态" in joined


def test_status_lines_follow_state(fake_pystray):
    app, agent, icon, cfg = _make()
    app.build_menu()  # 建菜单时 state 还是初始值，不应崩
    app.refresh()
    assert "🟢" in app.line_status()
    assert app.line_detail() == "DDC 输入源 0x0F"
    assert app.line_peer() == "windows · 在线"
    assert app.line_pair() == "配对状态 · 未配对（点「配对两端」自动协商）"


def test_pair_error_takes_priority(fake_pystray):
    app, agent, icon, cfg = _make()
    agent.pair_error = "未收到对端应答（已回滚）"
    app.refresh()
    assert app.line_pair() == "配对状态 · 未收到对端应答（已回滚）"


def test_switch_maps_result_to_message(fake_pystray):
    app, agent, icon, cfg = _make()
    app.do_switch(icon, None)
    assert agent.calls[-1] == ("switch", "托盘手动切换")
    assert icon.notified[-1] == ("Display KVM", "本机直接切换已下发")

    agent.switch_result = "no-peer"
    app.do_switch(icon, None)
    assert icon.notified[-1] == ("Display KVM", "还没发现对端，无法切换")

    app.do_switch_peer(icon, None)
    assert agent.calls[-1] == ("switch_peer", "托盘手动切换")
    assert icon.notified[-1] == ("Display KVM", "已委托对端切换")


def test_toggle_auto_syncs_agent_and_cfg(fake_pystray):
    app, agent, icon, cfg = _make()
    app.refresh()
    app.toggle_auto(icon, None)
    assert agent.calls[-1] == ("set_auto", False)
    assert cfg["auto_switch"] is False
    assert "关闭" in icon.notified[-1][1]


def test_on_auto_sync_mirrors_to_cfg(fake_pystray):
    app, agent, icon, cfg = _make()
    app.on_auto_sync(False)
    assert cfg["auto_switch"] is False
    assert "按对端同步为关闭" in icon.notified[-1][1]

    app.on_auto_sync(True)
    assert cfg["auto_switch"] is True
    assert "按对端同步为开启" in icon.notified[-1][1]


def test_quit_stops_agent_and_icon(fake_pystray):
    app, agent, icon, cfg = _make()
    app.do_quit(icon, None)
    assert agent.running is False
    assert icon.stopped is True


def test_settings_save_writes_back(fake_pystray, monkeypatch):
    app, agent, icon, cfg = _make()
    saved = {}

    def fake_save(pairs):
        saved.update(pairs)
        return True

    monkeypatch.setattr(tray_mod, "save_cfg_fields", fake_save)

    captured = {}

    def fake_open_settings(c, fallback=None, on_save=None):
        captured["on_save"] = on_save
        return "ok"

    monkeypatch.setattr(tray_mod.settings, "open_settings", fake_open_settings)
    monkeypatch.setattr(tray_mod.settings, "FALLBACK", "fallback")

    app.do_settings(icon, None)
    captured["on_save"]({"arm_after_idle_seconds": 5.0})
    assert cfg["arm_after_idle_seconds"] == 5.0
    assert saved == {"arm_after_idle_seconds": 5.0}
    assert agent.cfg_updates[-1] == {"arm_after_idle_seconds": 5.0}


def test_run_registers_agent_callbacks(fake_pystray, monkeypatch):
    """run_tray 必须把三个回调挂到 agent 上，否则配对/同步的提示永远不出现。"""
    app, agent, icon, cfg = _make()

    # 让 Icon 一构造就假装跑完，避免真的进事件循环
    def fake_icon(*a, **kw):
        return icon

    fake_pystray.Icon = fake_icon
    monkeypatch.setattr(_StubIcon, "run", lambda self, setup=None: None, raising=False)
    # tray_image 需要 PIL；这里只验证回调接线，图标用假的
    monkeypatch.setattr(tray_mod, "tray_image", lambda role="": None)

    assert tray_mod.run_tray(agent, cfg) == 0
    assert callable(getattr(agent, "on_paired", None))
    assert callable(getattr(agent, "on_discovered", None))
    assert callable(getattr(agent, "on_auto_sync", None))


# ------------------------------------------------- Windows 右键菜单高亮不消失
#
# pystray 的 win32 后端是"快照式"的：update_menu() 会 DestroyMenu 再重建，
# 而弹出菜单是 TrackPopupMenuEx 阻塞显示的。以前刷新线程每 2 秒硬重建一次，
# 一旦砸在菜单打开期间，Windows 就在照着已释放的结构画菜单 —— 悬停高亮消失
# （但还能点）。下面几条守住"显示期间绝不重建 + 弹出前重建"。


def test_windows_never_rebuilds_while_menu_open(fake_pystray, monkeypatch):
    monkeypatch.setattr(tray_mod, "IS_WIN", True)
    app, agent, icon, cfg = _make()
    app._menu_open = True

    before = icon.menu_updates
    app.update_menu()  # 模拟后台刷新线程在菜单打开期间刷新

    assert icon.menu_updates == before, "菜单显示期间重建 = 高亮消失"
    assert app._menu_dirty is True, "刷新应被记成脏标记，等关闭后补上"


def test_windows_hook_rebuilds_before_popup_and_flushes_after_close(
    fake_pystray, monkeypatch
):
    monkeypatch.setattr(tray_mod, "IS_WIN", True)
    app, agent, icon, cfg = _make()
    seen = {}

    def _on_notify(wparam, lparam):
        # 进到这里说明菜单要弹了：此刻必须已经重建过，且已标记为打开
        seen["updates_at_popup"] = icon.menu_updates
        seen["open"] = app._menu_open
        app.update_menu()  # 模拟菜单项动作里的刷新（显示期间）
        return 0

    # 键名无所谓，钩子是靠处理函数的名字 "_on_notify" 找到槽位的（和 pystray 一致）
    icon._message_handlers = {0x0400: _on_notify}
    app._install_win_menu_hook(icon)
    assert app._win_menu_hooked is True

    before = icon.menu_updates
    icon._message_handlers[0x0400](1, 0x0205)  # WM_RBUTTONUP

    assert seen["updates_at_popup"] == before + 1, "弹出前必须先重建一次"
    assert seen["open"] is True
    assert app._menu_open is False, "菜单关闭后标记必须复位"
    assert app._menu_dirty is False
    assert icon.menu_updates == before + 2, "关闭后要补一次显示期间的刷新"


def test_windows_hook_passes_through_other_messages(fake_pystray, monkeypatch):
    monkeypatch.setattr(tray_mod, "IS_WIN", True)
    app, agent, icon, cfg = _make()
    hits = []
    icon._message_handlers = {0x0400: lambda w, l: hits.append(l) or 0}
    app._install_win_menu_hook(icon)

    before = icon.menu_updates
    icon._message_handlers[0x0400](0, 0x0202)  # WM_LBUTTONUP，不该触发重建

    assert hits == [0x0202]
    assert icon.menu_updates == before
    assert app._menu_open is False


def test_windows_hook_never_rebuilds_on_reentry(fake_pystray, monkeypatch):
    """菜单还开着又来一次右键：不许重建（否则又把正在显示的菜单毁了）。"""
    monkeypatch.setattr(tray_mod, "IS_WIN", True)
    app, agent, icon, cfg = _make()
    hits = []
    icon._message_handlers = {0x0400: lambda w, l: hits.append(l) or 0}
    app._install_win_menu_hook(icon)
    app._menu_open = True

    before = icon.menu_updates
    icon._message_handlers[0x0400](1, 0x0205)

    assert hits == [0x0205]  # 仍然交给 pystray 处理
    assert icon.menu_updates == before


def test_windows_hook_without_internals_is_safe(fake_pystray, monkeypatch):
    """pystray 换了内部结构也不能崩，只是退回定时重建的老行为。"""
    monkeypatch.setattr(tray_mod, "IS_WIN", True)
    app, agent, icon, cfg = _make()
    app._install_win_menu_hook(icon)  # 桩对象没有 _message_handlers
    assert app._win_menu_hooked is False
    # 老路径仍可用
    before = icon.menu_updates
    app.update_menu()
    assert icon.menu_updates == before + 1


def test_windows_periodic_refresh_only_touches_title_when_hooked(
    fake_pystray, monkeypatch
):
    monkeypatch.setattr(tray_mod, "IS_WIN", True)
    monkeypatch.setattr(tray_mod, "tray_image", lambda role="": None)
    app, agent, icon, cfg = _make()
    app._win_menu_hooked = True

    before = icon.menu_updates
    app._apply_state(icon)

    assert icon.title.startswith(tray_mod.APP_TITLE)  # 标题照常刷新
    assert icon.menu_updates == before, "菜单留到下次弹出前再重建"

