"""全局日志管理器（单例 + Qt Signal）。

特性：
- RotatingFileHandler：5MB 轮转，保留 3 个历史文件
- Qt Signal：UI 组件 connect log.appended 即可自动显示
- 模块路径：自动从调用栈提取，格式如 dnspick.runner
- 全局可调用：from dnspick.log import log; log.info("...")
- CLI 输出：log.set_console(console) 可将日志实时打印到终端
"""

from __future__ import annotations

import enum
import inspect
import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import ClassVar

from PySide6.QtCore import QObject, Signal

_LOG_DIR = Path(".dnspick")
_LOG_FILE = _LOG_DIR / "dnspick.log"
_MAX_BYTES = 5 * 1024 * 1024  # 5MB
_BACKUP_COUNT = 3

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # src/

# CLI 控制台 handler（rich Console 或标准 stderr stream）
_cli_handler: logging.Handler | None = None


class LogLevel(enum.Enum):
    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class _LogManager(QObject):
    """全局日志管理器。

    公开接口：
      - log.info(msg) / log.warn(msg) / log.error(msg) / log.debug(msg)
      - log.appended  → Signal(str, str, str, str)  (ts, module, level, message)
      - log.set_console(console) → 为 CLI 模式启用终端实时输出
    """

    appended = Signal(str, str, str, str)

    _LEVEL_MAP: ClassVar[dict[LogLevel, int]] = {
        LogLevel.DEBUG: logging.DEBUG,
        LogLevel.INFO: logging.INFO,
        LogLevel.WARN: logging.WARNING,
        LogLevel.ERROR: logging.ERROR,
    }

    def __init__(self) -> None:
        super().__init__()
        self._logger = logging.getLogger("dnspick")
        self._logger.setLevel(logging.DEBUG)
        self._logger.propagate = False
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            str(_LOG_FILE),
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        fmt = logging.Formatter(
            "[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(fmt)
        self._logger.addHandler(handler)

    # ── CLI 终端输出 ─────────────────────────────────────────

    def set_console(self, console_object: object | None = None) -> None:
        """为 CLI 模式启用终端输出。

        console_object: rich.Console 实例，或 None 使用 stderr。
        调用后，所有 log.xxx() 同时输出到终端（带 ANSI 颜色）。
        """
        global _cli_handler
        if _cli_handler is not None:
            self._logger.removeHandler(_cli_handler)
            _cli_handler = None

        if console_object is None:
            _cli_handler = logging.StreamHandler(sys.stderr)
            _cli_handler.setFormatter(_CliFormatter())
        else:
            _cli_handler = _RichHandler(console_object)
        self._logger.addHandler(_cli_handler)
        _cli_handler.setLevel(logging.INFO)

    # ── 公开 API ──────────────────────────────────────────────

    def info(self, msg: str) -> None:
        self._emit(LogLevel.INFO, msg)

    def warn(self, msg: str) -> None:
        self._emit(LogLevel.WARN, msg)

    def error(self, msg: str) -> None:
        self._emit(LogLevel.ERROR, msg)

    def debug(self, msg: str) -> None:
        """debug 不写文件，只通过 appended Signal 推送。"""
        ts = datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        modpath = self._caller_module()
        self.appended.emit(ts, modpath, LogLevel.DEBUG.value, msg)

    # ── 内部 ─────────────────────────────────────────────────

    def _emit(self, level: LogLevel, msg: str) -> None:
        ts = datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        modpath = self._caller_module()
        # 写入文件（含模块路径）
        self._logger.log(self._LEVEL_MAP[level], "[%s] %s", modpath, msg)
        # 推送 Signal
        self.appended.emit(ts, modpath, level.value, msg)

    @staticmethod
    def _caller_module() -> str:
        """从调用栈提取调用者模块的短路径。

        逐帧上溯，跳过 log.py 自身以及 stdlib / site-packages，找到第一个
        位于项目 src/ 目录内的帧。

        输出示例：dnspick.runner, gui.main_window, cli
        """
        frame = inspect.currentframe()
        _log_file = Path(__file__).resolve()
        _pkg_root = _PACKAGE_ROOT.resolve()
        while frame is not None:
            fname = Path(frame.f_code.co_filename).resolve()
            # 跳过 log.py 自身
            if fname == _log_file:
                frame = frame.f_back
                continue
            # 在项目 src/ 内 → 这就是调用者
            try:
                fname.relative_to(_pkg_root)
                break
            except ValueError:
                pass
            # 在 CPython 标准库或 site-packages 内 → 继续上溯
            try:
                fname.relative_to(sys.prefix)
                # 是解释器自带文件（如 asyncio, logging）→ 跳过
                frame = frame.f_back
                continue
            except ValueError:
                pass
            # 其他位置（用户脚本 / 测试）→ 接受它
            break
        if frame is None:
            return "?"

        fname = Path(frame.f_code.co_filename).resolve()
        try:
            rel = fname.relative_to(_pkg_root).as_posix()
        except ValueError:
            # 不在 src/ 内 → 使用文件名（不含扩展名）作为标识
            stem = fname.stem
            return stem if stem != "<string>" else "?"
        # 去掉 .py 后缀，路径分隔符 → 点
        modpath = rel.replace(".py", "").replace("/", ".")
        return modpath


class _CliFormatter(logging.Formatter):
    """纯 ANSI 颜色的 CLI 格式化器，不依赖 rich。"""

    _COLORS = {
        "INFO": "\033[36m",     # cyan
        "WARNING": "\033[33m",  # yellow
        "ERROR": "\033[31m",    # red
    }
    _RESET = "\033[0m"
    _DIM = "\033[2m"

    def format(self, record: logging.LogRecord) -> str:
        color = self._COLORS.get(record.levelname, "")
        return (
            f"{self._DIM}{self.formatTime(record, '%Y-%m-%d %H:%M:%S')}{self._RESET} "
            f"{color}[{record.levelname}]{self._RESET} "
            f"{record.getMessage()}"
        )


class _RichHandler(logging.Handler):
    """通过 rich Console 输出日志。"""

    _STYLES = {
        "INFO": "[cyan]",
        "WARNING": "[yellow]",
        "ERROR": "[bold red]",
    }

    def __init__(self, console: object) -> None:
        super().__init__()
        self._console = console

    def emit(self, record: logging.LogRecord) -> None:
        style = self._STYLES.get(record.levelname, "")
        formatter = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        text = formatter.format(record)
        self._console.print(f"{style}{text}[/]", highlight=False)


# 模块级单例
log = _LogManager()