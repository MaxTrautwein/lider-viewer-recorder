"""Viewer window (offscreen Qt, OpenGL drawing stubbed out)."""

import os
from datetime import UTC

import numpy as np
import pytest
from fake_sensor import load_info, room_frame
from ouster.sdk import osf

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pg_gl = pytest.importorskip("pyqtgraph.opengl")
from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

from viewer import window  # noqa: E402
from viewer.config import Config  # noqa: E402


@pytest.fixture
def app(monkeypatch, tmp_path):
    # Keep the remembered last folder out of the user's real settings.
    QtCore.QSettings.setPath(QtCore.QSettings.IniFormat, QtCore.QSettings.UserScope, str(tmp_path))
    # Offscreen platforms have no usable OpenGL; the tests check data, not pixels.
    monkeypatch.setattr(pg_gl.GLViewWidget, "initializeGL", lambda self: None)
    monkeypatch.setattr(pg_gl.GLViewWidget, "paintGL", lambda self, *a, **k: None)
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def recording(tmp_path):
    """4 frames of the empty room, then 2 with an obstacle."""
    meta, info = load_info()
    path = tmp_path / "room.osf"
    writer = osf.Writer(str(path), info)
    for fid in range(6):
        frame = room_frame(info, fid + 1, obstacle=fid >= 4)
        # The writer indexes frames by packet (host) time, which room_frame leaves at 0.
        frame.packet_timestamp[:] = frame.timestamp[0] + fid * 100_000_000
        writer.save(0, frame)
    writer.close()
    return path


def test_export_png(app, recording, tmp_path, monkeypatch):
    win = window.ViewerWindow(Config(UTC, None))
    win.open(recording)
    win.az_start_box.setValue(0)
    win.az_end_box.setValue(90_000)  # a quarter of the columns
    grid_while_grabbing = []

    def grab():
        grid_while_grabbing.append(win.grid.visible())
        return QtGui.QImage(64, 48, QtGui.QImage.Format_RGB32)

    monkeypatch.setattr(win.view3d, "grabFramebuffer", grab)
    monkeypatch.setattr(window, "_ask_export", lambda parent, grid: (True, True, False))
    name = tmp_path / "out.png"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", lambda *a, **k: (str(name), ""))
    win._on_export()

    _, info = load_info()
    fmt = info.format
    image = QtGui.QImage(str(tmp_path / "out_2d.png"))
    assert (image.width(), image.height()) == (fmt.columns_per_frame, fmt.pixels_per_column)
    alpha = [image.pixelColor(x, 0).alpha() for x in range(image.width())]
    assert 0 < alpha.count(255) < image.width() / 2  # transparent outside the window
    assert set(alpha) == {0, 255}
    assert QtGui.QImage(str(tmp_path / "out_3d.png")).width() == 64
    assert grid_while_grabbing == [False]  # "Include grid" off
    assert win.grid.visible()  # restored to the View toggle
    win.close()


def test_grid_toggle_is_remembered(app):
    win = window.ViewerWindow(Config(UTC, None))
    assert win.grid.visible()
    win.grid_action.setChecked(False)
    assert not win.grid.visible()
    win.close()

    win2 = window.ViewerWindow(Config(UTC, None))
    assert not win2.grid_action.isChecked() and not win2.grid.visible()
    win2.close()


def test_reopens_last_folder(app, recording):
    cfg = Config(UTC, None)
    win = window.ViewerWindow(cfg)
    win.open_folder(recording.parent)
    win.close()

    win2 = window.ViewerWindow(cfg)
    app.processEvents()  # the folder is opened from a zero-delay timer
    assert win2.folder == recording.parent.resolve()
    win2.close()


def test_azimuth_wraps(app):
    win = window.ViewerWindow(Config(UTC, None))
    start, end = win.az_start_box, win.az_end_box
    start.setValue(0)
    start.stepBy(-1)
    assert start.value() == 359_000
    start.setValue(500)
    start.stepBy(-1)
    assert start.value() == 359_500
    end.setValue(360_000)
    end.stepBy(1)
    assert end.value() == 1000
    end.setValue(359_000)
    end.stepBy(1)
    assert end.value() == 360_000
    win.close()


def test_levels_clip_and_transform():
    values = np.arange(101, dtype=np.float32)
    assert window._levels(values) == pytest.approx((1, 99))
    assert window._levels(values, 90) == pytest.approx((1, 90))
    np.testing.assert_allclose(window._transform(values, "linear"), values)
    np.testing.assert_allclose(window._transform(values, "sqrt"), np.sqrt(values))
    np.testing.assert_allclose(window._transform(values, "log"), np.log1p(values))
    assert window._transform(np.array([-5.0]), "log")[0] == 0
    lo, hi = window._levels(np.full(10, 0.5, np.float32))
    assert hi > lo  # constant input still gives a usable span


def test_reversed_colormap_leaves_cached_map_alone():
    original = window.pg.colormap.get("magma")
    before = original.map([0.0, 0.3, 1.0], mode="byte").copy()
    flipped = window._reversed(original)
    np.testing.assert_array_equal(flipped.map([1.0, 0.7, 0.0], mode="byte"), before)
    np.testing.assert_array_equal(
        window.pg.colormap.get("magma").map([0.0, 0.3, 1.0], mode="byte"), before
    )
    np.testing.assert_array_equal(
        window._reversed(flipped).map([0.0, 0.3, 1.0], mode="byte"), before
    )


def test_white_background_and_color_settings_remembered(app):
    win = window.ViewerWindow(Config(UTC, None))
    assert not win.white_action.isChecked()
    win.white_action.setChecked(True)
    assert win.view3d.opts["bgcolor"] == (1.0, 1.0, 1.0, 1.0)
    win.cmap_box.setCurrentText("viridis")
    win.reverse_box.setChecked(True)
    win.scale_box.setCurrentText("log")
    win.clip_box.setValue(95)
    win.close()

    win = window.ViewerWindow(Config(UTC, None))
    assert win.white_action.isChecked()
    assert win.view3d.opts["bgcolor"] == (1.0, 1.0, 1.0, 1.0)
    assert win.cmap_box.currentText() == "viridis"
    assert win.reverse_box.isChecked()
    assert win.scale_box.currentText() == "log"
    assert win.clip_box.value() == 95
    win.close()


def test_export_log_scale_is_brighter(app, recording, tmp_path, monkeypatch):
    monkeypatch.setattr(window, "_ask_export", lambda parent, grid: (True, False, False))
    win = window.ViewerWindow(Config(UTC, None))
    win.open(recording)
    win.field_box.setCurrentText("RANGE")  # continuous (the fake reflectivity is two-valued)
    win.cmap_box.setCurrentText("CET-L1")  # grayscale: brightness = value

    def export(scale: str) -> float:
        win.scale_box.setCurrentText(scale)
        name = tmp_path / f"{scale}.png"
        monkeypatch.setattr(
            QtWidgets.QFileDialog, "getSaveFileName", lambda *a, **k: (str(name), "")
        )
        win._on_export()
        image = QtGui.QImage(str(tmp_path / f"{scale}_2d.png"))
        return float(
            np.mean(
                [
                    QtGui.QColor(image.pixel(x, y)).value()
                    for x in range(0, image.width(), 4)
                    for y in range(image.height())
                ]
            )
        )

    assert export("log") > export("linear")
    win.close()
