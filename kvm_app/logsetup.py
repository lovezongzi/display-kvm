"""日志初始化：把 stdout/stderr 同时写到滚动文件。

模块名故意叫 logsetup 而不是 logging —— 后者会遮蔽标准库。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from .paths import LOG_PATH


class _Tee:
    """把多个流合成一个。任何一路失败都不影响其他路。"""

    def __init__(self, *streams):
        self._streams = [s for s in streams if s is not None]

    def write(self, data):
        n = 0
        for s in self._streams:
            try:
                n = s.write(data) or 0
            except Exception:  # noqa: BLE001
                pass
        return n if n else len(data)

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:  # noqa: BLE001
                pass

    def isatty(self):
        return False


class _RotatingFile:
    """按大小滚动的日志文件：超过 max_bytes 就改名为 .log.1 重新开。

    避免长期运行的托盘程序日志无限增长、占满磁盘（常被误当成内存泄漏）。
    """

    def __init__(self, path: Path, max_bytes: int = 2 * 1024 * 1024):
        self._path = path
        self._max = max_bytes
        self._f = open(path, "a", encoding="utf-8", buffering=1)

    def write(self, data):
        try:
            self._f.write(data)
            if self._f.tell() > self._max:
                self._rotate()
        except Exception:  # noqa: BLE001
            pass
        return len(data)

    def flush(self):
        try:
            self._f.flush()
        except Exception:  # noqa: BLE001
            pass

    def _rotate(self):
        try:
            self._f.close()
            backup = self._path.with_suffix(".log.1")
            try:
                if backup.exists():
                    backup.unlink()
            except OSError:
                pass
            try:
                self._path.rename(backup)
            except OSError:
                pass
            self._f = open(self._path, "a", encoding="utf-8", buffering=1)
            self._f.write("\n===== 日志已轮转（旧内容见 .log.1）=====\n")
        except Exception:  # noqa: BLE001
            self._f = open(self._path, "a", encoding="utf-8", buffering=1)


def setup_logging() -> None:
    """windowed 模式下 sys.stdout 是 None，print 会静默丢失——重定向到文件。"""
    try:
        MAX = 2 * 1024 * 1024
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX:
            backup = LOG_PATH.with_suffix(".log.1")
            try:
                if backup.exists():
                    backup.unlink()
                LOG_PATH.rename(backup)
            except OSError:
                pass
        fh = _RotatingFile(LOG_PATH, MAX)
        fh.write("\n===== %s 启动 =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        sys.stdout = _Tee(sys.__stdout__, fh)
        sys.stderr = _Tee(sys.__stderr__, fh)
    except Exception as exc:  # noqa: BLE001
        print("日志初始化失败: %s" % exc)
