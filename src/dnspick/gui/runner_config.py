"""运行配置单例 —— UI 控件双向绑定的唯一数据源。

MainWindow 和 SettingsDialog 共享同一个 RunnerConfig 实例。
当任一端修改属性时，另一端通过 Qt 信号自动同步。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from dnspick.config import Config


class RunnerConfig(QObject):
    """运行配置的 Qt 信号包装。

    Config（dataclass）自身不能发信号，本类充当「活页夹」——
    在 Config 属性之上提供 Qt 信号，实现 UI 控件到配置的双向绑定。
    """

    use_ecs_changed = Signal(bool)
    deep_traceroute_changed = Signal(bool)
    log_level_changed = Signal(str)
    profile_changed = Signal(str)
    servers_changed = Signal()

    def __init__(self, config: Config, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config = config

    # ── use_ecs ──────────────────────────────────────────────────────

    @property
    def use_ecs(self) -> bool:
        return self._config.use_ecs

    @use_ecs.setter
    def use_ecs(self, v: bool) -> None:
        if self._config.use_ecs != v:
            self._config.use_ecs = v
            self.use_ecs_changed.emit(v)

    # ── deep_traceroute ──────────────────────────────────────────────

    @property
    def deep_traceroute(self) -> bool:
        return self._config.tunables.deep_traceroute

    @deep_traceroute.setter
    def deep_traceroute(self, v: bool) -> None:
        if self._config.tunables.deep_traceroute != v:
            self._config.tunables.deep_traceroute = v
            self.deep_traceroute_changed.emit(v)

    # ── log_level ────────────────────────────────────────────────────

    @property
    def log_level(self) -> str:
        return self._config.log_level

    @log_level.setter
    def log_level(self, v: str) -> None:
        if self._config.log_level != v:
            self._config.log_level = v
            self.log_level_changed.emit(v)

    # ── profile ──────────────────────────────────────────────────────

    @property
    def profile(self) -> str:
        return self._config.tunables.profile

    @profile.setter
    def profile(self, v: str) -> None:
        if self._config.tunables.profile != v:
            self._config.tunables.with_profile(v)
            self.profile_changed.emit(v)

    # ── 直接访问底层 Config ─────────────────────────────────────────

    @property
    def config(self) -> Config:
        return self._config

    def save(self) -> None:
        self._config.save()

    def replace_config(self, config: Config) -> None:
        """替换底层 Config 并重新发射当前状态（用于载入新配置文件）。"""
        old = self._config
        self._config = config
        # 变更的属性需要向外通知
        if old.use_ecs != config.use_ecs:
            self.use_ecs_changed.emit(config.use_ecs)
        if old.tunables.deep_traceroute != config.tunables.deep_traceroute:
            self.deep_traceroute_changed.emit(config.tunables.deep_traceroute)
        if old.tunables.profile != config.tunables.profile:
            self.profile_changed.emit(config.tunables.profile)
        if old.log_level != config.log_level:
            self.log_level_changed.emit(config.log_level)
        # 服务器列表接口不同，interface 一定是变了
        self.servers_changed.emit()