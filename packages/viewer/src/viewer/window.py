"""Qt viewer for OSF recordings: 3D point cloud, 2D field image and playback controls."""

import copy
from pathlib import Path

import numpy as np
import pyqtgraph as pg
import pyqtgraph.opengl as gl
from ouster.sdk import core
from PySide6 import QtCore, QtGui, QtWidgets

from staticmodel import StaticModel, StaticParams, model_path

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
MODEL_FILTER = "Static model (*.npz)"
# Static model display modes.
SHOW_ALL, HIDE_STATIC, ONLY_STATIC, HIGHLIGHT_STATIC, DIM_STATIC = (
    "Show all",
    "Hide static",
    "Only static",
    "Highlight static",
    "Dim static",
)
STATIC_MODES = (SHOW_ALL, HIDE_STATIC, ONLY_STATIC, HIGHLIGHT_STATIC, DIM_STATIC)
DIM_FACTOR = 0.3  # brightness of dimmed static points
MODEL_LAYER_COLOR = (0.35, 0.55, 1.0, 1.0)


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
        self.model: StaticModel | None = None
        self.model_path: Path | None = None
        self.model_dirty = False  # changed since loaded / saved
        self.static_counts: tuple[int, int] | None = None  # static, all points in the view
        self.highlight_color = QtGui.QColor(255, 0, 255)
        self._model_cloud: tuple[tuple, np.ndarray, np.ndarray] | None = None  # cache
        # ~/.config/lidar/viewer.ini: remembers the last opened folder.
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
        grid = gl.GLGridItem()
        grid.setSize(40, 40)
        grid.setSpacing(1, 1)
        grid.setColor((255, 255, 255, 40))
        self.view3d.addItem(grid)
        self.view3d.addItem(gl.GLAxisItem(size=QtGui.QVector3D(1, 1, 1)))
        self.model_scatter = gl.GLScatterPlotItem(pxMode=True)  # the static model's surface
        self.model_scatter.setGLOptions("opaque")
        self.view3d.addItem(self.model_scatter)
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
        self.static_overlay = pg.ImageItem(axisOrder="row-major")  # highlight / dim static
        self.image_plot.addItem(self.static_overlay)
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
        self._build_model_actions()
        self._build_model_dock()
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

    def _build_model_actions(self) -> None:
        def action(text: str, slot, tip: str, key=None) -> QtGui.QAction:
            a = QtGui.QAction(text, self, toolTip=tip, statusTip=tip)
            if key is not None:
                a.setShortcut(key)
            a.triggered.connect(slot)
            return a

        self.new_model_action = action(
            "&New from recording…",
            lambda: self._on_build_model(new=True),
            "Build a new static model from frames of the current recording",
        )
        self.add_model_action = action(
            "&Add recording…",
            lambda: self._on_build_model(new=False),
            "Enhance the static model with frames of the current recording",
        )
        self.open_model_action = action(
            "&Open…", self._on_open_model, "Open a static model", QtGui.QKeySequence("Ctrl+M")
        )
        self.save_model_action = action(
            "&Save", lambda: self._on_save_model(), "Save the static model"
        )
        self.save_model_as_action = action(
            "Save &as…", lambda: self._on_save_model(ask=True), "Save the static model as"
        )
        self.close_model_action = action("&Close", self._on_close_model, "Close the static model")
        self.model_actions = [
            self.new_model_action,
            self.add_model_action,
            self.open_model_action,
            self.save_model_action,
            self.save_model_as_action,
            self.close_model_action,
        ]

    def _build_model_dock(self) -> None:
        """Dock with the static model: file actions, display mode and tolerances."""
        panel = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(panel)

        self.model_label = QtWidgets.QLabel(wordWrap=True)
        self.model_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        form.addRow(self.model_label)

        buttons = QtWidgets.QGridLayout()
        for i, action in enumerate(self.model_actions):
            b = QtWidgets.QToolButton()
            b.setDefaultAction(action)
            b.setText(action.text().replace("&", "").rstrip("…").replace(" from recording", ""))
            b.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
            buttons.addWidget(b, i // 2, i % 2)
        form.addRow(buttons)

        self.static_mode_box = QtWidgets.QComboBox()
        self.static_mode_box.addItems(STATIC_MODES)
        self.static_mode_box.currentTextChanged.connect(self._on_static_changed)
        form.addRow("Display:", self.static_mode_box)

        self.highlight_btn = QtWidgets.QPushButton(toolTip="Color of highlighted static points")
        self.highlight_btn.clicked.connect(self._on_pick_highlight)
        self.highlight_btn.setEnabled(False)  # only in "Highlight static" mode
        self._update_highlight_btn()
        form.addRow("Highlight:", self.highlight_btn)

        defaults = StaticParams()
        self.tol_m_box = QtWidgets.QDoubleSpinBox(
            minimum=0, maximum=5, singleStep=0.05, decimals=2, suffix=" m",
            value=defaults.abs_tolerance_m,
            toolTip="A point is static if its range is within this distance of the background",
        )  # fmt: skip
        self.tol_pct_box = QtWidgets.QDoubleSpinBox(
            minimum=0, maximum=20, singleStep=0.5, decimals=1, suffix=" %",
            value=defaults.rel_tolerance * 100,
            toolTip="... or within this fraction of the range, whichever is larger",
        )  # fmt: skip
        self.min_seen_box = QtWidgets.QSpinBox(
            minimum=1, maximum=100, suffix=" %", value=round(defaults.min_fraction * 100),
            toolTip="A background range must be seen in at least this share of the model's frames",
        )  # fmt: skip
        form.addRow("Tolerance:", self.tol_m_box)
        form.addRow("or:", self.tol_pct_box)
        form.addRow("Min. seen:", self.min_seen_box)
        for box in (self.tol_m_box, self.tol_pct_box, self.min_seen_box):
            box.valueChanged.connect(self._on_static_changed)

        self.show_model_box = QtWidgets.QCheckBox(
            "Show model", toolTip="Draw the model's background surface as its own layer"
        )
        self.show_model_box.toggled.connect(self._on_static_changed)
        form.addRow(self.show_model_box)

        self.model_dock = QtWidgets.QDockWidget("Static model", self)
        self.model_dock.setObjectName("static_model")
        self.model_dock.setWidget(panel)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, self.model_dock)
        self.model_dock.hide()
        self._update_model_ui()

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction("&Open…", QtGui.QKeySequence.Open, self._on_open)
        file_menu.addAction(
            "Open &folder…", QtGui.QKeySequence("Ctrl+Shift+O"), self._on_open_folder
        )
        self.save_action = file_menu.addAction(
            "&Save frame as PLY…", QtGui.QKeySequence.Save, self._on_save
        )
        file_menu.addSeparator()
        file_menu.addAction("&Quit", QtGui.QKeySequence.Quit, self.close)

        edit_menu = self.menuBar().addMenu("&Edit")
        self.copy_ts_action = edit_menu.addAction(
            "Copy frame &timestamp", QtGui.QKeySequence("Ctrl+Shift+C"), self._on_copy_timestamp
        )

        model_menu = self.menuBar().addMenu("&Model")
        for action in self.model_actions[:2]:
            model_menu.addAction(action)
        model_menu.addSeparator()
        for action in self.model_actions[2:]:
            model_menu.addAction(action)

        view_menu = self.menuBar().addMenu("&View")
        view_menu.addAction(self.browser_dock.toggleViewAction())
        view_menu.addAction(self.model_dock.toggleViewAction())

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
            QtWidgets.QSpinBox(
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
        self._model_cloud = None  # new XYZ table
        self._update_model_ui()
        self._auto_load_model()
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
        name = self.field_box.currentText()
        if name not in frame.fields:
            name = "RANGE"
        values = frame.fields[name].astype(np.float32)
        rng = frame.range

        cols = azimuth_window_columns(
            self.az_start_box.value(), self.az_end_box.value(), rng.shape[1]
        )
        in_window = np.broadcast_to(cols, rng.shape)
        self.window_columns = (int(cols.sum()), cols.size)
        max_range = self.max_range_box.value()
        in_view = in_window & (rng > 0)
        if max_range > 0:
            in_view &= rng < max_range * 1000

        static = self._static_mask(rng)
        mode = self.static_mode_box.currentText()
        shown = _apply_static_mode(in_view, static, mode)
        self.static_counts = None
        if static is not None:
            self.static_counts = (int((static & in_view).sum()), int(in_view.sum()))

        xyz = self.xyz_lut(rng).reshape(-1, 3)
        mask = shown.reshape(-1)
        flat = values.reshape(-1)[mask]
        # Scaled over the whole frame: azimuth, max range and static mode hide points but do
        # not change the colors of the others.
        levels = _levels(values[rng > 0])
        lo, hi = levels
        cmap = pg.colormap.get(self.cmap_box.currentText())
        norm = np.clip((flat - lo) / max(hi - lo, 1e-6), 0, 1)
        colors = cmap.map(norm, mode="float")
        if static is not None and mode == HIGHLIGHT_STATIC:
            colors[static.reshape(-1)[mask]] = self.highlight_color.getRgbF()
        elif static is not None and mode == DIM_STATIC:
            colors[static.reshape(-1)[mask], :3] *= DIM_FACTOR
        self.scatter.setData(pos=xyz[mask], color=colors, size=self.size_box.value())
        self.n_points = int(mask.sum())
        self._draw_model_layer(in_window, max_range)

        info = self.recording.info
        # Pixels hidden by the static mode are drawn transparent (NaN) like those outside
        # the azimuth window.
        visible = _apply_static_mode(in_window, static, mode)
        image = destagger(info, np.where(visible, values, np.nan))
        self.image_item.setImage(image, levels=levels)
        self.image_item.setColorMap(cmap)
        self._draw_static_overlay(info, static, in_window, mode)

    def _draw_static_overlay(
        self, info: core.SensorInfo, static: np.ndarray | None, in_window: np.ndarray, mode: str
    ) -> None:
        """Tint (highlight) or darken (dim) the static pixels of the 2D image."""
        if static is None or mode not in (HIGHLIGHT_STATIC, DIM_STATIC):
            self.static_overlay.hide()
            return
        if mode == HIGHLIGHT_STATIC:
            rgba = self.highlight_color.getRgb()[:3] + (255,)
        else:
            rgba = (0, 0, 0, round(255 * (1 - DIM_FACTOR)))
        overlay = np.zeros((*static.shape, 4), np.uint8)
        overlay[destagger(info, (static & in_window).astype(np.uint8)).astype(bool)] = rgba
        self.static_overlay.setImage(overlay, levels=(0, 255))
        self.static_overlay.show()

    def _draw_model_layer(self, in_window: np.ndarray, max_range: float) -> None:
        """The model's background surface as its own point layer (cached per model state)."""
        if not self.show_model_box.isChecked() or not self._model_active():
            self.model_scatter.setVisible(False)
            return
        params = self._static_params()
        key = (id(self.model), self.model.frames, params)
        if self._model_cloud is None or self._model_cloud[0] != key:
            bg = self.model.background_range(params)
            self._model_cloud = (key, self.xyz_lut(bg).reshape(-1, 3), bg.reshape(-1))
        _, xyz, bg = self._model_cloud
        mask = (bg > 0) & in_window.reshape(-1)
        if max_range > 0:
            mask &= bg < max_range * 1000
        self.model_scatter.setData(
            pos=xyz[mask], color=MODEL_LAYER_COLOR, size=self.size_box.value()
        )
        self.model_scatter.setVisible(True)

    # ---- static model ------------------------------------------------------------

    def _model_active(self) -> bool:
        """A model is loaded and fits the current recording's sensor."""
        return (
            self.model is not None
            and self.recording is not None
            and self.model.mismatch(self.recording.info) is None
        )

    def _static_params(self) -> StaticParams:
        return StaticParams(
            abs_tolerance_m=self.tol_m_box.value(),
            rel_tolerance=self.tol_pct_box.value() / 100,
            min_fraction=self.min_seen_box.value() / 100,
        )

    def _static_mask(self, rng: np.ndarray) -> np.ndarray | None:
        if not self._model_active():
            return None
        return self.model.static_mask(rng, self._static_params())

    def _on_static_changed(self) -> None:
        self.highlight_btn.setEnabled(self.static_mode_box.currentText() == HIGHLIGHT_STATIC)
        self._redraw()
        self._update_status()

    def _on_pick_highlight(self) -> None:
        color = QtWidgets.QColorDialog.getColor(self.highlight_color, self, "Highlight color")
        if color.isValid():
            self.highlight_color = color
            self._update_highlight_btn()
            self._redraw()

    def _update_highlight_btn(self) -> None:
        self.highlight_btn.setStyleSheet(f"background-color: {self.highlight_color.name()}")

    def _set_model(self, model: StaticModel | None, path: Path | None, dirty: bool) -> None:
        self.model, self.model_path, self.model_dirty = model, path, dirty
        self._model_cloud = None
        self._update_model_ui()
        self._redraw()
        self._update_status()

    def _update_model_ui(self) -> None:
        model, rec = self.model, self.recording
        has_model = model is not None
        self.new_model_action.setEnabled(rec is not None)
        self.add_model_action.setEnabled(has_model and self._model_active())
        for action in (self.save_model_action, self.save_model_as_action, self.close_model_action):
            action.setEnabled(has_model)
        if model is None:
            self.model_label.setText(
                "No static model. Build one from the current recording (New) or open one."
            )
            return
        name = self.model_path.name if self.model_path else "unsaved"
        recordings = len({s["recording"] for s in model.sources})
        text = (
            f"<b>{name}{' *' if self.model_dirty else ''}</b><br>"
            f"{model.prod_line} S/N {model.serial}, {model.shape[0]} x {model.shape[1]}<br>"
            f"{model.frames} frames from {recordings} recording(s)"
        )
        if rec is not None and (reason := model.mismatch(rec.info)):
            text += f"<br><span style='color:#e55'>Not used: {reason}</span>"
        self.model_label.setText(text)
        self.model_label.setToolTip(
            "\n".join(
                f"{s['recording']}: frames {s['first'] + 1}–{s['last'] + 1} ({s['added']})"
                for s in model.sources
            )
        )

    def _confirm_discard_model(self) -> bool:
        """True if there are no unsaved model changes, or the user saved / discarded them."""
        if self.model is None or not self.model_dirty:
            return True
        answer = QtWidgets.QMessageBox.question(
            self,
            "Unsaved static model",
            "The static model has unsaved changes. Save them?",
            QtWidgets.QMessageBox.Save
            | QtWidgets.QMessageBox.Discard
            | QtWidgets.QMessageBox.Cancel,
        )
        if answer == QtWidgets.QMessageBox.Save:
            return self._on_save_model()
        return answer == QtWidgets.QMessageBox.Discard

    def _on_build_model(self, new: bool) -> None:
        rec = self.recording
        if rec is None or (new and not self._confirm_discard_model()):
            return
        if not new and (self.model is None or not self._model_active()):
            return
        frames = _ask_frame_range(self, len(rec), "New static model" if new else "Add recording")
        if frames is None:
            return
        first, last = frames
        self.pause()
        # "Add" works on a copy, so cancelling leaves the model unchanged.
        model = StaticModel.new(rec.info) if new else copy.deepcopy(self.model)
        progress = QtWidgets.QProgressDialog(
            f"Adding frames of {rec.path.name}…", "Cancel", 0, last - first + 1, self
        )
        progress.setWindowTitle("Static model")
        progress.setWindowModality(QtCore.Qt.WindowModal)
        progress.setMinimumDuration(0)
        for n, i in enumerate(range(first, last + 1)):
            progress.setValue(n)
            if progress.wasCanceled():
                return
            model.update(rec.frame(i).range)
        progress.setValue(last - first + 1)
        model.add_source(rec.path.name, last - first + 1, first, last)
        self._set_model(model, None if new else self.model_path, dirty=True)
        self.model_dock.show()
        self.statusBar().showMessage(f"Added {last - first + 1} frames to the static model", 5000)

    def _default_model_path(self) -> Path:
        serial = self.model.serial if self.model else ""
        if self.cfg.static_model:
            return model_path(self.cfg.static_model, serial)
        return Path(self._start_dir()) / f"{serial}_static.npz"

    def _on_save_model(self, ask: bool = False) -> bool:
        if self.model is None:
            return False
        path = self.model_path
        if ask or path is None:
            name, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "Save static model", str(path or self._default_model_path()), MODEL_FILTER
            )
            if not name:
                return False
            path = Path(name)
        try:
            self.model.save(path)
        except OSError as e:
            QtWidgets.QMessageBox.critical(self, "Cannot save static model", f"{path}\n\n{e}")
            return False
        self._set_model(self.model, path, dirty=False)
        self.statusBar().showMessage(f"Saved {path}", 5000)
        return True

    def _on_open_model(self) -> None:
        if not self._confirm_discard_model():
            return
        start = self.model_path or self._default_model_path()
        name, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open static model", str(start.parent), MODEL_FILTER
        )
        if name:
            self.load_model(name)

    def load_model(self, path: str | Path, quiet: bool = False) -> bool:
        try:
            model = StaticModel.load(path)
        except (OSError, ValueError) as e:
            if not quiet:
                QtWidgets.QMessageBox.critical(self, "Cannot open static model", f"{path}\n\n{e}")
            return False
        self._set_model(model, Path(path), dirty=False)
        self.model_dock.show()
        self.statusBar().showMessage(f"Static model {path}", 5000)
        return True

    def _on_close_model(self) -> None:
        if self._confirm_discard_model():
            self._set_model(None, None, dirty=False)

    def _auto_load_model(self) -> None:
        """Open the configured model of the recording's sensor, unless a fitting or unsaved
        model is already loaded."""
        if self.cfg.static_model is None or self.recording is None:
            return
        if self.model is not None and (self._model_active() or self.model_dirty):
            return
        path = model_path(self.cfg.static_model, self.recording.info.sn)
        if path.is_file():
            self.load_model(path, quiet=True)

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
            self.copy_ts_action.setEnabled(False)
            return
        self.save_action.setEnabled(True)
        ts = self.frame.wall_clock_ns
        self.timestamp_edit.setText(format_timestamptz(ts, self.cfg.timezone) if ts else "")
        self.copy_ts_action.setEnabled(bool(ts))
        self.position_label.setText(
            f"{self.index + 1} / {len(rec)}   "
            f"{rec.seconds(self.frame):6.2f} / {rec.duration_s:.2f} s"
        )
        self.status_label.setText(
            f"{rec.path.name}   |   {rec.info.prod_line} S/N {rec.info.sn}   |   "
            f"frame id {self.frame.frame_id}   |   {self.n_points:,} points"
            + self._window_status()
            + self._static_status()
        )

    def _static_status(self) -> str:
        if self.static_counts is None:
            return ""
        n, total = self.static_counts
        return f"   |   static {n / max(total, 1):.1%}"

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

    def _on_save(self) -> None:
        if self.frame is None or self.recording is None:
            return
        default = self.recording.path.with_name(
            f"{self.recording.path.stem}_frame{self.index + 1:04d}.ply"
        )
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save frame", str(default), "Point cloud (*.ply)"
        )
        if not path:
            return
        rng = self.frame.range
        static = self._static_mask(rng)
        mask = _apply_static_mode(rng > 0, static, self.static_mode_box.currentText()).reshape(-1)
        xyz = self.xyz_lut(rng).reshape(-1, 3)[mask]
        fields = {k: v.reshape(-1)[mask] for k, v in self.frame.fields.items()}
        write_ply(path, xyz, fields)
        self.statusBar().showMessage(f"Saved {path}", 5000)

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
        if not self._confirm_discard_model():
            event.ignore()
            return
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


