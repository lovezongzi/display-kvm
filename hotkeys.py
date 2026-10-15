#!/usr/bin/env python3
"""跨平台全局热键。

组合键用字符串描述，方便直接放进配置文件：

    "ctrl+alt+1"        "cmd+shift+m"       "ctrl+alt+f13"

修饰键别名（大小写、顺序都无所谓）：

    ctrl / control / ctl      Mac 上是 ⌃Control，Windows 上是 Ctrl
    alt / option / opt        Mac 上是 ⌥Option，Windows 上是 Alt
    shift                     ⇧Shift
    cmd / command / super / meta / win     Mac 上是 ⌘Command，Windows 上是 Win

主键可以是：单个字母或数字、f1~f20、space、enter、tab、esc、delete、
home、end、pageup、pagedown、以及 up/down/left/right。

实现
----
macOS    首选 Carbon 的 RegisterEventHotKey —— 它走系统的热键注册表，
         不需要「辅助功能」权限。这点很关键：CGEventTap 那条路必须在
         系统设置里授权，而 ad-hoc 签名的 app 每次重新打包 cdhash 都会变，
         旧授权立刻失效，用户得每构建一次就重新勾一次。Carbon 没这个毛病。
         Carbon 用不了时才退回 CGEventTap。
         事件派发依赖 Carbon/Cocoa 的事件循环：有 Cocoa 主循环（托盘模式）
         就用它，否则在独立线程里跑 RunApplicationEventLoop。
Windows  RegisterHotKey + 消息循环，跑在后台线程。不需要管理员权限。

回调在热键线程/主线程里执行，**不要在里面碰 UI**（AppKit / Win32 窗口都要
求主线程）。本项目的两个回调只往 agent 的任务队列里丢一条指令，线程安全。
"""

from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

# ---------------------------------------------------------------- 修饰键

_MOD_ALIASES = {
    "ctrl": "ctrl", "control": "ctrl", "ctl": "ctrl",
    "alt": "alt", "option": "alt", "opt": "alt",
    "shift": "shift",
    "cmd": "cmd", "command": "cmd", "super": "cmd", "meta": "cmd", "win": "cmd",
}

# Mac 的 CGEventFlags
_MAC_MOD_BITS = {
    "ctrl": 0x00040000,   # kCGEventFlagMaskControl
    "alt": 0x00080000,    # kCGEventFlagMaskAlternate
    "shift": 0x00020000,  # kCGEventFlagMaskShift
    "cmd": 0x00100000,    # kCGEventFlagMaskCommand
}
# Windows 的 MOD_*
_WIN_MOD_BITS = {"alt": 0x0001, "ctrl": 0x0002, "shift": 0x0004, "cmd": 0x0008}

_MOD_LABEL = (
    {"ctrl": "⌃", "alt": "⌥", "shift": "⇧", "cmd": "⌘"}
    if IS_MAC
    else {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "cmd": "Win"}
)
_MOD_ORDER = ["ctrl", "alt", "shift", "cmd"]


class Combo:
    """一个组合键。mods 是 {'ctrl','alt','shift','cmd'} 的子集，key 是主键名。"""

    __slots__ = ("mods", "key")

    def __init__(self, mods, key: str):
        self.mods = frozenset(mods)
        self.key = str(key).lower()

    def __str__(self) -> str:
        parts = [_MOD_LABEL[m] for m in _MOD_ORDER if m in self.mods]
        parts.append(_key_label(self.key))
        return "".join(parts) if IS_MAC else "+".join(parts)

    def to_config(self) -> str:
        """写回配置文件的形式（纯 ascii，两端都能读）。"""
        parts = [m for m in _MOD_ORDER if m in self.mods]
        parts.append(self.key)
        return "+".join(parts)

    def is_empty(self) -> bool:
        return not self.key


def _key_label(key: str) -> str:
    if key.startswith("f") and key[1:].isdigit():
        return key.upper()
    return {"up": "↑", "down": "↓", "left": "←", "right": "→"}.get(key, key.upper())


