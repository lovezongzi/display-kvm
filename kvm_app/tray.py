"""托盘/菜单栏界面。

原先是一个 400 多行的 run_tray()，里面几十个嵌套闭包共享 agent / cfg / icon，
既读不下去也没法单测。现在收成一个 TrayApp 类：状态放实例字段，动作放方法，
`run()` 只负责建 icon 并阻塞。

几处不能动的历史约束（每一处都对应一次线上崩溃，注释保留在原位）：
  * Menu(...) 构造时就会调用 line_* 回调取文本，此时 icon 还不存在 ——
    所以 state 必须在建 Menu 之前初始化完整，否则打包后启动即 SIGABRT。
  * macOS 的 NSStatusItem 只能在主线程改，后台线程必须经 _ui_call 投递，
    否则 -[BSServiceMainRunLoopQueue assertBarrierOnQueue] 直接 SIGTRAP。
  * 菜单动作回调里不能同步调用 icon.update_menu()，否则 AppKit 会释放正在
    被追踪的 NSMenuItem，表现为 EXC_BAD_ACCESS。
"""

from __future__ import annotations

import threading
import time

# 必须先导入 .paths：它在 import 时把项目根塞进 sys.path，后面的
# `import hotkeys` / `import settings` 才找得到（它们不是本包的一部分）。
from .paths import APP_NAME, APP_TITLE, CONFIG_PATH, IS_MAC, LOG_PATH, resource_path

import hotkeys  # noqa: E402
import settings  # noqa: E402

from .autostart import disable_autostart, enable_autostart, is_autostart_enabled  # noqa: E402
from .config_store import save_cfg_fields  # noqa: E402
from .dialogs import ask_text, open_in_system, popup_once  # noqa: E402

# 角色 -> 状态色。绿=显示器正在显示本机，红=在对端，灰=还没探明。
ROLE_COLOR = {
    "active": (46, 204, 113, 255),    # 绿
    "idle": (231, 76, 60, 255),       # 红
}
ROLE_NEUTRAL = (140, 145, 155, 255)   # 灰

# 动作返回值 -> 提示文案。两个切换动作只有成功时的措辞不同。
_SWITCH_MSG = {
    "local": "本机直接切换已下发",
    "delegated": "已委托对端切换",
    "no-peer": "还没发现对端，无法切换",
    "cooldown": "冷却中，请稍候",
    "pending": "已提交，稍后生效",
}
_SWITCH_PEER_MSG = {
    "local": "已把画面交给对端",
    "delegated": "已委托对端切换",
    "no-peer": "还没发现对端，无法切换",
    "cooldown": "冷却中，请稍候",
    "pending": "已提交，稍后生效",
}


def tray_image(role: str = ""):
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


