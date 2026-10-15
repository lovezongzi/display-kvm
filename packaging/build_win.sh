#!/bin/bash
# display-kvm Windows 一键构建脚本 —— 产出 exe 和安装包
#
# 用法（在项目根目录或任意位置）：
#   bash packaging/build_win.sh
#
# 前置条件：
#   * Python 3.10+（脚本会自建隔离虚拟环境，不污染系统）
#   * 可选：Inno Setup 6（有则顺带产出安装包；没有只出 exe）
#       下载：https://jrsoftware.org/isdl.php
#       或：  winget install --id JRSoftware.InnoSetup -e
#
# 产物：
#   dist/display-kvm.exe          主程序（托盘常驻，无控制台窗口）
#   dist/display-kvm-ctl.exe      控制台客户端（绑热键用）
#   dist/installer/Display-KVM-Setup-1.0.0.exe   安装包（若装了 Inno Setup）

set -euo pipefail

VERSION="1.0.0"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[警告]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[错误]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 找 Python

say "查找 Python"
PY_SYS=""
for c in py python python3; do
    if command -v "$c" >/dev/null 2>&1; then
        if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
            PY_SYS="$c"; break
        fi
    fi
done
[ -n "$PY_SYS" ] || die "找不到 Python 3.10+，请先安装：https://www.python.org/downloads/"
echo "使用 $($PY_SYS -V 2>&1)"

# ---------------------------------------------------------------- 虚拟环境

say "准备隔离构建环境 .venv-build"
VENV_PY=".venv-build/Scripts/python.exe"     # Windows
[ -f "$VENV_PY" ] || VENV_PY=".venv-build/bin/python"   # Git Bash 下的其它情况

if [ ! -f "$VENV_PY" ]; then
    "$PY_SYS" -m venv .venv-build
fi
"$VENV_PY" -m pip install --upgrade pip -q
"$VENV_PY" -m pip install -q pyinstaller pystray pillow

# ---------------------------------------------------------------- 图标

say "生成图标"
"$VENV_PY" packaging/make_icons.py

# ---------------------------------------------------------------- exe

say "PyInstaller 构建 exe"
"$VENV_PY" -m PyInstaller packaging/display-kvm.spec     --noconfirm --distpath dist --workpath build
"$VENV_PY" -m PyInstaller packaging/display-kvm-ctl.spec --noconfirm --distpath dist --workpath build

# ---------------------------------------------------------------- 安装包

say "查找 Inno Setup"
ISCC=""
for c in \
    "/c/Program Files (x86)/Inno Setup 6/ISCC.exe" \
    "/c/Program Files/Inno Setup 6/ISCC.exe" \
    "$LOCALAPPDATA/Programs/Inno Setup 6/ISCC.exe" \
    "$(command -v iscc 2>/dev/null || true)"; do
    if [ -n "$c" ] && [ -f "$c" ]; then ISCC="$c"; break; fi
done

if [ -z "$ISCC" ] && command -v winget >/dev/null 2>&1; then
    echo "未找到，尝试用 winget 安装 Inno Setup ..."
    winget install --id JRSoftware.InnoSetup -e --silent \
        --accept-source-agreements --accept-package-agreements >/dev/null 2>&1 || true
    for c in "/c/Program Files (x86)/Inno Setup 6/ISCC.exe" \
             "$LOCALAPPDATA/Programs/Inno Setup 6/ISCC.exe"; do
        [ -f "$c" ] && ISCC="$c" && break
    done
fi

if [ -n "$ISCC" ]; then
    say "编译安装包"
    "$ISCC" packaging/installer.iss
else
    warn "没找到 Inno Setup，跳过安装包（手动装：winget install JRSoftware.InnoSetup）"
fi

rm -rf build

say "构建完成"
ls -la dist/ dist/installer/ 2>/dev/null || ls -la dist/