_KNOWN_SPECIAL = {
    "space", "enter", "return", "tab", "esc", "escape", "delete", "backspace",
    "home", "end", "pageup", "pagedown", "up", "down", "left", "right",
}


def _valid_key(key: str) -> bool:
    if len(key) == 1 and key.isalnum():
        return True
    if key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 20:
        return True
    return key in _KNOWN_SPECIAL


def parse_combo(text) -> Combo:
    """把 "ctrl+alt+1" 解析成 Combo。认不出来时 is_empty() 为真。"""
    if not text:
        return Combo((), "")
    raw = str(text).strip().lower().replace("-", "+")
    parts = [p.strip() for p in raw.split("+") if p.strip()]
    if not parts:
        return Combo((), "")

    mods: set[str] = set()
    key = ""
    for p in parts:
        if p in _MOD_ALIASES:
            mods.add(_MOD_ALIASES[p])
        else:
            key = p  # 最后一个非修饰键当主键
    if not key or not _valid_key(key):
        return Combo((), "")
    return Combo(mods, key)


def validate_combo(text) -> tuple[bool, str]:
    """给 UI 用：返回 (能不能用, 说明)。"""
    c = parse_combo(text)
    if c.is_empty():
        return False, "没法识别这个组合键，用类似 ctrl+alt+1 的写法"
    if not c.mods:
        return False, "至少要带一个修饰键，单独一个字母键会和正常打字打架"
    return True, str(c)


# ---------------------------------------------------------------- 主键 -> 键码

# Mac 虚拟键码（Events.h 的 kVK_ANSI_* / kVK_*）
_MAC_KEYS = {
    "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04, "g": 0x05,
    "z": 0x06, "x": 0x07, "c": 0x08, "v": 0x09, "b": 0x0B,
    "q": 0x0C, "w": 0x0D, "e": 0x0E, "r": 0x0F, "y": 0x10, "t": 0x11,
    "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "5": 0x17, "6": 0x16,
    "7": 0x1A, "8": 0x1C, "9": 0x19, "0": 0x1D,
    "o": 0x1F, "u": 0x20, "i": 0x22, "p": 0x23,
    "l": 0x25, "j": 0x26, "k": 0x28, "n": 0x2D, "m": 0x2E,
    "space": 0x31, "return": 0x24, "enter": 0x24, "tab": 0x30,
    "esc": 0x35, "escape": 0x35, "delete": 0x33, "backspace": 0x33,
    "home": 0x73, "end": 0x77, "pageup": 0x74, "pagedown": 0x79,
    "up": 0x7E, "down": 0x7D, "left": 0x7B, "right": 0x7C,
    "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76, "f5": 0x60, "f6": 0x61,
    "f7": 0x62, "f8": 0x64, "f9": 0x65, "f10": 0x6D, "f11": 0x67, "f12": 0x6F,
    "f13": 0x69, "f14": 0x6B, "f15": 0x71, "f16": 0x6A, "f17": 0x40,
    "f18": 0x4F, "f19": 0x50, "f20": 0x5A,
}

# Windows 虚拟键码
_WIN_KEYS = {
    "space": 0x20, "return": 0x0D, "enter": 0x0D, "tab": 0x09,
    "esc": 0x1B, "escape": 0x1B, "delete": 0x2E, "backspace": 0x08,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
}
_WIN_KEYS.update({"f%d" % i: 0x6F + i for i in range(1, 25)})


def _mac_keycode(key: str) -> int | None:
    return _MAC_KEYS.get(str(key).lower())


def _win_vk(key: str) -> int | None:
    k = str(key).lower()
    if k in _WIN_KEYS:
        return _WIN_KEYS[k]
    if len(k) == 1 and k.isalnum():
        return ord(k.upper())  # Win 上字母数字的 VK 就是 ASCII 大写
    return None


