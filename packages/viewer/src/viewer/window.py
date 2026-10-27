"""Qt viewer for OSF recordings: 3D point cloud, 2D field image and playback controls."""

import logging
import signal
from pathlib import Path

import numpy as np
import pyqtgraph as pg
import pyqtgraph.opengl as gl
from ouster.sdk import core
from PySide6 import QtCore, QtGui, QtWidgets

from .config import Config
from .frame import (
    FIELDS,
    Frame,
    azimuth_window_columns,
    destagger,
    format_timestamptz,
    write_ply,
)
from .recording import Recording

COLORMAPS = ("turbo", "viridis", "magma", "CET-L1")
SPEEDS = (0.25, 0.5, 1.0, 2.0, 4.0)
FULL_AZIMUTH = (0, 360_000)  # sensor azimuth_window range, millidegrees
FILE_FILTER = "Ouster recordings (*.osf)"
PNG_FILTER = "PNG image (*.png)"
log = logging.getLogger(__name__)


class _AzimuthSpinBox(QtWidgets.QSpinBox):
    """Spin box whose arrow / wheel steps wrap around the circle: 0 - 1000 gives 359000,
    360000 + 1000 gives 1000. Exactly 360000 stays reachable for the full window."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.setWrapping(True)  # keeps stepping enabled at the limits

    def stepBy(self, steps: int) -> None:  # noqa: N802 (Qt API)
        v = self.value() + steps * self.singleStep()
        span = self.maximum() - self.minimum()
        if v > self.maximum():
            v -= span
        elif v < self.minimum():
            v += span
        self.setValue(v)


class ViewerWindow(QtWidgets.QMainWindow):
    def __init__(self, cfg: Config, path: str | Path | None = None) -> None:
        super().__init__()
        self.cfg = cfg
        self.recording: Recording | None = None
        self.xyz_lut: core.XYZLutFloat | None = None
        self.frame: Frame | None = None
        self.folder: Path | None = None
        self.index = 0
        self.n_points = 0
        self.window_columns = (0, 0)  # columns inside the azimuth window, total columns
        # ~/.config/lidar/viewer.ini: remembers the last opened folder and the grid toggle.
        self.settings = QtCore.QSettings(
            QtCore.QSettings.IniFormat, QtCore.QSettings.UserScope, "lidar", "viewer"
        )

        self.resize(1400, 900)
        self.setAcceptDrops(True)
        self._build_ui()
        self._play_timer = QtCore.QTimer(self, timeout=self._on_tick)
        self._update_title()
        self._update_status()
        if not path:
            last = self.settings.value("last_folder", "")
            for folder in (Path(last) if last else None, cfg.recordings_dir):
                if folder and folder.is_dir():
                    path = folder
                    break
        if path:
            opener = self.open_folder if Path(path).is_dir() else self.open
            QtCore.QTimer.singleShot(0, lambda: opener(path))

    # ---- UI construction ------------------------------------------------

    def _build_ui(self) -> None:
        self.view3d = gl.GLViewWidget()
        self.view3d.setBackgroundColor("k")
        self._reset_view()
        self.grid = gl.GLGridItem()
        self.grid.setSize(40, 40)
        self.grid.setSpacing(1, 1)
        self.grid.setColor((255, 255, 255, 40))
        self.view3d.addItem(self.grid)
        self.view3d.addItem(gl.GLAxisItem(size=QtGui.QVector3D(1, 1, 1)))
        self.scatter = gl.GLScatterPlotItem(pxMode=True)
        self.scatter.setGLOptions("opaque")
        self.view3d.addItem(self.scatter)

        self.image_widget = pg.GraphicsLayoutWidget()
        self.image_plot = self.image_widget.addPlot()
        self.image_plot.hideAxis("left")
        self.image_plot.hideAxis("bottom")
        self.image_plot.invertY(True)
        self.image_item = pg.ImageItem(axisOrder="row-major")
        self.image_plot.addItem(self.image_item)
        self.image_plot.setMouseEnabled(x=True, y=False)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        splitter.addWidget(self.view3d)
        splitter.addWidget(self.image_widget)
        splitter.setSizes([700, 200])

        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter, 1)
        layout.addWidget(self._build_playback_bar())
        self.setCentralWidget(central)

        self._build_browser()
        self._build_menu()
        self._build_display_toolbar()

        self.status_label = QtWidgets.QLabel()
        self.statusBar().addWidget(self.status_label)

    def _build_browser(self) -> None:
        """Dock listing the .osf files of a folder; selecting one loads it."""
        self.fs_model = QtWidgets.QFileSystemModel(self)
        self.fs_model.setNameFilters(["*.osf"])
        self.fs_model.setNameFilterDisables(False)  # hide non-matching files instead of graying
        self.browser = QtWidgets.QTreeView()
        self.browser.setModel(self.fs_model)
        self.browser.setColumnHidden(2, True)  # "Type"
        self.browser.setSortingEnabled(True)
        self.browser.sortByColumn(0, QtCore.Qt.AscendingOrder)
        # Name takes the remaining width, cut from the start, so size and date stay visible.
        self.browser.setTextElideMode(QtCore.Qt.ElideLeft)
        header = self.browser.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        for column in (1, 3):  # "Size", "Date Modified"
            header.setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeToContents)
        # currentChanged covers both mouse clicks and Up/Down in the list.
        self.browser.selectionModel().currentChanged.connect(self._on_browse)

        self.browser_dock = QtWidgets.QDockWidget("Recordings", self)
        self.browser_dock.setObjectName("browser")
        self.browser_dock.setWidget(self.browser)
        self.addDockWidget(QtCore.Qt.LeftDockWidgetArea, self.browser_dock)
        self.browser_dock.hide()

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction("&Open…", QtGui.QKeySequence.Open, self._on_open)
        file_menu.addAction(
            "Open &folder…", QtGui.QKeySequence("Ctrl+Shift+O"), self._on_open_folder
        )
        self.save_action = file_menu.addAction(
            "&Save frame as PLY…", QtGui.QKeySequence.Save, self._on_save
        )
        self.export_action = file_menu.addAction(
            "&Export frame as PNG…", QtGui.QKeySequence("Ctrl+E"), self._on_export
        )
        file_menu.addSeparator()
        file_menu.addAction("&Quit", QtGui.QKeySequence.Quit, self.close)

        edit_menu = self.menuBar().addMenu("&Edit")
        self.copy_ts_action = edit_menu.addAction(
            "Copy frame &timestamp", QtGui.QKeySequence("Ctrl+Shift+C"), self._on_copy_timestamp
        )

        view_menu = self.menuBar().addMenu("&View")
        view_menu.addAction(self.browser_dock.toggleViewAction())
        self.grid_action = view_menu.addAction("Show &grid")
        self.grid_action.setCheckable(True)
        self.grid_action.setChecked(self.settings.value("show_grid", True, type=bool))
        self.grid_action.toggled.connect(self._on_grid_toggled)
        self.grid.setVisible(self.grid_action.isChecked())

    def _build_display_toolbar(self) -> None:
        tb = self.addToolBar("Display")
        tb.setMovable(False)

        self.field_box = QtWidgets.QComboBox()
        self.field_box.addItems(FIELDS)
        self.field_box.setCurrentText("REFLECTIVITY")
        self.field_box.currentTextChanged.connect(self._redraw)
        tb.addWidget(QtWidgets.QLabel(" Color: "))
        tb.addWidget(self.field_box)

        self.cmap_box = QtWidgets.QComboBox()
        self.cmap_box.addItems(COLORMAPS)
        self.cmap_box.currentTextChanged.connect(self._redraw)
        tb.addWidget(QtWidgets.QLabel(" Map: "))
        tb.addWidget(self.cmap_box)

        self.size_box = QtWidgets.QDoubleSpinBox(minimum=0.5, maximum=10, singleStep=0.5, value=2)
        self.size_box.valueChanged.connect(self._redraw)
        tb.addWidget(QtWidgets.QLabel(" Point size: "))
        tb.addWidget(self.size_box)

        self.max_range_box = QtWidgets.QDoubleSpinBox(
            minimum=0, maximum=200, singleStep=5, value=0, suffix=" m"
        )
        self.max_range_box.setSpecialValueText("no limit")
        self.max_range_box.valueChanged.connect(self._redraw)
        tb.addWidget(QtWidgets.QLabel(" Max range: "))
        tb.addWidget(self.max_range_box)

        # Simulates the sensor's azimuth_window: only columns inside [start, end] are kept.
        tb.addWidget(QtWidgets.QLabel(" Azimuth: "))
        self.az_start_box, self.az_end_box = (
            _AzimuthSpinBox(
                minimum=FULL_AZIMUTH[0],
                maximum=FULL_AZIMUTH[1],
                singleStep=1000,
                value=value,
                suffix=" m°",
                toolTip=f"azimuth_window {which} in millidegrees (start > end wraps through 0)",
            )
            for which, value in zip(("start", "end"), FULL_AZIMUTH, strict=True)
        )
        for box in (self.az_start_box, self.az_end_box):
            box.valueChanged.connect(self._on_azimuth_changed)
            tb.addWidget(box)
        full_btn = QtWidgets.QPushButton("Full", toolTip="Reset the azimuth window to 0…360000")
        full_btn.clicked.connect(self._reset_azimuth)
        tb.addWidget(full_btn)
        tb.addSeparator()

        reset_btn = QtWidgets.QPushButton("Reset view")
        reset_btn.clicked.connect(self._reset_view)
        tb.addWidget(reset_btn)

    def _build_playback_bar(self) -> QtWidgets.QWidget:
        bar = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(bar)
        row.setContentsMargins(8, 4, 8, 4)

        def button(text: str, tip: str, slot) -> QtWidgets.QPushButton:
            b = QtWidgets.QPushButton(text, toolTip=tip)
            b.setFocusPolicy(QtCore.Qt.NoFocus)
            b.setFixedWidth(40)
            b.clicked.connect(slot)
            row.addWidget(b)
            return b

        button("⏮", "First frame (Home)", lambda: self.seek(0))
        button("|◀", "Previous frame (←)", lambda: self.step(-1))
        self.play_btn = button("▶", "Play / pause (Space)", self.toggle_play)
        button("▶|", "Next frame (→)", lambda: self.step(1))
        button("⏭", "Last frame (End)", lambda: self.seek(len(self.recording or ()) - 1))

        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider.setFocusPolicy(QtCore.Qt.NoFocus)
        self.slider.valueChanged.connect(self._show_index)
        row.addWidget(self.slider, 1)

        self.position_label = QtWidgets.QLabel()
        self.position_label.setMinimumWidth(170)
        row.addWidget(self.position_label)

        self.timestamp_edit = QtWidgets.QLineEdit(readOnly=True, toolTip="Frame timestamp")
        self.timestamp_edit.setFocusPolicy(QtCore.Qt.ClickFocus)
        self.timestamp_edit.setFixedWidth(
            self.timestamp_edit.fontMetrics().horizontalAdvance(format_timestamptz(0) + "00") + 16
        )
        row.addWidget(self.timestamp_edit)
        copy_btn = button("Copy", "Copy frame timestamp (Ctrl+Shift+C)", self._on_copy_timestamp)
        copy_btn.setFixedWidth(50)

        self.speed_box = QtWidgets.QComboBox()
        self.speed_box.addItems([f"{s:g}×" for s in SPEEDS])
        self.speed_box.setCurrentIndex(SPEEDS.index(1.0))
        self.speed_box.setFocusPolicy(QtCore.Qt.NoFocus)
        self.speed_box.currentIndexChanged.connect(self._update_timer)
        row.addWidget(self.speed_box)

        self.loop_box = QtWidgets.QCheckBox("Loop", checked=True)
        self.loop_box.setFocusPolicy(QtCore.Qt.NoFocus)
        row.addWidget(self.loop_box)

        for key, slot in (
            (QtCore.Qt.Key_Space, self.toggle_play),
            (QtCore.Qt.Key_Left, lambda: self.step(-1)),
            (QtCore.Qt.Key_Right, lambda: self.step(1)),
            (QtCore.Qt.Key_Home, lambda: self.seek(0)),
            (QtCore.Qt.Key_End, lambda: self.seek(len(self.recording or ()) - 1)),
        ):
            QtGui.QShortcut(QtGui.QKeySequence(key), self, slot)
        return bar

    def _reset_azimuth(self) -> None:
        self.az_start_box.setValue(FULL_AZIMUTH[0])
        self.az_end_box.setValue(FULL_AZIMUTH[1])

    def _on_azimuth_changed(self) -> None:
        self._redraw()
        self._update_status()

    def _on_grid_toggled(self, checked: bool) -> None:
        self.grid.setVisible(checked)
        self.settings.setValue("show_grid", checked)

    def _reset_view(self) -> None:
        self.view3d.setCameraPosition(
            pos=QtGui.QVector3D(0, 0, 0), distance=30, elevation=30, azimuth=-135
        )

    # ---- recording / navigation ------------------------------------------

    def open(self, path: str | Path) -> None:
        try:
            recording = Recording(path)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Cannot open recording", f"{path}\n\n{e}")
            return
        if len(recording) == 0:
            QtWidgets.QMessageBox.warning(self, "Empty recording", f"{path} contains no frames.")
            recording.close()
            return
        self.pause()
        if self.recording is not None:
            self.recording.close()
        self.recording = recording
        self.xyz_lut = core.XYZLutFloat(recording.info, use_extrinsics=False)
        self._update_title()
        self.slider.blockSignals(True)
        self.slider.setRange(0, len(recording) - 1)
        self.slider.setValue(0)
        self.slider.blockSignals(False)
        self._update_timer()
        self._show_index(0)
        self._select_in_browser()

    def open_folder(self, path: str | Path) -> None:
        folder = Path(path).resolve()
        self.folder = folder
        self.settings.setValue("last_folder", str(folder))
        self.browser.setRootIndex(self.fs_model.setRootPath(str(folder)))
        self.browser_dock.setWindowTitle(f"Recordings — {folder.name}")
        self.browser_dock.show()
        self.resizeDocks([self.browser_dock], [380], QtCore.Qt.Horizontal)
        self.browser.setFocus()
        self._select_in_browser()

    def _select_in_browser(self) -> None:
        """Highlight the loaded recording if it is in the browsed folder."""
        if self.folder is None or self.recording is None:
            return
        path = self.recording.path.resolve()
        if path.is_relative_to(self.folder):
            # _on_browse ignores the already loaded file, so this does not reload it.
            self.browser.setCurrentIndex(self.fs_model.index(str(path)))

    def _on_browse(self, index: QtCore.QModelIndex) -> None:
        if not index.isValid() or self.fs_model.isDir(index):
            return
        path = Path(self.fs_model.filePath(index))
        if self.recording is None or path != self.recording.path.resolve():
            self.open(path)

    def seek(self, index: int) -> None:
        if self.recording is None:
            return
        self.slider.setValue(max(0, min(index, len(self.recording) - 1)))

    def step(self, delta: int) -> None:
        self.pause()
        self.seek(self.index + delta)

    def toggle_play(self) -> None:
        if self._play_timer.isActive():
            self.pause()
        elif self.recording is not None:
            if self.index >= len(self.recording) - 1:
                self.seek(0)
            self._play_timer.start()
            self.play_btn.setText("⏸")

    def pause(self) -> None:
        self._play_timer.stop()
        self.play_btn.setText("▶")

    def _update_timer(self) -> None:
        fps = self.recording.fps if self.recording else 10
        speed = SPEEDS[self.speed_box.currentIndex()]
        self._play_timer.setInterval(max(1, round(1000 / (fps * speed))))

    def _on_tick(self) -> None:
        assert self.recording is not None
        if self.index + 1 < len(self.recording):
            self.seek(self.index + 1)
        elif self.loop_box.isChecked():
            self.seek(0)
        else:
            self.pause()

    def _show_index(self, index: int) -> None:
        if self.recording is None:
            return
        self.index = index
        self.frame = self.recording.frame(index)
        self._redraw()
        self._update_status()

    # ---- rendering -------------------------------------------------------

    def _redraw(self) -> None:
        frame = self.frame
        if frame is None or self.recording is None:
            return
        values = self._field_values()
        image, levels, cmap = self._field_image(values)
        rng = frame.range

        in_window = self._in_window()
        max_range = self.max_range_box.value()
        in_view = in_window & (rng > 0)
        if max_range > 0:
            in_view &= rng < max_range * 1000

        xyz = self.xyz_lut(rng).reshape(-1, 3)
        mask = in_view.reshape(-1)
        flat = values.reshape(-1)[mask]
        colors = cmap.map(_normalize(flat, levels), mode="float")
        self.scatter.setData(pos=xyz[mask], color=colors, size=self.size_box.value())
        self.n_points = int(mask.sum())
        self.image_item.setImage(image, levels=levels)
        self.image_item.setColorMap(cmap)

    def _field_values(self) -> np.ndarray:
        """The selected field of the current frame (staggered, float)."""
        name = self.field_box.currentText()
        if name not in self.frame.fields:
            name = "RANGE"
        return self.frame.fields[name].astype(np.float32)

    def _in_window(self) -> np.ndarray:
        """Staggered mask of the pixels inside the azimuth window."""
        rng = self.frame.range
        cols = azimuth_window_columns(
            self.az_start_box.value(), self.az_end_box.value(), rng.shape[1]
        )
        self.window_columns = (int(cols.sum()), cols.size)
        return np.broadcast_to(cols, rng.shape)

    def _field_image(
        self, values: np.ndarray
    ) -> tuple[np.ndarray, tuple[float, float], pg.ColorMap]:
        """The 2D image of ``values`` as drawn: destaggered, NaN (transparent) outside the
        azimuth window, with its color levels and colormap. The levels are scaled over the
        whole frame, so azimuth and max range hide points but do not change the colors of
        the others (also in 3D)."""
        levels = _levels(values[self.frame.range > 0])
        image = destagger(self.recording.info, np.where(self._in_window(), values, np.nan))
        return image, levels, pg.colormap.get(self.cmap_box.currentText())

    def _grab_3d(self, include_grid: bool) -> QtGui.QImage:
        """The 3D view as shown (at the widget's size), with or without the grid."""
        self.grid.setVisible(include_grid)
        try:
            return self.view3d.grabFramebuffer()
        finally:
            self.grid.setVisible(self.grid_action.isChecked())

    def _update_title(self) -> None:
        name = self.recording.path.name if self.recording else "no recording"
        self.setWindowTitle(f"LidarViewer — {name}")

    def _update_status(self) -> None:
        rec = self.recording
        if rec is None or self.frame is None:
            self.position_label.setText("")
            self.status_label.setText(
                "Open a recording (Ctrl+O) or folder (Ctrl+Shift+O), or drop one here"
            )
            self.timestamp_edit.clear()
            self.save_action.setEnabled(False)
            self.export_action.setEnabled(False)
            self.copy_ts_action.setEnabled(False)
            return
        self.save_action.setEnabled(True)
        self.export_action.setEnabled(True)
        ts = self.frame.wall_clock_ns
        self.timestamp_edit.setText(format_timestamptz(ts, self.cfg.timezone) if ts else "")
        self.copy_ts_action.setEnabled(bool(ts))
        self.position_label.setText(
            f"{self.index + 1} / {len(rec)}   "
            f"{rec.seconds(self.frame):6.2f} / {rec.duration_s:.2f} s"
        )
        self.status_label.setText(
            f"{rec.path.name}   |   {rec.info.prod_line} S/N {rec.info.sn}   |   "
            f"frame id {self.frame.frame_id}   |   {self.n_points:,} points" + self._window_status()
        )

    def _window_status(self) -> str:
        n, width = self.window_columns
        if n == width:
            return ""
        return f"   |   azimuth window {n} / {width} columns"

    # ---- actions -------------------------------------------------------------

    def _start_dir(self) -> str:
        if self.recording is not None:
            return str(self.recording.path.parent)
        return str(self.folder or self.cfg.recordings_dir or "")

    def _on_open(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open recording", self._start_dir(), FILE_FILTER
        )
        if path:
            self.open(path)

    def _on_open_folder(self) -> None:
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Open folder", str(self.folder or self._start_dir())
        )
        if path:
            self.open_folder(path)

    def _on_copy_timestamp(self) -> None:
        text = self.timestamp_edit.text()
        if not text:
            return
        QtWidgets.QApplication.clipboard().setText(text)
        self.statusBar().showMessage(f"Copied {text}", 3000)

    def _default_frame_path(self, suffix: str) -> Path:
        rec = self.recording
        return rec.path.with_name(f"{rec.path.stem}_frame{self.index + 1:04d}{suffix}")

    def _on_save(self) -> None:
        if self.frame is None or self.recording is None:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save frame", str(self._default_frame_path(".ply")), "Point cloud (*.ply)"
        )
        if not path:
            return
        mask = (self.frame.range > 0).reshape(-1)
        xyz = self.xyz_lut(self.frame.range).reshape(-1, 3)[mask]
        fields = {k: v.reshape(-1)[mask] for k, v in self.frame.fields.items()}
        write_ply(path, xyz, fields)
        self.statusBar().showMessage(f"Saved {path}", 5000)

    def _on_export(self) -> None:
        if self.frame is None or self.recording is None:
            return
        choice = _ask_export(self, self.grid_action.isChecked())
        if choice is None:
            return
        want_2d, want_3d, include_grid = choice
        name, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export frame", str(self._default_frame_path(".png")), PNG_FILTER
        )
        if not name:
            return
        base = Path(name).with_suffix("")
        images = []
        if want_2d:
            image, levels, cmap = self._field_image(self._field_values())
            images.append((base.with_name(base.name + "_2d.png"), _rgba_image(image, levels, cmap)))
        if want_3d:
            images.append((base.with_name(base.name + "_3d.png"), self._grab_3d(include_grid)))
        for path, image in images:
            if not image.save(str(path), "PNG"):
                QtWidgets.QMessageBox.critical(self, "Cannot export frame", f"{path}")
                return
        self.statusBar().showMessage("Exported " + ", ".join(str(p) for p, _ in images), 5000)

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:  # noqa: N802 (Qt API)
        if _dropped_path(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event: QtGui.QDropEvent) -> None:  # noqa: N802 (Qt API)
        path = _dropped_path(event.mimeData())
        if path is None:
            return
        if path.is_dir():
            self.open_folder(path)
        else:
            self.open(path)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802 (Qt API)
        self.pause()
        if self.recording is not None:
            self.recording.close()
        super().closeEvent(event)


