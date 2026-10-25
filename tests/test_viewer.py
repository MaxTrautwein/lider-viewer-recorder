import numpy as np

from viewer.frame import Frame, azimuth_window_columns, format_timestamptz


def test_format_timestamptz():
    ns = 1_792_592_442_800_342_257  # 2026-10-21 14:20:42.800342257 UTC
    assert format_timestamptz(ns) == "2026-10-21 14:20:42.800342+00:00"
    assert format_timestamptz(0) == "1970-01-01 00:00:00.000000+00:00"


def test_azimuth_window_columns():
    def cols(start, end):
        return np.flatnonzero(azimuth_window_columns(start, end, 1024)).tolist()

    assert cols(0, 360_000) == list(range(1024))
    # column_window (1, 489) the firmware sent for this azimuth_window (OS-1, 1024x10)
    assert cols(188_000, 360_000) == list(range(1, 490))
    assert cols(0, 180_000) == [0] + list(range(512, 1024))
    assert cols(270_000, 90_000) == list(range(257)) + list(range(768, 1024))
    assert cols(90_000, 90_000) == [768]


def test_wall_clock_falls_back_to_host_time():
    ptp = Frame(frame_id=1, timestamp_ns=1_792_592_442_800_342_257, host_timestamp_ns=5)
    assert ptp.wall_clock_ns == ptp.timestamp_ns
    since_boot = Frame(
        frame_id=1, timestamp_ns=3_600 * 10**9, host_timestamp_ns=1_792_592_442 * 10**9
    )
    assert since_boot.wall_clock_ns == since_boot.host_timestamp_ns