def key_supported(key: str) -> bool:
    return (_mac_keycode(key) if IS_MAC else _win_vk(key)) is not None


# ---------------------------------------------------------------- 管理器


class HotkeyManager:
    """注册若干全局热键，后台线程监听，触发时调用对应回调。

    热键绑定后不会自动开始：调用方要先 bind() 完，再 start()。
    中途改绑定就 clear() + bind() + start()（会起新线程）。
    """

    def __init__(self):
        self._bindings: dict[int, tuple[Combo, object]] = {}
        self._next_id = 1
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.error = ""  # start() 失败的原因
        self._carbon: _CarbonBackend | None = None
        self._using_carbon = False

    def bind(self, combo_text, callback) -> bool:
        """登记一个热键。空值或非法时什么都不做，返回 False。"""
        c = parse_combo(combo_text)
        if c.is_empty() or not c.mods:
            return False
        if not key_supported(c.key):
            return False
        with self._lock:
            self._bindings[self._next_id] = (c, callback)
            self._next_id += 1
        return True

    def clear(self) -> None:
        if self._carbon is not None:
            self._carbon.unregister_all()
            self._carbon = None
        self._using_carbon = False
        with self._lock:
            self._bindings.clear()

    def count(self) -> int:
        with self._lock:
            return len(self._bindings)

    def bindings(self) -> list[tuple[Combo, object]]:
        with self._lock:
            return [(c, cb) for c, cb in self._bindings.values()]

    def active(self) -> bool:
        # Carbon 模式下事件由外部主循环派发，自己没有线程，别误判成"没在跑"
        return self._using_carbon or bool(self._thread and self._thread.is_alive())

    def start(self) -> bool:
        """开始监听。**必须在主线程调用**：Carbon Event Manager 是主线程绑定的，
        在别处注册事件不会派发过来（实测子线程里跑 RunApplicationEventLoop
        收不到任何按键）。
        """
        if self.count() == 0 or self.active():
            return self.active()
        self.error = ""

        if IS_MAC:
            # 首选 Carbon：不需要辅助功能权限
            carbon = _CarbonBackend()
            if carbon.available:
                with self._lock:
                    items = list(self._bindings.items())
                ok_any = False
                for hid, (combo, cb) in items:
                    if carbon.register(hid, combo, cb):
                        ok_any = True
                if ok_any:
                    self._carbon = carbon
                    self._using_carbon = True
                    return True
                carbon.unregister_all()

            # Carbon 不可用，退回 CGEventTap（要辅助功能权限）
            global _PROMPTED
            ok, err = _check_mac_permission(prompt=not _PROMPTED)
            _PROMPTED = True
            if not ok:
                self.error = err
                return False

        self._thread = threading.Thread(target=self._run, name="hotkeys", daemon=True)
        self._thread.start()
        return True

    def run_event_loop_if_needed(self) -> None:
        """进程没有现成主循环时（headless），在主线程跑 Carbon 事件循环。

        会阻塞。托盘模式下别调 —— 那里 pystray 的 NSApplication 主循环已经
        在派发 Carbon 事件了。
        """
        if self._carbon is not None:
            self._carbon.run_loop()

    def stop(self) -> None:
        """停止监听。

        Windows 上发一条 WM_QUIT 让消息循环收工；Mac 的 Carbon 只要把热键注销
        掉就不会再有事件进来（事件循环是外部主循环的，不能也不该停）。
        """
        if IS_WIN and self._thread is not None:
            try:
                tid = getattr(self._thread, "native_id", None)
                if tid:
                    ctypes.windll.user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
            except Exception:  # noqa: BLE001
                pass
        if self._carbon is not None:
            self._carbon.unregister_all()
            self._carbon = None
        self._using_carbon = False
        self._thread = None

    # -- 内部 ---------------------------------------------------------

    def _fire(self, code: int, mods: frozenset) -> bool:
        """命中就调回调，返回 True 表示这个按键要被吞掉。"""
        with self._lock:
            items = list(self._bindings.items())
        for _id, (combo, cb) in items:
            if combo.mods != mods:
                continue
            want = _mac_keycode(combo.key) if IS_MAC else _win_vk(combo.key)
            if want is None or want != code:
                continue
            try:
                cb()
            except Exception:  # noqa: BLE001  回调出错不能连累监听线程
                pass
            return True
        return False

    def _run(self) -> None:
        if IS_MAC:
            _mac_event_tap_loop(self._fire)
        elif IS_WIN:
            _win_message_loop(self._snapshot, self._fire)

    def _snapshot(self):
        with self._lock:
            return list(self._bindings.items())


