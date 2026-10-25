"""Synthetic lidar packets (a box-shaped room) for tests without hardware."""

import socket
import time
from pathlib import Path

import numpy as np
from ouster.sdk import core

METADATA_PATH = Path(__file__).parent / "data" / "os-0-128_metadata.json"


def load_info() -> tuple[str, core.SensorInfo]:
    text = METADATA_PATH.read_text()
    return text, core.SensorInfo(text)


# Destaggered pixel block covered by the optional obstacle (rows, columns).
OBSTACLE = (slice(40, 80), slice(100, 160))
OBSTACLE_RANGE_M = 3.0


def room_frame(info: core.SensorInfo, frame_id: int, obstacle: bool = False) -> core.LidarFrame:
    """A 20 x 14 x 4 m room around the sensor (mounted 1.5 m high), optionally with an
    obstacle 3 m away covering the :data:`OBSTACLE` pixels."""
    fmt = info.format
    h, w = fmt.pixels_per_column, fmt.columns_per_frame
    frame = core.LidarFrame(h, w, core.get_field_types(info), fmt.columns_per_packet)
    dirs = core.XYZLutFloat(info, use_extrinsics=False)(np.full((h, w), 1000, np.uint32))
    dirs /= np.linalg.norm(dirs, axis=2, keepdims=True)
    half = np.array([10.0, 7.0, 0.0])
    with np.errstate(divide="ignore"):
        tx = half[0] / np.abs(dirs[..., 0])
        ty = half[1] / np.abs(dirs[..., 1])
        tz = np.where(dirs[..., 2] > 0, 2.5, 1.5) / np.abs(dirs[..., 2])
    dist = np.minimum(np.minimum(tx, ty), tz)
    if obstacle:
        dist[OBSTACLE] = OBSTACLE_RANGE_M
    frame.field("RANGE")[:] = core.destagger(info, (dist * 1000).astype(np.uint32), inverse=True)
    stripes = ((np.arange(w) // 32) % 2 * 150 + 50).astype(np.uint8)
    frame.field("REFLECTIVITY")[:] = core.destagger(
        info, np.broadcast_to(stripes, (h, w)).copy(), inverse=True
    )
    frame.field("SIGNAL")[:] = 100
    frame.field("NEAR_IR")[:] = 200
    frame.frame_id = frame_id
    frame.measurement_id[:] = np.arange(w)
    frame.status[:] = 1
    frame.timestamp[:] = time.time_ns() + np.arange(w) * 100_000
    return frame


def packets(info: core.SensorInfo, frame_id: int, obstacle: bool = False) -> list[bytes]:
    pf = core.PacketFormat(info)
    pkts = core.frame_to_packets(room_frame(info, frame_id, obstacle), pf, info.init_id, info.sn)
    return [bytes(p.buf) for p in pkts]


def send_frames(
    info: core.SensorInfo, port: int, n_frames: int, start_id: int = 1, obstacle: bool = False
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for fid in range(start_id, start_id + n_frames):
            for p in packets(info, fid, obstacle):
                s.sendto(p, ("127.0.0.1", port))
                time.sleep(0.0002)
