# -*- mode: python ; coding: utf-8 -*-
"""display-kvm macOS 打包配置（.app bundle）。

只能在 macOS 上构建：
    pyinstaller packaging/display-kvm-mac.spec --noconfirm
产物：dist/Display KVM.app
"""

import os
from pathlib import Path

HERE = Path(SPECPATH).parent  # noqa: F821

datas = [(str(HERE / "packaging" / "assets" / "icon.png"), "assets")]

# 构建脚本会把 m1ddc 二进制拷到 packaging/m1ddc，存在则一并打包
m1ddc = HERE / "packaging" / "m1ddc"
if m1ddc.exists():
    datas.append((str(m1ddc), "bin"))

hiddenimports = [
    # 本地包：agent.py 是在 main() 里才 import 的，kvm_platform 也是懒加载，
    # 显式列出来避免依赖 PyInstaller 的字节码扫描碰运气。
    "kvm_app",
    "kvm_app.entry",
    "kvm_app.tray",
    "kvm_core",
    "kvm_platform",
    "hotkeys",
    "settings",
    "pystray._darwin",
    # app / tray / settings 里是函数内懒加载，PyInstaller 扫不到
    "PyObjCTools.AppHelper",
    "AppKit",
    "Foundation",
    "objc",
]

a = Analysis(
    [str(HERE / "app.py")],
    pathex=[str(HERE)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc_data", "xmlrpc", "sqlite3", "test"],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="display-kvm",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # 配合 LSUIElement：无 Dock 图标、无控制台
    icon=str(HERE / "packaging" / "assets" / "icon.icns"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Display KVM",
)

app = BUNDLE(
    coll,
    name="Display KVM.app",
    icon=str(HERE / "packaging" / "assets" / "icon.icns"),
    info_plist={
        "CFBundleName": "Display KVM",
        "CFBundleDisplayName": "Display KVM",
        "CFBundleIdentifier": "cn.displaykvm.agent",
        "CFBundleShortVersionString": "1.0.12",
        "CFBundleVersion": "1.0.12",
        "NSHighResolutionCapable": True,
        # 菜单栏应用：不占 Dock、不弹窗口
        "LSUIElement": True,
        "NSHumanReadableCopyright": "MIT License",
    },
)