# ---------------------------------------------------------------- macOS 后端


class _EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


_EVENT_HANDLER_PROC = ctypes.CFUNCTYPE(
    ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
)

# Carbon 修饰键常量（Events.h）
_CARBON_MOD_BITS = {"cmd": 0x0100, "shift": 0x0200, "alt": 0x0800, "ctrl": 0x1000}
_K_EVENT_CLASS_KEYBOARD = 0x6B657962  # 'keyb'
_K_EVENT_HOTKEY_PRESSED = 5
_K_EVENT_PARAM_DIRECT_OBJECT = 0x2D2D2D2D  # '----'
_TYPE_EVENT_HOTKEY_ID = 0x686B6964         # 'hkid'
_HOTKEY_SIGNATURE = 0x6B766D6B             # 'kvmk'


class _CarbonBackend:
    """RegisterEventHotKey —— 不需要「辅助功能」权限的全局热键。

    为什么不用 CGEventTap：后者必须让用户在 系统设置 → 辅助功能 里把本程序
    勾上；而 ad-hoc（--sign -）签名的 app 每次重新打包 cdhash 都会变，TCC 里
    那条授权随即失配 —— 每构建一次用户就得重新勾一次。Carbon 这套 API 走系统
    热键注册表，不受 TCC 约束，实测（macOS 27）注册返回 0、按键能命中。
    """

    def __init__(self):
        self._c = None
        self._cbs: dict[int, object] = {}   # hotkey id -> 回调
        self._hotkey_refs: list[object] = []
        self._handler_ref = None
        self._proc = None                   # 保持 CFUNCTYPE 存活，防 GC
        try:
            c = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
            c.GetEventDispatcherTarget.restype = ctypes.c_void_p
            c.InstallEventHandler.restype = ctypes.c_int32
            c.InstallEventHandler.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                ctypes.POINTER(_EventTypeSpec), ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            c.RegisterEventHotKey.restype = ctypes.c_int32
            c.RegisterEventHotKey.argtypes = [
                ctypes.c_uint32, ctypes.c_uint32, _EventHotKeyID,
                ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p),
            ]
            c.GetEventParameter.restype = ctypes.c_int32
            c.GetEventParameter.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
            ]
            self._c = c
        except Exception:  # noqa: BLE001
            self._c = None

    @property
    def available(self) -> bool:
        return self._c is not None

    def register(self, hid: int, combo: Combo, callback) -> bool:
        """注册一个热键。必须在主线程调用（Carbon Event Manager 是主线程绑定的）。"""
        c = self._c
        if c is None:
            return False
        code = _mac_keycode(combo.key)
        if code is None:
            return False
        mods = 0
        for m in combo.mods:
            mods |= _CARBON_MOD_BITS.get(m, 0)
        if not mods:
            return False
        if not self._install_handler():
            return False

        target = c.GetEventDispatcherTarget()
        href = ctypes.c_void_p()
        if c.RegisterEventHotKey(
            code, mods, _EventHotKeyID(_HOTKEY_SIGNATURE, hid), target, 0,
            ctypes.byref(href),
        ) != 0:
            return False
        self._hotkey_refs.append(href)
        self._cbs[hid] = callback
        return True

    def _install_handler(self) -> bool:
        """只装一个 handler，事件到了再按热键 id 分发。

        不能给每个热键各装一个 handler：InstallEventHandler 是按**事件类型**
        注册的，不是按热键 id —— 装两个的话，按下任意一个热键两个回调都会跑
        （实测按 ⌃⌥1 会同时触发「切到本机」和「切到对端」）。
        """
        c = self._c
        if c is None:
            return False
        if self._handler_ref is not None:
            return True
        proc = _EVENT_HANDLER_PROC(self._dispatch)
        spec = _EventTypeSpec(_K_EVENT_CLASS_KEYBOARD, _K_EVENT_HOTKEY_PRESSED)
        eref = ctypes.c_void_p()
        if c.InstallEventHandler(
            c.GetEventDispatcherTarget(), ctypes.cast(proc, ctypes.c_void_p), 1,
            ctypes.byref(spec), None, ctypes.byref(eref),
        ) != 0:
            return False
        self._proc = proc
        self._handler_ref = eref
        return True

    def _dispatch(self, call_ref, event, user_data) -> int:
        c = self._c
        try:
            hkid = _EventHotKeyID()
            if c.GetEventParameter(
                event, _K_EVENT_PARAM_DIRECT_OBJECT, _TYPE_EVENT_HOTKEY_ID,
                None, ctypes.sizeof(hkid), None, ctypes.byref(hkid),
            ) == 0:
                cb = self._cbs.get(int(hkid.id))
                if cb is not None:
                    cb()
                    return 0  # noErr
        except Exception:  # noqa: BLE001  ctypes 回调里抛异常会直接崩进程
            pass
        return 1  # eventNotHandledErr

    def unregister_all(self) -> None:
        c = self._c
        if c is None:
            return
        for href in self._hotkey_refs:
            try:
                c.UnregisterEventHotKey(href)
            except Exception:  # noqa: BLE001
                pass
        if self._handler_ref is not None:
            try:
                c.RemoveEventHandler(self._handler_ref)
            except Exception:  # noqa: BLE001
                pass
        self._hotkey_refs.clear()
        self._cbs.clear()
        self._handler_ref = None
        self._proc = None

    def run_loop(self) -> None:
        """没有 Cocoa 主循环时（headless），自己跑一个 Carbon 事件循环。"""
        if self._c is not None:
            self._c.RunApplicationEventLoop()


