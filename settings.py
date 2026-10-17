"""参数设置窗口 —— 把 kvm.json 里的旋钮变成看得见的控件。

为什么自己写而不用 tkinter：
  * macOS 上打包用的 Python 不一定带 Tcl/Tk，tkinter 会直接 ImportError；
  * 程序本身已经在跑 AppKit 的事件循环（pystray 的 macOS 后端），
    再起一套 Tk 主循环容易打架。

所以：
  * macOS   -> 原生 AppKit 模态窗口（一个窗口填完所有参数，runModalForWindow_ 阻塞到关闭，
               并由 delegate 保证红叉也结束模态会话，可反复打开）
  * Windows -> PowerShell 的 WinForms 表单（同样是单窗口多字段，同步返回）
  * 原生窗口都弹不出 -> 由调用方打开配置文件兜底，绝不再退化成「每个参数弹一次」。

调用方：open_settings(cfg, fallback=...)。
  * 返回「被改动的字段」字典（取消 / 无改动返回 None）；
  * 原生窗口弹不出来则返回 FALLBACK，并触发 fallback（打开配置文件）。

注意：macOS 上必须在**主线程**调用（和所有 AppKit 调用一样，pystray 的菜单动作即主线程）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field as dc_field
from pathlib import Path

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"


@dataclass
class Field:
    key: str
    title: str
    hint: str
    kind: str = "float"  # float | bool
    lo: float = 0.0
    hi: float = 999.0


# 顺序 = 窗口里的显示顺序。说明文字刻意写成"人话"：参数名本身
# （arm_after_idle_seconds 这种）对使用者没有任何信息量。
FIELDS: list[Field] = [
    Field(
        "auto_switch",
        "启用自动切换",
        "碰哪台机器的键鼠，画面就自动切到哪台",
        "bool",
    ),
    Field(
        "cooldown_seconds",
        "切换后的保护时间（秒）",
        "刚切完画面的这段时间内，碰鼠标键盘都不会再触发自动切换。"
        "托盘菜单和快捷键的手工切换不受此限制",
        "float",
        0.0,
        120.0,
    ),
    Field(
        "arm_after_idle_seconds",
        "离开多久后才允许被唤醒（秒）",
        "画面在对端时，本机键鼠要空闲超过这么久，才会进入待命状态。"
        "调大它可以避免走过碰一下桌子就被切走",
        "float",
        0.5,
        300.0,
    ),
    Field(
        "fire_below_idle_seconds",
        "判定为「有操作」的阈值（秒）",
        "待命状态下，键鼠空闲时间低于这个值就认为你动了它，立刻切过来",
        "float",
        0.05,
        5.0,
    ),
    Field(
        "role_poll_seconds",
        "状态检测周期（秒）",
        "每隔多久查一次画面现在在谁那里。越小越灵敏，但 DDC 查询也越频繁",
        "float",
        0.5,
        30.0,
    ),
    Field(
        "peer_timeout_seconds",
        "对端失联判定（秒）",
        "超过这么久收不到对端心跳，就认为它下线了",
        "float",
        3.0,
        300.0,
    ),
    Field(
        "dry_run",
        "演练模式（只写日志，不真的切）",
        "排错时开：所有切换只记进日志，显示器不动",
        "bool",
    ),
]


def fmt(v) -> str:
    if isinstance(v, bool):
        return "开" if v else "关"
    if isinstance(v, (int, float)):
        return "%g" % v
    return str(v)


def _coerce(f: Field, raw) -> object | None:
    """把用户填的东西转成配置该有的类型。非法值返回 None（调用方跳过）。"""
    if f.kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            s = raw.strip().lower()
            if s in ("1", "true", "yes", "on", "开", "是"):
                return True
            if s in ("0", "false", "no", "off", "关", "否"):
                return False
        return None
    try:
        v = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if v < f.lo:
        v = f.lo
    if v > f.hi:
        v = f.hi
    return v


def summarize(changed: dict) -> str:
    """把刚改的字段变成一句人话，用于通知和日志。"""
    titles = {f.key: f.title for f in FIELDS}
    parts = []
    for k, v in changed.items():
        if isinstance(v, bool):
            shown = "开" if v else "关"
        else:
            shown = fmt(v)
        parts.append("%s %s" % (titles.get(k, k), shown))
    return "、".join(parts)


def _diff(cfg: dict, answers: dict) -> dict:
    """只保留真正变了的字段——省得把没动的也写回一遍配置。"""
    out = {}
    for f in FIELDS:
        if f.key not in answers:
            continue
        v = _coerce(f, answers[f.key])
        if v is None:
            continue
        old = cfg.get(f.key)
        if f.kind == "bool":
            if bool(old) != bool(v):
                out[f.key] = bool(v)
        elif float(old) != float(v):
            out[f.key] = v
    return out


FALLBACK = "__fallback__"  # 原生窗口弹不出时返回这个，调用方据此打开配置文件

# 非模态窗口单例：防止「点一次后打不开第二次」，并把已开窗口提到最前。
#
# 必须**持有** win/ctl 的 Python 引用（不能退化成只记 bool 标志）：
#   PyObjC 的 NSObject 子类实例若只被 ObjC 侧（按钮 target）持有、Python 侧没有
#   引用，它的 Python __dict__ 会被 GC 回收，导致 ctl._win / ctl._read 丢失，
#   点「保存/取消」时 close() 压根不会执行 -> **窗口不消失**（实测现象）。
#
# 但绝不能在 windowWillClose_ 里**同步**清除这些引用：
#   那会让 Python 在 AppKit 关闭流程进行中释放 win，与 AppKit 竞争 -> 悬垂
#   -> 下一轮 run loop 的 autorelease pool drain 时 objc_release EXC_BAD_ACCESS
#   （即"保存后退出"崩溃）。
#
# 也不能用 weakref：PyObjC 对象不支持弱引用（实测 TypeError: cannot create
#   weak reference to 'NSWindow' object），一用就抛异常、被 except 吞掉后
#   退化成 FALLBACK（弹出文本编辑器）。
#
# 正确做法只有一条：**持有引用，并把清除推迟到窗口完全关闭后的下一轮 run loop**。
_OPEN = {"win": None, "ctl": None}


def open_settings(cfg: dict, fallback=None, on_save=None):
    """打开设置窗口（一个集中面板，填完点保存即时生效）。

    参数：
      * fallback —— 原生窗口弹不出来时调用（通常用来打开配置文件）。
      * on_save  —— 用户点「保存」且确有改动后回调，签名 on_save(changed: dict)。
                   传了 on_save 时，保存结果通过该回调异步交付，本函数返回 None
                   （macOS 非模态窗口本身就是异步的；Windows 会同步算好 diff 后调用）。

    返回：
      * dict     —— 被改动的字段（仅在不传 on_save 且为同步路径时）
      * None     —— 窗口已异步打开 / 用户取消 / 没改动
      * FALLBACK —— 原生窗口弹不出来（headless、显示服务异常等），已执行 fallback

    重要（崩溃修复）：macOS 曾经用 NSApp.runModalForWindow_ 在 pystray 的运行循环里
    再开一个嵌套模态会话，窗口对象会被 modal 会话的 autorelease pool 持有，又在
    _form_mac 返回时被 Python 包装器 release 一次 → 双重释放 → 保存后 EXC_BAD_ACCESS。
    现改为**非模态窗口 + 关闭回调**：窗口随普通按钮事件关闭，不存在嵌套模态，不再
    双重释放。窗口关闭时调用 NSApp.deactivate() 让状态栏菜单恢复正常（否则会复现
    v1.0.4 的「点一次后托盘打不开」）。
    """
    if IS_MAC:
        r = _form_mac(cfg, on_save)
        if r is FALLBACK:
            if callable(fallback):
                try:
                    fallback()
                except Exception:  # noqa: BLE001
                    pass
            return FALLBACK
        return r
    if IS_WIN:
        answers = _form_win(cfg)
        if answers is None and callable(fallback):
            try:
                fallback()
            except Exception:  # noqa: BLE001
                pass
            return FALLBACK
        if not answers:
            return None
        diff = _diff(cfg, answers) or None
        if callable(on_save) and diff:
            on_save(diff)
            return None
        return diff
    # 非 mac/win 平台：没有原生窗口，直接走 fallback
    if callable(fallback):
        try:
            fallback()
        except Exception:  # noqa: BLE001
            pass
        return FALLBACK
    return None


# ==========================================================================
# macOS：原生 AppKit 窗口
# ==========================================================================


def _form_mac(cfg: dict, on_save=None):
    """macOS 原生设置窗口（单窗口集中面板，**非模态**，关闭后通过回调交付结果）。

    为什么改成非模态：之前用 NSApp.runModalForWindow_ 在 pystray 的运行循环里再开
    一个嵌套模态会话，窗口对象会被 modal 会话的 autorelease pool 持有，又在
    _form_mac 返回时被 Python 包装器 release 一次 → 双重释放 → 点「保存」后
    EXC_BAD_ACCESS 闪退。非模态窗口随普通按钮事件关闭，不存在嵌套模态，不再双重释放。

    行为：
      * 已有一个打开的窗口时，只把它提到最前（避免打不开第二次 / 叠多个窗口）。
      * 点「保存」：先读值（关窗前，转为 Python 原生值），关闭窗口，
        再经 AppHelper.callAfter 把改动交给 on_save 回调（写配置 / 同步 agent / 刷新托盘）。
      * 点「取消」或红叉：关闭窗口，不调用 on_save。
      * 窗口关闭时调用 NSApp.deactivate() 让状态栏菜单恢复正常。

    返回：FALLBACK（窗口建不出来）/ None（已异步打开，结果走 on_save）。
    """
    try:
        import objc
        from AppKit import (
            NSApp,
            NSBackingStoreBuffered,
            NSButton,
            NSClosableWindowMask,
            NSColor,
            NSFont,
            NSSwitchButton,
            NSTextField,
            NSTitledWindowMask,
            NSView,
            NSWindow,
        )
        from Foundation import NSObject, NSMakeRect
    except Exception:  # noqa: BLE001
        return FALLBACK

    # 设置窗口**只创建一次，之后永久复用**：点关闭只是隐藏，再点设置就刷新
    # 控件值后重新显示。这样 Python 侧永远不释放 NSWindow / ctl，也就彻底
    # 不存在"Python 与 AppKit 抢着释放"导致的 EXC_BAD_ACCESS（"保存后退出"）。
    existing = _OPEN["win"]
    if existing is not None:
        try:
            refresh = _OPEN.get("refresh")
            if callable(refresh):
                refresh()
            # 换上本次的保存回调（里头捕获的是当前 cfg/icon/agent）
            ctl0 = _OPEN.get("ctl")
            if ctl0 is not None:
                ctl0._on_save = on_save
                ctl0._answers = None
            NSApp.activateIgnoringOtherApps_(True)
            existing.makeKeyAndOrderFront_(None)
        except Exception:  # noqa: BLE001
            return FALLBACK
        return None

    try:
        row_h = 62
        W = 560
        H = 96 + row_h * len(FIELDS)
        top = H - 52  # 第一行控件的 y（AppKit 的坐标原点在左下）

        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H),
            NSTitledWindowMask | NSClosableWindowMask,
            NSBackingStoreBuffered,
            False,
        )
        # 【关键】NSWindow 的 releasedWhenClosed 默认就是 YES —— 窗口一关闭，
        # AppKit 会**自己把它 release 掉**。而 Python 侧（_OPEN / ctl._win）还
        # 握着同一个对象的引用，于是在后续清理里 Python 释放 __dict__ 时会去
        # objc_release 一个已被 AppKit 释放的地址 -> EXC_BAD_ACCESS（表现为
        # "点保存/取消后程序退出"，崩溃栈落在 AppHelper.callAfter 回调里）。
        # 关闭这个默认行为后，窗口的生命周期**完全由 Python 引用计数**决定，
        # AppKit 不再插手释放，双重释放从根上消失。
        win.setReleasedWhenClosed_(False)
        win.setTitle_("自动切换参数设置")
        win.center()

        root = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
        win.setContentView_(root)

        controls: dict = {}

        def add_label(text, x, y, w, h, size, bold):
            lb = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
            lb.setStringValue_(text)
            lb.setBezeled_(False)
            lb.setDrawsBackground_(False)
            lb.setEditable_(False)
            lb.setSelectable_(False)
            lb.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
            root.addSubview_(lb)
            return lb

        for i, f in enumerate(FIELDS):
            y = top - i * row_h
            add_label(f.title, 24, y - 18, 400, 20, 13.0, True)
            hint = NSTextField.alloc().initWithFrame_(NSMakeRect(24, y - 42, 400, 34))
            hint.setStringValue_(f.hint)
            hint.setBezeled_(False)
            hint.setDrawsBackground_(False)
            hint.setEditable_(False)
            hint.setSelectable_(False)
            hint.setFont_(NSFont.systemFontOfSize_(11.0))
            hint.setTextColor_(NSColor.secondaryLabelColor())
            hint.cell().setWraps_(True)  # 说明文字较长，允许折行
            root.addSubview_(hint)

            if f.kind == "bool":
                sw = NSButton.alloc().initWithFrame_(NSMakeRect(500, y - 22, 40, 22))
                sw.setButtonType_(NSSwitchButton)
                sw.setTitle_("")
                sw.setState_(1 if cfg.get(f.key) else 0)
                root.addSubview_(sw)
                controls[f.key] = sw
            else:
                tf = NSTextField.alloc().initWithFrame_(NSMakeRect(455, y - 22, 85, 24))
                tf.setStringValue_(fmt(cfg.get(f.key, "")))
                tf.setEditable_(True)
                root.addSubview_(tf)
                controls[f.key] = tf

        def read_answers():
            # 必须在窗口关闭【之前】调用：close() 会把窗口连同子控件一起释放，
            # 之后再去读 c.state()/c.stringValue() 会访问已释放的 ObjC 对象。
            #
            # 【重要】数值字段必须还原成 float/int，不能一律 str()。早期版本
            # 统一 str() 写回，kvm.json 里于是出现 "3" / "0.45" 这类字符串，
            # 主循环执行 `idle >= cfg["arm_after_idle_seconds"]` 时抛
            #   TypeError: '>=' not supported between instances of 'float' and 'str'
            # 异常被主循环 except 吞掉 —— 表现为**自动切换彻底失效**，且日志
            # 每秒刷两行错误（实测 30 分钟 7000+ 行）。这里按字段类型写回。
            ans = {}
            for f in FIELDS:
                c = controls[f.key]
                if f.kind == "bool":
                    ans[f.key] = bool(c.state())
                    continue
                raw = str(c.stringValue()).strip()
                old = cfg.get(f.key)
                try:
                    v = float(raw)
                except (TypeError, ValueError):
                    # 用户填了非数字：保留原值，绝不把脏数据写进配置
                    ans[f.key] = old
                    continue
                # 原本是整数字段（如 tick_ms）就还写成整数，其余写成浮点
                ans[f.key] = int(v) if isinstance(old, int) and not isinstance(old, bool) else v
            return ans

        def apply_values():
            # 窗口复用时把 cfg 的当前值刷回控件（用户可能在别处改过配置）。
            for f in FIELDS:
                c = controls[f.key]
                v = cfg.get(f.key)
                if f.kind == "bool":
                    c.setState_(1 if v else 0)
                else:
                    c.setStringValue_(fmt(v if v is not None else ""))

        class _Ctl(NSObject):
            # 持有窗口引用用于关窗。这份引用随 ctl 一起存在，而 ctl 被按钮
            # target retain，所以它是在**关闭流程结束之后**才随 ctl 释放的，
            # 不会与 AppKit 抢着释放窗口。
            # 切勿在 windowWillClose_ 里把它置 None——那正是"保存后崩溃"的根因。
            def ok_(self, sender):  # noqa: ANN001
                self._answers = self._read()  # 关窗前先把值读成原生类型
                w = self._win
                if w is not None:
                    w.close()

            def cancel_(self, sender):  # noqa: ANN001
                self._answers = None
                w = self._win
                if w is not None:
                    w.close()

            def windowShouldClose_(self, sender):  # noqa: ANN001
                return True

            def windowWillClose_(self, notification):  # noqa: ANN001
                # 这里**绝不**同步清除 _OPEN：那会与 AppKit 抢着释放 win 而崩溃。
                # 只记下要交付的结果，把「清引用 + 恢复菜单 + 保存回调」整体推迟。
                cb = self._on_save
                ans = self._answers

                def do_close():
                    # 【刻意不清理 _OPEN】窗口和 ctl 一旦创建就常驻，永不释放：
                    # 任何一次 Python 侧释放都可能与 AppKit 的释放撞车，表现为
                    # "点保存/取消后程序退出"（EXC_BAD_ACCESS）。既然窗口要复用，
                    # 就干脆一次都不释放 —— 没有释放，就没有双重释放。
                    # delegate 也因此永远有效，不存在悬垂。
                    try:
                        NSApp.deactivate()
                    except Exception:  # noqa: BLE001
                        pass
                    if callable(cb) and ans is not None:
                        try:
                            cb(ans)
                        except Exception:  # noqa: BLE001
                            pass

                try:
                    from PyObjCTools import AppHelper

                    AppHelper.callAfter(do_close)
                except Exception:  # noqa: BLE001
                    do_close()

        ctl = _Ctl.alloc().init()
        # 持有窗口以便关窗（释放时机见 _Ctl 注释：随 ctl 释放，不在关闭流程中）
        ctl._win = win
        ctl._read = read_answers
        ctl._answers = None
        ctl._on_save = on_save

        btn_ok = NSButton.alloc().initWithFrame_(NSMakeRect(W - 190, 20, 80, 30))
        btn_ok.setTitle_("保存")
        btn_ok.setBezelStyle_(1)
        btn_ok.setTarget_(ctl)
        btn_ok.setAction_(objc.selector(ctl.ok_, selector=b"ok:"))
        root.addSubview_(btn_ok)

        btn_cancel = NSButton.alloc().initWithFrame_(NSMakeRect(W - 100, 20, 80, 30))
        btn_cancel.setTitle_("取消")
        btn_cancel.setBezelStyle_(1)
        btn_cancel.setTarget_(ctl)
        btn_cancel.setAction_(objc.selector(ctl.cancel_, selector=b"cancel:"))
        root.addSubview_(btn_cancel)

        win.setDelegate_(ctl)

        NSApp.activateIgnoringOtherApps_(True)
        win.makeKeyAndOrderFront_(None)
        # 永久持有：窗口/委托/刷新函数一旦建立就不再释放（复用设计，见 _OPEN 注释）。
        _OPEN["win"] = win
        _OPEN["ctl"] = ctl
        _OPEN["refresh"] = apply_values
        return None
    except Exception:  # noqa: BLE001
        return FALLBACK


# ==========================================================================
# Windows：PowerShell + WinForms 表单
# ==========================================================================

_PS_FORM = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$fields = Get-Content -Raw -Encoding utf8 -Path '%(json)s' | ConvertFrom-Json

$form = New-Object System.Windows.Forms.Form
$form.Text = '自动切换参数设置'
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.AutoScroll = $true
$form.Size = New-Object System.Drawing.Size(620, %(height)d)

$y = 16
$controls = @{}

foreach ($f in $fields) {
    $box = New-Object System.Windows.Forms.GroupBox
    $box.Location = New-Object System.Drawing.Point(14, $y)
    $box.Size = New-Object System.Drawing.Size(575, 62)
    $box.Text = $f.title
    $form.Controls.Add($box)

    $hint = New-Object System.Windows.Forms.Label
    $hint.Location = New-Object System.Drawing.Point(12, 20)
    $hint.Size = New-Object System.Drawing.Size(400, 34)
    $hint.Text = $f.hint
    $box.Controls.Add($hint)

    if ($f.kind -eq 'bool') {
        $cb = New-Object System.Windows.Forms.CheckBox
        $cb.Location = New-Object System.Drawing.Point(470, 24)
        $cb.Size = New-Object System.Drawing.Size(90, 22)
        $cb.Text = '启用'
        $cb.Checked = [bool]$f.value
        $box.Controls.Add($cb)
        $controls[$f.key] = $cb
    } else {
        $tb = New-Object System.Windows.Forms.TextBox
        $tb.Location = New-Object System.Drawing.Point(465, 24)
        $tb.Size = New-Object System.Drawing.Size(90, 22)
        $tb.Text = [string]$f.value
        $box.Controls.Add($tb)
        $controls[$f.key] = $tb
    }
    $y += 70
}

$ok = New-Object System.Windows.Forms.Button
$ok.Location = New-Object System.Drawing.Point(%(okx)d, %(btny)d)
$ok.Size = New-Object System.Drawing.Size(90, 30)
$ok.Text = '保存'
$ok.DialogResult = [System.Windows.Forms.DialogResult]::OK
$form.Controls.Add($ok)
$form.AcceptButton = $ok

$cancel = New-Object System.Windows.Forms.Button
$cancel.Location = New-Object System.Drawing.Point(%(cancelx)d, %(btny)d)
$cancel.Size = New-Object System.Drawing.Size(90, 30)
$cancel.Text = '取消'
$cancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
$form.Controls.Add($cancel)
$form.CancelButton = $cancel

$res = $form.ShowDialog()
if ($res -ne [System.Windows.Forms.DialogResult]::OK) {
    Write-Output '__CANCEL__'
    exit 0
}

foreach ($k in $controls.Keys) {
    $c = $controls[$k]
    if ($c -is [System.Windows.Forms.CheckBox]) {
        Write-Output ($k + '=' + $c.Checked.ToString().ToLower())
    } else {
        Write-Output ($k + '=' + $c.Text)
    }
}
"""


