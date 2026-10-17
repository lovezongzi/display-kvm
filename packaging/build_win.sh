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
#   dist/installer/Display-KVM-Setup-<版本>.exe   安装包（若装了 Inno Setup）

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[警告]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[错误]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 版本号
# 单一来源：仓库根的 VERSION 文件（可用 KVM_VERSION 环境变量覆盖）。
# 以前这里是硬编码，发版时漏改就会打出「tag 是 1.0.13、安装包却叫 1.0.12」的产物。
VERSION="${KVM_VERSION:-$(tr -d '[:space:]' < VERSION 2>/dev/null || true)}"
[ -n "$VERSION" ] || die "读不到版本号：根目录的 VERSION 文件为空或不存在。"

TAG="$(git describe --tags --exact-match 2>/dev/null || true)"
if [ -n "$TAG" ] && [ "v$VERSION" != "$TAG" ]; then
    die "版本不一致：git tag 是 $TAG，但 VERSION 文件写的是 $VERSION。"
fi
export KVM_VERSION="$VERSION"
echo "版本: $VERSION"

# ---------------------------------------------------------------- 找 Python

say "查找 Python"
PY_SYS=""
# CI（GitHub Actions 的 setup-python）会把装好的版本路径放在 pythonLocation 里。
# 优先用它：runner 的 PATH 里往往还躺着一个更新的 Python（实测抓到过 3.14），
# 用它构建会给 PyInstaller 带来不必要的版本风险。
if [ -n "${pythonLocation:-}" ] && [ -x "${pythonLocation}/python.exe" ]; then
    PY_SYS="${pythonLocation}/python.exe"
fi
if [ -z "$PY_SYS" ]; then
    for c in py python python3; do
        if command -v "$c" >/dev/null 2>&1; then
            if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
                PY_SYS="$c"; break
            fi
        fi
    done
fi
[ -n "$PY_SYS" ] || die "找不到 Python 3.10+，请先安装：https://www.python.org/downloads/"
echo "使用 $($PY_SYS -V 2>&1)"

# ---------------------------------------------------------------- 虚拟环境

say "准备隔离构建环境 .venv-build"
if [ ! -d .venv-build ]; then
    "$PY_SYS" -m venv .venv-build
fi

# 注意顺序：必须**建完 venv 之后**再找解释器。
# 之前的写法先按 Windows 路径猜、猜不到就退化成 Unix 路径，而这个退化值在
# venv 建好后不会被重新计算 —— 于是永远指向不存在的 .venv-build/bin/python，
# CI 上直接 127 退出（venv 在 Windows 下是 Scripts/python.exe）。
VENV_PY=""
for p in ".venv-build/Scripts/python.exe" ".venv-build/Scripts/python" \
         ".venv-build/bin/python" ".venv-build/bin/python3"; do
    if [ -f "$p" ]; then VENV_PY="$p"; break; fi
done
[ -n "$VENV_PY" ] || die "虚拟环境创建失败：.venv-build 里找不到 python"
"$VENV_PY" -m pip install --upgrade pip -q
"$VENV_PY" -m pip install -q pyinstaller pystray pillow

# ---------------------------------------------------------------- 图标

say "生成图标"
"$VENV_PY" packaging/make_icons.py

# ---------------------------------------------------------------- 版本资源
# version_info.txt 是 PyInstaller 的 --version-file，版本号由这里按 VERSION 就地
# 重写一遍：它没法从环境变量取值，之前只能手工改，一忘就是「exe 属性里还是旧版本」。
say "同步 Windows 版本资源 -> $VERSION"
"$VENV_PY" - "$VERSION" <<'PY'
import pathlib
import re
import sys

version = sys.argv[1]
nums = [int(p) for p in re.findall(r"\d+", version)][:4]
nums += [0] * (4 - len(nums))
fours = "(%d, %d, %d, %d)" % tuple(nums)

p = pathlib.Path("packaging/version_info.txt")
s = p.read_text(encoding="utf-8")
s = re.sub(r"filevers=\([^)]*\)", "filevers=" + fours, s)
s = re.sub(r"prodvers=\([^)]*\)", "prodvers=" + fours, s)
s = re.sub(r'StringStruct\("FileVersion", "[^"]*"\)',
           'StringStruct("FileVersion", "%s")' % version, s)
s = re.sub(r'StringStruct\("ProductVersion", "[^"]*"\)',
           'StringStruct("ProductVersion", "%s")' % version, s)
p.write_text(s, encoding="utf-8")
# 只能用 ASCII 打印：Windows runner 的控制台是 cp1252，
# print 中文会 UnicodeEncodeError 直接把构建打断。
print("version resource synced:", p)
PY

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
    # 版本号从命令行注入，installer.iss 里只留 #ifndef 兜底，不必手工维护。
    "$ISCC" "/DMyAppVersion=$VERSION" packaging/installer.iss
else
    warn "没找到 Inno Setup，跳过安装包（手动装：winget install JRSoftware.InnoSetup）"
fi

rm -rf build

say "构建完成"
ls -la dist/ dist/installer/ 2>/dev/null || ls -la dist/
