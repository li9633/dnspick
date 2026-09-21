"""优选历史 —— 结果分析与对比中心（Tab 化重构版）。

支持：
- 历史测量列表浏览（自定义卡片）
- 按协议 / 地区 / 标签 / 结论 / 厂商筛选（紧凑单行多选下拉）
- 三个 Tab：结果总览 / 分组对比 / 分析建议
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QPoint, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from dnspick.store import ResultStore, ScoreFilter

_VERDICT_COLOR: dict[str, QColor] = {
    "recommend": QColor("#1a9850"),
    "fallback": QColor("#f1a340"),
    "narrow": QColor("#4d90d9"),
    "avoid": QColor("#d73027"),
}
_VERDICT_TEXT: dict[str, str] = {
    "recommend": "推荐",
    "fallback": "备用",
    "narrow": "限定分组",
    "avoid": "避免",
}

_ALL_PROTOCOLS = ["udp", "tcp", "dot", "doh"]
_ALL_REGIONS = ["cn", "global"]
_ALL_VERDICTS = ["recommend", "fallback", "narrow", "avoid"]
_ALL_TAGS = ["ecs", "cdn", "encrypted", "anycast", "security", "block"]

_FLAT_COLS = [
    ("#", 35), ("名称", 120), ("地址", 160), ("协议", 50),
    ("地区", 45), ("综合分", 65), ("结论", 70),
    ("国内延迟", 75), ("国外延迟", 75), ("跳数", 50),
    ("解析率", 60), ("可达率", 55), ("就近", 50),
    ("劫持", 45), ("ECS", 55), ("建议", 280),
]


class _MultiSelectCombo(QWidget):
    """紧凑多选下拉控件：QPushButton + QMenu with checkable actions。"""

    filter_changed = Signal()

    def __init__(
        self, label: str, options: list[tuple[str, str]], parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        lbl = QLabel(f"{label}:")
        layout.addWidget(lbl)

        self._btn = QPushButton("全部")
        self._btn.setFixedHeight(24)
        self._btn.clicked.connect(self._show_menu)
        layout.addWidget(self._btn)

        self._menu = QMenu(self)
        for value, display in options:
            action = self._menu.addAction(display)
            action.setData(value)
            action.setCheckable(True)
            action.setChecked(True)
            action.toggled.connect(self._on_toggle)
        self._update_text()

    def _show_menu(self) -> None:
        pos = self._btn.mapToGlobal(QPoint(0, self._btn.height()))
        self._menu.exec(pos)

    def _on_toggle(self) -> None:
        self._update_text()
        self.filter_changed.emit()

    def _update_text(self) -> None:
        actions = self._menu.actions()
        checked = [a.text() for a in actions if a.isChecked()]
        total = len(actions)
        if len(checked) == total:
            self._btn.setText("全部")
        elif not checked:
            self._btn.setText("无")
        elif len(checked) <= 2:
            self._btn.setText(",".join(checked))
        else:
            self._btn.setText(f"{len(checked)}项")
        self._btn.setFixedWidth(
            max(55, self._btn.fontMetrics().boundingRect(self._btn.text()).width() + 18)
        )

    def get_checked(self) -> list[str]:
        """返回当前选中选项的 value 列表。"""
        return [a.data() for a in self._menu.actions() if a.isChecked()]

    def set_all_checked(self) -> None:
        """全选所有选项。"""
        for action in self._menu.actions():
            action.setChecked(True)
        self._update_text()

    def rebuild(self, options: list[tuple[str, str]]) -> None:
        """重建菜单选项（用于动态更新的厂商列表）。"""
        self._menu.clear()
        for value, display in options:
            action = self._menu.addAction(display)
            action.setData(value)
            action.setCheckable(True)
            action.setChecked(True)
            action.toggled.connect(self._on_toggle)
        self._update_text()


class _HistoryCard(QFrame):
    """历史记录条目卡片。结构化布局 + 底部 1px 分隔线。"""

    def __init__(self, entry: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.entry_id: str = entry.get("id", "")
        self.setFixedHeight(56)
        self.setStyleSheet(
            "_HistoryCard { border-bottom: 1px solid #3a3a3a; padding: 6px 8px; }"
            "_HistoryCard:hover { background: #2d2d2d; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(2)

        ts = entry.get("created_at", "")[:16]
        profile = entry.get("profile", "?")
        servers_cnt = entry.get("servers_cnt", 0)
        domains_cnt = entry.get("domains_cnt", 0)
        top = entry.get("top_name", "—")

        # Row 1: Date (left) + Profile (right)
        row1 = QHBoxLayout()
        row1.setContentsMargins(0, 0, 0, 0)
        ts_label = QLabel(ts)
        ts_label.setStyleSheet(
            "font-weight: bold; font-size: 12px; color: #ddd; background: transparent;"
        )
        profile_label = QLabel(profile)
        profile_label.setStyleSheet("font-size: 10px; color: #888; background: transparent;")
        row1.addWidget(ts_label)
        row1.addStretch(1)
        row1.addWidget(profile_label)
        layout.addLayout(row1)

        # Row 2: Stats
        stats = QLabel(f"{servers_cnt}台 · {domains_cnt}域名")
        stats.setStyleSheet("font-size: 11px; color: #888; background: transparent;")
        layout.addWidget(stats)

        # Row 3: Best server
        best = QLabel(f"\u25b8 {top}")
        best.setStyleSheet("font-size: 11px; color: #3a6ea5; background: transparent;")
        layout.addWidget(best)


class HistoryWindow(QDialog):
    """历史结果分析中心（Tab 化重构版）。"""

    def __init__(
        self,
        store: ResultStore,
        select_id: str | None = None,
        parent: QDialog | None = None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self._entries: list[dict] = []
        self._scores: list[dict] = []
        self._current_mid: str | None = None

        self._select_id = select_id

        self.setWindowTitle("优选历史 —— 结果分析")
        self.resize(1200, 780)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ── 工具栏 ──
        bar = QHBoxLayout()
        self._info_label = QLabel("正在加载...")
        bar.addWidget(self._info_label)
        bar.addStretch(1)
        self._export_btn = QPushButton("导出 JSON")
        self._export_btn.clicked.connect(self._export_json)
        self._delete_btn = QPushButton("删除此记录")
        self._delete_btn.clicked.connect(self._delete_entry)
        self._delete_btn.setEnabled(False)
        self._smartdns_btn = QPushButton("导出 SmartDNS")
        self._smartdns_btn.clicked.connect(self._export_smartdns)
        bar.addWidget(self._export_btn)
        bar.addWidget(self._smartdns_btn)
        bar.addWidget(self._delete_btn)
        bar.addWidget(QPushButton("关闭", clicked=self.close))
        root.addLayout(bar)

        # ── 主体：左列表 + 右（筛选栏 + Tabs）──
        main_splitter = QSplitter(Qt.Orientation.Horizontal)

        left_widget = self._build_left_panel()
        main_splitter.addWidget(left_widget)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(4)

        self._build_filter_bar(right)
        self._build_tabs(right)

        right_widget = QWidget()
        right_widget.setLayout(right)
        main_splitter.addWidget(right_widget)

        main_splitter.setStretchFactor(0, 1)
        main_splitter.setStretchFactor(1, 4)
        root.addWidget(main_splitter, 1)

        QTimer.singleShot(0, self._load_data)

    # =====================================================================
    #  左侧面板：历史列表
    # =====================================================================
    def _build_left_panel(self) -> QWidget:
        left_panel = QVBoxLayout()
        left_panel.setContentsMargins(0, 0, 0, 0)
        left_panel.setSpacing(4)

        left_label = QLabel("历史记录")
        left_label.setStyleSheet("font-weight: bold; color: #aaa;")
        left_panel.addWidget(left_label)

        self._list = QListWidget()
        self._list.setMaximumWidth(200)
        self._list.setMinimumWidth(160)
        self._list.setUniformItemSizes(True)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.currentRowChanged.connect(self._on_select_entry)
        self._list.setStyleSheet(
            "QListWidget { border: 1px solid #444; border-radius: 4px; background: #1e1e1e; }"
            "QListWidget::item:selected { background: #2d4a6d; }"
        )
        left_panel.addWidget(self._list)

        widget = QWidget()
        widget.setLayout(left_panel)
        return widget

    # =====================================================================
    #  筛选栏（紧凑单行）
    # =====================================================================
    def _build_filter_bar(self, parent: QVBoxLayout) -> None:
        bar = QHBoxLayout()
        bar.setContentsMargins(4, 2, 4, 2)
        bar.setSpacing(6)

        proto_opts = [(p, p.upper()) for p in _ALL_PROTOCOLS]
        self._proto_combo = _MultiSelectCombo("协议", proto_opts)
        self._proto_combo.filter_changed.connect(self._on_filter_changed)
        bar.addWidget(self._proto_combo)

        region_opts = [("cn", "国内"), ("global", "国外")]
        self._region_combo = _MultiSelectCombo("地区", region_opts)
        self._region_combo.filter_changed.connect(self._on_filter_changed)
        bar.addWidget(self._region_combo)

        verdict_opts = [(v, _VERDICT_TEXT[v]) for v in _ALL_VERDICTS]
        self._verdict_combo = _MultiSelectCombo("结论", verdict_opts)
        self._verdict_combo.filter_changed.connect(self._on_filter_changed)
        bar.addWidget(self._verdict_combo)

        tag_opts = [(t, t) for t in _ALL_TAGS]
        self._tag_combo = _MultiSelectCombo("标签", tag_opts)
        self._tag_combo.filter_changed.connect(self._on_filter_changed)
        bar.addWidget(self._tag_combo)

        vendor_opts: list[tuple[str, str]] = []
        self._vendor_combo = _MultiSelectCombo("厂商", vendor_opts)
        self._vendor_combo.filter_changed.connect(self._on_filter_changed)
        bar.addWidget(self._vendor_combo)
        self._vendor_combo.hide()

        bar.addStretch(1)

        bar.addWidget(QLabel("分数"))
        bar.addWidget(QLabel("0"))
        self._score_slider_low = QSlider(Qt.Orientation.Horizontal)
        self._score_slider_low.setRange(0, 100)
        self._score_slider_low.setValue(0)
        self._score_slider_low.setFixedWidth(60)
        self._score_slider_high = QSlider(Qt.Orientation.Horizontal)
        self._score_slider_high.setRange(0, 100)
        self._score_slider_high.setValue(100)
        self._score_slider_high.setFixedWidth(60)
        bar.addWidget(self._score_slider_low)
        bar.addWidget(QLabel("—"))
        bar.addWidget(self._score_slider_high)
        bar.addWidget(QLabel("100"))
        self._score_label = QLabel("0–100")
        self._score_label.setFixedWidth(45)
        bar.addWidget(self._score_label)

        self._score_slider_low.valueChanged.connect(self._on_score_changed)
        self._score_slider_high.valueChanged.connect(self._on_score_changed)

        btn_reset = QPushButton("重置")
        btn_reset.setFixedHeight(24)
        btn_reset.clicked.connect(self._reset_filter)
        bar.addWidget(btn_reset)

        parent.addLayout(bar)

    # =====================================================================
    #  Tab 区域
    # =====================================================================
    def _build_tabs(self, parent: QVBoxLayout) -> None:
        self._tabs = QTabWidget()
        self._tabs.currentChanged.connect(self._on_tab_changed)
        self._build_overview_tab()
        self._build_compare_tab()
        self._build_advice_tab()
        parent.addWidget(self._tabs, 1)

    # --- Tab 1: 结果总览 ---
    def _build_overview_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self._overview_stats = QLabel("")
        self._overview_stats.setStyleSheet(
            "font-size: 13px; color: #aaa; padding: 4px;"
        )
        layout.addWidget(self._overview_stats)

        self._flat_table = QTableWidget()
        self._flat_table.setColumnCount(len(_FLAT_COLS))
        self._flat_table.setHorizontalHeaderLabels([c[0] for c in _FLAT_COLS])
        for i, (_name, width) in enumerate(_FLAT_COLS):
            self._flat_table.setColumnWidth(i, width)
        self._flat_table.setSortingEnabled(True)
        self._flat_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._flat_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._flat_table.verticalHeader().setVisible(False)
        hdr = self._flat_table.horizontalHeader()
        hdr.setSectionResizeMode(len(_FLAT_COLS) - 1, QHeaderView.ResizeMode.Stretch)
        self._flat_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self._flat_table.customContextMenuRequested.connect(
            self._on_table_context_menu
        )
        layout.addWidget(self._flat_table)

        self._tabs.addTab(tab, "结果总览")

    # --- Tab 2: 分组对比 ---
    def _build_compare_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("分组方式:"))

        self._group_combo = QComboBox()
        self._group_combo.addItem("厂商 → 协议", ("server_name", "protocol"))
        self._group_combo.addItem("协议 → 地区", ("protocol", "region"))
        self._group_combo.addItem("厂商 → 结论", ("vendor", "verdict"))
        self._group_combo.addItem("协议 → 厂商", ("protocol", "vendor"))
        self._group_combo.currentIndexChanged.connect(self._on_group_changed)
        ctrl.addWidget(self._group_combo)

        ctrl.addStretch(1)

        btn_expand = QPushButton("展开全部")
        btn_expand.clicked.connect(self._expand_all)
        ctrl.addWidget(btn_expand)

        btn_collapse = QPushButton("折叠全部")
        btn_collapse.clicked.connect(self._collapse_all)
        ctrl.addWidget(btn_collapse)

        layout.addLayout(ctrl)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(7)
        self._tree.setHeaderLabels([
            "名称", "协议", "综合分", "国内延迟", "国外延迟", "跳数", "结论",
        ])
        for i, w in enumerate([160, 55, 70, 80, 80, 55, 80]):
            self._tree.setColumnWidth(i, w)
        self._tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tree.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        layout.addWidget(self._tree)

        self._tabs.addTab(tab, "分组对比")

    # --- Tab 3: 分析建议 ---
    def _build_advice_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")

        self._advice_content = QWidget()
        self._advice_layout = QVBoxLayout(self._advice_content)
        self._advice_layout.setContentsMargins(8, 8, 8, 8)
        self._advice_layout.setSpacing(8)
        self._advice_layout.addStretch(1)

        scroll.setWidget(self._advice_content)
        layout.addWidget(scroll)

        self._tabs.addTab(tab, "分析建议")

    # =====================================================================
    #  状态保存/恢复
    # =====================================================================
    def _load_data(self) -> None:
        """延迟加载：窗口先显示，数据再异步填充。"""
        self._refresh_list()
        if self._select_id:
            self._select_id(self._select_id)
        self._restore_state()

    def _restore_state(self) -> None:
        s = QSettings()
        geo = s.value("history_window/geometry")
        if geo:
            self.restoreGeometry(geo)
        tab = s.value("history_window/current_tab")
        if tab is not None:
            self._tabs.setCurrentIndex(int(tab))
        group = s.value("history_window/group_mode")
        if group is not None:
            idx = self._group_combo.findData(group)
            if idx >= 0:
                self._group_combo.setCurrentIndex(idx)

    def _save_state(self) -> None:
        s = QSettings()
        s.setValue("history_window/geometry", self.saveGeometry())
        s.setValue("history_window/current_tab", self._tabs.currentIndex())
        s.setValue(
            "history_window/group_mode",
            self._group_combo.currentData(),
        )

    def closeEvent(self, event) -> None:  # noqa: N802
        self._save_state()
        super().closeEvent(event)

    # =====================================================================
    #  筛选逻辑
    # =====================================================================
    def _on_filter_changed(self) -> None:
        self._apply_filter()

    def _on_tab_changed(self, _index: int) -> None:
        pass

    def _on_group_changed(self, _index: int) -> None:
        self._apply_filter()

    def _on_score_changed(self) -> None:
        low = self._score_slider_low.value()
        high = self._score_slider_high.value()
        if low > high:
            self._score_slider_low.setValue(high)
            low = high
        self._score_label.setText(f"{low}–{high}")

    def _build_filter(self) -> ScoreFilter:
        protocols = self._proto_combo.get_checked()
        regions = self._region_combo.get_checked()
        verdicts = self._verdict_combo.get_checked()
        tags = self._tag_combo.get_checked()
        vendors = (
            self._vendor_combo.get_checked()
            if self._vendor_combo.isVisible()
            else []
        )
        low = self._score_slider_low.value()
        high = self._score_slider_high.value()
        return ScoreFilter(
            protocols=(
                protocols if len(protocols) < len(_ALL_PROTOCOLS) else None
            ),
            regions=(
                regions if len(regions) < len(_ALL_REGIONS) else None
            ),
            verdicts=(
                verdicts if len(verdicts) < len(_ALL_VERDICTS) else None
            ),
            tags=tags if len(tags) < len(_ALL_TAGS) else None,
            vendors=vendors if vendors else None,
            min_score=low if low > 0 else None,
            max_score=high if high < 100 else None,
        )

    def _reset_filter(self) -> None:
        self._proto_combo.set_all_checked()
        self._region_combo.set_all_checked()
        self._verdict_combo.set_all_checked()
        self._tag_combo.set_all_checked()
        if self._vendor_combo.isVisible():
            self._vendor_combo.set_all_checked()
        self._score_slider_low.setValue(0)
        self._score_slider_high.setValue(100)
        self._apply_filter()

    def _refresh_vendor_filter(self) -> None:
        """动态更新厂商下拉选项。"""
        if self._current_mid is None:
            self._vendor_combo.hide()
            return
        try:
            scores = self.store.get_scores(self._current_mid)
        except Exception:
            self._vendor_combo.hide()
            return
        vendors = sorted(
            {s.get("vendor", "") for s in scores if s.get("vendor")}
        )
        if not vendors:
            self._vendor_combo.hide()
            return

        self._vendor_combo.rebuild([(v, v) for v in vendors])
        self._vendor_combo.show()

    # =====================================================================
    #  历史列表
    # =====================================================================
    def _refresh_list(self) -> None:
        self._entries = self.store.list_measurements()
        self._info_label.setText(f"共 {len(self._entries)} 条历史记录")
        self._list.clear()
        for entry in self._entries:
            card = _HistoryCard(entry)
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, entry.get("id", ""))
            item.setSizeHint(card.sizeHint())
            self._list.addItem(item)
            self._list.setItemWidget(item, card)

    def _select_id(self, entry_id: str) -> None:
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item and item.data(Qt.ItemDataRole.UserRole) == entry_id:
                self._list.setCurrentRow(i)
                return

    def _on_select_entry(self, row: int) -> None:
        if row < 0 or row >= len(self._entries):
            self._scores = []
            self._current_mid = None
            self._flat_table.setRowCount(0)
            self._tree.clear()
            self._clear_advice()
            self._delete_btn.setEnabled(False)
            self._overview_stats.setText("")
            return
        self._delete_btn.setEnabled(True)
        self._current_mid = self._entries[row]["id"]
        self._apply_filter()

    # =====================================================================
    #  筛选/刷新
    # =====================================================================
    def _apply_filter(self) -> None:
        if self._current_mid is None:
            return
        filters = self._build_filter()
        self._refresh_vendor_filter()
        self._show_flat(filters)
        self._show_grouped(filters)
        self._show_advice()

    def _show_flat(self, filters: ScoreFilter) -> None:
        assert self._current_mid is not None
        self._scores = self.store.get_scores(self._current_mid, filters)

        # 统计条
        rec = sum(1 for s in self._scores if s.get("verdict") == "recommend")
        fal = sum(1 for s in self._scores if s.get("verdict") == "fallback")
        avo = sum(1 for s in self._scores if s.get("verdict") == "avoid")
        nar = sum(1 for s in self._scores if s.get("verdict") == "narrow")
        stats_parts: list[str] = []
        if rec:
            stats_parts.append(
                f'<span style="color:#1a9850;">★ 推荐 {rec} 台</span>'
            )
        if fal:
            stats_parts.append(
                f'<span style="color:#f1a340;">○ 备用 {fal} 台</span>'
            )
        if nar:
            stats_parts.append(
                f'<span style="color:#4d90d9;">△ 限定 {nar} 台</span>'
            )
        if avo:
            stats_parts.append(
                f'<span style="color:#d73027;">✗ 不推荐 {avo} 台</span>'
            )
        self._overview_stats.setText(
            " · ".join(stats_parts) if stats_parts else "无数据"
        )

        self._flat_table.setSortingEnabled(False)
        self._flat_table.setRowCount(len(self._scores))
        for row, s in enumerate(self._scores):
            cn_lat = s.get("cn_delay_ms")
            gl_lat = s.get("gl_delay_ms")
            cn_hops = s.get("cn_hops")
            cn_succ = s.get("cn_success_rate")
            cn_reach = s.get("cn_reach_rate")
            cn_prox = s.get("cn_proximity")
            ecs = s.get("ecs_supported")

            vals = [
                (row + 1, str(row + 1)),
                (s["server_name"], s["server_name"]),
                (s["address"], s["address"]),
                (s["protocol"], s["protocol"]),
                (
                    s["region"],
                    "国内" if s["region"] == "cn" else "国外",
                ),
                (s["total_score"], f"{s['total_score']:.1f}"),
                (
                    s["verdict"],
                    _VERDICT_TEXT.get(s["verdict"], s["verdict"]),
                ),
                (
                    cn_lat if cn_lat is not None else 9999.0,
                    "-" if cn_lat is None else f"{cn_lat:.1f}",
                ),
                (
                    gl_lat if gl_lat is not None else 9999.0,
                    "-" if gl_lat is None else f"{gl_lat:.1f}",
                ),
                (
                    cn_hops if cn_hops is not None else 9999.0,
                    "-" if cn_hops is None else f"{cn_hops:.0f}",
                ),
                (
                    cn_succ if cn_succ is not None else -1.0,
                    "-" if cn_succ is None else f"{cn_succ:.0%}",
                ),
                (
                    cn_reach if cn_reach is not None else -1.0,
                    "-" if cn_reach is None else f"{cn_reach:.0%}",
                ),
                (
                    cn_prox if cn_prox is not None else -1.0,
                    "-" if cn_prox is None else f"{cn_prox:.0f}",
                ),
                (
                    1 if s.get("hijack") else 0,
                    "是" if s.get("hijack") else "否",
                ),
                (
                    0 if ecs is None else (1 if ecs else -1),
                    "-" if ecs is None else ("支持" if ecs else "无差异"),
                ),
                (s.get("advice", ""), s.get("advice", "")),
            ]
            for col, (sort_val, text) in enumerate(vals):
                item = QTableWidgetItem()
                item.setData(Qt.ItemDataRole.DisplayRole, sort_val)
                item.setText(text)
                if col == 6:
                    item.setForeground(
                        _VERDICT_COLOR.get(s["verdict"], QColor("#999"))
                    )
                if col == 13 and s.get("hijack"):
                    item.setForeground(_VERDICT_COLOR["avoid"])
                self._flat_table.setItem(row, col, item)
        self._flat_table.setSortingEnabled(True)
        self._flat_table.sortByColumn(5, Qt.SortOrder.DescendingOrder)

    def _on_table_context_menu(self, pos: QPoint) -> None:
        """结果总览表格右键菜单：复制地址、复制 SmartDNS 行。"""
        row = self._flat_table.rowAt(pos.y())
        if row < 0 or row >= len(self._scores):
            return

        addr_item = self._flat_table.item(row, 2)
        if addr_item is None:
            return
        address = addr_item.text()

        menu = QMenu(self)
        copy_addr = menu.addAction(f"复制地址: {address}")
        copy_addr.triggered.connect(lambda: self._copy_to_clipboard(address))

        score = self._scores[row]
        proto = str(score.get("protocol", "udp"))
        smartdns_line = f"server={address}#{proto}"
        copy_sd = menu.addAction(f"复制 SmartDNS 行: {smartdns_line}")
        copy_sd.triggered.connect(
            lambda: self._copy_to_clipboard(smartdns_line)
        )

        menu.exec(self._flat_table.viewport().mapToGlobal(pos))

    @staticmethod
    def _copy_to_clipboard(text: str) -> None:
        QApplication.clipboard().setText(text)

    # =====================================================================
    #  分组对比
    # =====================================================================
    def _show_grouped(self, filters: ScoreFilter) -> None:
        assert self._current_mid is not None
        group_by, sub_by = self._group_combo.currentData()
        groups = self.store.compare(
            self._current_mid, group_by, sub_by, filters,
        )
        self._tree.clear()

        for grp in groups:
            parent = QTreeWidgetItem(self._tree)
            parent.setText(0, grp.key)
            parent.setExpanded(True)
            font = parent.font(0)
            font.setBold(True)
            parent.setFont(0, font)

            for sg in grp.sub_groups:
                if not sg.items:
                    continue
                child = QTreeWidgetItem(parent)
                child.setText(0, f"  {sg.key}")
                child.setExpanded(True)
                cfont = child.font(0)
                cfont.setItalic(True)
                child.setFont(0, cfont)

                for s in sg.items:
                    leaf = QTreeWidgetItem(child)
                    leaf.setText(0, s.get("server_name", ""))
                    proto = s.get("protocol", "")
                    leaf.setText(1, proto.upper())
                    score = s.get("total_score", 0)
                    leaf.setText(2, f"{score:.1f}")
                    leaf.setForeground(
                        2,
                        _VERDICT_COLOR.get(s.get("verdict", ""), QColor("#999")),
                    )
                    cn_lat = s.get("cn_delay_ms")
                    leaf.setText(
                        3, "-" if cn_lat is None else f"{cn_lat:.1f}"
                    )
                    gl_lat = s.get("gl_delay_ms")
                    leaf.setText(
                        4, "-" if gl_lat is None else f"{gl_lat:.1f}"
                    )
                    cn_hops = s.get("cn_hops")
                    leaf.setText(
                        5, "-" if cn_hops is None else f"{cn_hops:.0f}"
                    )
                    leaf.setText(
                        6,
                        _VERDICT_TEXT.get(s.get("verdict", ""), ""),
                    )
                    leaf.setForeground(
                        6,
                        _VERDICT_COLOR.get(s.get("verdict", ""), QColor("#999")),
                    )

    def _expand_all(self) -> None:
        self._tree.expandAll()

    def _collapse_all(self) -> None:
        self._tree.collapseAll()

    # =====================================================================
    #  分析建议
    # =====================================================================
    def _clear_advice(self) -> None:
        while self._advice_layout.count() > 1:
            child = self._advice_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

    def _add_advice_card(
        self, title: str, color: str, items: list[dict],
    ) -> None:
        card = QFrame()
        card.setStyleSheet(
            "QFrame { background: #252525; border: 1px solid #3a3a3a;"
            " border-radius: 6px; }"
        )
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(10, 8, 10, 8)
        card_layout.setSpacing(4)

        header = QLabel(f'<b style="color:{color};">{title}</b>')
        card_layout.addWidget(header)

        for s in items[:5]:
            cn = s.get("cn_delay_ms")
            cn_str = f"国内 {cn:.1f}ms" if cn else "-"
            line = QLabel(
                f"<b>{s['server_name']}</b> [{s['protocol'].upper()}] "
                f"综合分 {s['total_score']:.1f} · {cn_str} · "
                f"{s.get('cn_hops', '-')}跳"
            )
            line.setStyleSheet(
                "font-size: 12px; color: #ccc; background: transparent;"
            )
            card_layout.addWidget(line)

        self._advice_layout.insertWidget(
            self._advice_layout.count() - 1, card,
        )

    def _show_advice(self) -> None:
        self._clear_advice()

        if not self._scores:
            placeholder = QLabel("选择一个历史测量记录后显示分析建议")
            placeholder.setStyleSheet("color: #888; font-size: 13px;")
            self._advice_layout.insertWidget(0, placeholder)
            return

        recs = [s for s in self._scores if s.get("verdict") == "recommend"]
        falls = [s for s in self._scores if s.get("verdict") == "fallback"]
        narrows = [s for s in self._scores if s.get("verdict") == "narrow"]
        avoids = [s for s in self._scores if s.get("verdict") == "avoid"]

        if recs:
            self._add_advice_card(
                f"★ 推荐 ({len(recs)})", "#1a9850", recs,
            )
        if falls:
            self._add_advice_card(
                f"○ 备用 ({len(falls)})", "#f1a340", falls,
            )
        if narrows:
            self._add_advice_card(
                f"△ 限定分组 ({len(narrows)})", "#4d90d9", narrows,
            )
        if avoids:
            self._add_advice_card(
                f"✗ 不推荐 ({len(avoids)})", "#d73027", avoids,
            )

        # 同厂商协议对比
        providers: dict[str, list[dict]] = {}
        for s in self._scores:
            vendor = s.get("vendor", "")
            if not vendor:
                continue
            providers.setdefault(vendor, []).append(s)

        multi_vendors = {
            v: items for v, items in providers.items() if len(items) >= 2
        }
        if multi_vendors:
            card = QFrame()
            card.setStyleSheet(
                "QFrame { background: #252525; border: 1px solid #3a3a3a;"
                " border-radius: 6px; }"
            )
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(10, 8, 10, 8)
            card_layout.setSpacing(4)

            header = QLabel(
                '<b style="color:#6baed6;">📊 同厂商协议对比</b>'
            )
            card_layout.addWidget(header)

            for vendor, items in sorted(multi_vendors.items()):
                items.sort(
                    key=lambda x: x.get("total_score", 0), reverse=True,
                )
                summary_parts = []
                for s in items:
                    cn = s.get("cn_delay_ms")
                    cn_str = f"{cn:.1f}ms" if cn else "-"
                    summary_parts.append(
                        f"{s['protocol'].upper()} "
                        f"{s['total_score']:.1f}分(国内 {cn_str})"
                    )
                summary = " · ".join(summary_parts)
                name = items[0].get("server_name", vendor)
                short = name.rsplit(" ", 1)[0] if " " in name else name
                line = QLabel(f"{short}: {summary}")
                line.setStyleSheet(
                    "font-size: 12px; color: #aaa; background: transparent;"
                )
                card_layout.addWidget(line)

            self._advice_layout.insertWidget(
                self._advice_layout.count() - 1, card,
            )

    # =====================================================================
    #  导出 / 删除
    # =====================================================================
    def _export_json(self) -> None:
        if not self._scores:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出结果", "dnspick-result.json", "JSON (*.json)",
        )
        if not path:
            return
        data = {
            "measurement_id": self._current_mid,
            "scores": self._scores,
        }
        Path(path).write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _export_smartdns(self) -> None:
        if not self._scores:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出 SmartDNS 配置",
            "smartdns-servers.conf",
            "配置 (*.conf);;所有文件 (*)",
        )
        if not path:
            return

        _SMARTDNS_PROTO = {
            "udp": "udp", "tcp": "tcp", "dot": "tls", "doh": "https",
        }
        seen: set[str] = set()
        blocks: list[str] = []

        for s in self._scores:
            proto = str(s.get("protocol", "udp"))
            sd_proto = _SMARTDNS_PROTO.get(proto, proto)
            addr = str(s.get("address", ""))
            vendor = str(s.get("vendor", "")).lower()
            name = (
                f"{vendor}_{proto}"
                if vendor
                else s.get("server_name", "unknown")
            )
            dedup_key = f"{addr}|{sd_proto}"
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            blocks.append(
                "config server\n"
                f"        option name '{name}'\n"
                f"        option type '{sd_proto}'\n"
                f"        option ip '{addr}'\n"
                "        option enabled '1'\n"
            )

        Path(path).write_text("\n".join(blocks), encoding="utf-8")

    def _delete_entry(self) -> None:
        if self._current_mid is None:
            return
        row = self._list.currentRow()
        if row < 0 or row >= len(self._entries):
            return
        entry = self._entries[row]
        ok = QMessageBox.question(
            self,
            "确认删除",
            f"确定删除 {entry.get('created_at', '')[:16]} 的记录？",
        )
        if ok == QMessageBox.StandardButton.Yes:
            self.store.delete(self._current_mid)
            self._current_mid = None
            self._scores = []
            self._flat_table.setRowCount(0)
            self._tree.clear()
            self._clear_advice()
            self._delete_btn.setEnabled(False)
            self._overview_stats.setText("")
            self._refresh_list()