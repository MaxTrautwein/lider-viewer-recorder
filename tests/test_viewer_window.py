"""Viewer window (offscreen Qt, OpenGL drawing stubbed out)."""

import os
from datetime import UTC

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
