"""The rerun dialog's production-engine fields.

The prod driver takes iscale/stagelo as call arguments and rewrites the
intensitythreshold vector itself, so the params table's threshold row would be
silently discarded under that engine. These tests pin the two behaviours that
keeps honest: the row goes read-only, and the prod fields produce the step
params the worker reads.
"""

from __future__ import annotations

import contextlib
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_API", "pyqt5")

QtWidgets = pytest.importorskip("qtpy.QtWidgets")

from embryodb.gui.rerun_dialog import RerunPipelineDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def dialog(qapp, monkeypatch, tmp_path):
    @contextlib.contextmanager
    def session_cm():
        yield None  # the DB lookup is stubbed out below

    monkeypatch.setattr(
        RerunPipelineDialog,
        "_load_series_data",
        lambda self: self._series_data.append({
            "id": 1, "name": "FAKE_L1", "image_loc": str(tmp_path),
            "timepts": "100", "params_path": None,
            "voxel_xy_um": None, "voxel_z_um": None, "runs": {},
        }),
    )
    dlg = RerunPipelineDialog(session_cm, ["FAKE_L1"])
    yield dlg
    dlg.deleteLater()


def _select_engine(dlg, engine: str) -> None:
    idx = dlg._engine_combo.findData(engine)
    assert idx >= 0, f"engine {engine!r} not offered"
    dlg._engine_combo.setCurrentIndex(idx)


def test_prod_box_only_shows_for_prod(dialog):
    _select_engine(dialog, "old")
    assert not dialog._prod_box.isVisibleTo(dialog)
    _select_engine(dialog, "prod")
    assert dialog._prod_box.isVisibleTo(dialog)


def test_threshold_row_locks_under_prod(dialog):
    from embryodb.parsers.matlab_params import TUNABLE_KEYS
    from qtpy import QtCore

    row = TUNABLE_KEYS.index("parameters.intensitythreshold")
    item = dialog._params_table.item(row, 2)

    _select_engine(dialog, "prod")
    assert not item.flags() & QtCore.Qt.ItemIsEditable
    _select_engine(dialog, "old")
    assert item.flags() & QtCore.Qt.ItemIsEditable


def test_defaults_produce_no_overrides(dialog):
    _select_engine(dialog, "prod")
    assert dialog._prod_overrides() == {}


def test_native_choice_is_passed_through(dialog):
    _select_engine(dialog, "prod")
    idx = dialog._prod_threshold.findData("none")
    assert idx >= 0
    dialog._prod_threshold.setCurrentIndex(idx)
    assert dialog._prod_overrides() == {"sn_uniform_threshold": "none"}


def test_typed_threshold_and_knobs(dialog):
    _select_engine(dialog, "prod")
    dialog._prod_threshold.setCurrentText("0.0025")
    dialog._prod_iscale.setText("0.5")
    dialog._prod_stagelo.setText("2")
    assert dialog._prod_overrides() == {
        "sn_uniform_threshold": "0.0025", "sn_iscale": "0.5", "sn_stagelo": "2"
    }
