"""What does gui_policy say about generated PySide6 code TODAY, unchanged?

Run with the council env from the worktree root.
"""
import json
import sys

sys.path.insert(0, r"C:\Users\apkun\Downloads\Council-Demo\Council-Demo\.claude\worktrees\priceless-vaughan-cc9023")
import gui_policy  # noqa: E402

# 1. A realistic slice of what a Qt emitter would write into ui/main_ui.py,
#    ui/widgets.py and ui/ports.py.
GENERATED = '''
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QFileDialog, QGridLayout, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QWidget)


class MainUi(QWidget):
    roi_changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._closing = False
        self._build()
        QTimer.singleShot(150, self._poll)

    def _build(self) -> None:
        grid = QGridLayout(self)
        self.title = QLabel("Frames", self)
        grid.addWidget(self.title, 0, 0)
        self.folder = QLineEdit(self)
        grid.addWidget(self.folder, 1, 0)
        self.scan = QPushButton("Scan", self)
        self.scan.clicked.connect(self.on_scan)
        grid.addWidget(self.scan, 2, 0)

    def browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose a frame")
        if path:
            self.folder.setText(path)

    def show_frame(self, arr) -> None:
        img = QImage(arr.data, arr.shape[1], arr.shape[0], arr.strides[0],
                     QImage.Format_Grayscale8)
        self.title.setPixmap(QPixmap.fromImage(img))

    def show_file(self, path) -> None:
        pix = QPixmap()
        pix.load(path)
        self.title.setPixmap(pix)

    def report_error(self, what, exc) -> None:
        QMessageBox.critical(self, f"{what} failed", str(exc))

    def _poll(self) -> None:
        pass

    def on_scan(self, *args) -> None:
        pass
'''

# 2. Escapes the gate MUST refuse, each in several spellings.
ESCAPES = {
    "QProcess import": "from PySide6.QtCore import QProcess\nQProcess().start('cmd')\n",
    "QProcess attr": "from PySide6 import QtCore\nQtCore.QProcess().start('cmd')\n",
    "QtNetwork module": "from PySide6 import QtNetwork\nm = QtNetwork.QNetworkAccessManager()\n",
    "QtNetwork from": "from PySide6.QtNetwork import QNetworkAccessManager\nm = QNetworkAccessManager()\n",
    "QDesktopServices": "from PySide6.QtGui import QDesktopServices\nfrom PySide6.QtCore import QUrl\nQDesktopServices.openUrl(QUrl('http://x'))\n",
    "QSettings registry": "from PySide6.QtCore import QSettings\nQSettings('a', 'b').setValue('k', 1)\n",
    "QLibrary": "from PySide6.QtCore import QLibrary\nQLibrary('evil.dll').load()\n",
    "QPluginLoader": "from PySide6.QtCore import QPluginLoader\nQPluginLoader('p.dll').instance()\n",
    "QtQml eval": "from PySide6.QtQml import QQmlEngine\nQQmlEngine().evaluate('x')\n",
    "QtSql": "from PySide6.QtSql import QSqlDatabase\nQSqlDatabase.addDatabase('QPSQL')\n",
    "QtWebEngine": "from PySide6.QtWebEngineWidgets import QWebEngineView\nQWebEngineView().load('http://x')\n",
    "QUiLoader": "from PySide6.QtUiTools import QUiLoader\nQUiLoader().load('x.ui')\n",
    "star import": "from PySide6.QtWidgets import *\nw = QWidget()\n",
}

out = {}
for mode in ("linked", "standalone"):
    ok, errs = gui_policy.validate(GENERATED, mode)
    out[f"generated_qt[{mode}] undeclared"] = {"ok": ok, "errors": errs}
    ok2, errs2 = gui_policy.validate(GENERATED, mode, extra_modules=["PySide6"])
    out[f"generated_qt[{mode}] requires=PySide6"] = {"ok": ok2, "errors": errs2}

esc = {}
for name, src in ESCAPES.items():
    ok_plain, errs_plain = gui_policy.validate(src, "linked")
    ok_decl, errs_decl = gui_policy.validate(src, "linked", extra_modules=["PySide6"])
    esc[name] = {
        "undeclared": {"refused": not ok_plain, "errors": errs_plain},
        "with requires=PySide6": {"refused": not ok_decl, "errors": errs_decl},
    }
out["escapes"] = esc

# 3. Does declaring PySide6 in `requires` even pass check_requires?
out["check_requires(PySide6)"] = gui_policy.check_requires(["PySide6"], "linked")
out["allowed_contains_PySide6_undeclared"] = "PySide6" in gui_policy.allowed_modules("linked")
out["SAFE_LOAD_RECEIVERS"] = sorted(gui_policy.SAFE_LOAD_RECEIVERS)
out["stdlib_has_tkinter"] = "tkinter" in gui_policy._stdlib_names()

print(json.dumps(out, indent=1))
