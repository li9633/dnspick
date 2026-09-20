"""优选历史 —— 结果分析与对比中心。

支持：
- 历史测量列表浏览
- 按协议 / 地区 / 标签 / 结论筛选
- 分组对比视图（如：阿里云 UDP vs DoH vs DoT）
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSlider,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
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

_VERDICT_BG: dict[str, str] = {
    "recommend": "#1a9850",
    "fallback": "#f1a340",
    "narrow": "#4d90d9",
    "avoid": "#d73027",
}

# 筛选面板中可用的协议、地区、结论选项
_ALL_PROTOCOLS = ["udp", "tcp", "dot", "doh"]
_ALL_REGIONS = ["cn", "global"]
_ALL_VERDICTS = ["recommend", "fallback", "narrow", "avoid"]
_ALL_TAGS = ["ecs", "cdn", "encrypted", "anycast", "security", "block"]

# 平铺表格列
_FLAT_COLS = [
    ("#", 35), ("名称", 120), ("地址", 160), ("协议", 50),
    ("地区", 45), ("综合分", 65), ("结论", 70),
    ("国内延迟", 75), ("国外延迟", 75), ("跳数", 50),
    ("解析率", 60), ("可达率", 55), ("就近", 50),
    ("劫持", 45), ("ECS", 55), ("建议", 280),
]


class HistoryWindow(QDialog):
    """历史结果分析中心。"""

    def __init__(
        self,
        store: ResultStore,
        select_id: str | None = None,
        parent: QDialog | None = None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self._entries: list[dict] = []
        self._scores: list[dict] = []  # 当前选中测量的得分
        self._current_mid: str | None = None

        self.setWindowTitle("优选历史 —— 结果分析")
        self.resize(1200, 780)

        root = QVBoxLayout(self)

        # ── 工具栏 ──
        bar = QHBoxLayout()
        self._info_label = QLabel("")
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

        # ── 主体：左列表 + 右筛选/结果 ──
        main_splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左侧：历史列表
        left_panel = QVBoxLayout()
        left_panel.addWidget(QLabel("历史记录"))
        self._list = QListWidget()
        self._list.setMaximumWidth(260)
        self._list.currentRowChanged.connect(self._on_select_entry)
        left_panel.addWidget(self._list)
        left_widget = QWidget()
        left_widget.setLayout(left_panel)
        main_splitter.addWidget(left_widget)

        # 右侧：筛选 + 结果 + 建议
        right = QVBoxLayout()

        # 筛选面板
        self._build_filter_panel(right)

        # 厂商协议推荐摘要
        self._build_vendor_cards(right)

        # 结果视图
        self._build_result_view(right)

        # 建议面板
        self._build_advice_panel(right)

        right_widget = QWidget()
        right_widget.setLayout(right)
        main_splitter.addWidget(right_widget)
        main_splitter.setStretchFactor(0, 1)
        main_splitter.setStretchFactor(1, 4)
        root.addWidget(main_splitter, 1)

        self._refresh_list()
        if select_id:
            self._select_id(select_id)

        self._restore_state()

    def _restore_state(self) -> None:
        s = QSettings()
        geo = s.value("history_window/geometry")
        if geo:
            self.restoreGeometry(geo)

        mode = s.value("history_window/view_mode")
        if mode:
            idx = self._view_mode.findData(mode)
            if idx >= 0:
                self._view_mode.setCurrentIndex(idx)

        for name, chks in [
            ("proto", self._proto_chks),
            ("region", self._region_chks),
            ("verdict", self._verdict_chks),
            ("tag", self._tag_chks),
        ]:
            saved = s.value(f"history_window/filter_{name}")
            if saved is not None:
                checked = set(saved if isinstance(saved, list) else [saved])
                for k, chk in chks.items():
                    chk.setChecked(k in checked)

        score_low = s.value("history_window/score_low")
        if score_low is not None:
            self._score_slider_low.setValue(int(score_low))
        score_high = s.value("history_window/score_high")
        if score_high is not None:
            self._score_slider_high.setValue(int(score_high))

    def _save_state(self) -> None:
        s = QSettings()
        s.setValue("history_window/geometry", self.saveGeometry())
        s.setValue("history_window/view_mode", self._view_mode.currentData())

        for name, chks in [
            ("proto", self._proto_chks),
            ("region", self._region_chks),
            ("verdict", self._verdict_chks),
            ("tag", self._tag_chks),
        ]:
            checked = [k for k, v in chks.items() if v.isChecked()]
            s.setValue(f"history_window/filter_{name}", checked)

        s.setValue("history_window/score_low", self._score_slider_low.value())
        s.setValue("history_window/score_high", self._score_slider_high.value())

    def closeEvent(self, event) -> None:  # noqa: N802
        self._save_state()
        super().closeEvent(event)

    # -- 筛选面板 ---------------------------------------------------------
    def _build_filter_panel(self, parent: QVBoxLayout) -> None:
        box = QGroupBox("筛选")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(6, 4, 6, 4)

        # 第一行：协议 + 地区 + 结论
        row1 = QHBoxLayout()

        proto_box = QGroupBox("协议")
        proto_lay = QHBoxLayout(proto_box)
        proto_lay.setContentsMargins(4, 2, 4, 2)
        self._proto_chks: dict[str, QCheckBox] = {}
        for p in _ALL_PROTOCOLS:
            chk = QCheckBox(p.upper())
            chk.setChecked(True)
            proto_lay.addWidget(chk)
            self._proto_chks[p] = chk
        row1.addWidget(proto_box)

        region_box = QGroupBox("地区")
        region_lay = QHBoxLayout(region_box)
        region_lay.setContentsMargins(4, 2, 4, 2)
        self._region_chks: dict[str, QCheckBox] = {}
        for r in _ALL_REGIONS:
            label = "国内" if r == "cn" else "国外"
            chk = QCheckBox(label)
            chk.setChecked(True)
            region_lay.addWidget(chk)
            self._region_chks[r] = chk
        row1.addWidget(region_box)

        verdict_box = QGroupBox("结论")
        verdict_lay = QHBoxLayout(verdict_box)
        verdict_lay.setContentsMargins(4, 2, 4, 2)
        self._verdict_chks: dict[str, QCheckBox] = {}
        for v in _ALL_VERDICTS:
            chk = QCheckBox(_VERDICT_TEXT.get(v, v))
            chk.setChecked(True)
            verdict_lay.addWidget(chk)
            self._verdict_chks[v] = chk
        row1.addWidget(verdict_box)

        layout.addLayout(row1)

        # 第二行：标签 + 视图模式 + 按钮
        row2 = QHBoxLayout()

        tag_box = QGroupBox("标签")
        tag_lay = QHBoxLayout(tag_box)
        tag_lay.setContentsMargins(4, 2, 4, 2)
        self._tag_chks: dict[str, QCheckBox] = {}
        for t in _ALL_TAGS:
            chk = QCheckBox(t)
            chk.setChecked(True)
            tag_lay.addWidget(chk)
            self._tag_chks[t] = chk
        row2.addWidget(tag_box)

        row2.addStretch(1)

        self._view_mode = QComboBox()
        self._view_mode.addItem("平铺列表", "flat")
        self._view_mode.addItem("分组对比 (厂商→协议)", "server_name")
        self._view_mode.addItem("分组对比 (协议→地区)", "protocol")
        self._view_mode.addItem("分组对比 (厂商→结论)", "vendor")
        self._view_mode.addItem("分组对比 (协议→厂商)", "protocol_vendor")
        self._view_mode.currentIndexChanged.connect(self._apply_filter)
        row2.addWidget(QLabel("视图:"))
        row2.addWidget(self._view_mode)

        btn_apply = QPushButton("应用筛选")
        btn_apply.clicked.connect(self._apply_filter)
        btn_apply.setFixedHeight(28)
        row2.addWidget(btn_apply)

        btn_reset = QPushButton("重置")
        btn_reset.clicked.connect(self._reset_filter)
        btn_reset.setFixedHeight(28)
        row2.addWidget(btn_reset)

        layout.addLayout(row2)

        # 第三行：厂商 + 分数区间
        row3 = QHBoxLayout()

        self._vendor_chks: dict[str, QCheckBox] = {}

        def _vendor_check_all() -> None:
            for chk in self._vendor_chks.values():
                chk.setChecked(True)

        def _vendor_check_none() -> None:
            for chk in self._vendor_chks.values():
                chk.setChecked(False)

        vendor_box = QGroupBox("厂商")
        vendor_lay = QHBoxLayout(vendor_box)
        vendor_lay.setContentsMargins(4, 2, 4, 2)
        # 初始预留一些常见厂商的 checkbox，后续动态更新
        self._vendor_container = QWidget()
        self._vendor_cb_layout = QHBoxLayout(self._vendor_container)
        self._vendor_cb_layout.setContentsMargins(0, 0, 0, 0)
        self._vendor_cb_layout.setSpacing(4)
        vendor_lay.addWidget(self._vendor_container)
        vendor_lay.addStretch(1)
        btn_vendor_all = QPushButton("全")
        btn_vendor_all.setFixedSize(22, 22)
        btn_vendor_all.clicked.connect(_vendor_check_all)
        btn_vendor_none = QPushButton("清")
        btn_vendor_none.setFixedSize(22, 22)
        btn_vendor_none.clicked.connect(_vendor_check_none)
        vendor_lay.addWidget(btn_vendor_all)
        vendor_lay.addWidget(btn_vendor_none)
        row3.addWidget(vendor_box, 1)

        score_box = QGroupBox("分数区间")
        score_lay = QHBoxLayout(score_box)
        score_lay.setContentsMargins(4, 2, 4, 2)
        score_lay.addWidget(QLabel("0"))
        self._score_slider_low = QSlider(Qt.Orientation.Horizontal)
        self._score_slider_low.setRange(0, 100)
        self._score_slider_low.setValue(0)
        self._score_slider_high = QSlider(Qt.Orientation.Horizontal)
        self._score_slider_high.setRange(0, 100)
        self._score_slider_high.setValue(100)
        score_lay.addWidget(self._score_slider_low)
        score_lay.addWidget(QLabel("—"))
        score_lay.addWidget(self._score_slider_high)
        score_lay.addWidget(QLabel("100"))
        self._score_label = QLabel("0–100")
        self._score_label.setFixedWidth(50)
        self._score_slider_low.valueChanged.connect(self._on_score_changed)
        self._score_slider_high.valueChanged.connect(self._on_score_changed)
        score_lay.addWidget(self._score_label)
        row3.addWidget(score_box, 1)

        layout.addLayout(row3)
        parent.addWidget(box)

    # -- 厂商协议推荐摘要卡片 ------------------------------------------------
    def _build_vendor_cards(self, parent: QVBoxLayout) -> None:
        self._vendor_box = QGroupBox("厂商协议推荐")
        self._vendor_layout = QVBoxLayout(self._vendor_box)
        self._vendor_layout.setContentsMargins(6, 4, 6, 4)
        self._vendor_content = QLabel("选择一个历史记录后显示")
        self._vendor_content.setWordWrap(True)
        self._vendor_content.setStyleSheet("color: #888; font-size: 12px;")
        self._vendor_layout.addWidget(self._vendor_content)
        self._vendor_box.hide()
        parent.addWidget(self._vendor_box)

    def _refresh_vendor_cards(self) -> None:
        """根据当前筛选刷新厂商协议推荐卡片。"""
        if self._current_mid is None:
            self._vendor_box.hide()
            return

        filters = self._build_filter()
        try:
            summary = self.store.vendor_protocol_summary(self._current_mid, filters)
        except Exception:
            self._vendor_box.hide()
            return

        if not summary:
            self._vendor_box.hide()
            return

        self._vendor_box.show()
        lines: list[str] = []

        _vlabel_map = {
            "recommend": ("✅ 推荐", "#1a9850"),
            "fallback": ("⚠ 备用", "#f1a340"),
            "narrow": ("△ 限定", "#4d90d9"),
            "avoid": ("❌ 不推荐", "#d73027"),
        }

        for item in summary:
            vendor_name = item["display_name"] or item["vendor"]
            lines.append(
                '<div style="margin:4px 0;padding:6px;background:#2b2b2b;'
                'border-radius:4px;">'
            )
            lines.append(
                f'<b style="color:#eee;font-size:13px;">★ {vendor_name}</b>'
                f'<br>'
            )
            parts = []
            for p in item["protocols"]:
                v = p["verdict"]
                label, color = _vlabel_map.get(v, ("?", "#999"))
                delay = (
                    f"{p['cn_delay_ms']:.0f}ms"
                    if p.get("cn_delay_ms") is not None
                    else "-"
                )
                hijack_note = " ⚡劫持" if p.get("hijack") else ""
                parts.append(
                    f'<span style="color:{color};">{label}</span> '
                    f'{p["protocol"].upper()} '
                    f'<span style="color:#888;">{p["score"]:.0f}分 · {delay}'
                    f'{hijack_note}</span>'
                )
            lines.append(" · ".join(parts))
            lines.append("</div>")

        self._vendor_content.setText("".join(lines))

    # -- 结果视图 ---------------------------------------------------------
    def _build_result_view(self, parent: QVBoxLayout) -> None:
        # 平铺表格
        self._flat_table = QTableWidget()
        self._flat_table.setColumnCount(len(_FLAT_COLS))
        self._flat_table.setHorizontalHeaderLabels([c[0] for c in _FLAT_COLS])
        for i, (_name, width) in enumerate(_FLAT_COLS):
            self._flat_table.setColumnWidth(i, width)
        self._flat_table.setSortingEnabled(True)
        self._flat_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._flat_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._flat_table.verticalHeader().setVisible(False)
        hdr = self._flat_table.horizontalHeader()
        hdr.setSectionResizeMode(len(_FLAT_COLS) - 1, QHeaderView.ResizeMode.Stretch)

        # 分组树
        self._tree = QTreeWidget()
        self._tree.setColumnCount(7)
        self._tree.setHeaderLabels(["名称", "协议", "综合分", "国内延迟", "国外延迟", "跳数", "结论"])
        for i, w in enumerate([160, 55, 70, 80, 80, 55, 80]):
            self._tree.setColumnWidth(i, w)
        self._tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)

        parent.addWidget(self._flat_table, 3)
        self._tree.hide()

    # -- 建议面板 ---------------------------------------------------------
    def _build_advice_panel(self, parent: QVBoxLayout) -> None:
        self._advice = QTextEdit()
        self._advice.setReadOnly(True)
        self._advice.setMaximumHeight(200)
        parent.addWidget(self._advice)

    # -- 构建筛选条件 -----------------------------------------------------
    def _on_score_changed(self) -> None:
        low = self._score_slider_low.value()
        high = self._score_slider_high.value()
        if low > high:
            self._score_slider_low.setValue(high)
            low = high
        self._score_label.setText(f"{low}–{high}")

    def _refresh_vendor_checkboxes(self) -> None:
        """从当前测量数据中动态获取厂商列表并更新复选框。"""
        if self._current_mid is None:
            return
        try:
            scores = self.store.get_scores(self._current_mid)
        except Exception:
            return
        vendors = sorted({s.get("vendor", "") for s in scores if s.get("vendor")})
        if not vendors:
            return

        # 清除旧复选框
        while self._vendor_cb_layout.count():
            child = self._vendor_cb_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        self._vendor_chks.clear()
        for v in vendors:
            chk = QCheckBox(v)
            chk.setChecked(True)
            self._vendor_cb_layout.addWidget(chk)
            self._vendor_chks[v] = chk
    def _build_filter(self) -> ScoreFilter:
        protocols = [k for k, v in self._proto_chks.items() if v.isChecked()]
        regions = [k for k, v in self._region_chks.items() if v.isChecked()]
        verdicts = [k for k, v in self._verdict_chks.items() if v.isChecked()]
        tags = [k for k, v in self._tag_chks.items() if v.isChecked()]
        vendors = [k for k, v in self._vendor_chks.items() if v.isChecked()]
        low = self._score_slider_low.value()
        high = self._score_slider_high.value()
        return ScoreFilter(
            protocols=protocols if len(protocols) < len(_ALL_PROTOCOLS) else None,
            regions=regions if len(regions) < len(_ALL_REGIONS) else None,
            verdicts=verdicts if len(verdicts) < len(_ALL_VERDICTS) else None,
            tags=tags if len(tags) < len(_ALL_TAGS) else None,
            vendors=vendors if vendors and len(vendors) < len(self._vendor_chks) else None,
            min_score=low if low > 0 else None,
            max_score=high if high < 100 else None,
        )

    def _reset_filter(self) -> None:
        for chks in (
            self._proto_chks, self._region_chks, self._verdict_chks, self._tag_chks,
            self._vendor_chks,
        ):
            for chk in chks.values():
                chk.setChecked(True)
        self._score_slider_low.setValue(0)
        self._score_slider_high.setValue(100)
        self._view_mode.setCurrentIndex(0)
        self._apply_filter()

    # -- 测量列表 ---------------------------------------------------------
    def _refresh_list(self) -> None:
        self._entries = self.store.list_measurements()
        self._info_label.setText(f"共 {len(self._entries)} 条历史记录")
        self._list.clear()
        for entry in self._entries:
            ts = entry.get("created_at", "")[:16]
            top = entry.get("top_name", "—")
            label = (
                f"{ts}\n"
                f"{entry.get('profile', '?')} · "
                f"{entry.get('servers_cnt', 0)}台/{entry.get('domains_cnt', 0)}域名\n"
                f"最快: {top}"
            )
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, entry.get("id", ""))
            self._list.addItem(item)

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
            self._advice.clear()
            self._delete_btn.setEnabled(False)
            return
        self._delete_btn.setEnabled(True)
        self._current_mid = self._entries[row]["id"]
        self._apply_filter()

    # -- 筛选/刷新 --------------------------------------------------------
    def _apply_filter(self) -> None:
        if self._current_mid is None:
            return
        filters = self._build_filter()
        mode = self._view_mode.currentData()

        # 动态更新厂商复选框
        self._refresh_vendor_checkboxes()

        if mode == "flat":
            self._tree.hide()
            self._flat_table.show()
            self._show_flat(filters)
        elif mode == "vendor":
            self._flat_table.hide()
            self._tree.show()
            self._show_grouped("vendor", "verdict", filters)
        elif mode == "protocol_vendor":
            self._flat_table.hide()
            self._tree.show()
            self._show_grouped("protocol", "vendor", filters)
        elif mode == "server_name":
            self._flat_table.hide()
            self._tree.show()
            self._show_grouped("server_name", "protocol", filters)
        elif mode == "protocol":
            self._flat_table.hide()
            self._tree.show()
            self._show_grouped("protocol", "region", filters)
        else:
            self._tree.hide()
            self._flat_table.show()
            self._show_flat(filters)

        self._generate_advice()
        self._refresh_vendor_cards()

    # -- 平铺视图 ---------------------------------------------------------
    def _show_flat(self, filters: ScoreFilter) -> None:
        assert self._current_mid is not None
        self._scores = self.store.get_scores(self._current_mid, filters)
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
                (s["region"], "国内" if s["region"] == "cn" else "国外"),
                (s["total_score"], f"{s['total_score']:.1f}"),
                (s["verdict"], _VERDICT_TEXT.get(s["verdict"], s["verdict"])),
                (cn_lat if cn_lat is not None else 9999.0, "-" if cn_lat is None else f"{cn_lat:.1f}"),
                (gl_lat if gl_lat is not None else 9999.0, "-" if gl_lat is None else f"{gl_lat:.1f}"),
                (cn_hops if cn_hops is not None else 9999.0, "-" if cn_hops is None else f"{cn_hops:.0f}"),
                (cn_succ if cn_succ is not None else -1.0, "-" if cn_succ is None else f"{cn_succ:.0%}"),
                (cn_reach if cn_reach is not None else -1.0, "-" if cn_reach is None else f"{cn_reach:.0%}"),
                (cn_prox if cn_prox is not None else -1.0, "-" if cn_prox is None else f"{cn_prox:.0f}"),
                (1 if s.get("hijack") else 0, "是" if s.get("hijack") else "否"),
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
                    item.setForeground(_VERDICT_COLOR.get(s["verdict"], QColor("#999")))
                if col == 13 and s.get("hijack"):
                    item.setForeground(_VERDICT_COLOR["avoid"])
                self._flat_table.setItem(row, col, item)
        self._flat_table.setSortingEnabled(True)
        self._flat_table.sortByColumn(5, Qt.SortOrder.DescendingOrder)

    # -- 分组对比视图 -----------------------------------------------------
    def _show_grouped(self, group_by: str, sub_by: str, filters: ScoreFilter) -> None:
        assert self._current_mid is not None
        groups = self.store.compare(self._current_mid, group_by, sub_by, filters)
        self._tree.clear()
        self._scores = []

        for grp in groups:
            self._scores.extend(
                item for sg in grp.sub_groups for item in sg.items
            )

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

                for item in sg.items:
                    leaf = QTreeWidgetItem(child)
                    leaf.setText(0, item.get("server_name", ""))

                    proto = item.get("protocol", "")
                    leaf.setText(1, proto.upper())

                    score = item.get("total_score", 0)
                    leaf.setText(2, f"{score:.1f}")
                    leaf.setForeground(2, _VERDICT_COLOR.get(item.get("verdict", ""), QColor("#999")))

                    cn_lat = item.get("cn_delay_ms")
                    leaf.setText(3, "-" if cn_lat is None else f"{cn_lat:.1f}")

                    gl_lat = item.get("gl_delay_ms")
                    leaf.setText(4, "-" if gl_lat is None else f"{gl_lat:.1f}")

                    cn_hops = item.get("cn_hops")
                    leaf.setText(5, "-" if cn_hops is None else f"{cn_hops:.0f}")

                    leaf.setText(6, _VERDICT_TEXT.get(item.get("verdict", ""), ""))
                    leaf.setForeground(6, _VERDICT_COLOR.get(item.get("verdict", ""), QColor("#999")))

    # -- 建议生成 ---------------------------------------------------------
    def _generate_advice(self) -> None:
        if not self._scores:
            self._advice.clear()
            return

        lines: list[str] = ["<h3>分析建议</h3>"]

        # 推荐/备用/避免分类
        recs = [s for s in self._scores if s.get("verdict") == "recommend"]
        falls = [s for s in self._scores if s.get("verdict") == "fallback"]
        avoids = [s for s in self._scores if s.get("verdict") == "avoid"]
        narrows = [s for s in self._scores if s.get("verdict") == "narrow"]

        if recs:
            lines.append("<p><b>★ 推荐</b></p><ul>")
            for s in recs[:5]:
                cn = s.get("cn_delay_ms")
                cn_str = f"{cn:.1f}ms" if cn else "-"
                lines.append(
                    f"<li><b>{s['server_name']}</b> [{s['protocol'].upper()}] "
                    f"综合分 {s['total_score']:.1f} · 国内 {cn_str}</li>"
                )
            lines.append("</ul>")

        if falls:
            lines.append("<p><b>○ 备用</b></p><ul>")
            for s in falls[:5]:
                cn = s.get("cn_delay_ms")
                cn_str = f"{cn:.1f}ms" if cn else "-"
                lines.append(
                    f"<li>{s['server_name']} [{s['protocol'].upper()}] "
                    f"综合分 {s['total_score']:.1f} · 国内 {cn_str}</li>"
                )
            lines.append("</ul>")

        if narrows:
            lines.append("<p><b>△ 限定分组</b></p><ul>")
            for s in narrows[:3]:
                lines.append(
                    f"<li><b>{s['server_name']}</b> [{s['protocol'].upper()}] "
                    f"— {s.get('advice', '')}</li>"
                )
            lines.append("</ul>")

        if avoids:
            lines.append("<p><b>✗ 不推荐</b></p><ul>")
            for s in avoids[:5]:
                reason = "NXDOMAIN 劫持" if s.get("hijack") else "综合表现不佳"
                lines.append(
                    f"<li>{s['server_name']} [{s['protocol'].upper()}] — {reason}</li>"
                )
            lines.append("</ul>")

        # 同厂商不同协议对比（使用 vendor 字段）
        providers: dict[str, list[dict]] = {}
        for s in self._scores:
            vendor = s.get("vendor", "")
            if not vendor:
                continue
            providers.setdefault(vendor, []).append(s)

        if len(providers) > 1:
            lines.append("<p><b>📊 同厂商协议对比</b></p>")
            for vendor, items in sorted(providers.items()):
                if len(items) < 2:
                    continue
                items.sort(key=lambda x: x.get("total_score", 0), reverse=True)
                summary_parts = []
                for s in items:
                    cn = s.get("cn_delay_ms")
                    cn_str = f"{cn:.1f}ms" if cn else "-"
                    summary_parts.append(
                        f"{s['protocol'].upper()} {s['total_score']:.1f}分(国内 {cn_str})"
                    )
                summary = " · ".join(summary_parts)
                name = items[0].get("server_name", vendor)
                short_name = name.rsplit(" ", 1)[0] if " " in name else name
                lines.append(f"<p>{short_name}: {summary}</p>")

        self._advice.setHtml("".join(lines))

    # -- 管理操作 ---------------------------------------------------------
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
            "filter": {
                "protocols": [k for k, v in self._proto_chks.items() if v.isChecked()],
                "regions": [k for k, v in self._region_chks.items() if v.isChecked()],
                "verdicts": [k for k, v in self._verdict_chks.items() if v.isChecked()],
            },
        }
        Path(path).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
        )

    def _export_smartdns(self) -> None:
        if not self._scores:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 SmartDNS 配置", "smartdns-servers.conf",
            "配置 (*.conf);;所有文件 (*)",
        )
        if not path:
            return

        _SMARTDNS_PROTO = {"udp": "udp", "tcp": "tcp", "dot": "tls", "doh": "https"}
        seen: set[str] = set()
        blocks: list[str] = []

        for s in self._scores:
            proto = str(s.get("protocol", "udp"))
            sd_proto = _SMARTDNS_PROTO.get(proto, proto)
            addr = str(s.get("address", ""))
            vendor = str(s.get("vendor", "")).lower()
            name = f"{vendor}_{proto}" if vendor else s.get("server_name", "unknown")

            dedup_key = f"{addr}|{sd_proto}"
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            blocks.append(
                f"config server\n"
                f"        option name '{name}'\n"
                f"        option type '{sd_proto}'\n"
                f"        option ip '{addr}'\n"
                f"        option enabled '1'\n"
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
            self._advice.clear()
            self._delete_btn.setEnabled(False)
            self._refresh_list()