def _dropped_path(mime: QtCore.QMimeData) -> Path | None:
    """The first dropped local .osf file or folder."""
    for url in mime.urls():
        if not url.isLocalFile():
            continue
        path = Path(url.toLocalFile())
        if path.is_dir() or path.suffix.lower() == ".osf":
            return path
    return None


def _ask_export(parent: QtWidgets.QWidget, grid: bool) -> tuple[bool, bool, bool] | None:
    """Ask which images to export; returns (2D image, 3D view, include grid) or None if
    cancelled."""
    dialog = QtWidgets.QDialog(parent, windowTitle="Export frame as PNG")
    form = QtWidgets.QFormLayout(dialog)
    image_2d = QtWidgets.QCheckBox("2D image (full sensor resolution)", checked=True)
    view_3d = QtWidgets.QCheckBox("3D view (as shown)", checked=True)
    include_grid = QtWidgets.QCheckBox("Include grid", checked=grid)
    view_3d.toggled.connect(include_grid.setEnabled)
    for box in (image_2d, view_3d, include_grid):
        form.addRow(box)
    buttons = QtWidgets.QDialogButtonBox(
        QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
    )
    ok = buttons.button(QtWidgets.QDialogButtonBox.Ok)
    for box in (image_2d, view_3d):
        box.toggled.connect(lambda: ok.setEnabled(image_2d.isChecked() or view_3d.isChecked()))
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    form.addRow(buttons)
    if dialog.exec() != QtWidgets.QDialog.Accepted:
        return None
    return image_2d.isChecked(), view_3d.isChecked(), include_grid.isChecked()


