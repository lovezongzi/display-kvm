# -*- mode: python ; coding: utf-8 -*-
"""display-kvm-ctl 控制客户端打包配置（控制台程序，绑热键用）。

    pyinstaller packaging/display-kvm-ctl.spec --noconfirm
产物：dist/display-kvm-ctl.exe
"""

from pathlib import Path

HERE = Path(SPECPATH).parent  # noqa: F821

a = Analysis(
    [str(HERE / "kvmctl.py")],
    pathex=[str(HERE)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    excludes=["tkinter", "unittest", "pydoc_data", "xmlrpc", "sqlite3", "test"],
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
    name="display-kvm-ctl",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,           # 控制台程序：热键触发时能看到回显
    icon=str(HERE / "packaging" / "assets" / "icon.ico"),
)
