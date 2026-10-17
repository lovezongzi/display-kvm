"""主循环与平台状态探测：空闲采样、自动切换判据、角色刷新、输入源自动探测。

线程模型（重要）
----------------
所有对显示器/系统的操作（DDC、EnumDisplayMonitors、空闲查询）**只在主循环
线程里执行**。UDP 线程与控制口线程只做两件事：收发消息、往任务队列里塞命令。
原因是 DDC 与 EnumDisplayMonitors 都不是线程安全的，多线程并发调用会卡死。
"""

from __future__ import annotations

import threading
import time

from kvm_platform import input_source_name

from .common import log
from .config import _to_float


class ProbeMixin:
    def _sample_idle(self, now: float) -> None:
        """单独、高频地采样键鼠空闲时长。

        空闲值原本只在 _refresh_platform_cache() 里顺带取一次，而那个函数要
        跑 m1ddc 子进程、每 role_poll_seconds（默认 2 秒）才跑一次。等于是
        每 2 秒才看一眼「有没有键鼠活动」，而触发条件又是 idle <= 0.45 秒
        —— 要求采样点正好落在你动手后的 0.45 秒窗口内。碰一下鼠标几乎必然
        错过，表现就是「我明明动了鼠标，屏幕就是不切」。

        空闲查询本身很便宜（Mac 是 Quartz 调用，Windows 是 GetLastInputInfo
        调用，都不碰显示器），所以拆出来单独高频采样；平台层若只能靠子进程
        查询（idle_is_cheap=False），则退回跟随角色轮询的频率。
        """
        cheap = getattr(self.platform, "idle_is_cheap", False)
        interval = 0.2 if cheap else float(self.cfg.get("role_poll_seconds", 2.0))
        if now < self._next_idle_at:
            return
        self._next_idle_at = now + interval
        try:
            with self._plat_lock:
                idle = self.platform.idle_seconds()
        except Exception:  # noqa: BLE001
            return
        if idle is not None:
            self.cached_idle = idle

    def _tick_auto(self) -> None:
        if self.probing:
            return  # 正在探测输入源，别来抢画面
        idle = self.cached_idle
        if idle is None:
            return

        prev = self._last_idle
        self._last_idle = idle

        if self.role != "idle":
            self.armed = False
            return

        # 用 _to_float 兜底：万一配置被外部改成字符串/空值，这里不会抛
        # TypeError 把整个自动切换拖死（历史上这里每秒报错刷屏）。
        arm = _to_float(self.cfg.get("arm_after_idle_seconds"), 3.0)
        fire = _to_float(self.cfg.get("fire_below_idle_seconds"), 0.45)

        if not self.armed:
            if idle >= arm:
                self.armed = True
            return

        # 两种"本机有输入活动"的证据，任一成立即触发：
        #   1) 采样到刚发生输入（空闲时间极短）——判据严、容易被采样漏掉；
        #   2) 空闲计时器被重置过（这一拍比上一拍明显变小）——只要这两拍之间
        #      发生过任何一次输入就一定能看到，不再依赖采样时机。
        activity = idle <= fire or (prev is not None and prev - idle > 0.25 and idle < arm)
        if activity:
            self.armed = False
            self.request_switch("检测到本机键鼠活动")

    def _refresh_platform_cache(self) -> None:
        t0 = time.time()
        with self._plat_lock:
            present, detail, idle = self.platform.probe()
        cost = time.time() - t0
        if cost > 1.0:
            log("警告：探测显示器耗时 %.1f 秒，偏慢" % cost)

        # 读不到输入源时不要改角色：切换完的头几秒 DDC 读必然失败
        #（m1ddc 返回 -7），这时把角色翻成 idle 会让自动切换误判，
        # 甚至在本机刚拿到画面的情况下又立刻反向切一次。
        if not getattr(self.platform, "last_read_ok", True):
            self.cached_detail = "%s（输入源读取失败，角色保持 %s）" % (detail, self.role)
            self.cached_idle = idle
            return

        new_role = "active" if present else "idle"
        if new_role != self.role:
            log("角色变更：%s -> %s" % (self.role, new_role))
        self.role = new_role
        if self.role == "active":
            self.armed = False
        self.cached_detail = detail
        self.cached_idle = idle

    def _main_loop(self) -> None:
        next_role = 0.0
        next_hello = 0.0
        while self.running:
            now = time.time()

            self._drain_ops()
            self._sample_idle(now)

            if now >= next_role:
                # 用探测「开始前」的时间排下一轮：万一 m1ddc 卡住（超时 4s），
                # 不会一探测完就立刻又排一次，否则主循环会被持续占满、收不到
                # 心跳/控制口/快捷键——表现为程序「卡死」。先排时间再探测。
                t_probe = time.time()
                try:
                    self._refresh_platform_cache()
                except Exception as exc:  # noqa: BLE001
                    log("探测显示器状态出错: %s" % exc)
                next_role = t_probe + float(self.cfg["role_poll_seconds"])

            if now >= next_hello:
                self._send_hello()
                next_hello = now + float(self.cfg["hello_interval_seconds"])
                # 未配对时顺便广播 announce，让对端托盘能发现「可配对设备」。
                # 已配对后不再广播，避免被同局域网其他实例骚扰。
                if not self.paired:
                    self._send_raw(
                        {
                            "t": "pairing_announce",
                            "from": self.name,
                            "platform": self.platform.name,
                            "nonce": self._session_nonce,
                        },
                        "255.255.255.255",
                    )

            self._expire_peer(now)

            if self.auto_on:
                try:
                    self._tick_auto()
                except Exception as exc:  # noqa: BLE001
                    log("自动检测出错: %s" % exc)

            time.sleep(float(self.cfg["tick_ms"]) / 1000.0)

    # ------------------------------------------------------------ 输入源自动探测

    def autodetect_async(self) -> None:
        """后台跑一次输入源自动探测（会真的切画面，不能在主循环里跑）。"""
        threading.Thread(target=self._autodetect_worker, name="autodetect", daemon=True).start()

    def _autodetect_worker(self) -> None:
        """判断本机到底接在显示器的哪个输入口上。

        难点：显示器只知道"哪个口有信号"，不知道这个信号属于哪台机器 ——
        DDC 没有反向通道，纯软件推断做不到可靠。实测过"空口会不会自动跳回"
        这条思路：这块 H27T22C3 要 12 秒才跳回，等 3 秒去读会把空口全判成
        有信号（四个口全中），等 14 秒虽然准但一轮要一分多钟。

        所以主流程改成**问一句**，而且给三个选项（本机 / 另一台 / 无信号）：
          1. 先问当前正在显示的那个口 —— 多数人是在用本机时点的探测，
             于是一次问答就完事，连画面都不用切；
          2. 答"不是本机"就切到下一个候选再问，直到确认。
        只有在没有交互手段时（headless），才退回慢速的活口探测。
        """
        name = input_source_name
        self.probing = True
        try:
            if self.cfg["dry_run"]:
                log("DRY-RUN 模式下无法探测 —— 探测依赖真的把画面切过去看反应")
                return

            cands = [
                int(x, 0) if isinstance(x, str) else int(x)
                for x in (self.cfg.get("input_candidates") or [15, 16, 17, 18, 27])
            ]
            with self._plat_lock:
                cur = self.platform.current_input()
            log("当前输入源 %s，开始确认哪个口是本机 ..." % name(cur))

            my = None
            peer = None

            if callable(self.ask_user):
                # 先问当前正在显示的这个口：多数人是在用本机的时候点探测，
                # 于是一次问答就结束，连画面都不用切。
                order = ([cur] if cur is not None else []) + [v for v in cands if v != cur]
                for idx, v in enumerate(order):
                    if idx > 0:
                        log("切到 %s 再看 ..." % name(v))
                        with self._plat_lock:
                            self.platform.switch_to(v)
                        time.sleep(3.0)
                    ans = self.ask_user(v)
                    if ans == "mac":
                        my = v
                        break
                    if ans == "peer" and peer is None:
                        peer = v
                        log("记下 %s 是对端的口" % name(v))
                if my is None:
                    log("所有候选输入口都确认不是本机，配置保持原样")
                    return

            elif hasattr(self.platform, "probe_live_inputs"):
                # 没有交互手段（headless）：退回"切过去看会不会自动跳回"的办法。
                # 空口要十几秒才跳回（H27T22C3 实测 12 秒），settle 必须给足，
                # 否则会把空口当成有信号 —— 实测 3 秒时四个口全被判成活的。
                with self._plat_lock:
                    live = self.platform.probe_live_inputs(cands, settle_seconds=14.0)
                log("有信号的输入口：%s" % ("、".join(name(v) for v in live) or "一个都没有"))
                if len(live) == 1:
                    my = live[0]
                elif len(live) >= 2 and self.peer_online:
                    v = live[0]
                    with self._plat_lock:
                        self.platform.switch_to(v)
                    log("已切到 %s，等对端上报它是否正在被显示 ..." % name(v))
                    time.sleep(float(self.cfg.get("peer_settle_seconds", 6.0)))
                    if self.peer_role == "active":
                        peer, my = v, next((x for x in live if x != v), None)
                        log("对端报告它在被显示 -> %s 是对端的口" % name(v))
                    else:
                        my, peer = v, next((x for x in live if x != v), None)
                        log("对端没有在显示 -> %s 是本机的口" % name(v))
                else:
                    log("有 %d 个口有信号，无交互模式下判断不了哪个是本机" % len(live))
                    return

            else:
                log(
                    "当前平台既没有交互提问手段、也不支持活口探测（目前只有 macOS 实现了），"
                    "无法自动识别本机输入口，请手动填 my_input_source。"
                )
                return

            self.my_src = my
            self.cfg["my_input_source"] = "0x%02X" % my
            if peer is not None:
                self.peer_src = peer
                self.cfg["peer_input_source"] = "0x%02X" % peer

            # 探完把画面交回本机，顺便让角色重新判定一次
            with self._plat_lock:
                self.platform.switch_to(my)
            self.last_switch_ts = time.time()
            self._save_cfg()
            log(
                "自动探测完成：本机 = %s，对端 = %s"
                % (name(my), name(peer) if peer is not None else "未确定")
            )
        except Exception as exc:  # noqa: BLE001
            log("自动探测出错: %s" % exc)
        finally:
            self.probing = False
