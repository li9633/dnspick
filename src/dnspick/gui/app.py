"""GUI 入口：dnspick-gui"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from dnspick import __version__
from dnspick.gui.main_window import MainWindow


def main() -> int:
    # Qt6 默认已启用高分屏缩放，不要再设 AA_EnableHighDpiScaling（已废弃且无效）
    QApplication.setApplicationName("dnspick")
    QApplication.setApplicationVersion(__version__)
    QApplication.setOrganizationName("dnspick")

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