class TrayApp:
    """托盘界面：状态展示 + 全部菜单动作。"""

    def __init__(self, agent, cfg: dict):
        self.agent = agent
        self.cfg = cfg
        self.icon = None

        # 注意：Menu(...) 构造时就会调用 line_* 这些回调取文本，此时 refresh() 还没跑过。
        # 所以这里必须把回调会读到的键全部初始化，否则第一次取文本就 KeyError
        # —— 在打包后的 app 里这会表现为启动即 SIGABRT（刷新 stdout 失败 -> 致命错误）。
        self.state = {
            "role": "",
            "detail": "",
            "peer": "",
            "peer_online": False,
            "peer_role": "unknown",
            "peer_age": None,
            "auto": agent.auto_on,
            "paired": agent.paired,
            "token_mismatch": False,
        }

        # ---------------------------------------------------------- 全局快捷键
        self.HK = hotkeys.HotkeyManager()
        self.HK_FIELD = {"to_me": "hotkey_to_me", "to_peer": "hotkey_to_peer"}
        self.HK_TITLE = {
            "to_me": "切到本机",
            "to_peer": "切到对端（%s）" % (cfg.get("peer_name") or "对端"),
        }

    # ------------------------------------------------------------------ 状态

    def refresh(self) -> None:
        try:
            r = self.agent.api_status()
            self.state["role"] = r.get("role", "")
            self.state["detail"] = r.get("detail", "")
            self.state["peer"] = r.get("peer_ip") or "未发现"
            self.state["peer_online"] = bool(r.get("peer_online"))
            self.state["peer_role"] = r.get("peer_role") or "unknown"
            self.state["peer_age"] = r.get("peer_age_seconds")
            self.state["auto"] = bool(r.get("auto"))
            self.state["paired"] = bool(r.get("paired"))
            self.state["token_mismatch"] = bool(r.get("token_mismatch"))
        except Exception as exc:  # noqa: BLE001
            self.state["detail"] = "状态读取失败: %s" % exc

    def update_menu(self) -> None:
        """刷新状态并重建托盘菜单。

        必须「推迟到当前菜单会话结束之后」再执行：在菜单动作回调里同步调用
        icon.update_menu() 会令 AppKit 释放正在被追踪的 NSMenuItem，表现为
        EXC_BAD_ACCESS（崩溃在 -[NSStatusItem _beginExpandedInterfaceSession:]
        的 autorelease pool pop，即 objc_release 访问已释放对象）。统一走 _ui_call
        —— macOS 上经 AppHelper.callAfter 投递回主线程下一轮 run loop，此时没有
        任何菜单追踪会话，重建安全。
        """
        self.refresh()
        self.icon.update_menu()

    def role_label(self, role: str) -> str:
        # 彩色圆点 emoji。菜单栏图标可能被系统当 template 渲染成黑白
        # （见 tray_image 的注释），但菜单文本里的 emoji 一定是彩色的，
        # 所以颜色靠这里保证，图标那边只是锦上添花。
        return {
            "active": "🟢 正在显示本机",
            "idle": "🔴 不在本机（对端占用）",
        }.get(role, "⚪ %s" % (role or "…"))

    def line_status(self, item=None) -> str:
        return "%s · %s" % (self.agent.name, self.role_label(self.state["role"]))

    def line_detail(self, item=None) -> str:
        return (self.state["detail"] or "…")[:64]

    def line_peer(self, item=None) -> str:
        # 只报在线 / 离线 / 未发现。
        # 以前这里会显示对端 IP 和"xx 秒前心跳"：那两个数字对使用者没有
        # 任何 actionable 的信息，却让菜单每两秒变一次宽度、读起来也费劲。
        # 真要排查连接问题，日志里都有（对端下线也照样记）。
        who = self.cfg.get("peer_name") or "对端"
        if self.state.get("peer_online"):
            return "%s · 在线" % who
        if self.state.get("peer_age") is None:
            return "%s · 未发现" % who
        return "%s · 离线" % who

    def line_pair(self, item=None):
        # 配对失败原因优先显示：Windows 的系统通知经常被静默抑制，
        # 菜单状态行是唯一一定能看到的反馈。
        err = getattr(self.agent, "pair_error", None)
        if err:
            return "配对状态 · %s" % err
        if self.state.get("paired"):
            # 已配对却持续收到校验不过的报文：过去这个状态只在日志里刷屏，
            # 界面上完全看不出来，用户只会觉得「切换没反应」。
            if self.state.get("token_mismatch"):
                return "配对状态 · token 不一致，正在自动解除以便重新配对"
            return "配对状态 · 已配对"
        cands = getattr(self.agent, "pair_candidates", {})
        if cands:
            names = "、".join(dict.fromkeys(cands.values()))
            return "配对状态 · 发现可配对设备：%s" % names
        return "配对状态 · 未配对（点「配对两端」自动协商）"

    # ------------------------------------------------------------------ 动作

    def do_switch(self, icon, item):
        r = self.agent.api_switch("托盘手动切换")
        self._notify(_SWITCH_MSG.get(r, r))
        self._ui_call(self.update_menu)

    def do_switch_peer(self, icon, item):
        r = self.agent.api_switch_peer("托盘手动切换")
        self._notify(_SWITCH_PEER_MSG.get(r, r))
        self._ui_call(self.update_menu)

    def do_autodetect(self, icon, item):
        self._notify("开始探测输入口，画面会切换几次")
        self.agent.autodetect_async()

    # ---------------------------------------------------------- 快捷键相关

    def hk_label(self, kind: str) -> str:
        raw = (self.cfg.get(self.HK_FIELD[kind]) or "").strip()
        if not raw:
            return "未设置"
        c = hotkeys.parse_combo(raw)
        if c.is_empty():
            return "%s ?" % raw
        if not c.mods:
            return "%s（缺修饰键）" % raw
        return str(c)

    def hk_fire(self, kind: str) -> None:
        """热键回调。必须挪到独立线程：api_switch 最多会等 6 秒，而 CGEventTap
        的回调卡太久会被系统判定超时、把 tap 直接禁用掉。"""

        def go():
            try:
                if kind == "to_me":
                    self.agent.api_switch("快捷键触发")
                else:
                    self.agent.api_switch_peer("快捷键触发")
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=go, name="hk-fire", daemon=True).start()

    def rebind_hotkeys(self) -> None:
        # 同一个 HotkeyManager 的 bindings 是后台线程每帧重新读的，所以换绑定
        # 只要 clear + bind 就生效，不用（也没法）重启那个线程。
        HK = self.HK
        HK.clear()
        if self.cfg.get("hotkeys_enabled", True):
            HK.bind(self.cfg.get("hotkey_to_me"), lambda: self.hk_fire("to_me"))
            HK.bind(self.cfg.get("hotkey_to_peer"), lambda: self.hk_fire("to_peer"))
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
                popup_once(HK.error)
        else:
            print(
                "[快捷键] 未启用（enabled=%s me=%r peer=%r 绑定=%d 线程存活=%s）"
                % (
                    self.cfg.get("hotkeys_enabled", True),
                    self.cfg.get("hotkey_to_me"),
                    self.cfg.get("hotkey_to_peer"),
                    HK.count(),
                    HK.active(),
                ),
                flush=True,
            )

    def set_hotkey(self, icon, kind) -> None:
        cur = (self.cfg.get(self.HK_FIELD[kind]) or "").strip()
        ans = ask_text(
            "给「%s」设一个全局快捷键。\n\n"
            "写法：修饰键 + 主键，例如 ctrl+alt+1\n"
            "修饰键：ctrl（Mac 上是 ⌃）/ alt（⌥）/ shift（⇧）/ cmd（⌘）\n"
            "主键：字母、数字、f1~f20、方向键、space 等\n\n"
            "留空 = 取消这个快捷键。" % self.HK_TITLE[kind],
            cur,
        )
        if ans is None:
            return  # 点了取消
        ans = ans.strip()
        if ans:
            ok, note = hotkeys.validate_combo(ans)
            if not ok:
                self._notify(note)
                return
            ans = hotkeys.parse_combo(ans).to_config()
        self.cfg[self.HK_FIELD[kind]] = ans
        save_cfg_fields({self.HK_FIELD[kind]: ans})
        self.rebind_hotkeys()
        self._notify("「%s」：%s" % (self.HK_TITLE[kind], self.hk_label(kind)))
        self._ui_call(self.update_menu)

    def toggle_hotkeys(self, icon, item) -> None:
        self.cfg["hotkeys_enabled"] = not self.cfg.get("hotkeys_enabled", True)
        save_cfg_fields({"hotkeys_enabled": self.cfg["hotkeys_enabled"]})
        self.rebind_hotkeys()
        self._notify("全局快捷键已%s" % ("开启" if self.cfg["hotkeys_enabled"] else "关闭"))
        self._ui_call(self.update_menu)

    # ------------------------------------------------------------ 自动/配对

    def toggle_auto(self, icon, item):
        new_on = not self.agent.auto_on
        self.cfg["auto_switch"] = new_on
        self.agent.set_auto_switch(new_on)
        self._notify("自动切换已%s（已同步到对端）" % ("开启" if new_on else "关闭"))
        self._ui_call(self.update_menu)

    def do_pair(self, icon, item):
        self._notify("正在通过局域网配对，请确保对端也已运行 display-kvm …")
        self.agent.start_pairing()
        self._ui_call(self.update_menu)

    def do_unpair(self, icon, item):
        # reset_peer：顺带请对端一起解除。否则本端解了、对端还卡在「已配对」，
        # 它就会一直拒绝新的配对请求 —— 这正是 v1.0.7 那个死局的另一半。
        self.agent.unpair(reset_peer=True)
        self._notify("已解除配对，并已通知对端一并解除")
        self._ui_call(self.update_menu)

    def on_paired(self, ok, peer, msg):
        # agent 的 UDP 线程回调，必须回到主线程刷新托盘（macOS 尤其）
        def ui():
            self._notify(msg)
            self.refresh()
            self.icon.update_menu()

        self._ui_call(ui)

    def on_discovered(self, who, ip):
        def ui():
            self.refresh()
            self.icon.update_menu()

        self._ui_call(ui)

    def on_auto_sync(self, on: bool):
        # 对端把自动切换推过来：更新本端内存 + 外壳配置 + 托盘菜单 + 提示。
        # 注意只在 UDP 线程回调里被打，必须用 _ui_call 回到主线程碰 UI。
        self.cfg["auto_switch"] = on

        def ui():
            self._notify("自动切换已按对端同步为%s" % ("开启" if on else "关闭"))
            self.refresh()
            self.icon.update_menu()

        self._ui_call(ui)

    # ------------------------------------------------------------------ 设置

    def do_settings(self, icon, item) -> None:
        """打开参数设置窗口（一个集中面板，填完点保存即时生效）。

        macOS 走**非模态**原生窗口：点「保存」后通过 on_save 回调异步交付改动，
        这里统一写回 cfg / agent / 托盘。Windows 走 PowerShell 表单，同样经 on_save
        交付。原生窗口弹不出来时，才打开配置文件兜底。
        """

        def on_save(changed: dict) -> None:
            if not changed:
                return
            self.cfg.update(changed)
            save_cfg_fields(changed)
            # 主循环读的是 agent 自己那份 cfg（构造时拷贝过的），必须同步过去，
            # 否则要重启才生效。
            self.agent.update_cfg(changed)
            self._notify("已保存：%s" % settings.summarize(changed))
            self._ui_call(self.update_menu)

        res = settings.open_settings(
            self.cfg, fallback=lambda: open_in_system(CONFIG_PATH), on_save=on_save
        )
        if res == settings.FALLBACK:
            self._notify("设置窗口打不开，已打开配置文件，改完保存后重启程序生效")

    def open_cfg(self, icon, item):
        open_in_system(CONFIG_PATH)

    def open_log(self, icon, item):
        open_in_system(LOG_PATH)

    def toggle_startup(self, icon, item):
        if is_autostart_enabled():
            disable_autostart()
            self._notify("已关闭开机自启")
        else:
            enable_autostart()
            self._notify("已开启开机自启")
        self._ui_call(self.update_menu)

    def do_quit(self, icon, item):
        try:
            self.agent.running = False
        finally:
            icon.stop()

    # -------------------------------------------------------------- 菜单构建

    def build_menu(self):
        Menu, MenuItem = _menu_types()
        HK_TITLE = self.HK_TITLE
        return Menu(
            MenuItem(self.line_status, None, enabled=False),
            MenuItem(self.line_detail, None, enabled=False),
            MenuItem(self.line_peer, None, enabled=False),
            Menu.SEPARATOR,
            MenuItem("立即切到本机", self.do_switch),
            MenuItem("立即切到对端（%s）" % (self.cfg.get("peer_name") or "对端"), self.do_switch_peer),
            MenuItem("自动探测本机输入口", self.do_autodetect),
            MenuItem("自动切换", self.toggle_auto, checked=lambda item: self.state["auto"]),
            MenuItem("参数设置 …", self.do_settings),
            Menu.SEPARATOR,
            MenuItem(self.line_pair, None, enabled=False),
            MenuItem("配对两端 …", self.do_pair),
            MenuItem("解除配对", self.do_unpair),
            Menu.SEPARATOR,
            MenuItem(
                lambda item: "快捷键 · %s：%s" % (HK_TITLE["to_me"], self.hk_label("to_me")),
                lambda icon, item: self.set_hotkey(icon, "to_me"),
            ),
            MenuItem(
                lambda item: "快捷键 · %s：%s" % (HK_TITLE["to_peer"], self.hk_label("to_peer")),
                lambda icon, item: self.set_hotkey(icon, "to_peer"),
            ),
            MenuItem(
                "启用快捷键",
                self.toggle_hotkeys,
                checked=lambda item: self.cfg.get("hotkeys_enabled", True),
            ),
            Menu.SEPARATOR,
            MenuItem("打开配置文件", self.open_cfg),
            MenuItem("打开日志", self.open_log),
            MenuItem("开机自启", self.toggle_startup, checked=lambda item: is_autostart_enabled()),
            Menu.SEPARATOR,
            MenuItem("退出", self.do_quit),
        )

    # ------------------------------------------------------------------ 运行

    def _ui_call(self, fn, *a) -> None:
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

    def _notify(self, msg: str, title: str | None = None) -> None:
        icon = self.icon
        if icon is None:
            print("[notify] %s: %s" % (title or APP_TITLE, msg))
            return
        try:
            icon.notify(msg, title or APP_TITLE)
        except Exception:  # noqa: BLE001
            print("[notify] %s: %s" % (title or APP_TITLE, msg))

    def _apply_state(self, ic) -> None:
        try:
            st = self.agent.api_status()
            role = st.get("role", "")
            ic.title = "%s · %s" % (APP_TITLE, self.role_label(role))
            # 图标（右下角状态圆点）只在角色变化时才重绘，避免每 2 秒都新建
            # 一个 PIL/NSImage 对象，长期运行累积内存压力。
            if getattr(ic, "_dk_role", None) != role:
                ic._dk_role = role
                ic.icon = tray_image(role)
        except Exception:  # noqa: BLE001
            pass
        try:
            ic.update_menu()
        except Exception:  # noqa: BLE001
            pass

    def _on_setup(self, ic) -> None:
        self.icon = ic
        ic.visible = True
        threading.Thread(target=self.agent.run, name="agent", daemon=True).start()
        self.rebind_hotkeys()

        def refresher():
            while True:
                time.sleep(2.0)
                self.refresh()  # 只写普通 dict，任意线程安全
                self._ui_call(self._apply_state, ic)

        threading.Thread(target=refresher, name="tray-refresh", daemon=True).start()

    def run(self) -> int:
        import pystray

        # 先在构造 Menu 之前把回调挂好：Menu(...) 会立刻调用文本回调一次
        self.icon = pystray.Icon(APP_NAME, tray_image(), APP_TITLE, self.build_menu())
        self.icon.run(setup=self._on_setup)
        return 0


def _menu_types():
    """延迟导入 pystray（headless / --status 路径下不该拉起 GUI 依赖）。"""
    from pystray import Menu, MenuItem

    return Menu, MenuItem


def run_tray(agent, cfg: dict) -> int:
    """兼容入口：建一个 TrayApp 并阻塞运行。"""
    app = TrayApp(agent, cfg)
    agent.on_paired = app.on_paired
    agent.on_discovered = app.on_discovered
    agent.on_auto_sync = app.on_auto_sync
    return app.run()
