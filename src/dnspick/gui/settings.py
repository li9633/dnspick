"""设置对话框：服务器勾选 + 权重与参数。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from dnspick.config import Config
from dnspick.models import DnsServer, Protocol

_VENDOR_NAMES: dict[str, str] = {
    "alibaba": "阿里巴巴",
    "tencent": "腾讯云",
    "baidu": "百度",
    "cloudflare": "Cloudflare",
    "google": "Google",
    "quad9": "Quad9",
    "adguard": "AdGuard",
    "opendns": "OpenDNS",
    "dnssb": "DNS.SB",
    "controld": "ControlD",
    "114dns": "114DNS",
    "360": "360 DNS",
    "cnnic": "CNNIC",
    "huawei": "华为云",
    "onevps": "ONE DNS",
    "test": "测试",
}

_VENDOR_ORDER: dict[str, int] = {
    "alibaba": 1, "tencent": 2, "baidu": 3, "114dns": 4, "360": 5,
    "cnnic": 6, "huawei": 7, "onevps": 8,
    "cloudflare": 10, "google": 11, "quad9": 12, "adguard": 13,
    "opendns": 14, "dnssb": 15, "controld": 16,
    "test": 99,
}


class SettingsDialog(QDialog):
    def __init__(self, config: Config, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.resize(680, 520)
        self.config = config
        self._server_rows: list[DnsServer] = []

        tabs = QTabWidget(self)
        tabs.addTab(self._build_servers_tab(), "DNS 服务器")
        tabs.addTab(self._build_tuning_tab(), "测量参数")
        tabs.addTab(self._build_weight_tab(), "评分权重")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self._apply)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

    # -- 服务器 -----------------------------------------------------------
    def _build_servers_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(QLabel("勾选参与测量的 DNS（默认只启用各厂商的 UDP 主力节点）"))

        self.server_table = QTableWidget()
        self.server_table.setColumnCount(5)
        self.server_table.setHorizontalHeaderLabels(["启用", "厂商", "协议", "地址", "名称"])
        self.server_table.horizontalHeader().setStretchLastSection(True)
        self.server_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.server_table.setColumnWidth(0, 36)
        self.server_table.setColumnWidth(1, 80)
        self.server_table.setColumnWidth(2, 44)
        self.server_table.setColumnWidth(3, 180)
        self.server_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.server_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.server_table.verticalHeader().setVisible(False)

        # 按 vendor 排序，同 vendor 按 protocol 排序
        servers = sorted(
            self.config.servers,
            key=lambda s: (
                _VENDOR_ORDER.get(s.vendor, 50),
                {Protocol.UDP: 0, Protocol.TCP: 1, Protocol.DOT: 2, Protocol.DOH: 3}.get(s.protocol, 9),
            ),
        )
        self._server_rows = servers

        last_vendor = None
        self.server_table.setRowCount(len(servers))
        for row, s in enumerate(servers):
            chk = QTableWidgetItem("")
            chk.setFlags(chk.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            chk.setCheckState(Qt.CheckState.Checked if s.enabled else Qt.CheckState.Unchecked)
            self.server_table.setItem(row, 0, chk)

            vendor_display = _VENDOR_NAMES.get(s.vendor, s.vendor or "其他")
            self.server_table.setItem(row, 1, QTableWidgetItem(vendor_display))
            self.server_table.setItem(row, 2, QTableWidgetItem(s.protocol.value.upper()))
            self.server_table.setItem(row, 3, QTableWidgetItem(s.address))
            self.server_table.setItem(row, 4, QTableWidgetItem(s.name))

            # 厂商间加背景色分隔
            if s.vendor != last_vendor:
                bg = QColor("#e8f0fe") if s.vendor and s.region == "cn" else QColor("#fef3e4")
                for col in range(1, 5):
                    item = self.server_table.item(row, col)
                    if item:
                        item.setBackground(bg)

            last_vendor = s.vendor

        layout.addWidget(self.server_table)

        row = QHBoxLayout()
        btn_all = QPushButton("全选")
        btn_all.clicked.connect(self._sel_all)
        btn_none = QPushButton("全不选")
        btn_none.clicked.connect(self._sel_none)
        btn_inv = QPushButton("反选")
        btn_inv.clicked.connect(self._sel_invert)
        btn_udp = QPushButton("仅 UDP")
        btn_udp.clicked.connect(self._sel_udp)
        btn_enc = QPushButton("仅 DoH/DoT")
        btn_enc.clicked.connect(self._sel_encrypted)
        for btn in (btn_all, btn_none, btn_inv, btn_udp, btn_enc):
            btn.setFixedHeight(28)
            row.addWidget(btn)
        layout.addLayout(row)
        return w

    def _sel_all(self) -> None:
        for row in range(self.server_table.rowCount()):
            self.server_table.item(row, 0).setCheckState(Qt.CheckState.Checked)

    def _sel_none(self) -> None:
        for row in range(self.server_table.rowCount()):
            self.server_table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)

    def _sel_invert(self) -> None:
        for row in range(self.server_table.rowCount()):
            item = self.server_table.item(row, 0)
            item.setCheckState(
                Qt.CheckState.Unchecked
                if item.checkState() is Qt.CheckState.Checked
                else Qt.CheckState.Checked
            )

    def _sel_udp(self) -> None:
        for row, s in enumerate(self._server_rows):
            self.server_table.item(row, 0).setCheckState(
                Qt.CheckState.Checked if s.protocol == Protocol.UDP else Qt.CheckState.Unchecked
            )

    def _sel_encrypted(self) -> None:
        for row, s in enumerate(self._server_rows):
            self.server_table.item(row, 0).setCheckState(
                Qt.CheckState.Checked if s.encrypted else Qt.CheckState.Unchecked
            )

    # -- 参数 -------------------------------------------------------------
    def _build_tuning_tab(self) -> QWidget:
        t = self.config.tunables
        form = QFormLayout()

        self.repeat = QSpinBox()
        self.repeat.setRange(1, 10)
        self.repeat.setValue(t.resolve_repeat)
        form.addRow("每个域名查询次数", self.repeat)

        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(0.5, 10.0)
        self.timeout.setSingleStep(0.5)
        self.timeout.setValue(t.resolve_timeout)
        form.addRow("DNS 超时（秒）", self.timeout)

        self.tcp_timeout = QDoubleSpinBox()
        self.tcp_timeout.setRange(0.3, 5.0)
        self.tcp_timeout.setSingleStep(0.1)
        self.tcp_timeout.setValue(t.tcp_timeout)
        form.addRow("TCP 握手超时（秒）", self.tcp_timeout)

        self.tcp_conc = QSpinBox()
        self.tcp_conc.setRange(8, 256)
        self.tcp_conc.setValue(t.tcp_concurrency)
        form.addRow("TCP 并发数", self.tcp_conc)

        self.dns_conc = QSpinBox()
        self.dns_conc.setRange(4, 96)
        self.dns_conc.setValue(t.resolve_concurrency)
        form.addRow("DNS 全局并发", self.dns_conc)

        self.deep = QCheckBox("深度模式：对每个 IP 跑完整 traceroute（很慢）")
        self.deep.setChecked(t.deep_traceroute)
        form.addRow(self.deep)

        self.icmp = QCheckBox("启用 ICMP ping 辅助")
        self.icmp.setChecked(t.icmp_enabled)
        form.addRow(self.icmp)

        self.geoip = QCheckBox("启用归属地查询（在线，失败自动降级）")
        self.geoip.setChecked(t.geoip_enabled)
        form.addRow(self.geoip)

        self.ecs = QCheckBox("启用 ECS 对比查询（需探测公网 IP）")
        self.ecs.setChecked(self.config.use_ecs)
        form.addRow(self.ecs)

        self.api_discovery = QCheckBox("API 域名自动发现：从主站首页 + 证书日志寻找关联域名（耗时较长）")
        self.api_discovery.setChecked(t.api_discovery_enabled)
        form.addRow(self.api_discovery)

        w = QWidget()
        w.setLayout(form)
        return w

    # -- 权重 -------------------------------------------------------------
    def _build_weight_tab(self) -> QWidget:
        t = self.config.tunables
        self.w_speed = self._slider("延迟", t.w_speed)
        self.w_path = self._slider("跳数/路径", t.w_path)
        self.w_stability = self._slider("稳定性", t.w_stability)
        self.w_proximity = self._slider("就近性", t.w_proximity)

        form = QFormLayout()
        form.addRow("延迟", self.w_speed)
        form.addRow("跳数/路径", self.w_path)
        form.addRow("稳定性", self.w_stability)
        form.addRow("就近性", self.w_proximity)

        PROFILES: dict[str, str] = {
            "balanced": "均衡模式：延迟、跳数、稳定性、就近性各占 30/15/25/30",
            "speed":    "速度优先：大幅偏向延迟 (55)，适合游戏/实时通信",
            "stable":   "稳定优先：大幅偏向稳定性 (50)，适合长时间下载/串流",
            "cdn":      "就近优先：大幅偏向就近性 (55)，适合网站/CDN 加速",
            "privacy":  "隐私优先：各维度较均衡，跳数权重略低，适合避免路由追踪",
        }

        self.profile = QComboBox()
        for name in PROFILES:
            self.profile.addItem(name)
            self.profile.setItemData(
                self.profile.count() - 1, PROFILES[name], Qt.ItemDataRole.ToolTipRole
            )
        self.profile.setCurrentText(t.profile)
        form.addRow("预设档位", self.profile)

        self.profile_hint = QLabel(PROFILES[t.profile])
        self.profile_hint.setWordWrap(True)
        self.profile_hint.setStyleSheet("color: #888; font-size: 12px;")
        self.profile.currentTextChanged.connect(
            lambda name: self.profile_hint.setText(PROFILES.get(name, ""))
        )
        form.addRow("", self.profile_hint)

        self.cn_weight = QDoubleSpinBox()
        self.cn_weight.setRange(0.0, 1.0)
        self.cn_weight.setSingleStep(0.1)
        self.cn_weight.setValue(t.cn_weight)
        form.addRow("国内组权重", self.cn_weight)

        self.gl_weight = QDoubleSpinBox()
        self.gl_weight.setRange(0.0, 1.0)
        self.gl_weight.setSingleStep(0.1)
        self.gl_weight.setValue(t.global_weight)
        form.addRow("国外组权重", self.gl_weight)

        hint = QLabel("权重会按总和归一化到 100，无需手动凑数。")
        hint.setWordWrap(True)
        form.addRow(hint)

        w = QWidget()
        w.setLayout(form)
        return w

    @staticmethod
    def _slider(label: str, value: float) -> QSpinBox:
        box = QSpinBox()
        box.setRange(0, 100)
        box.setValue(int(value))
        box.setSuffix(" %")
        return box

    # -- 应用 -------------------------------------------------------------
    def _apply(self) -> None:
        wanted = {
            self._server_rows[row].id
            for row in range(self.server_table.rowCount())
            if self.server_table.item(row, 0).checkState() is Qt.CheckState.Checked
        }
        self.config.set_enabled(wanted)

        t = self.config.tunables
        t.resolve_repeat = self.repeat.value()
        t.resolve_timeout = self.timeout.value()
        t.tcp_timeout = self.tcp_timeout.value()
        t.tcp_concurrency = self.tcp_conc.value()
        t.resolve_concurrency = self.dns_conc.value()
        t.deep_traceroute = self.deep.isChecked()
        t.icmp_enabled = self.icmp.isChecked()
        t.geoip_enabled = self.geoip.isChecked()
        t.w_speed = float(self.w_speed.value())
        t.w_path = float(self.w_path.value())
        t.w_stability = float(self.w_stability.value())
        t.w_proximity = float(self.w_proximity.value())
        t.cn_weight = self.cn_weight.value()
        t.global_weight = self.gl_weight.value()
        if self.profile.currentText():
            t.with_profile(self.profile.currentText())
            if self.profile.currentText() != "balanced":
                t.w_speed = float(self.w_speed.value())
        t.api_discovery_enabled = self.api_discovery.isChecked()
        self.config.use_ecs = self.ecs.isChecked()
        self.accept()