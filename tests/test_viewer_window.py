"""Static model workflow in the viewer window (offscreen Qt, OpenGL drawing stubbed out)."""

import os
from datetime import UTC

import pytest
from fake_sensor import load_info, room_frame
from ouster.sdk import osf

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pg_gl = pytest.importorskip("pyqtgraph.opengl")
from PySide6 import QtWidgets  # noqa: E402

from viewer import window  # noqa: E402
from viewer.config import Config  # noqa: E402


@pytest.fixture
def app(monkeypatch):
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


def test_static_model_workflow(app, recording, tmp_path, monkeypatch):
    cfg = Config(UTC, None, str(tmp_path / "models" / "{serial}.npz"))
    win = window.ViewerWindow(cfg)
    win.open(recording)
    assert win.model is None and not win.add_model_action.isEnabled()

    monkeypatch.setattr(window, "_ask_frame_range", lambda parent, n, title: (0, 3))
    win._on_build_model(new=True)
    assert win.model.frames == 4 and win.model_dirty

    win.seek(5)  # obstacle frame
    counts = {}
    for mode in window.STATIC_MODES:
        win.static_mode_box.setCurrentText(mode)
        counts[mode] = win.n_points
    total = counts[window.SHOW_ALL]
    assert counts[window.HIDE_STATIC] + counts[window.ONLY_STATIC] == total
    assert 0 < counts[window.HIDE_STATIC] < total / 10  # only the obstacle is left
    assert counts[window.HIGHLIGHT_STATIC] == counts[window.DIM_STATIC] == total
    win.show_model_box.setChecked(True)
    assert win.model_scatter.visible()

    monkeypatch.setattr(window, "_ask_frame_range", lambda parent, n, title: (4, 5))
    win._on_build_model(new=False)
    assert win.model.frames == 6 and len(win.model.sources) == 2

    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", lambda *a, **k: (str(a[2]), ""))
    assert win._on_save_model() and not win.model_dirty
    _, info = load_info()
    assert win.model_path == tmp_path / "models" / f"{info.sn}.npz"
    win.close()

    win2 = window.ViewerWindow(cfg)
    win2.open(recording)  # auto-loads the sensor's model
    assert win2.model_path == win.model_path and win2.model.frames == 6
    win2.close()
