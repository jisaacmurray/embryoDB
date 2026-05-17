"""QApplication entry point for embryoDB GUI.

Launch with `embryodb-gui` (from the installed package) or
`python -m embryodb.gui.app` during development.
"""

from __future__ import annotations

import sys

from qtpy import QtWidgets

from .main_window import MainWindow


def main() -> int:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setApplicationName("embryoDB")
    app.setOrganizationName("Murray Lab")

    window = MainWindow()
    window.resize(1400, 800)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
