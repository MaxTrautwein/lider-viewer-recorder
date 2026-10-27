"""Round trip across both packages: record with the recorder, read back with the viewer."""

import numpy as np
from fake_sensor import room_frame, send_frames
from test_recorder import record_in_thread

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
