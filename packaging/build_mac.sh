#!/bin/bash
# display-kvm macOS 一键构建脚本 —— 在 Mac 上产出 .app 和 .dmg
#
# 用法：
#   bash packaging/build_mac.sh
#
# 前置条件：
#   * macOS 12+（Apple Silicon 走 m1ddc；Intel Mac 需把配置里的 m1ddc 换成 ddcctl）
#   * Xcode Command Line Tools:  xcode-select --install
#   * （可选）Homebrew：用于自动安装 m1ddc
#
# 产物：
#   dist/Display KVM.app
#   dist/Display-KVM-1.0.3.dmg

set -euo pipefail

VERSION="1.0.3"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

APP_NAME="Display KVM"
APP_BUNDLE="dist/${APP_NAME}.app"
DMG="dist/Display-KVM-${VERSION}.dmg"
PY=".venv-build/bin/python"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[警告]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[错误]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 环境检查

say "检查构建环境"

[ "$(uname)" = "Darwin" ] || die "这个脚本只能在 macOS 上运行（dmg 无法在 Windows/Linux 上交叉构建）。"

ARCH="$(uname -m)"
echo "架构: ${ARCH}"

# Apple Silicon 优先用 arm64 的 python3；Rosetta 下的 x86_64 python 也能跑，但产物不通用
if [ "$ARCH" = "arm64" ]; then
    if command -v /opt/homebrew/bin/python3 >/dev/null 2>&1; then
        PY_SYS="/opt/homebrew/bin/python3"
    fi
fi
PY_SYS="${PY_SYS:-python3}"

"$PY_SYS" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "需要 Python 3.10+，请先安装（brew install python）。"

# ---------------------------------------------------------------- 虚拟环境

say "准备构建虚拟环境"
[ -x "$PY" ] || "$PY_SYS" -m venv .venv-build
"$PY" -m pip install --upgrade pip -q
"$PY" -m pip install -q pyinstaller pystray pillow pyobjc-framework-Cocoa

# ---------------------------------------------------------------- m1ddc

say "准备 m1ddc（DDC 控制工具）"
if [ -x "packaging/m1ddc" ]; then
    echo "已存在 packaging/m1ddc，跳过下载"
elif command -v m1ddc >/dev/null 2>&1; then
    cp "$(command -v m1ddc)" packaging/m1ddc
    echo "已从系统复制 m1ddc"
elif command -v brew >/dev/null 2>&1; then
    echo "通过 Homebrew 安装 m1ddc ..."
    brew install m1ddc
    cp "$(command -v m1ddc)" packaging/m1ddc
else
    warn "找不到 m1ddc 且没有 Homebrew。"
    warn "程序仍会构建，但配置里的 DDC 命令将回退到 PATH 中的 m1ddc，"
    warn "请你自行安装：brew install m1ddc（推荐装好后再跑一次本脚本）"
fi

# ---------------------------------------------------------------- 构建 .app

say "PyInstaller 构建 .app"
"$PY" -m PyInstaller packaging/display-kvm-mac.spec --noconfirm --distpath dist --workpath build

# m1ddc 是外部拷入的二进制，确保可执行位
BUNDLED_M1DDC="${APP_BUNDLE}/Contents/Frameworks/bin/m1ddc"
[ -f "$BUNDLED_M1DDC" ] && chmod +x "$BUNDLED_M1DDC" && echo "已打包 m1ddc: $BUNDLED_M1DDC"

# ---------------------------------------------------------------- 签名

say "ad-hoc 签名（本地使用无需开发者账号）"
codesign --force --deep -s - "$APP_BUNDLE" 2>/dev/null \
    && echo "签名完成" \
    || warn "签名失败，不影响本机使用；拷给别人的机器可能被 Gatekeeper 拦（右键 -> 打开 可绕过）。"

# ---------------------------------------------------------------- 组装 dmg

say "组装 DMG 镜像"
STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT

cp -R "$APP_BUNDLE" "$STAGING/"
cp README.md "$STAGING/使用说明.md" 2>/dev/null || true
ln -s /Applications "$STAGING/Applications"   # 拖拽安装用的快捷方式

if command -v create-dmg >/dev/null 2>&1; then
    create-dmg \
        --volname "$APP_NAME" \
        --window-size 560 360 \
        --icon-size 110 \
        --icon "$APP_NAME.app" 145 205 \
        --app-drop-link 415 205 \
        --hide-extension "$APP_NAME.app" \
        "$DMG" "$STAGING" || true
fi

if [ ! -f "$DMG" ]; then
    echo "（未安装 create-dmg 或它失败了，改用系统自带的 hdiutil）"
    hdiutil create -volname "$APP_NAME" -srcfolder "$STAGING" -ov -format UDZO "$DMG"
fi

rm -rf build .venv-build 2>/dev/null || true

say "构建完成"
echo "  $APP_BUNDLE"
echo "  $DMG"
cat <<'EOF'

首次使用（重要）：
  1. 打开 dmg，把 Display KVM 拖进 Applications，双击运行。
     如果提示"无法验证开发者"：右键 -> 打开；或执行
        xattr -cr "/Applications/Display KVM.app"
  2. 菜单栏会出现显示器小图标，点开 -> 打开配置文件，
     把 token 改成与 Windows 端完全一致，确认两端输入源、
     dry_run=true 先跑几轮，日志确认无误后再改 false。
  3. 菜单栏图标 -> 退出，再重新打开，配置生效。
EOF