_PROMPTED = False  # 系统授权弹窗只主动弹一次，之后不再打扰


def _check_mac_permission(prompt: bool = False) -> tuple[bool, str]:
    """CGEventTap 要「辅助功能」权限。

    prompt=True 时如果还没授权，系统会自己弹一个引导框，用户勾完即可。
    优先走 pyobjc（打包的 app 里本来就有），拿不到再退回 ctypes。
    """
    trusted = None
    if prompt:
        try:
            from ApplicationServices import AXIsProcessTrustedWithOptions
            from Foundation import NSDictionary, NSNumber

            opts = NSDictionary.dictionaryWithObject_forKey_(
                NSNumber.numberWithBool_(True), "AXTrustedCheckOptionPrompt"
            )
            trusted = bool(AXIsProcessTrustedWithOptions(opts))
        except Exception:  # noqa: BLE001
            trusted = None
    if trusted is None:
        try:
            lib = ctypes.CDLL(
                "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
            )
            lib.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
            lib.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
            trusted = bool(lib.AXIsProcessTrustedWithOptions(None))
        except Exception:  # noqa: BLE001
            return True, ""  # 查不到就先试，tap 创建失败会兜住
    if trusted:
        return True, ""
    return False, (
        "macOS 没给本程序「辅助功能」权限，全局热键用不了。\n"
        "系统设置 → 隐私与安全性 → 辅助功能 → 把 Display KVM 勾上，然后重启本程序。"
    )


