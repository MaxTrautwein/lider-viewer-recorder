"""Round trip across both packages: record with the recorder, read back with the viewer."""

import numpy as np
from fake_sensor import room_frame, send_frames
from test_recorder import record_in_thread
from test_staticmodel import obstacle_mask

from staticmodel import StaticFilter, StaticModel
from viewer.frame import Frame
from viewer.recording import Recording


def test_record_and_read_back(receiver, tmp_path):
    path = tmp_path / "rec.osf"
    t, result = record_in_thread(receiver, 3, path)
    send_frames(receiver.info, receiver.port, 4)
    t.join(timeout=10)
    assert result["value"].frames == 3

    rec = Recording(path)
    assert len(rec) == 3
    assert [rec.frame(i).frame_id for i in range(3)] == [1, 2, 3]
    expected = Frame.from_lidar_frame(room_frame(rec.info, 2))
    np.testing.assert_array_equal(rec.frame(1).range, expected.range)
    np.testing.assert_array_equal(
        rec.frame(1).fields["REFLECTIVITY"], expected.fields["REFLECTIVITY"]
    )
    assert rec.info.sn == receiver.info.sn
    assert rec.seconds(rec.frame(0)) == 0
    assert rec.frame(0).host_timestamp_ns > 0
    rec.close()


def test_record_drops_static_points(make_receiver, tmp_path):
    """Recording with a model of the empty room keeps only the obstacle, and is smaller."""
    receiver = make_receiver()
    info = receiver.info
    model = StaticModel.new(info)
    for fid in range(3):
        model.update(room_frame(info, fid).field("RANGE"))

    sizes = {}
    for name, static in (("all", None), ("dynamic", StaticFilter(model))):
        path = tmp_path / f"{name}.osf"
        receiver = make_receiver()
        t, result = record_in_thread(receiver, 3, path, static)
        send_frames(info, receiver.port, 4, obstacle=True)
        t.join(timeout=10)
        sizes[name] = path.stat().st_size

    res = result["value"]
    assert res.frames == 3 and res.static_fraction > 0.9
    rec = Recording(path)
    rng = rec.frame(1).range
    obstacle = obstacle_mask(info)
    assert (rng[obstacle] > 0).all()
    assert not rng[~obstacle].any()
    for name in ("REFLECTIVITY", "SIGNAL", "NEAR_IR"):
        assert not rec.frame(1).fields[name][~obstacle].any()
    rec.close()
    assert sizes["dynamic"] < sizes["all"]
