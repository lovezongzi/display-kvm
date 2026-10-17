"""自动切换状态的两端同步。

任一端改「自动切换」（GUI 开关 / 设置面板保存 / kvmctl auto on|off）都通过
config_sync 报文广播给对端，对端镜像并写回自己的配置文件。

防回声环路做了两层：
  1. 接收端只镜像、不回播；
  2. 值相同直接跳过。
所以两边同时改也只会收敛到同一个值，不会无限互发。
"""

from __future__ import annotations

from .common import log


class SyncMixin:
    def set_auto_switch(self, on: bool, broadcast: bool = True) -> None:
        """改自动切换的唯一入口：写内存、落盘、（可选）广播给对端。

        broadcast=False 用于「接收对端同步消息」时，否则两端会无限互发形成回声环路。
        只有用户在本端主动改（GUI 开关 / 设置面板保存 / kvmctl auto on|off）才走 broadcast=True。
        """
        on = bool(on)
        self.auto_on = on
        self.cfg["auto_switch"] = on
        self._save_cfg()
        if broadcast:
            payload = {"t": "config_sync", "auto_switch": on}
            self._send(payload, "255.255.255.255")
            if self.peer_ip:
                self._send(payload, self.peer_ip)

    def _on_config_sync(self, msg: dict) -> None:
        """对端把可同步配置推过来（目前只有 auto_switch），本端镜像并落盘。

        这里**不**回播：set_auto_switch 默认 broadcast=True 才会广播，而本函数
        直接改 auto_on/cfg 并落盘，不走广播，因此不会触发对端再回推，无环路。
        """
        if "auto_switch" in msg:
            val = bool(msg["auto_switch"])
            if val == self.auto_on and bool(self.cfg.get("auto_switch")) == val:
                return
            self.auto_on = val
            self.cfg["auto_switch"] = val
            self._save_cfg()
            log("已按对端设置同步自动切换：%s" % ("开启" if val else "关闭"))
            if callable(self.on_auto_sync):
                try:
                    self.on_auto_sync(val)
                except Exception:  # noqa: BLE001
                    pass