def _mac_event_tap_loop(fire) -> None:
    CG = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

    CG.CGEventTapCreate.restype = ctypes.c_void_p
    CG.CGEventTapCreate.argtypes = [
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_uint64, ctypes.c_void_p, ctypes.c_void_p,
    ]
    CG.CGEventGetFlags.restype = ctypes.c_uint64
    CG.CGEventGetFlags.argtypes = [ctypes.c_void_p]
    CG.CGEventGetIntegerValueField.restype = ctypes.c_longlong
    CG.CGEventGetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    CG.CGEventTapEnable.argtypes = [ctypes.c_void_p, ctypes.c_bool]

    TAP_CALLBACK = ctypes.CFUNCTYPE(
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p
    )

    CG_EVENT_KEY_DOWN = 1 << 10          # kCGEventKeyDown
    KC_KEYCODE = 9                        # kCGKeyboardEventKeycode
    DISABLED_TIMEOUT = 0xFFFFFFFE         # kCGEventTapDisabledByTimeout
    DISABLED_USER_INPUT = 0xFFFFFFFF      # kCGEventTapDisabledByUserInput

    holder = [None]

    def _tap_cb(proxy, etype, event, refcon):
        try:
            if etype in (DISABLED_TIMEOUT, DISABLED_USER_INPUT):
                if holder[0]:
                    CG.CGEventTapEnable(holder[0], True)
                return event
            if not event:
                return event
            code = CG.CGEventGetIntegerValueField(event, KC_KEYCODE)
            flags = CG.CGEventGetFlags(event)
            mods = frozenset(m for m, bit in _MAC_MOD_BITS.items() if flags & bit)
            if fire(code, mods):
                return None  # 吞掉，别再传给前台程序
        except Exception:  # noqa: BLE001  ctypes 回调抛异常会直接崩进程
            pass
        return event

    cb = TAP_CALLBACK(_tap_cb)
    # kCGSessionEventTap=1, 插到队首=0, kCGEventTapOptionDefault=0（要能吞事件）
    tap = CG.CGEventTapCreate(1, 0, 0, CG_EVENT_KEY_DOWN, cb, None)
    if not tap:
        return
    holder[0] = tap

    CF.CFMachPortCreateRunLoopSource.restype = ctypes.c_void_p
    CF.CFMachPortCreateRunLoopSource.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
    ]
    CF.CFRunLoopGetCurrent.restype = ctypes.c_void_p
    CF.CFRunLoopGetMain.restype = ctypes.c_void_p
    CF.CFRunLoopAddSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    CF.CFRunLoopRun.restype = None

    try:
        common_modes = ctypes.c_void_p.in_dll(CF, "kCFRunLoopCommonModes")
    except Exception:  # noqa: BLE001
        common_modes = None

    loop = CF.CFRunLoopGetCurrent()
    source = CF.CFMachPortCreateRunLoopSource(None, tap, 0, None)
    CF.CFRunLoopAddSource(loop, source, common_modes)
    CF.CFRunLoopRun()   # 阻塞到这里，直到进程退出


# ---------------------------------------------------------------- Windows 后端

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_NOREPEAT = 0x4000


def _win_message_loop(snapshot, fire) -> None:
    user32 = ctypes.windll.user32
    RegisterHotKey = user32.RegisterHotKey
    RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    RegisterHotKey.restype = wintypes.BOOL
    UnregisterHotKey = user32.UnregisterHotKey
    UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    GetMessageW = user32.GetMessageW
    GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
    ]

    registered = []
    for hid, (combo, _cb) in snapshot():
        vk = _win_vk(combo.key)
        if vk is None:
            continue
        mods = MOD_NOREPEAT
        for m in combo.mods:
            mods |= _WIN_MOD_BITS.get(m, 0)
        if RegisterHotKey(None, hid, mods, vk):
            registered.append(hid)

    msg = wintypes.MSG()
    while GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY:
            for hid, (_combo, cb) in snapshot():
                if hid == msg.wParam:
                    try:
                        cb()
                    except Exception:  # noqa: BLE001
                        pass
                    break
        elif msg.message == WM_QUIT:
            break

    for hid in registered:
        UnregisterHotKey(None, hid)
