"""
Qt (PySide6) desktop mockup -- LAYOUT CANDIDATE 3
=================================================

A native desktop variant of the same core.  Demonstrates that the headless
:mod:`smi_acquire` core (samples / techniques / guidance / codegen) is framework-agnostic:
this file contains **zero** acquisition logic, only widgets bound to the core.

Layout: a classic three-pane desktop app driven by a ``QSplitter``:

    +---------------------+--------------------------+--------------------------+
    | Sample bar (QTable) | Technique + param form   | Generated script (mono)  |
    | add / del / dup     | (QComboBox + dynamic     | + Copy / Save buttons    |
    | load CSV            |  form fields)            |                          |
    +---------------------+--------------------------+--------------------------+

PySide6 is not installed in the smi-browser pixi env by default; add it with
``pixi add pyside6`` (or run in the ``[feature.qt]`` env -- see pixi.toml) and then::

    python apps/qt_app.py

This file is deliberately self-contained and commented as a *layout reference*; the wiring
mirrors the Panel apps one-for-one so they can be compared directly.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from smi_acquire import samples, techniques, guidance, codegen

try:
    from PySide6 import QtWidgets, QtGui, QtCore
except Exception as exc:  # pragma: no cover - Qt optional
    raise SystemExit(
        "PySide6 not installed. Try `pixi add pyside6` then rerun. (" + str(exc) + ")")


SAMPLE_COLS = samples.SAMPLE_FIELDS + ["md"]
STARTER = [
    ["sample_01", "55000", "5000", "7000", "", "10", "", "", "", "0.1 0.2", ""],
    ["sample_02", "42000", "5000", "7000", "", "10", "", "", "", "0.1 0.2", ""],
]


class AcquireWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SMI-SWAXS Acquire - Qt")
        self.resize(1400, 720)
        self._param_readers = {}

        splitter = QtWidgets.QSplitter()
        splitter.addWidget(self._build_samples())
        splitter.addWidget(self._build_technique())
        splitter.addWidget(self._build_script())
        splitter.setSizes([460, 440, 500])
        self.setCentralWidget(splitter)

        self._on_technique_changed()  # initial form + script

    # -- LEFT: sample table --------------------------------------------------
    def _build_samples(self):
        box = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(box)
        v.addWidget(QtWidgets.QLabel("<b>Sample bar</b>"))

        self.table = QtWidgets.QTableWidget(len(STARTER), len(SAMPLE_COLS))
        self.table.setHorizontalHeaderLabels(SAMPLE_COLS)
        for r, row in enumerate(STARTER):
            for c, val in enumerate(row):
                self.table.setItem(r, c, QtWidgets.QTableWidgetItem(val))
        self.table.itemChanged.connect(self._regen)
        v.addWidget(self.table)

        btns = QtWidgets.QHBoxLayout()
        for label, slot in (("+ Row", self._add_row), ("- Row", self._del_row),
                            ("Duplicate", self._dup_row), ("Load CSV...", self._load_csv)):
            b = QtWidgets.QPushButton(label)
            b.clicked.connect(slot)
            btns.addWidget(b)
        v.addLayout(btns)

        self.project = QtWidgets.QLineEdit()
        self.project.setPlaceholderText("Project name (md), e.g. 311234_Demo")
        self.project.textChanged.connect(self._regen)
        v.addWidget(self.project)
        return box

    def _add_row(self):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QtWidgets.QTableWidgetItem("sample_%02d" % (r + 1)))
        self._regen()

    def _del_row(self):
        if self.table.rowCount() > 1:
            self.table.removeRow(self.table.rowCount() - 1)
            self._regen()

    def _dup_row(self):
        r = self.table.rowCount() - 1
        if r < 0:
            return
        self.table.insertRow(r + 1)
        for c in range(self.table.columnCount()):
            src = self.table.item(r, c)
            txt = (src.text() if src else "")
            if c == 0:
                txt += "_copy"
            self.table.setItem(r + 1, c, QtWidgets.QTableWidgetItem(txt))
        self._regen()

    def _load_csv(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load CSV", "", "CSV (*.csv)")
        if not path:
            return
        import csv
        with open(path, newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, col in enumerate(SAMPLE_COLS):
                self.table.setItem(r, c, QtWidgets.QTableWidgetItem(str(row.get(col, ""))))
        self._regen()

    def _table_records(self):
        recs = []
        for r in range(self.table.rowCount()):
            rec = {}
            for c, col in enumerate(SAMPLE_COLS):
                item = self.table.item(r, c)
                rec[col] = item.text() if item else ""
            recs.append(rec)
        return recs

    # -- CENTER: technique + params -----------------------------------------
    def _build_technique(self):
        box = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(box)
        v.addWidget(QtWidgets.QLabel("<b>Technique &amp; parameters</b>"))

        self.tech = QtWidgets.QComboBox()
        for letter in techniques.all_letters():
            spec = techniques.get(letter)
            title = spec.title if spec else techniques.SPECIAL[letter]["title"]
            self.tech.addItem("{} - {}".format(letter, title), letter)
        self.tech.currentIndexChanged.connect(self._on_technique_changed)
        v.addWidget(self.tech)

        self.tech_info = QtWidgets.QLabel()
        self.tech_info.setWordWrap(True)
        self.tech_info.setStyleSheet("color:#555;")
        v.addWidget(self.tech_info)

        self.form_host = QtWidgets.QWidget()
        self.form_layout = QtWidgets.QFormLayout(self.form_host)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.form_host)
        v.addWidget(scroll)
        return box

    def _on_technique_changed(self):
        letter = self.tech.currentData()
        spec = techniques.get(letter)
        self.tech_info.setText(
            spec.summary if spec else techniques.SPECIAL[letter]["summary"])
        # rebuild form
        while self.form_layout.count():
            item = self.form_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._param_readers = {}
        if spec is not None:
            for p in spec.params:
                w, reader = self._param_widget(p)
                self.form_layout.addRow(p.label, w)
                self._param_readers[p.name] = reader
        self._regen()

    def _param_widget(self, p: techniques.ParamSpec):
        if p.kind == "bool":
            w = QtWidgets.QCheckBox()
            w.setChecked(bool(p.default))
            w.stateChanged.connect(self._regen)
            return w, lambda: w.isChecked()
        if p.kind in ("choice", "token") and p.choices:
            w = QtWidgets.QComboBox()
            w.addItems([str(c) for c in p.choices])
            w.setCurrentText(str(p.default))
            w.currentIndexChanged.connect(self._regen)
            return w, lambda: w.currentText()
        # text-like (covers float/int/optfloat/floats/tuple/str): core re-parses strings
        default = ", ".join(str(x) for x in p.default) if isinstance(p.default, (list, tuple)) \
            else ("" if p.default is None else str(p.default))
        w = QtWidgets.QLineEdit(default)
        w.textChanged.connect(self._regen)

        def reader():
            txt = w.text().strip()
            if p.kind == "optfloat":
                return None if txt == "" else float(txt)
            if p.kind in ("floats", "tuple"):
                return [float(x) for x in txt.replace(";", " ").replace(",", " ").split()]
            if p.kind == "int":
                return int(float(txt))
            if p.kind == "float":
                return float(txt)
            return txt
        return w, reader

    # -- RIGHT: script -------------------------------------------------------
    def _build_script(self):
        box = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(box)
        v.addWidget(QtWidgets.QLabel("<b>Generated script</b>"))
        self.code = QtWidgets.QPlainTextEdit()
        self.code.setReadOnly(True)
        self.code.setFont(QtGui.QFont("monospace", 10))
        v.addWidget(self.code)
        btns = QtWidgets.QHBoxLayout()
        copy = QtWidgets.QPushButton("Copy")
        copy.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(
            self.code.toPlainText()))
        save = QtWidgets.QPushButton("Save .py...")
        save.clicked.connect(self._save)
        btns.addWidget(copy)
        btns.addWidget(save)
        v.addLayout(btns)
        return box

    def _save(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save", "plan.py", "Python (*.py)")
        if path:
            with open(path, "w") as fh:
                fh.write(self.code.toPlainText())

    # -- regenerate ----------------------------------------------------------
    def _regen(self, *_):
        try:
            bar = samples.records_to_samples(self._table_records())
            values = {name: r() for name, r in self._param_readers.items()}
            pmd = {"project_name": self.project.text()} if self.project.text().strip() else None
            self.code.setPlainText(
                codegen.generate_script(bar, self.tech.currentData(), values, project_md=pmd))
        except Exception as exc:
            self.code.setPlainText("# ERROR: {}".format(exc))


def main():
    app = QtWidgets.QApplication(sys.argv)
    win = AcquireWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
