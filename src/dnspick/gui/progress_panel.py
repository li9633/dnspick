"""进度面板组件：阶段时间线 + 当前阶段进度条 + ETA 预估。

独立 QWidget，通过 @Slot(StageProgress) 接收外部进度更新，
通过 set_stages() 预设阶段列表，通过 start()/reset() 控制生命周期。
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from dnspick.models import StageProgress

# 各阶段经验权重（用于 ETA 预估）
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


def _fmt_duration(seconds: float) -> str:
    """格式化耗时。"""
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    m = seconds / 60
    return f"{m:.1f}min"


class ProgressPanel(QWidget):
    """进度与阶段时间线面板。

    公开接口：
      - set_stages(stages, weights)  预设阶段列表和权重
      - start()                      开始计时
      - reset()                      恢复到初始状态
      - on_progress(StageProgress)   接收进度更新 (@Slot)
      - finish(success)              标记所有阶段完成
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._stages: list[str] = []
        self._weights: dict[str, float] = {}
        self._stage_items: dict[str, QListWidgetItem] = {}
        self._stage_started: float = 0.0
        self._stage_durations: dict[str, float] = {}
        self._stage_results: dict[str, str] = {}
        self._active_stage: str = ""
        self._build_ui()
        self.reset()

    # ── UI 构建 ────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        # 阶段标题
        self._stage_header = QLabel("就绪")
        self._stage_header.setStyleSheet("font-weight: bold; font-size: 13px; color: #3a6ea5;")
        root.addWidget(self._stage_header)

        # 进度条
        self._progress_bar = QProgressBar()
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFormat("%p%  %v/%m")
        self._progress_bar.setVisible(False)
        root.addWidget(self._progress_bar)

        # 状态描述
        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color: #666; font-size: 12px;")
        root.addWidget(self._status_label)

        # 主体：时间线 + ETA
        body = QHBoxLayout()
        body.setSpacing(8)

        # 时间线列表
        self._timeline = QListWidget()
        self._timeline.setFixedWidth(210)
        self._timeline.setStyleSheet("QListWidget { border: 1px solid #ddd; border-radius: 4px; }")
        self._timeline.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        body.addWidget(self._timeline)

        # ETA 区域
        eta_col = QVBoxLayout()
        eta_col.setSpacing(2)
        self._eta_label = QLabel("")
        self._eta_label.setStyleSheet("color: #888; font-size: 12px;")
        self._eta_label.setWordWrap(True)
        self._eta_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        eta_col.addWidget(self._eta_label)
        eta_col.addStretch(1)
        body.addLayout(eta_col)

        root.addLayout(body, 1)

    # ── 生命周期 ────────────────────────────────────────────────

    def start(self) -> None:
        """重置面板，开始计时。"""
        self._stage_started = time.time()
        self._stage_durations.clear()
        self._stage_results.clear()
        self._active_stage = ""
        self._stage_header.setText("准备中…")
        self._progress_bar.setVisible(False)
        self._status_label.setText("")
        self._eta_label.setText("")

        for name in self._stages:
            item = self._stage_items.get(name)
            if item is None:
                item = QListWidgetItem()
                self._timeline.addItem(item)
                self._stage_items[name] = item
            item.setText(name)
            item.setToolTip("")
            item.setForeground(Qt.GlobalColor.gray)
            font = item.font()
            font.setBold(False)
            item.setFont(font)

    def reset(self) -> None:
        """恢复到初始「等待任务开始」状态。"""
        self._stage_header.setText("就绪")
        self._stage_header.setStyleSheet("font-weight: bold; font-size: 13px; color: #3a6ea5;")
        self._progress_bar.setVisible(False)
        self._status_label.setText("")
        self._eta_label.setText("等待任务开始")
        self._timeline.clear()
        self._stage_items.clear()
        self._stage_durations.clear()
        self._stage_results.clear()
        self._active_stage = ""

    def finish(self, success: bool = True) -> None:
        """标记所有阶段完成。"""
        self._stage_header.setText("已完成" if success else "已中断")
        color = "#4a9" if success else "#d44"
        self._stage_header.setStyleSheet(f"font-weight: bold; font-size: 13px; color: {color};")
        self._progress_bar.setVisible(False)
        self._status_label.setText("")
        self._eta_label.setText("所有阶段已完成")

    # ── 阶段配置 ────────────────────────────────────────────────

    def set_stages(self, stages: list[str], weights: dict[str, float] | None = None) -> None:
        """预设阶段列表和权重。weights 用于 ETA 计算。"""
        self._stages = stages
        self._weights = weights or _DEFAULT_WEIGHTS
        self._stage_items.clear()
        self._timeline.clear()

    # ── 进度更新（Slot） ────────────────────────────────────────

    def on_progress(self, sp: StageProgress) -> None:
        """接收 Runner 发来的阶段进度快照。"""

        # ── done 状态：更新已完成阶段 ──
        if sp.status == "done":
            self._stage_durations[sp.stage_name] = sp.elapsed_s
            self._stage_results[sp.stage_name] = sp.msg
            self._update_timeline_item(sp.stage_name, "done", sp.elapsed_s, sp.msg)
            self._update_eta()
            return

        # ── skipped 状态 ──
        if sp.status == "skipped":
            self._update_timeline_item(sp.stage_name, "skipped", 0, sp.msg)
            return

        # ── running 状态 ──
        self._active_stage = sp.stage_name
        self._update_timeline_item(sp.stage_name, "running", sp.elapsed_s, sp.msg)

        # 更新标题
        self._stage_header.setText(f"阶段 {sp.stage}/{sp.total_stages}  {sp.stage_name}")
        self._stage_header.setStyleSheet(
            "font-weight: bold; font-size: 13px; color: #3a6ea5;"
        )

        # 更新进度条
        if sp.total > 1:
            self._progress_bar.setVisible(True)
            self._progress_bar.setMaximum(sp.total)
            self._progress_bar.setValue(sp.done)
        else:
            self._progress_bar.setVisible(False)

        # 更新状态文本
        self._status_label.setText(sp.msg)

        # 更新 ETA
        self._update_eta()

    # ── 内部方法 ────────────────────────────────────────────────

    def _update_timeline_item(
        self, name: str, status: str, elapsed: float, msg: str
    ) -> None:
        """更新时间线中某个阶段的显示。"""
        item = self._stage_items.get(name)
        if item is None:
            return

        font = item.font()

        if status == "done":
            item.setText(f"✓  {name}")
            item.setToolTip(f"耗时 {_fmt_duration(elapsed)}\n{msg}")
            item.setForeground(Qt.GlobalColor.darkGreen)
            font.setBold(False)
        elif status == "skipped":
            item.setText(f"–  {name}")
            item.setToolTip(msg or "已跳过")
            item.setForeground(Qt.GlobalColor.gray)
            font.setBold(False)
        else:  # running
            item.setText(f"●  {name}")
            item.setForeground(Qt.GlobalColor.darkCyan)
            font.setBold(True)

        item.setFont(font)

    def _update_eta(self) -> None:
        """基于已完成阶段的加权速率估算剩余时间。"""
        completed_durations = self._stage_durations
        if not completed_durations:
            # 冷启动：无已完成阶段，不做预估
            if self._active_stage:
                self._eta_label.setText("估算中…")
            return

        # 已完成阶段加权速率
        total_weight_done = 0.0
        total_time_done = 0.0
        for name, dur in completed_durations.items():
            w = self._weights.get(name, 1)
            total_weight_done += w
            total_time_done += dur

        if total_weight_done == 0:
            return

        rate = total_time_done / total_weight_done  # 秒/权重单位

        # 剩余阶段总权重
        remaining_weight = 0.0
        remaining_stages: list[str] = []
        for name in self._stages:
            if name not in completed_durations and name not in ("域名收集", "评分"):
                w = self._weights.get(name, 1)
                if w > 0:
                    remaining_weight += w
                    remaining_stages.append(name)

        if remaining_weight == 0:
            self._eta_label.setText("即将完成")
            return

        remaining_sec = rate * remaining_weight
        elapsed = time.time() - self._stage_started if self._stage_started else 0
        total_est = elapsed + remaining_sec

        parts = []
        if total_est > 60:
            parts.append(f"预计总耗时 ~{_fmt_duration(total_est)}")
        else:
            parts.append(f"总耗时 ~{_fmt_duration(total_est)}")
        parts.append(f"已用 {_fmt_duration(elapsed)}")
        if remaining_sec > 60:
            parts.append(f"剩余 ~{_fmt_duration(remaining_sec)}")
        else:
            parts.append(f"剩余 ~{_fmt_duration(remaining_sec)}")

        self._eta_label.setText(" · ".join(parts))