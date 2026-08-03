"""Editor for the per-series AceTree config XML.

The file lives at `<annot_loc>/dats/<series>.xml` and controls how AceTree
displays the series: start/end timepoint range, axis convention, voxel
resolution. Users sometimes need to tweak these after import (e.g. flip
the axis label to ADL/AVR/PVL/PDR, or extend the end index after re-running
StarryNite past the original cutoff).

The format we read+write matches what AceTree's XMLConfig.java expects;
fields not present in the file are surfaced as empty and dropped on save.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from qtpy import QtCore, QtWidgets

from ..fsutil import safe_write_text


_AXIS_CHOICES = ("", "ADL", "AVR", "PVL", "PDR")


@dataclass
class AceTreeConfig:
    image_file: str = ""
    nuclei_file: str = ""
    start_index: int = 1
    end_index: int = 0
    axis: str = ""
    xy_res: str = ""
    z_res: str = ""
    plane_end: int = 0


def load_config(path: Path) -> AceTreeConfig:
    tree = ET.parse(path)
    root = tree.getroot()
    out = AceTreeConfig()
    el = root.find("image")
    if el is not None:
        out.image_file = el.get("file", "")
    el = root.find("nuclei")
    if el is not None:
        out.nuclei_file = el.get("file", "")
    el = root.find("start")
    if el is not None:
        try:
            out.start_index = int(el.get("index", "1"))
        except ValueError:
            out.start_index = 1
    el = root.find("end")
    if el is not None:
        try:
            out.end_index = int(el.get("index", "0"))
        except ValueError:
            out.end_index = 0
    el = root.find("axis")
    if el is not None:
        out.axis = el.get("axis", "")
    el = root.find("resolution")
    if el is not None:
        out.xy_res = el.get("xyRes", "")
        out.z_res = el.get("zRes", "")
        try:
            out.plane_end = int(el.get("planeEnd", "0"))
        except ValueError:
            out.plane_end = 0
    return out


def render_config(cfg: AceTreeConfig) -> str:
    return (
        "<?xml version='1.0' encoding='utf-8'?>\n"
        "<embryo>\n"
        f'<image file="{cfg.image_file}"/>\n'
        f'<nuclei file="{cfg.nuclei_file}"/>\n'
        f'<start index="{cfg.start_index}"/>\n'
        f'<end index="{cfg.end_index}"/>\n'
        f'<axis axis="{cfg.axis}"/>\n'
        f'<resolution xyRes="{cfg.xy_res}" zRes="{cfg.z_res}" planeEnd="{cfg.plane_end}"/>\n'
        "</embryo>\n"
    )


class AceTreeConfigDialog(QtWidgets.QDialog):
    """Modal editor over one series' AceTree config file."""

    def __init__(self, path: Path, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._path = Path(path)
        self.setWindowTitle(f"AceTree config — {self._path.name}")
        self.resize(640, 480)

        try:
            cfg = load_config(self._path)
        except (FileNotFoundError, ET.ParseError) as exc:
            QtWidgets.QMessageBox.warning(
                self, "AceTree config",
                f"Could not load {self._path}:\n{exc}"
            )
            cfg = AceTreeConfig()

        outer = QtWidgets.QVBoxLayout(self)
        outer.addWidget(QtWidgets.QLabel(f"<b>Path:</b> {self._path}"))

        form = QtWidgets.QFormLayout()

        self._image_file = QtWidgets.QLineEdit(cfg.image_file)
        form.addRow("image file:", self._image_file)

        self._nuclei_file = QtWidgets.QLineEdit(cfg.nuclei_file)
        form.addRow("nuclei file:", self._nuclei_file)

        self._start = QtWidgets.QSpinBox()
        self._start.setRange(1, 100000)
        self._start.setValue(max(1, cfg.start_index))
        form.addRow("start index:", self._start)

        self._end = QtWidgets.QSpinBox()
        self._end.setRange(1, 100000)
        self._end.setValue(max(1, cfg.end_index or 1))
        form.addRow("end index:", self._end)

        self._axis = QtWidgets.QComboBox()
        self._axis.setEditable(True)
        self._axis.addItems(_AXIS_CHOICES)
        self._axis.setCurrentText(cfg.axis)
        self._axis.setToolTip(
            "Body-axis convention used by AceTree's reconstruction frame "
            "(ADL/AVR/PVL/PDR or blank). GetACD honors this; future v3 will "
            "use AP/LR vectors directly."
        )
        form.addRow("axis:", self._axis)

        self._xyres = QtWidgets.QLineEdit(cfg.xy_res)
        self._xyres.setToolTip("µm per pixel in XY (default 0.087).")
        form.addRow("xyRes:", self._xyres)

        self._zres = QtWidgets.QLineEdit(cfg.z_res)
        self._zres.setToolTip("µm between Z planes (default 0.5).")
        form.addRow("zRes:", self._zres)

        self._plane_end = QtWidgets.QSpinBox()
        self._plane_end.setRange(0, 10000)
        self._plane_end.setValue(cfg.plane_end)
        form.addRow("planeEnd:", self._plane_end)

        outer.addLayout(form)

        warn = QtWidgets.QLabel(
            "<i>Editing resolution here does not update matlabParams. If "
            "StarryNite has already run, changing xyRes/zRes here will only "
            "affect future AceTree display, not the tracked nuclei.</i>"
        )
        warn.setWordWrap(True)
        outer.addWidget(warn)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _save(self) -> None:
        cfg = AceTreeConfig(
            image_file=self._image_file.text().strip(),
            nuclei_file=self._nuclei_file.text().strip(),
            start_index=self._start.value(),
            end_index=self._end.value(),
            axis=self._axis.currentText().strip(),
            xy_res=self._xyres.text().strip(),
            z_res=self._zres.text().strip(),
            plane_end=self._plane_end.value(),
        )
        try:
            safe_write_text(self._path, render_config(cfg))
        except OSError as exc:
            QtWidgets.QMessageBox.warning(self, "AceTree config", f"Save failed: {exc}")
            return
        self.accept()


__all__ = ["AceTreeConfigDialog", "AceTreeConfig", "load_config", "render_config"]
