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
OPENED = "__opened__"  # 已废弃（v1.0.5 起 macOS 走同步模态窗口，不再使用）


def open_settings(cfg: dict, fallback=None):
    """打开设置窗口（一个集中面板，填完点保存即时生效）。

    返回：
      * dict     —— 被改动的字段（用户保存了且确有改动）
      * None     —— 用户取消 / 没改动
      * FALLBACK —— 原生窗口弹不出来（headless、显示服务异常等），已执行 fallback

    macOS / Windows 都走原生同步窗口：窗口关闭后返回被改动的字段，
    调用方负责写配置、同步 agent、刷新托盘。弹不出来时才走 fallback，
    绝不再退化成「每个参数弹一次输入框」。

    macOS 用 NSApp.runModalForWindow_ 模态弹出，并给窗口加 delegate 保证红叉关闭也
    结束模态会话——否则红叉只关窗不结束模态，下一次再开会抛
    “may not be invoked inside of a modal session”，被 except 吞掉后回退成打开 JSON。
    """
    if IS_MAC:
        r = _form_mac(cfg)
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
        return _diff(cfg, answers) or None
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


def _form_mac(cfg: dict):
    """macOS 原生设置窗口（单窗口集中面板，模态弹出）。

    用 NSApp.runModalForWindow_ 以模态方式显示：菜单动作会一直阻塞到窗口关闭，
    天然不会「叠多个窗口」，也不会在 NSApp 上留下挂起的模态会话。

    关键修复：给窗口加 delegate，红叉关闭也走 windowWillClose_ -> stopModal，
    否则红叉只关窗不结束模态会话，下一次再开就会抛
    “may not be invoked inside of a modal session”，被 except 吞掉后回退成打开 JSON。

    返回：被改动的字段字典 / 取消或没改动返回 None / 窗口弹不出来返回 FALLBACK。
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
            ans = {}
            for f in FIELDS:
                c = controls[f.key]
                ans[f.key] = bool(c.state()) if f.kind == "bool" else c.stringValue()
            return ans

        class _Ctl(NSObject):
            def init(self):
                self = super().init()
                self._code = 0      # 1=保存 0=取消/关闭
                self._win = None
                return self

            def ok_(self, sender):  # noqa: ANN001
                self._code = 1
                self._win.close()

            def cancel_(self, sender):  # noqa: ANN001
                self._code = 0
                self._win.close()

            def windowShouldClose_(self, sender):  # noqa: ANN001
                return True

            def windowWillClose_(self, notification):  # noqa: ANN001
                # 无论是点「保存」「取消」还是红叉，窗口一关就结束模态会话，
                # 保证下一次还能再开（否则红叉只关窗不结束模态，下次再开会崩）。
                NSApp.stopModalWithCode_(self._code)

        ctl = _Ctl.alloc().init()
        ctl._win = win

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
        rc = NSApp.runModalForWindow_(win)
        win.orderOut_(None)

        if rc != 1:
            return None
        return _diff(cfg, read_answers()) or None
    except Exception:  # noqa: BLE001
        return FALLBACK


# ==========================================================================
# Windows：PowerShell + WinForms 表单
# ==========================================================================

_PS_FORM = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$fields = Get-Content -Raw -Path '%(json)s' | ConvertFrom-Json

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
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    ps = ps.replace("'%(json)s'", "'%s'" % str(tmp).replace("\\", "\\\\").replace("'", "''"))

    script = Path(tempfile.gettempdir()) / "display-kvm-settings.ps1"
    script.write_text(ps, encoding="utf-8")

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

    answers = {}
    for line in out.splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        answers[k.strip()] = v.strip()
    return answers or None


# ==========================================================================
# （无兜底逐参数弹窗：原生窗口弹不出时由调用方打开配置文件，见 open_settings）
# ==========================================================================
