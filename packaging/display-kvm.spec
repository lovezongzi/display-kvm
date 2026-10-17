# -*- mode: python ; coding: utf-8 -*-
"""display-kvm Windows 打包配置。

构建（在 display-kvm 目录下）：
    pyinstaller packaging/display-kvm.spec --noconfirm
产物：dist/display-kvm.exe
"""

import sys
from pathlib import Path

HERE = Path(SPECPATH).parent  # noqa: F821  PyInstaller 注入（packaging/ 的父级，即项目根）

hiddenimports = [
    # 本地包：agent.py 是在 main() 里才 import 的，kvm_platform 也是懒加载，
    # 显式列出来避免依赖 PyInstaller 的字节码扫描碰运气。
    "kvm_app",
    "kvm_app.entry",
    "kvm_app.tray",
    "kvm_core",
    "kvm_platform",
    "ddc_win",
    "hotkeys",
    "settings",
    "pystray._win32",
    "pystray._util",
    "PIL._tkinter_finder",
]

a = Analysis(
    [str(HERE / "app.py")],
    pathex=[str(HERE)],
    binaries=[],
    datas=[(str(HERE / "packaging" / "assets" / "icon.png"), "assets")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "unittest",
        "pydoc_data",
        "xmlrpc",
        "sqlite3",
        "test",
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="display-kvm",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # 无黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(HERE / "packaging" / "assets" / "icon.ico"),
    version=str(HERE / "packaging" / "version_info.txt"),
)
