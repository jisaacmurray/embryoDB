"""Regression guard for editable-combo case mangling.

Typing initials that differ only by case from an existing picklist entry
(``BW`` vs ``bw`` -- the corpus has both) used to come back lowercased as soon
as the field lost focus. See ``embryodb.gui.combo.use_exact_case``.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_API", "pyqt5")

QtWidgets = pytest.importorskip("qtpy.QtWidgets")
QtTest = pytest.importorskip("qtpy.QtTest")

from embryodb.gui.combo import use_exact_case  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def _type_then_leave(qapp, typed: str, *, fix: bool) -> str:
    """Type into an editable combo, tab away, return what a save would read."""
    holder = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(holder)
    combo = QtWidgets.QComboBox()
    combo.setEditable(True)
    if fix:
        use_exact_case(combo)
    combo.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
    # Lowercase first, matching the Postgres collation order distinct_values returns.
    combo.addItems(["", "bw", "BW", "jmurr"])
    sink = QtWidgets.QLineEdit()
    layout.addWidget(combo)
    layout.addWidget(sink)
    holder.show()

    combo.setEditText("")
    combo.setFocus()
    qapp.processEvents()
    QtTest.QTest.keyClicks(combo.lineEdit(), typed)
    sink.setFocus()  # user tabs to the next field or clicks Save
    qapp.processEvents()
    return combo.currentText()


@pytest.mark.parametrize("typed", ["BW", "bw", "Bw"])
def test_typed_case_survives_focus_out(qapp, typed):
    assert _type_then_leave(qapp, typed, fix=True) == typed


def test_without_the_fix_case_is_mangled(qapp):
    """Pin the Qt behaviour the fix exists to counter, so a binding upgrade that
    changes it doesn't leave us silently carrying a no-op."""
    assert _type_then_leave(qapp, "BW", fix=False) == "bw"


def test_returns_the_combo_for_chaining(qapp):
    combo = QtWidgets.QComboBox()
    combo.setEditable(True)
    assert use_exact_case(combo) is combo


def test_noop_on_non_editable_combo(qapp):
    """Non-editable combos have no completer; must not raise."""
    combo = QtWidgets.QComboBox()
    assert combo.completer() is None
    use_exact_case(combo)
