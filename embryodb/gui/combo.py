"""Shared behaviour for editable comboboxes."""

from __future__ import annotations

from qtpy import QtCore, QtWidgets


def use_exact_case(combo: QtWidgets.QComboBox) -> QtWidgets.QComboBox:
    """Stop an editable combo rewriting the case of what the user typed.

    On focus-out Qt runs ``QComboBoxPrivate::_q_editingFinished``, which looks
    the typed text up with ``findText(text, matchFlags())`` and, on a hit, snaps
    the editable text to that item's own spelling. ``matchFlags()`` adds
    ``MatchCaseSensitive`` only when the combo's completer is case-sensitive --
    and an editable QComboBox's completer is case-INsensitive by default. So
    where the picklist holds both ``bw`` and ``BW`` (the corpus has each),
    whichever the database collation sorts first wins: typing ``BW`` and tabbing
    away silently yielded ``bw``, and picking from the dropdown was the only way
    to get the intended case. Note this fires regardless of ``insertPolicy`` --
    ``NoInsert`` guards the sibling ``_q_returnPressed`` path, not this one.

    Initials and names are data, not a controlled vocabulary, so match case
    exactly. Re-apply if the model is ever replaced wholesale
    (``setModel`` resets the completer; ``clear`` + ``addItems`` does not).
    """
    completer = combo.completer()
    if completer is not None:
        completer.setCaseSensitivity(QtCore.Qt.CaseSensitive)
    return combo


__all__ = ["use_exact_case"]
