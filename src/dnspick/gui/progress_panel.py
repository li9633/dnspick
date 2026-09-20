"""进度面板组件：阶段徽章行 + 当前阶段进度条 + ETA 预估。

独立 QWidget，通过 @Slot(StageProgress) 接收外部进度更新，
通过 set_stages() 预设阶段列表，通过 start()/reset() 控制生命周期。

紧凑水平布局：时间线为横排徽章，进度条+ETA 合并行，总高 ~55px。
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from dnspick.models import StageProgress

# 权重为 0 的阶段不参与剩余时间估算（瞬时完成的元信息阶段）
_ZERO_WEIGHT_SENTINEL = 0.0

_DEFAULT_WEIGHTS: dict[str, float] = {
    "环境探测": 1,
    "域名收集": 0,
    "API发现": 2,
    "DNS 解析": 8,
    "ECS 对比": 3,
    "IP 探测": 12,
    "归属地": 1,
    "评分": 0,
}

_STAGE_ABBREV: dict[str, str] = {
    "环境探测": "环境",
    "域名收集": "域名",
    "API发现": "API",
    "DNS 解析": "DNS",
    "ECS 对比": "ECS",
    "IP 探测": "IP",
    "归属地": "归属",
    "评分": "⏱评分",
}


def _fmt_duration(seconds: float) -> str:
    """格式化耗时。"""
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    m = seconds / 60
    return f"{m:.1f}min"


class ProgressPanel(QWidget):
    """进度与阶段时间线面板（紧凑模式）。

    公开接口：
      - set_stages(stages, weights)  预设阶段列表和权重
      - start()                      开始计时
      - reset()                      恢复到初始状态（隐藏）
      - on_progress(StageProgress)   接收进度更新 (@Slot)
      - finish(success)              标记所有阶段完成
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._stages: list[str] = []
        self._weights: dict[str, float] = {}
        self._badge_labels: dict[str, QLabel] = {}
        self._stage_started: float = 0.0
        self._stage_durations: dict[str, float] = {}
        self._active_stage: str = ""
        self._build_ui()
        self.reset()

    # ── UI 构建 ────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 2, 4, 2)
        root.setSpacing(3)

        # ── 第一行：进度条 + ETA ──
        row1 = QHBoxLayout()
        row1.setSpacing(8)

        self._stage_header = QLabel("")
        self._stage_header.setStyleSheet("font-weight: bold; font-size: 11px; color: #3a6ea5;")
        row1.addWidget(self._stage_header)

        self._progress_bar = QProgressBar()
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFormat("%p%  %v/%m")
        self._progress_bar.setFixedHeight(18)
        self._progress_bar.setVisible(False)
        row1.addWidget(self._progress_bar, 1)

        self._eta_label = QLabel("")
        self._eta_label.setStyleSheet("color: #888; font-size: 11px;")
        self._eta_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        row1.addWidget(self._eta_label)

        root.addLayout(row1)

        # ── 第二行：阶段徽章 ──
        self._badge_row = QHBoxLayout()
        self._badge_row.setSpacing(3)
        self._badge_row.addStretch(1)
        root.addLayout(self._badge_row)

        self.setVisible(False)

    # ── 生命周期 ────────────────────────────────────────────────

    def start(self) -> None:
        """显示面板，开始计时。"""
        self._stage_started = time.time()
        self._stage_durations.clear()
        self._active_stage = ""
        self._stage_header.setText("准备中…")
        self._progress_bar.setVisible(False)
        self._eta_label.setText("")

        for name in self._stages:
            lbl = self._badge_labels.get(name)
            if lbl is None:
                lbl = self._make_badge()
                self._badge_row.insertWidget(self._badge_row.count() - 1, lbl)
                self._badge_labels[name] = lbl
            lbl.setText(_STAGE_ABBREV.get(name, name[:3]))
            lbl.setStyleSheet(self._badge_style("pending"))
            lbl.setToolTip(f"{name}: 等待中")

        self._badge_row.itemAt(self._badge_row.count() - 1).widget()  # keep stretch
        self.setVisible(True)
        self.updateGeometry()

    def reset(self) -> None:
        """恢复到初始状态并隐藏。"""
        self._stage_header.setText("")
        self._progress_bar.setVisible(False)
        self._eta_label.setText("")
        for lbl in self._badge_labels.values():
            self._badge_row.removeWidget(lbl)
            lbl.deleteLater()
        self._badge_labels.clear()
        self._stage_durations.clear()
        self._active_stage = ""
        self.setVisible(False)

    def finish(self, success: bool = True) -> None:
        """标记所有阶段完成。"""
        self._stage_header.setText("已完成" if success else "已中断")
        color = "#4a9" if success else "#d44"
        self._stage_header.setStyleSheet(f"font-weight: bold; font-size: 11px; color: {color};")
        self._progress_bar.setVisible(False)
        if self._stage_started:
            elapsed = time.time() - self._stage_started
            self._eta_label.setText(f"总耗时 {_fmt_duration(elapsed)}")
        else:
            self._eta_label.setText("")

    # ── 阶段配置 ────────────────────────────────────────────────

    def set_stages(self, stages: list[str], weights: dict[str, float] | None = None) -> None:
        """预设阶段列表和权重。weights 用于 ETA 计算。"""
        self._stages = stages
        self._weights = weights or _DEFAULT_WEIGHTS

    # ── 进度更新（Slot） ────────────────────────────────────────

    def on_progress(self, sp: StageProgress) -> None:
        """接收 Runner 发来的阶段进度快照。"""

        if sp.status == "done":
            self._stage_durations[sp.stage_name] = sp.elapsed_s
            self._update_badge(sp.stage_name, "done", sp.elapsed_s, sp.msg)
            self._update_eta()
            return

        if sp.status == "skipped":
            self._update_badge(sp.stage_name, "skipped", 0, sp.msg)
            return

        # ── running ──
        self._active_stage = sp.stage_name
        self._update_badge(sp.stage_name, "running", sp.elapsed_s, sp.msg)

        self._stage_header.setText(f"阶段 {sp.stage}/{sp.total_stages}  {sp.stage_name}")
        self._stage_header.setStyleSheet("font-weight: bold; font-size: 11px; color: #3a6ea5;")

        if sp.total > 1:
            self._progress_bar.setVisible(True)
            self._progress_bar.setMaximum(sp.total)
            self._progress_bar.setValue(sp.done)
        else:
            self._progress_bar.setVisible(False)

        self._update_eta()

    # ── 徽章 ────────────────────────────────────────────────────

    @staticmethod
    def _make_badge() -> QLabel:
        lbl = QLabel()
        lbl.setFixedHeight(20)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setContentsMargins(6, 0, 6, 0)
        return lbl

    @staticmethod
    def _badge_style(status: str) -> str:
        if status == "done":
            return (
                "background: #e6f4ea; color: #1b7a2d; border: 1px solid #b6d7b9; "
                "border-radius: 4px; font-size: 10px; font-weight: normal;"
            )
        if status == "running":
            return (
                "background: #d4e6f1; color: #1a5276; border: 1px solid #7fb3d3; "
                "border-radius: 4px; font-size: 10px; font-weight: bold;"
            )
        if status == "skipped":
            return (
                "background: #eee; color: #999; border: 1px solid #ddd; "
                "border-radius: 4px; font-size: 10px;"
            )
        # pending
        return (
            "background: #fafafa; color: #bbb; border: 1px solid #e8e8e8; "
            "border-radius: 4px; font-size: 10px;"
        )

    def _update_badge(
        self, name: str, status: str, elapsed: float, msg: str
    ) -> None:
        lbl = self._badge_labels.get(name)
        if lbl is None:
            return

        lbl.setStyleSheet(self._badge_style(status))

        abbrev = _STAGE_ABBREV.get(name, name[:3])
        if status == "done":
            lbl.setText(f"✓ {abbrev}")
            lbl.setToolTip(f"{name}: 完成 · 耗时 {_fmt_duration(elapsed)}\n{msg}")
        elif status == "skipped":
            lbl.setText(f"— {abbrev}")
            lbl.setToolTip(f"{name}: {msg or '已跳过'}")
        else:
            lbl.setText(f"● {abbrev}")
            lbl.setToolTip(f"{name}: {msg}")

    # ── ETA ─────────────────────────────────────────────────────

    def _update_eta(self) -> None:
        completed_durations = self._stage_durations
        if not completed_durations:
            self._eta_label.setText("")
            return

        total_weight_done = 0.0
        total_time_done = 0.0
        for name, dur in completed_durations.items():
            w = self._weights.get(name, 1)
            total_weight_done += w
            total_time_done += dur

        if total_weight_done == 0:
            return

        rate = total_time_done / total_weight_done

        remaining_weight = 0.0
        for name in self._stages:
            if name not in completed_durations:
                w = self._weights.get(name, 1)
                if w > _ZERO_WEIGHT_SENTINEL:
                    remaining_weight += w

        if remaining_weight == 0:
            self._eta_label.setText("即将完成")
            return

        remaining_sec = rate * remaining_weight
        elapsed = time.time() - self._stage_started if self._stage_started else 0
        self._eta_label.setText(
            f"已用 {_fmt_duration(elapsed)} · 剩余 ~{_fmt_duration(remaining_sec)}"
        )