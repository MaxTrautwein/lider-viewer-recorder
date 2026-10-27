import socket
import threading
import time
from pathlib import Path

import pytest
from fake_sensor import load_info, packets, send_frames
from ouster.sdk import open_source

from recorder.receiver import NoDataError
from recorder.record import default_path, frames_for_duration, parse_duration, record


def record_in_thread(receiver, n_frames, path):
    result = {}

    def run():
        try:
            result["value"] = record(receiver, n_frames, path)
        except Exception as e:
            result["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(0.2)  # let the socket bind
    return t, result


def frame_ids(path) -> list[int]:
    src = open_source(str(path), sensor_idx=0)
    try:
        return [int(src[i][0].frame_id) for i in range(len(src))]
    finally:
        src.close()


def test_record(receiver, tmp_path):
    path = tmp_path / "rec.osf"
    t, result = record_in_thread(receiver, 3, path)
    send_frames(receiver.info, receiver.port, 4)
    t.join(timeout=10)

    res = result["value"]
    assert res.frames == 3 and res.incomplete_frames == 0 and not res.interrupted
    assert path.exists() and not path.with_name("rec.osf.part").exists()
    assert frame_ids(path) == [1, 2, 3]


def test_partial_first_frame_is_dropped(receiver, tmp_path):
    path = tmp_path / "rec.osf"
    t, result = record_in_thread(receiver, 2, path)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for p in packets(receiver.info, 1)[32:]:  # second half of a revolution
            s.sendto(p, ("127.0.0.1", receiver.port))
    send_frames(receiver.info, receiver.port, 3, start_id=2)
    t.join(timeout=10)

    assert result["value"].frames == 2
    assert frame_ids(path) == [2, 3]


def test_no_data_raises(receiver):
    with pytest.raises(NoDataError):
        next(receiver.frames(no_data_timeout_s=0.6))


@pytest.mark.parametrize(
    ("text", "seconds"), [("5", 5), ("5s", 5), ("1500ms", 1.5), ("1m", 60), ("1min", 60)]
)
def test_parse_duration(text, seconds):
    assert parse_duration(text, 1, 60) == seconds


@pytest.mark.parametrize("text", ["0", "500ms", "61s", "2m", "abc", "-1s"])
def test_parse_duration_rejects(text):
    with pytest.raises(ValueError):
        parse_duration(text, 1, 60)


def test_parse_duration_uses_limits():
    assert parse_duration("2m", 1, 300) == 120
    with pytest.raises(ValueError):
        parse_duration("5s", 10, 60)


def test_frames_for_duration():
    assert frames_for_duration(1, 10) == 10
    assert frames_for_duration(1.5, 10) == 15
    assert frames_for_duration(60, 20) == 1200
    assert frames_for_duration(1.01, 10) == 11


def test_default_path():
    _, info = load_info()
    path = default_path("out", "{serial}_{time:%Y}_{frames}f.osf", info, 7)
    assert path.parent == Path("out")
    assert path.name == f"{info.sn}_{time.strftime('%Y')}_7f.osf"