def _apply_static_mode(mask: np.ndarray, static: np.ndarray | None, mode: str) -> np.ndarray:
    """``mask`` without the pixels the static display mode hides."""
    if static is None or mode == SHOW_ALL:
        return mask
    if mode == HIDE_STATIC:
        return mask & ~static
    if mode == ONLY_STATIC:
        return mask & static
    return mask  # highlight / dim: shown, but colored differently


def _ask_frame_range(parent: QtWidgets.QWidget, n: int, title: str) -> tuple[int, int] | None:
    """Ask for frames first..last (shown 1-based) of a recording with ``n`` frames;
    returns 0-based indices or None if cancelled."""
    dialog = QtWidgets.QDialog(parent, windowTitle=title)
    form = QtWidgets.QFormLayout(dialog)
    form.addRow(QtWidgets.QLabel("Use these frames of the current recording:"))
    first = QtWidgets.QSpinBox(minimum=1, maximum=n, value=1)
    last = QtWidgets.QSpinBox(minimum=1, maximum=n, value=n)
    first.valueChanged.connect(lambda v: last.setValue(max(v, last.value())))
    last.valueChanged.connect(lambda v: first.setValue(min(v, first.value())))
    form.addRow("First:", first)
    form.addRow("Last:", last)
    buttons = QtWidgets.QDialogButtonBox(
        QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
    )
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    form.addRow(buttons)
    if dialog.exec() != QtWidgets.QDialog.Accepted:
        return None
    return first.value() - 1, last.value() - 1


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
    return app.exec()
