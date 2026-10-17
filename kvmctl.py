#!/usr/bin/env python3
"""display-kvm 本地控制客户端。

通过 127.0.0.1 上的控制端口与正在运行的 agent 对话。可以把它绑到热键、
Alfred / 快捷指令 / AutoHotkey 上，实现一键切换。

用法：
    python kvmctl.py status        查看当前状态
    python kvmctl.py switch        立即把显示器切到本机
    python kvmctl.py switch-peer   立即把显示器切到对端（把画面交出去）
    python kvmctl.py autodetect    自动探测本机接在哪个输入口上
    python kvmctl.py auto off      暂停自动切换
    python kvmctl.py auto on       恢复自动切换
    python kvmctl.py pair          发起局域网配对
    python kvmctl.py unpair        解除配对（并请对端一并解除）
    python kvmctl.py reset-peer    只请对端解除配对，本端不动
    python kvmctl.py --port 47824 status

unpair / reset-peer 是配对死锁的正规出口：对端卡在「已配对」时会拒绝一切
新配对请求，过去只能跑到那台机器上去点托盘，现在从命令行就能解开。
"""

from __future__ import annotations

import argparse
import json
import socket
import sys


def talk(port: int, cmd: str) -> dict:
    # 超时要留够：agent 侧的 switch 命令是排队给主循环执行的，
    # 最坏情况要等一个 tick（默认 250ms），加上 DDC 下发本身的开销。
    s = socket.create_connection(("127.0.0.1", port), timeout=8)
    with s:
        s.sendall((cmd + "\n").encode("utf-8"))
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode("utf-8"))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="display-kvm 控制客户端")
    ap.add_argument("--port", type=int, default=47822, help="agent 的本地控制端口")
    ap.add_argument("--json", action="store_true", help="输出原始 JSON")
    ap.add_argument(
        "cmd",
        nargs=argparse.REMAINDER,
        help="status | switch | switch-peer | autodetect | auto on | auto off | "
             "pair | unpair | reset-peer | quit",
    )
    args = ap.parse_args(argv)

    cmd = " ".join(args.cmd).strip() or "status"

    try:
        resp = talk(args.port, cmd)
    except OSError as exc:
        print("连不上本地 agent（127.0.0.1:%d）：%s" % (args.port, exc))
        print("请确认 agent 已经启动。")
        return 1

    if args.json or not resp.get("ok"):
        print(json.dumps(resp, ensure_ascii=False, indent=2))
        return 0 if resp.get("ok") else 1

    st = resp.get("status")
    if st:
        idle = st.get("idle_seconds")
        print("身份        : %s (%s)" % (st.get("name"), st.get("platform")))
        print("当前角色    : %s%s" % (st.get("role"), "   <- 显示器正显示本机" if st.get("role") == "active" else ""))
        print("显示器      : %s" % st.get("detail"))
        print("键鼠空闲    : %s" % ("%.1f 秒" % idle if isinstance(idle, (int, float)) else "未知"))
        print("自动切换    : %s%s" % ("开" if st.get("auto") else "关", "（已就绪，等待本机键鼠活动）" if st.get("armed") else ""))
        paired = "已配对" if st.get("paired") else "未配对"
        if st.get("token_mismatch"):
            paired += "  <-- 与 %s 的 token 不一致，即将自动解除" % st.get("token_mismatch_with")
        print("配对        : %s" % paired)
        print("DRY-RUN     : %s" % ("是" if st.get("dry_run") else "否"))
        if st.get("peer_online"):
            peer = "%s  角色=%s  %.0fs 前心跳" % (
                st.get("peer_ip"),
                st.get("peer_role"),
                st.get("peer_age_seconds") or 0,
            )
        elif st.get("peer_age_seconds") is None:
            peer = "尚未发现（从未收到心跳）"
        else:
            peer = "离线（%s，%.0fs 前失去心跳）" % (st.get("peer_ip") or "?", st.get("peer_age_seconds"))
        print("对端        : %s" % peer)
        print(
            "输入源      : 本机 %s / 对端 %s"
            % (
                st.get("my_input_source_name") or st.get("my_input_source"),
                st.get("peer_input_source_name") or st.get("peer_input_source"),
            )
        )
    else:
        print("已执行：%s" % json.dumps(resp, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
