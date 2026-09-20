"""主窗口 —— 运行配置仪表盘。

职责：展示当前生效的 DNS 协议分布 + 域名配置摘要 + 运行控制 + 进度。
服务器管理 → SettingsDialog，结果分析 → HistoryWindow。
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from dnspick.config import load_config
from dnspick.domain_store import DomainStore
from dnspick.gui.domain_manager import DomainManagerDialog
from dnspick.gui.history import HistoryWindow
from dnspick.gui.progress_panel import ProgressPanel
from dnspick.gui.settings import SettingsDialog
from dnspick.gui.worker import ProbeWorker
from dnspick.models import Group, Protocol, RunResult
from dnspick.relation_store import RelationStore
from dnspick.store import ResultStore


class LogLevel(enum.Enum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


_LOG_COLOR: dict[LogLevel, str] = {
    LogLevel.INFO: "#e0e0e0",
    LogLevel.WARN: "#f1a340",
    LogLevel.ERROR: "#d73027",
}

_PROFILE_HINTS: dict[str, str] = {
    "balanced": "均衡：延迟/跳数/稳定/就近 = 30/15/25/30",
    "speed":    "速度优先：延迟权重 55，适合游戏/实时通信",
    "stable":   "稳定优先：稳定性权重 50，适合下载/串流",
    "cdn":      "就近优先：就近性权重 55，适合网站/CDN",
    "privacy":  "隐私优先：均衡偏隐私，跳数权重略低",
}

_PROTOCOL_NAMES: dict[Protocol, str] = {
    Protocol.UDP: "UDP",
    Protocol.TCP: "TCP",
    Protocol.DOT: "DoT",
    Protocol.DOH: "DoH",
}

_PROTOCOL_ORDER: list[Protocol] = [Protocol.UDP, Protocol.TCP, Protocol.DOT, Protocol.DOH]


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("dnspick —— DNS 优选测量器")
        self.resize(1000, 720)
        self.config = load_config()
        self.config.load_saved()
        self.dom_store = DomainStore()
        self.dom_store.init()
        self.store = ResultStore()
        self.worker: ProbeWorker | None = None
        self.last_mid: str | None = None

        self._build_ui()
        self._build_menu()
        self._restore_geometry()
        self._refresh_protocol_cards()
        self._refresh_domain_summary()

    # -- UI ---------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 8, 10, 8)

        # ── 控制栏 ──
        bar = QHBoxLayout()
        self.btn_start = QPushButton("开始测量")
        self.btn_start.clicked.connect(self.start_run)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_run)
        self.profile = QComboBox()
        for name in _PROFILE_HINTS:
            self.profile.addItem(name)
            self.profile.setItemData(
                self.profile.count() - 1, _PROFILE_HINTS[name], Qt.ItemDataRole.ToolTipRole
            )
        self.profile.currentTextChanged.connect(self._on_profile)

        self.profile_label = QLabel("")
        self.profile_label.setStyleSheet("color: #888; font-size: 12px;")
        self.profile.currentTextChanged.connect(self._on_profile_changed)
        self.profile_label.setText(_PROFILE_HINTS.get(self.profile.currentText(), ""))
        self.chk_deep = QCheckBox("深度 traceroute")
        self.chk_ecs = QCheckBox("ECS 对比")

        bar.addWidget(self.btn_start)
        bar.addWidget(self.btn_stop)
        bar.addWidget(QLabel("档位"))
        bar.addWidget(self.profile)
        bar.addWidget(self.profile_label)
        bar.addWidget(self.chk_deep)
        bar.addWidget(self.chk_ecs)
        bar.addStretch(1)
        self.btn_history = QPushButton("优选历史")
        self.btn_history.clicked.connect(self.show_history)
        self.btn_settings = QPushButton("设置")
        self.btn_settings.clicked.connect(self.open_settings)
        bar.addWidget(self.btn_history)
        bar.addWidget(self.btn_settings)
        root.addLayout(bar)

        # ── 进度面板 ──
        self.progress_panel = ProgressPanel()
        self.progress_panel.set_stages([
            "环境探测", "域名收集", "API发现",
            "DNS 解析", "ECS 对比", "IP 探测",
            "归属地", "评分",
        ])
        root.addWidget(self.progress_panel)

        # ── 主体：DNS 协议分布 + 域名摘要 ──
        # 左侧：协议分布卡片
        server_box = QGroupBox("当前 DNS 协议分布")
        sv_layout = QVBoxLayout(server_box)
        sv_layout.setContentsMargins(6, 6, 6, 6)

        self.card_scroll = QScrollArea()
        self.card_scroll.setWidgetResizable(True)
        self.card_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.card_container = QWidget()
        self.card_layout = QVBoxLayout(self.card_container)
        self.card_layout.setContentsMargins(0, 0, 0, 0)
        self.card_layout.setSpacing(6)
        self.card_layout.addStretch(1)
        self.card_scroll.setWidget(self.card_container)

        self.protocol_cards: dict[Protocol, QFrame] = {}
        self.card_detail_labels: dict[Protocol, QLabel] = {}
        for proto in _PROTOCOL_ORDER:
            card = self._make_protocol_card(proto)
            self.card_layout.insertWidget(self.card_layout.count() - 1, card)
            self.protocol_cards[proto] = card

        sv_layout.addWidget(self.card_scroll)

        btn_manage = QPushButton("管理服务器…")
        btn_manage.setFixedHeight(28)
        btn_manage.clicked.connect(self.open_settings)
        sv_layout.addWidget(btn_manage)

        # 右侧：域名摘要 + 状态
        right = QVBoxLayout()

        dom_box = QGroupBox("域名配置")
        dom_layout = QVBoxLayout(dom_box)
        dom_layout.setContentsMargins(8, 8, 8, 8)
        self.dom_label = QLabel("")
        self.dom_label.setWordWrap(True)
        dom_layout.addWidget(self.dom_label)
        dom_layout.addStretch(1)

        btn_row = QHBoxLayout()
        self.btn_manage_domains = QPushButton("管理域名…")
        self.btn_manage_domains.clicked.connect(self._open_domain_manager)
        btn_row.addWidget(self.btn_manage_domains)
        btn_row.addStretch(1)
        dom_layout.addLayout(btn_row)
        right.addWidget(dom_box)

        status_box = QGroupBox("上次测量")
        status_layout = QVBoxLayout(status_box)
        status_layout.setContentsMargins(8, 8, 8, 8)
        self.last_run_label = QLabel("尚未测量")
        self.last_run_label.setWordWrap(True)
        self.last_run_label.setStyleSheet("color: #888;")
        status_layout.addWidget(self.last_run_label)
        status_layout.addStretch(1)
        right.addWidget(status_box)

        body_splitter = QSplitter(Qt.Orientation.Horizontal)
        body_splitter.addWidget(server_box)
        right_widget = QWidget()
        right_widget.setLayout(right)
        body_splitter.addWidget(right_widget)
        body_splitter.setStretchFactor(0, 3)
        body_splitter.setStretchFactor(1, 1)
        root.addWidget(body_splitter, 1)

        # ── 日志 ──
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(150)
        root.addWidget(self.log)

        self.setCentralWidget(central)

    def _build_menu(self) -> None:
        menu = self.menuBar().addMenu("文件")
        act_load = QAction("载入自定义服务器清单…", self)
        act_load.triggered.connect(self.load_servers_file)
        act_dom = QAction("管理域名集…", self)
        act_dom.triggered.connect(self._open_domain_manager)
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self.close)
        menu.addAction(act_load)
        menu.addAction(act_dom)
        menu.addSeparator()
        menu.addAction(act_quit)

        about = self.menuBar().addMenu("帮助")
        act_about = QAction("关于", self)
        act_about.triggered.connect(self.show_about)
        about.addAction(act_about)

    # -- 协议分布卡片 ----------------------------------------------------
    def _make_protocol_card(self, proto: Protocol) -> QFrame:
        """构建单个协议的分布卡片。"""
        card = QFrame()
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card.setStyleSheet(
            "QFrame { background: #2b2b2b; border-radius: 6px; padding: 4px; }"
        )
        card.setCursor(Qt.CursorShape.PointingHandCursor)
        card.mouseDoubleClickEvent = lambda e: self._on_card_double_clicked(proto)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(2)

        header = QHBoxLayout()
        proto_name = _PROTOCOL_NAMES.get(proto, proto.value)
        self.card_count_labels: dict[Protocol, QLabel] = getattr(
            self, 'card_count_labels', {}
        )
        count_label = QLabel("0 台")
        count_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        self.card_count_labels[proto] = count_label

        name_label = QLabel(proto_name)
        name_label.setStyleSheet("font-size: 14px; font-weight: bold; color: #ccc;")

        header.addWidget(name_label)
        header.addStretch(1)
        header.addWidget(count_label)
        layout.addLayout(header)

        detail = QLabel("")
        detail.setWordWrap(True)
        detail.setStyleSheet("color: #888; font-size: 11px;")
        detail.setVisible(False)
        self.card_detail_labels[proto] = detail
        layout.addWidget(detail)

        card.mousePressEvent = lambda e: self._on_card_clicked(proto)

        # 存储对 count_label 的引用以便刷新
        if not hasattr(self, 'card_count_labels'):
            self.card_count_labels = {}
        self.card_count_labels[proto] = count_label

        return card

    def _refresh_protocol_cards(self) -> None:
        """根据 config.servers 刷新协议卡片内容和样式。"""
        enabled = self.config.active_servers()
        # 按协议分组
        by_proto: dict[Protocol, list[str]] = {}
        for proto in _PROTOCOL_ORDER:
            by_proto[proto] = []
        for s in enabled:
            by_proto.setdefault(s.protocol, []).append(
                s.vendor if s.vendor else s.name
            )

        for proto in _PROTOCOL_ORDER:
            servers = by_proto.get(proto, [])
            cnt = len(servers)
            card = self.protocol_cards.get(proto)
            if card is None:
                continue

            # 更新数量标签
            count_label = self.card_count_labels.get(proto)
            if count_label:
                count_label.setText(f"{cnt} 台")

            # 更新详情标签
            detail = self.card_detail_labels.get(proto)
            if detail:
                if cnt == 0:
                    detail.setText("暂无启用的服务器")
                else:
                    detail.setText(" · ".join(servers))

            # 卡片样式：有启用和没有启用
            if cnt > 0:
                card.setStyleSheet(
                    "QFrame { background: #2b2b2b; border-left: 3px solid #4caf50; "
                    "border-radius: 6px; padding: 4px; }"
                )
                count_label.setStyleSheet(
                    "font-weight: bold; font-size: 13px; color: #4caf50;"
                )
            else:
                card.setStyleSheet(
                    "QFrame { background: #222; border-radius: 6px; padding: 4px; }"
                )
                card.setGraphicsEffect(None)
                count_label.setStyleSheet(
                    "font-weight: bold; font-size: 13px; color: #666;"
                )

    def _on_card_clicked(self, proto: Protocol) -> None:
        """单击展开/折叠协议卡片详情。"""
        detail = self.card_detail_labels.get(proto)
        if detail:
            detail.setVisible(not detail.isVisible())

    def _on_card_double_clicked(self, proto: Protocol) -> None:
        """双击打开 Settings 对话框。"""
        self.open_settings()

    # -- 域名摘要 ---------------------------------------------------------
    def _refresh_domain_summary(self) -> None:
        ds = self.dom_store.get_enabled()
        cn = len([d for d in ds if d.group is Group.CN])
        gl = len([d for d in ds if d.group is Group.GLOBAL])
        total = len(ds)

        counts = self.dom_store.count_by_source()
        parts: list[str] = []
        if counts.get("builtin"):
            parts.append(f"内置 <b>{counts['builtin']}</b>")
        if counts.get("history"):
            parts.append(f"浏览器 <b>{counts['history']}</b>")
        if counts.get("manual"):
            parts.append(f"手动 <b>{counts['manual']}</b>")
        if counts.get("html") or counts.get("crt_sh"):
            api_count = counts.get("html", 0) + counts.get("crt_sh", 0)
            parts.append(f"API发现 <b>{api_count}</b>")

        source_text = " · ".join(parts) if parts else f"共 <b>{total}</b> 个"

        self.dom_label.setText(
            f"国内 <b>{cn}</b> 个 · 国外 <b>{gl}</b> 个\n"
            f"{source_text}\n"
            f"<span style='color:#888;font-size:11px'>管理域名可启用/禁用、添加、扫描浏览器历史</span>"
        )

    # -- 交互 -------------------------------------------------------------
    def _on_profile(self, name: str) -> None:
        if name:
            self.config.tunables.with_profile(name)
            self.config.save()

    def _on_profile_changed(self, name: str) -> None:
        self.profile_label.setText(_PROFILE_HINTS.get(name, ""))

    def _reset_progress(self) -> None:
        self.progress_panel.reset()

    def open_settings(self) -> None:
        dlg = SettingsDialog(self.config, self)
        if dlg.exec():
            self.config.save()
            self.profile.blockSignals(True)
            self.profile.setCurrentText(self.config.tunables.profile)
            self.profile.blockSignals(False)
            self.profile_label.setText(_PROFILE_HINTS.get(self.config.tunables.profile, ""))
            self._refresh_protocol_cards()
            self._refresh_domain_summary()
            self._reset_progress()

    def load_servers_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 servers.toml", "", "TOML (*.toml)")
        if path:
            self.config = load_config(Path(path))
            self._refresh_protocol_cards()
            self._refresh_domain_summary()
            self._reset_progress()
            self._log_info(f"已载入服务器清单：{path}")

    def _open_domain_manager(self) -> None:
        """打开域名管理器对话框。"""
        rel_store = RelationStore()
        dlg = DomainManagerDialog(self.dom_store, rel_store, self)
        if dlg.exec():
            self._refresh_domain_summary()
            self._reset_progress()
            self._log_info("域名配置已更新")

    def show_about(self) -> None:
        QMessageBox.information(
            self,
            "关于",
            "dnspick —— 为家庭网络挑选最优 DNS 上游的测量器。\n\n"
            "两段式：DNS 解析矩阵 → IP 链路探测 → 综合评分。\n"
            "只做测量与建议，不修改任何系统设置。",
        )

    def _log(self, level: LogLevel, msg: str) -> None:
        ts = datetime.now(UTC).astimezone().strftime("%H:%M:%S")
        color = _LOG_COLOR[level]
        tag = level.name
        self.log.append(f'<span style="color:#666">{ts}</span> '
                        f'<span style="color:{color}">[{tag}]</span>&nbsp; '
                        f'{msg}')

    def _log_info(self, msg: str) -> None:
        self._log(LogLevel.INFO, msg)

    def _log_warn(self, msg: str) -> None:
        self._log(LogLevel.WARN, msg)

    def _log_error(self, msg: str) -> None:
        self._log(LogLevel.ERROR, msg)

    # -- 测量 -------------------------------------------------------------
    def start_run(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        self.config.tunables.deep_traceroute = self.chk_deep.isChecked()
        self.config.use_ecs = self.chk_ecs.isChecked()
        self.config.tunables.with_profile(self.profile.currentText())

        if not self.config.active_servers():
            QMessageBox.warning(self, "无可用服务器", "请在 DNS 服务器列表中勾选至少一台。")
            return

        self.progress_panel.start()
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_history.setText("优选历史")

        self.worker = ProbeWorker(self.config, self)
        self.worker.progress.connect(self.progress_panel.on_progress)
        self.worker.succeeded.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()
        self._log_info("测量开始…")

    def stop_run(self) -> None:
        if self.worker:
            self.worker.stop()
            self.btn_stop.setEnabled(False)

    def on_finished(self, result: RunResult) -> None:
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress_panel.finish(success=True)
        self._reset_status()
        self._log_info("测量完成。")

        QTimer.singleShot(50, lambda: self._handle_result(result))

    def on_failed(self, message: str) -> None:
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress_panel.finish(success=False)
        self._reset_status()
        self._log_error(message)
        QMessageBox.critical(self, "测量失败", message[:1200])

    def _handle_result(self, result: RunResult) -> None:
        """延迟执行：先让 UI 刷新，再入库 + 弹窗。"""
        config_snapshot = {
            "profile": self.config.tunables.profile,
            "deep": self.chk_deep.isChecked(),
            "ecs": self.chk_ecs.isChecked(),
            "api_discovery": self.config.tunables.api_discovery_enabled,
            "servers": result.summary.servers,
            "domains": result.summary.domains,
            "api_discovered": result.summary.api_discovered,
        }
        try:
            self.last_mid = self.store.save(result, config_snapshot)
            self.last_run_label.setText(
                f"<b>最近测量</b> {datetime.now(UTC).astimezone().strftime('%m-%d %H:%M')}<br>"
                f"耗时 {result.summary.duration_s}s · "
                f"出口 IP {result.summary.public_ip or '未知'}<br>"
                f"DNS {result.summary.servers} 台 · "
                f"域名 {result.summary.domains} 个 · "
                f"去重 IP {result.summary.unique_ips} 个"
            )
            self.btn_history.setText("查看结果 ✅")
        except Exception as exc:
            self._log_error(f"保存结果失败：{exc}")
            self.last_mid = None

        msg = QMessageBox(self)
        msg.setWindowTitle("测量完成")
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setText("测量完成！")
        msg.setInformativeText(
            f"耗时 {result.summary.duration_s}s · "
            f"出口 IP {result.summary.public_ip or '未知'} · "
            f"去重 IP {result.summary.unique_ips} 个\n"
            f"服务器 {result.summary.servers} 台 · 域名 {result.summary.domains} 个"
        )
        msg.addButton("关闭", QMessageBox.ButtonRole.RejectRole)
        open_btn = msg.addButton("打开优选历史", QMessageBox.ButtonRole.AcceptRole)
        msg.exec()
        if msg.clickedButton() is open_btn:
            self.show_history(self.last_mid)

    def _reset_status(self) -> None:
        self.progress_panel.reset()

    def show_history(self, select_id: str | None = None) -> None:
        dlg = HistoryWindow(self.store, select_id=select_id, parent=self)
        dlg.exec()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(3000)
        self._save_geometry()
        self.config.save()
        super().closeEvent(event)

    def _restore_geometry(self) -> None:
        s = QSettings()
        geo = s.value("main_window/geometry")
        if geo:
            self.restoreGeometry(geo)
        state = s.value("main_window/state")
        if state:
            self.restoreState(state)

    def _save_geometry(self) -> None:
        s = QSettings()
        s.setValue("main_window/geometry", self.saveGeometry())
        s.setValue("main_window/state", self.saveState())