def _normalize(values: np.ndarray, levels: tuple[float, float]) -> np.ndarray:
    lo, hi = levels
    return np.clip((values - lo) / max(hi - lo, 1e-6), 0, 1)


def _rgba_image(image: np.ndarray, levels: tuple[float, float], cmap: pg.ColorMap) -> QtGui.QImage:
    """``image`` (H x W, NaN = no data) colored like the 2D view, NaN transparent."""
    nan = np.isnan(image)
    rgba = cmap.map(_normalize(np.nan_to_num(image), levels).reshape(-1), mode="byte")
    rgba = np.ascontiguousarray(rgba.reshape(*image.shape, 4), np.uint8)
    rgba[nan, 3] = 0
    h, w = image.shape
    # copy(): the QImage must not keep pointing into the numpy buffer.
    return QtGui.QImage(rgba.data, w, h, 4 * w, QtGui.QImage.Format_RGBA8888).copy()


def _levels(values: np.ndarray) -> tuple[float, float]:
    """Robust color scaling: 1st to 99th percentile of the valid values."""
    if values.size == 0:
        return 0.0, 1.0
    lo, hi = np.percentile(values, (1, 99))
    return float(lo), float(max(hi, lo + 1))


def run(path: str | None, cfg: Config) -> int:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    pg.setConfigOptions(antialias=False)
    win = ViewerWindow(cfg, path)
    win.showMaximized()

    # Ctrl+C in the console closes the window; a second Ctrl+C kills the process.
    def on_sigint(*_) -> None:
        log.info("Ctrl+C: closing viewer (press again to force quit)")
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        win.close()

    signal.signal(signal.SIGINT, on_sigint)
    # Python only runs signal handlers between bytecodes; wake it up from the Qt loop.
    wake = QtCore.QTimer(win, interval=200)
    wake.timeout.connect(lambda: None)
    wake.start()
    return app.exec()
