"""域名管理器对话框。

展示所有域名，支持类别筛选、启用/禁用、添加/删除、扫描浏览器历史、查看关联域名。
与 DomainStore / RelationStore 直接交互，不依赖 Config。
"""

from __future__ import annotations

import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from dnspick.domain_store import DomainStore
from dnspick.models import Group
from dnspick.relation_store import RelationStore

# 类别颜色（与设计文档一致）
_CATEGORY_COLORS: dict[str, QColor] = {
    "CDN调度": QColor("#e74c3c"),
    "国内普通": QColor("#3498db"),
    "国外": QColor("#2ecc71"),
    "浏览器历史": QColor("#f39c12"),
    "API发现": QColor("#9b59b6"),
    "手动添加": QColor("#95a5a6"),
}

_SOURCE_LABELS: dict[str, str] = {
    "builtin": "内置",
    "manual": "手动",
    "history": "历史",
    "html": "发现",
    "crt_sh": "发现",
}


def _derive_categories(name: str, group: Group, source: str, note: str) -> list[str]:
    """根据域名属性推导所属类别。"""
    cats: list[str] = []
    if group is Group.CN:
        if source == "builtin":
            cats.append("CDN调度")
        else:
            cats.append("国内普通")
    elif group is Group.GLOBAL:
        cats.append("国外")

    if source == "history":
        cats.append("浏览器历史")
    elif source in ("html", "crt_sh"):
        cats.append("API发现")
    elif source == "manual":
        cats.append("手动添加")

    if not cats:
        cats.append("国内普通")
    return cats


@dataclass
class DomainItem:
    """域名列表中的一行。"""
    name: str
    group: Group
    source: str
    note: str
    enabled: bool = True
    visit_count: int = 0
    categories: list[str] = field(default_factory=list)


