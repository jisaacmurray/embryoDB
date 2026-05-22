"""Editor for the per-series AuxInfo CSV.

The file lives at `<annot_loc>/dats/<series>AuxInfo.csv` and contains the
per-embryo coordinate frame that Measure (acebatch3.jar Measure1) writes:
ellipse center, axes, rotation angle, Z scaling, and the axis-convention
label. Downstream analysis (GetACD, tree-plotting) reads it for AP/LR
orientation.

Format: 2 rows — header + one data row, e.g.
  name,slope,intercept,xc,yc,maj,min,ang,zc,zslope,time,zpixres,axis
  <series>,0.5518,-16.8387,399,401,577,366,-70.8346,43.8,4.3818,96,5.7471,XXX

Users sometimes need to tweak this when Measure got the ellipse fit wrong
or to override the axis convention (ADL/AVR/PVL/PDR/XXX).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field, fields
from io import StringIO
from pathlib import Path

from qtpy import QtCore, QtWidgets

from ..fsutil import safe_write_text


_AXIS_CHOICES = ("XXX", "ADL", "AVR", "PVL", "PDR")

# Header order matches the legacy Measure output.
_HEADER = (
    "name", "slope", "intercept", "xc", "yc", "maj", "min", "ang",
    "zc", "zslope", "time", "zpixres", "axis",
)


@dataclass
class AuxInfo:
    name: str = ""
    slope: str = ""
    intercept: str = ""
    xc: str = ""
    yc: str = ""
    maj: str = ""
    min: str = ""
    ang: str = ""
    zc: str = ""
    zslope: str = ""
    time: str = ""
    zpixres: str = ""
    axis: str = "XXX"


def load_auxinfo(path: Path) -> AuxInfo:
    """Parse the 2-row CSV. Unknown columns are dropped silently."""
    text = path.read_text(encoding="utf-8", errors="replace")
    reader = csv.DictReader(StringIO(text))
    row = next(reader, None) or {}
    out = AuxInfo()
    for name in _HEADER:
        if name in row and row[name] is not None:
            setattr(out, name, str(row[name]).strip())
    return out


def render_auxinfo(info: AuxInfo) -> str:
    buf = StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(_HEADER)
    w.writerow([getattr(info, k) for k in _HEADER])
    return buf.getvalue()


class AuxInfoDialog(QtWidgets.QDialog):
    """Read+edit+write a per-series AuxInfo.csv."""

    def __init__(self, path: Path, parent=None) -> None:
        super().__init__(parent)
        self._path = path
        self.setWindowTitle(f"Edit AuxInfo — {path.name}")
        self.setMinimumWidth(420)
        try:
            self._info = load_auxinfo(path)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(
                self, "AuxInfo load failed",
                f"Could not parse {path}:\n{exc}\n\nStarting from blank."
            )
            self._info = AuxInfo()
        self._build()

    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        path_label = QtWidgets.QLabel(f"<i>{self._path}</i>")
        path_label.setWordWrap(True)
        path_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        outer.addWidget(path_label)

        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(QtCore.Qt.AlignRight)
        outer.addLayout(form)

        # Build a QLineEdit for every text-y field; QComboBox for axis.
        self._widgets: dict[str, QtWidgets.QWidget] = {}
        labels = {
            "name": "Series name:",
            "slope": "Time slope:",
            "intercept": "Time intercept:",
            "xc": "Ellipse xc:",
            "yc": "Ellipse yc:",
            "maj": "Major axis:",
            "min": "Minor axis:",
            "ang": "Angle (deg):",
            "zc": "Z center:",
            "zslope": "Z slope:",
            "time": "Reference time:",
            "zpixres": "Z pixel res:",
        }
        for key, label in labels.items():
            edit = QtWidgets.QLineEdit(getattr(self._info, key))
            self._widgets[key] = edit
            form.addRow(label, edit)

        axis_combo = QtWidgets.QComboBox()
        axis_combo.setEditable(True)
        axis_combo.addItems(list(_AXIS_CHOICES))
        current = self._info.axis or "XXX"
        idx = axis_combo.findText(current)
        if idx >= 0:
            axis_combo.setCurrentIndex(idx)
        else:
            axis_combo.setEditText(current)
        self._widgets["axis"] = axis_combo
        form.addRow("Axis convention:", axis_combo)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel
        )
        btns.accepted.connect(self._on_save)
        btns.rejected.connect(self.reject)
        outer.addWidget(btns)

    def _on_save(self) -> None:
        for key, widget in self._widgets.items():
            if isinstance(widget, QtWidgets.QLineEdit):
                setattr(self._info, key, widget.text().strip())
            elif isinstance(widget, QtWidgets.QComboBox):
                setattr(self._info, key, widget.currentText().strip())
        try:
            safe_write_text(self._path, render_auxinfo(self._info))
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "AuxInfo save failed", str(exc))
            return
        self.accept()


__all__ = ["AuxInfoDialog", "AuxInfo", "load_auxinfo", "render_auxinfo"]
