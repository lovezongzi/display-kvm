#!/usr/bin/env bash
# display-kvm 本机自测
#
# 在同一台机器上起两到三个 agent 实例，验证 网络发现 / 委托切换 / ACK / 控制口
# 这几条链路是否通顺。全程 DRY-RUN，不会真的切换显示器。
#
# 用法：
#   bash selftest/run.sh
#   PY=/usr/bin/python3 bash selftest/run.sh
#
# 注意：Windows 上请在 Git Bash 里跑。

set -u
cd "$(dirname "$0")/.." || exit 1

PY="${PY:-python3}"
PIDS=()

cleanup() {
    for p in "${PIDS[@]:-}"; do
        kill "$p" >/dev/null 2>&1
    done
    wait >/dev/null 2>&1
}
trap cleanup EXIT

kill_ports() {
    command -v netstat >/dev/null 2>&1 || return 0
    for port in "$@"; do
        # 只取真正在监听的 TCP 行和 UDP 行。
        # 必须排除 TIME_WAIT / SYN_SENT 以及 PID 0 —— 否则 taskkill /PID 0
        # 会命中系统空闲进程，直接把当前会话干掉。
        pids=$(netstat -ano 2>/dev/null \
            | grep -E "[:.]${port}[[:space:]]" \
            | grep -vE "TIME_WAIT|SYN_SENT|CLOSE_WAIT|FIN_WAIT" \
            | awk '{print $NF}' \
            | grep -E '^[0-9]+$' \
            | grep -v '^0$' \
            | sort -u)
        for pid in $pids; do
            [ -z "$pid" ] && continue
            taskkill //F //PID "$pid" >/dev/null 2>&1 || kill -9 "$pid" >/dev/null 2>&1
        done
    done
}

echo "清理可能残留的实例 ..."
kill_ports 47821 47822 47823 47824 47825 47826
sleep 1

rm -f selftest/*.log

echo "启动 Windows 端实例（应为 active）..."
"$PY" agent.py --config selftest/win.json >selftest/win.log 2>&1 &
PIDS+=($!)

echo "启动模拟 Mac 端实例（应为 idle）..."
"$PY" agent.py --config selftest/mac.json --platform mac >selftest/mac.log 2>&1 &
PIDS+=($!)

echo "启动模拟「不在显示器上」的实例，用于验证对端同样 idle 的分支..."
"$PY" agent.py --config selftest/win-idle.json >selftest/idle.log 2>&1 &
PIDS+=($!)

sleep 4

echo
echo "==================== 1. 控制口连通性 ===================="
"$PY" kvmctl.py --port 47822 status
echo
"$PY" kvmctl.py --port 47824 status
echo
"$PY" kvmctl.py --port 47826 status

echo
echo "==================== 2. 正向委托：idle 端请 active 端代切 ===================="
"$PY" kvmctl.py --port 47824 switch
sleep 1.5

echo
echo "==================== 3. 无效委托：两端都不在显示器上 ===================="
"$PY" kvmctl.py --port 47826 switch
sleep 1.5

echo
echo "==================== 4. 关闭自动切换 ===================="
"$PY" kvmctl.py --port 47822 auto off
sleep 0.5

echo
echo "==================== Windows 端日志 ===================="
cat selftest/win.log
echo
echo "==================== 模拟 Mac 端日志 ===================="
cat selftest/mac.log
echo
echo "==================== 模拟 idle 端日志 ===================="
cat selftest/idle.log

echo
echo "自测结束。所有实例已停止。"
