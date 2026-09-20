"""GUI 侧的并发桥。

PySide6 的事件循环和 asyncio 不合并——QtAsyncio 在 3.13 上的坑不值得踩，
而且合并会把核心引擎和 Qt 绑死，CLI 就复用不了。
这里让 asyncio 在自己的线程里跑，通过 Qt 信号把进度和结果送回主线程。
"""

from __future__ import annotations

import asyncio
import contextlib
import traceback

from PySide6.QtCore import QThread, Signal

from dnspick.config import Config
from dnspick.models import RunResult
from dnspick.runner import Runner


class ProbeWorker(QThread):
    """在独立线程里跑 asyncio 测量任务。"""

    progress = Signal(object)  # StageProgress
    succeeded = Signal(object)  # RunResult
    failed = Signal(str)

    def __init__(self, config: Config, parent=None) -> None:
        super().__init__(parent)
        self.config = config
        self._runner: Runner | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            runner = Runner(self.config)
            self._runner = runner
            runner.on_progress = lambda sp: self.progress.emit(sp)
            result: RunResult = loop.run_until_complete(runner.run())
            self.succeeded.emit(result)
        except asyncio.CancelledError:
            self.failed.emit("已取消")
        except Exception as exc:  # noqa: BLE001 - 需要把任何异常带回主线程展示
            # 注意：不要给 format_exc 加 limit。之前用 limit=6 只保留最外层 6 帧，
            # 真正出错的深层栈（如 dns/edns.py）被截掉，日志里只剩一串无关帧，难以定位。
            self.failed.emit(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        finally:
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            self._loop = None

    def stop(self) -> None:
        """主线程调用：请求中断。"""
        self.requestInterruption()
        runner = self._runner
        loop = self._loop
        if runner is not None and loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(runner.request_cancel)


class GeoIpProbeWorker(QThread):
    """独立小任务：探测出口公网 IP，供 ECS / 就近判定参考。"""

    done = Signal(str)

    def run(self) -> None:
        from dnspick.runner import _detect_public_ip

        ip = _detect_public_ip()
        self.done.emit(ip or "")