def _form_win(cfg: dict) -> dict | None:
    payload = [
        {
            "key": f.key,
            "title": f.title,
            "hint": f.hint,
            "kind": f.kind,
            "value": cfg.get(f.key, False if f.kind == "bool" else 0),
        }
        for f in FIELDS
    ]
    height = min(760, 130 + 70 * len(FIELDS))
    ps = _PS_FORM
    ps = ps.replace("%(height)d", str(height))
    ps = ps.replace("%(btny)d", str(height - 78))
    ps = ps.replace("%(okx)d", "400")
    ps = ps.replace("%(cancelx)d", "500")

    # 字段用临时文件传，避免中文和引号在命令行里被转义搞坏
    tmp = Path(tempfile.gettempdir()) / "display-kvm-settings.json"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8-sig")
    ps = ps.replace("'%(json)s'", "'%s'" % str(tmp).replace("\\", "\\\\").replace("'", "''"))

    script = Path(tempfile.gettempdir()) / "display-kvm-settings.ps1"
    script.write_text(ps, encoding="utf-8-sig")

    try:
        p = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
    except Exception:  # noqa: BLE001
        return None

    out = (p.stdout or "").strip()
    if not out or "__CANCEL__" in out:
        return None

    # 按字段类型还原。PowerShell 一律输出字符串，若直接写回配置，
    # 数值字段会变成 "3" 这种字符串，主循环拿它和 float 比较就抛
    # TypeError -> 自动切换彻底失效 + 日志每秒刷屏（详见 read_answers 注释）。
    kind_of = {f.key: f.kind for f in FIELDS}
    old_of = {f.key: cfg.get(f.key) for f in FIELDS}
    answers = {}
    for line in out.splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        kind = kind_of.get(k)
        if kind == "bool":
            answers[k] = v.lower() in ("1", "true", "yes", "on")
        elif kind == "float":
            old = old_of.get(k)
            try:
                fv = float(v)
            except (TypeError, ValueError):
                answers[k] = old  # 填了非数字：保留原值，不写脏数据
            else:
                answers[k] = (
                    int(fv) if isinstance(old, int) and not isinstance(old, bool) else fv
                )
        else:
            answers[k] = v
    return answers or None


# ==========================================================================
# （无兜底逐参数弹窗：原生窗口弹不出时由调用方打开配置文件，见 open_settings）
# ==========================================================================
