"""跨模块共享的小工具。"""

from __future__ import annotations

import time


def log(msg: str) -> None:
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)
