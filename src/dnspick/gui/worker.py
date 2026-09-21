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
from dnspick.log import log
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
        self._force_stopped: bool = False
        self._safety_handle: asyncio.TimerHandle | None = None

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
            log.info("测量任务已取消")
        except RuntimeError:
            if self._force_stopped:
                self.failed.emit("已取消")
                log.info("测量任务已强制中止")
            else:
                raise
        except Exception as exc:  # noqa: BLE001 - 需要把任何异常带回主线程展示
            tb = traceback.format_exc()
            log.error(f"测量异常: {type(exc).__name__}: {exc}")
            self.failed.emit(f"{type(exc).__name__}: {exc}\n{tb}")
        finally:
            self._runner = None
            if self._safety_handle:
                with contextlib.suppress(Exception):
                    self._safety_handle.cancel()
                self._safety_handle = None
            if not self._force_stopped:
                with contextlib.suppress(Exception):
                    loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            self._loop = None

    def stop(self) -> None:
        """主线程调用：请求中断。

        1. 取消 Runner 的 asyncio Task，触发 CancelledError 正常传播
        2. 注册 3 秒安全网：若 as_completed() 的子任务清理仍过慢则 force-stop
        """
        self.requestInterruption()
        runner = self._runner
        loop = self._loop
        if runner is not None and loop is not None and not loop.is_closed():
            self._force_stopped = True
            loop.call_soon_threadsafe(runner.request_cancel)
            loop.call_soon_threadsafe(self._schedule_force_stop)

    def _schedule_force_stop(self) -> None:
        """在 worker 线程内注册 force-stop 超时回调。"""
        loop = asyncio.get_running_loop()
        self._safety_handle = loop.call_later(3.0, self._force_stop_if_stuck)

    def _force_stop_if_stuck(self) -> None:
        """3 秒后仍未结束则强制停止事件循环。"""
        if self._force_stopped and self._loop and not self._loop.is_closed():
            self._loop.stop()


class GeoIpProbeWorker(QThread):
    """独立小任务：探测出口公网 IP，供 ECS / 就近判定参考。"""

    done = Signal(str)

    def run(self) -> None:
        from dnspick.runner import _detect_public_ip

        ip = _detect_public_ip()
        self.done.emit(ip or "")