class DomainManagerDialog(QDialog):
    """域名管理器对话框。"""

    # 信号：域名数据已变更，通知外部刷新
    domains_changed = Signal()

    def __init__(
        self,
        domain_store: DomainStore,
        relation_store: RelationStore | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = domain_store
        self._rel_store = relation_store or RelationStore()
        self._rel_store.init()
        self._items: list[DomainItem] = []
        self._filter_cats: set[str] = set()

        self.setWindowTitle("域名管理器")
        self.resize(960, 640)
        self._build_ui()
        self._load()

    # -- UI 构建 ----------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # 主体：左侧类别筛选 + 右侧域名表格
        body = QHBoxLayout()

        # 左侧：类别筛选
        cat_box = QGroupBox("域名类别")
        cat_layout = QVBoxLayout(cat_box)
        self._cat_checks: dict[str, QCheckBox] = {}
        for cat in ["CDN调度", "国内普通", "国外", "浏览器历史", "API发现", "手动添加"]:
            cb = QCheckBox(cat)
            cb.setChecked(True)
            cb.stateChanged.connect(self._on_filter_changed)
            cat_layout.addWidget(cb)
            self._cat_checks[cat] = cb
        cat_layout.addStretch(1)
        body.addWidget(cat_box)

        # 右侧：表格 + 操作按钮
        right = QVBoxLayout()

        self._table = QTableWidget()
        self._table.setColumnCount(7)
        self._table.setHorizontalHeaderLabels(["启用", "域名", "分组", "类别", "来源", "访问", "备注"])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(0, 40)
        self._table.setColumnWidth(1, 220)
        self._table.setColumnWidth(2, 50)
        self._table.setColumnWidth(3, 80)
        self._table.setColumnWidth(4, 50)
        self._table.setColumnWidth(5, 50)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        right.addWidget(self._table, 1)

        # 全选/反选
        sel_row = QHBoxLayout()
        btn_all = QPushButton("全选")
        btn_all.clicked.connect(self._select_all)
        btn_none = QPushButton("全不选")
        btn_none.clicked.connect(self._select_none)
        btn_invert = QPushButton("反选")
        btn_invert.clicked.connect(self._invert_selection)
        sel_row.addWidget(btn_all)
        sel_row.addWidget(btn_none)
        sel_row.addWidget(btn_invert)
        sel_row.addStretch(1)
        right.addLayout(sel_row)

        # 操作按钮
        op_row = QHBoxLayout()
        self._btn_scan = QPushButton("扫描浏览器历史")
        self._btn_scan.clicked.connect(self._scan_browser)
        self._btn_add = QPushButton("+ 添加域名")
        self._btn_add.clicked.connect(self._add_domain)
        self._btn_del = QPushButton("− 删除选中")
        self._btn_del.clicked.connect(self._delete_selected)
        self._btn_relation = QPushButton("查看关联")
        self._btn_relation.clicked.connect(self._show_relations)
        self._btn_import = QPushButton("导入 TOML…")
        self._btn_import.clicked.connect(self._import_toml)
        op_row.addWidget(self._btn_scan)
        op_row.addWidget(self._btn_add)
        op_row.addWidget(self._btn_del)
        op_row.addWidget(self._btn_relation)
        op_row.addWidget(self._btn_import)
        op_row.addStretch(1)
        right.addLayout(op_row)

        body.addLayout(right, 1)
        root.addLayout(body, 1)

        # 底部：选中域名详情 + 关联预览
        detail_box = QGroupBox("域名详情")
        detail_layout = QVBoxLayout(detail_box)
        detail_layout.setContentsMargins(8, 6, 8, 6)
        self._detail_label = QLabel("")
        self._detail_label.setWordWrap(True)
        self._detail_label.setStyleSheet("color: #888; font-size: 12px;")
        detail_layout.addWidget(self._detail_label)

        self._relation_label = QLabel("")
        self._relation_label.setWordWrap(True)
        self._relation_label.setStyleSheet("color: #9b59b6; font-size: 12px; padding-top: 4px;")
        self._relation_label.setVisible(False)
        detail_layout.addWidget(self._relation_label)
        root.addWidget(detail_box)

        # 确定/取消
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(self._on_accept)
        btn_box.rejected.connect(self.reject)
        root.addWidget(btn_box)

    # -- 数据加载 ---------------------------------------------------------

    def _load(self) -> None:
        """从 DomainStore 载入全部域名并刷新表格。"""
        with self._store._conn() as conn:
            rows = conn.execute(
                "SELECT name, group_name, source, note, enabled, visit_count"
                " FROM domain ORDER BY source, name"
            ).fetchall()

        self._items = []
        for row in rows:
            name, group_name, source, note, enabled, visit_count = row
            group = Group(group_name)
            self._items.append(
                DomainItem(
                    name=name,
                    group=group,
                    source=source,
                    note=note or "",
                    enabled=bool(enabled),
                    visit_count=visit_count or 0,
                    categories=_derive_categories(name, group, source, note or ""),
                )
            )
        self._refresh_table()

    def _refresh_table(self) -> None:
        """根据筛选条件刷新表格。"""
        self._table.setRowCount(0)
        visible: list[DomainItem] = []
        for item in self._items:
            if self._filter_cats and not set(item.categories) & self._filter_cats:
                continue
            visible.append(item)

        self._table.setRowCount(len(visible))
        for row, item in enumerate(visible):
            # 启用复选框
            chk = QTableWidgetItem("✓" if item.enabled else "✗")
            chk.setFlags(chk.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            chk.setCheckState(Qt.CheckState.Checked if item.enabled else Qt.CheckState.Unchecked)
            chk.setData(Qt.ItemDataRole.UserRole, item.name)
            self._table.setItem(row, 0, chk)

            self._table.setItem(row, 1, QTableWidgetItem(item.name))
            self._table.setItem(row, 2, QTableWidgetItem(Group.CN.value if item.group is Group.CN else "global"))

            # 类别（多类别用 · 分隔，带颜色）
            cat_text = " · ".join(item.categories)
            cat_item = QTableWidgetItem(cat_text)
            if item.categories:
                cat_item.setForeground(_CATEGORY_COLORS.get(item.categories[0], QColor("#888")))
            self._table.setItem(row, 3, cat_item)

            self._table.setItem(row, 4, QTableWidgetItem(_SOURCE_LABELS.get(item.source, item.source)))
            self._table.setItem(row, 5, QTableWidgetItem(str(item.visit_count) if item.visit_count > 0 else "-"))
            self._table.setItem(row, 6, QTableWidgetItem(item.note))

        # 表头统计
        enabled_count = sum(1 for it in visible if it.enabled)
        self._detail_label.setText(
            f"共 {len(self._items)} 个域名，筛选显示 {len(visible)} 个（已启用 {enabled_count} 个）"
        )

    # -- 事件 -------------------------------------------------------------

    def _on_filter_changed(self) -> None:
        self._filter_cats = {
            cat for cat, cb in self._cat_checks.items() if cb.isChecked()
        }
        self._refresh_table()

    def _on_selection_changed(self) -> None:
        rows = set()
        for idx in self._table.selectedIndexes():
            rows.add(idx.row())

        self._relation_label.setVisible(False)
        self._relation_label.setText("")

        if len(rows) == 1:
            row = next(iter(rows))
            name_item = self._table.item(row, 1)
            if name_item:
                name = name_item.text()
                item = next((it for it in self._items if it.name == name), None)
                if item:
                    cats = " · ".join(item.categories)
                    vc = f"访问 {item.visit_count} 次" if item.visit_count > 0 else ""
                    self._detail_label.setText(
                        f"选中: <b>{item.name}</b>  [{cats}]  "
                        f"来源: {_SOURCE_LABELS.get(item.source, item.source)}  {vc}"
                    )

                    # 查询关联域名
                    rels = self._rel_store.get_relations(name)
                    if rels:
                        rel_preview = " · ".join(rels[:20])
                        more = f" …等 {len(rels)} 个" if len(rels) > 20 else ""
                        self._relation_label.setText(
                            f"├─ 关联域名 ({len(rels)} 个): {rel_preview}{more}"
                        )
                        self._relation_label.setVisible(True)

        if len(rows) == 0:
            enabled_count = sum(1 for it in self._items if it.enabled)
            self._detail_label.setText(
                f"共 {len(self._items)} 个域名，已启用 {enabled_count} 个"
            )

    def _select_all(self) -> None:
        for row in range(self._table.rowCount()):
            chk = self._table.item(row, 0)
            if chk:
                chk.setCheckState(Qt.CheckState.Checked)
        self._apply_enabled_state()

    def _select_none(self) -> None:
        for row in range(self._table.rowCount()):
            chk = self._table.item(row, 0)
            if chk:
                chk.setCheckState(Qt.CheckState.Unchecked)
        self._apply_enabled_state()

    def _invert_selection(self) -> None:
        for row in range(self._table.rowCount()):
            chk = self._table.item(row, 0)
            if chk:
                current = chk.checkState()
                chk.setCheckState(
                    Qt.CheckState.Unchecked if current == Qt.CheckState.Checked else Qt.CheckState.Checked
                )
        self._apply_enabled_state()

    def _apply_enabled_state(self) -> None:
        """将表格勾选状态同步回 _items。"""
        # 构建 name → checked 映射
        name_checked: dict[str, bool] = {}
        for row in range(self._table.rowCount()):
            chk = self._table.item(row, 0)
            name_item = self._table.item(row, 1)
            if chk and name_item:
                name_checked[name_item.text()] = chk.checkState() == Qt.CheckState.Checked

        for item in self._items:
            if item.name in name_checked:
                item.enabled = name_checked[item.name]

    # -- 操作 -------------------------------------------------------------

    def _scan_browser(self) -> None:
        """在后台线程扫描浏览器历史，避免阻塞 UI。"""
        try:
            from dnspick.discover import mine_browser_history
        except ImportError:
            QMessageBox.warning(self, "不可用", "浏览器历史扫描需要 discover 模块。")
            return

        self._btn_scan.setEnabled(False)
        self._btn_scan.setText("扫描中…")
        self._scan_result: list[str] | None = None
        self._scan_error: str | None = None

        def _run() -> None:
            try:
                self._scan_result = mine_browser_history(min_hits=2)
            except Exception as exc:
                self._scan_error = str(exc)

        scan_thread = threading.Thread(target=_run, daemon=True)
        self._scan_timer = QTimer(self)
        self._scan_timer.setInterval(100)

        def _check() -> None:
            if scan_thread.is_alive():
                return
            self._scan_timer.stop()
            self._on_scan_finished()

        self._scan_timer.timeout.connect(_check)
        self._scan_timer.start()
        scan_thread.start()

    def _on_scan_finished(self) -> None:
        """扫描线程完成后在主线程处理结果。"""
        if self._scan_error:
            QMessageBox.critical(self, "扫描失败", f"扫描浏览器历史时出错：{self._scan_error}")
            self._btn_scan.setEnabled(True)
            self._btn_scan.setText("扫描浏览器历史")
            return

        found = self._scan_result
        if not found:
            QMessageBox.information(self, "无结果", "未找到浏览器历史记录。")
            self._btn_scan.setEnabled(True)
            self._btn_scan.setText("扫描浏览器历史")
            return

        cn_tlds = (".cn", ".com.cn", ".net.cn", ".org.cn", ".中国")
        existing = {it.name for it in self._items}
        cn_new = [d for d in found if d not in existing and any(d.endswith(s) for s in cn_tlds)]
        gl_new = [d for d in found if d not in existing and d not in cn_new]
        skip_count = len(found) - len(cn_new) - len(gl_new)
        new_count = 0
        if cn_new:
            new_count += self._store.upsert_batch(cn_new, Group.CN, "history", "")
        if gl_new:
            new_count += self._store.upsert_batch(gl_new, Group.GLOBAL, "history", "")

        QMessageBox.information(
            self,
            "扫描完成",
            f"发现 {len(found)} 个域名\n"
            f"  · 新增 {new_count} 个\n"
            f"  · 已存在 {skip_count} 个",
        )

        self._btn_scan.setEnabled(True)
        self._btn_scan.setText("扫描浏览器历史")
        self._load()

    def _add_domain(self) -> None:
        name, ok = QInputDialog.getText(self, "添加域名", "输入完整域名（如 www.example.com）：")
        if not ok or not name.strip():
            return
        name = name.strip()

        if self._store.exists(name):
            QMessageBox.information(self, "已存在", f"域名 {name} 已在列表中。")
            return

        group_text, ok2 = QInputDialog.getItem(
            self, "选择分组", "域名分组：", ["cn", "global"], 0, False
        )
        if not ok2:
            return

        group = Group(group_text)
        self._store.upsert(name, group, "manual", "")
        self._load()
        self.domains_changed.emit()

    def _delete_selected(self) -> None:
        rows = set()
        for idx in self._table.selectedIndexes():
            rows.add(idx.row())

        names_to_delete: set[str] = set()
        for row in rows:
            name_item = self._table.item(row, 1)
            if name_item:
                names_to_delete.add(name_item.text())

        if not names_to_delete:
            return

        deleted = self._store.delete_batch(names_to_delete)
        skipped = len(names_to_delete) - deleted

        if deleted > 0:
            self._load()
            self.domains_changed.emit()

        if skipped > 0:
            QMessageBox.information(
                self,
                "删除结果",
                f"已删除 {deleted} 个域名，{skipped} 个内置域名不可删除。",
            )

    def _show_relations(self) -> None:
        """显示选中域名的全部关联域名。"""
        rows = set()
        for idx in self._table.selectedIndexes():
            rows.add(idx.row())
        if not rows:
            QMessageBox.information(self, "提示", "请先在列表中选中一个域名。")
            return

        row = next(iter(rows))
        name_item = self._table.item(row, 1)
        if not name_item:
            return
        name = name_item.text()

        all_rels = self._rel_store.get_all_relations(name)
        if not all_rels:
            QMessageBox.information(self, "无关联", f"域名 {name} 没有任何关联域名记录。")
            return

        lines = [f"<b>{name}</b> 的关联域名："]
        lines.append("")
        by_source: dict[str, list[str]] = {}
        for r in all_rels:
            by_source.setdefault(r["source"], []).append(r["related_domain"])

        source_labels = {"html": "HTML 抓取", "crt_sh": "crt.sh 证书", "manual": "手动关联"}
        for src, domains in by_source.items():
            label = source_labels.get(src, src)
            lines.append(f"  [{label}] {len(domains)} 个:")
            for d in sorted(domains):
                lines.append(f"    · {d}")

        QMessageBox.information(self, f"关联域名 - {name}", "<br>".join(lines[:50]))

    def _import_toml(self) -> None:
        """从 TOML 文件导入域名到 DomainStore。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "导入 TOML 域名文件", "", "TOML 文件 (*.toml);;所有文件 (*)"
        )
        if not path:
            return

        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except Exception as exc:
            QMessageBox.critical(self, "导入失败", f"读取 TOML 文件失败：{exc}")
            return

        domains_to_import: list[tuple[str, str]] = []

        # 支持两种格式：
        # 1) [[domain]] name = "..." group = "cn"   (旧格式)
        # 2) [domains] cn = ["..."] global = ["..."] (discover 输出格式)
        if "domain" in data:
            for entry in data.get("domain", []):
                if isinstance(entry, dict) and "name" in entry:
                    g = entry.get("group", "cn")
                    domains_to_import.append((entry["name"], g))
        elif "domains" in data:
            doms = data["domains"]
            for g in ("cn", "global"):
                for d in doms.get(g, []):
                    if isinstance(d, str):
                        domains_to_import.append((d, g))

        if not domains_to_import:
            QMessageBox.information(self, "导入结果", "TOML 文件中未找到域名条目。")
            return

        existing = {it.name for it in self._items}
        cn_new = [name for name, g in domains_to_import if name not in existing and g == "cn"]
        gl_new = [name for name, g in domains_to_import if name not in existing and g == "global"]
        skip_count = len(domains_to_import) - len(cn_new) - len(gl_new)
        new_count = 0
        note = f"从 {Path(path).name} 导入"
        if cn_new:
            self._store.upsert_batch(cn_new, Group.CN, "manual", note)
            new_count += len(cn_new)
        if gl_new:
            self._store.upsert_batch(gl_new, Group.GLOBAL, "manual", note)
            new_count += len(gl_new)

        QMessageBox.information(
            self,
            "导入完成",
            f"从 {Path(path).name} 导入：\n"
            f"  · 新增 {new_count} 个\n"
            f"  · 已存在（跳过）{skip_count} 个",
        )
        self._load()
        self.domains_changed.emit()

    def _on_accept(self) -> None:
        """确定：将启用/禁用状态批量写回 DomainStore。"""
        enabled_names = {item.name for item in self._items if item.enabled}
        disabled_names = {item.name for item in self._items if not item.enabled}
        if enabled_names:
            self._store.set_enabled_batch(enabled_names, True)
        if disabled_names:
            self._store.set_enabled_batch(disabled_names, False)
        self.domains_changed.emit()
        self.accept()

    # -- 公开方法 ---------------------------------------------------------

    def get_enabled_domains(self) -> list[str]:
        """返回当前启用的域名的 name 列表。"""
        return [it.name for it in self._items if it.enabled]