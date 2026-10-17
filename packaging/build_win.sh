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
# CI 的 checkout 是浅的，git describe 拿不到 tag；用 GITHUB_REF_NAME 兜底，
# 让「tag 与 VERSION 不一致」在流水线上当场失败，而不是产出一个错版本号的包。
case "${GITHUB_REF_NAME:-}" in
    v*) TAG="$GITHUB_REF_NAME" ;;
esac
if [ -n "$TAG" ] && [ "v$VERSION" != "$TAG" ]; then
    die "版本不一致：git tag 是 ${TAG}，但 VERSION 文件写的是 ${VERSION}。"
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
# 两个文件都得带上版本号，又都不能从环境变量取值，所以在这里按 VERSION 就地重写：
#   version_info.txt  PyInstaller 的 --version-file（exe 属性里看到的东西）
#   installer.iss     Inno Setup 的 #define（安装包文件名与「应用和功能」里的版本）
# 以前这两处靠手工维护，一忘就是「tag 是新的、产物还是旧版本」。
# 顺带一提：别想着用 ISCC /DMyAppVersion= 注入 —— Git Bash 会把 /D... 当路径转换，
# 结果 ISCC 收到两个脚本名，报 "You may not specify more than one script filename"。
say "同步 Windows 版本资源 -> $VERSION"
"$VENV_PY" - "$VERSION" <<'PY'
import re
import sys

version = sys.argv[1]
nums = [int(p) for p in re.findall(r"\d+", version)][:4]
nums += [0] * (4 - len(nums))
fours = "(%d, %d, %d, %d)" % tuple(nums)


def rewrite(path, subs):
    # newline="" 是为了原样保留换行符（installer.iss 在 Windows 上可能是 CRLF）
    with open(path, encoding="utf-8", newline="") as f:
        s = f.read()
    for pattern, repl in subs:
        s = re.sub(pattern, repl, s)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(s)
    print("version synced:", path)


rewrite("packaging/version_info.txt", [
    (r"filevers=\([^)]*\)", "filevers=" + fours),
    (r"prodvers=\([^)]*\)", "prodvers=" + fours),
    (r'StringStruct\("FileVersion", "[^"]*"\)',
     'StringStruct("FileVersion", "%s")' % version),
    (r'StringStruct\("ProductVersion", "[^"]*"\)',
     'StringStruct("ProductVersion", "%s")' % version),
])

rewrite("packaging/installer.iss", [
    (r'(?m)^(#define MyAppVersion )"[^"]*"', r'\1"%s"' % version),
])
# 只能用 ASCII 打印：Windows runner 的控制台是 cp1252，
# print 中文会 UnicodeEncodeError 直接把构建打断。
print("done")
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
    "$ISCC" packaging/installer.iss
else
    warn "没找到 Inno Setup，跳过安装包（手动装：winget install JRSoftware.InnoSetup）"
fi

rm -rf build

say "构建完成"
ls -la dist/ dist/installer/ 2>/dev/null || ls -la dist/
