"""Sensor-independent frame representation and point cloud export."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo

import numpy as np
from ouster.sdk import core

# Channel fields the viewer knows how to display, in UI order.
FIELDS = ("RANGE", "REFLECTIVITY", "SIGNAL", "NEAR_IR")


@dataclass
class Frame:
    """One full sensor revolution.

    Field arrays are H x W and *staggered*, exactly as the sensor delivers them;
    use :func:`destagger` for image display. RANGE is in millimeters.
    """

    frame_id: int
    timestamp_ns: int
    host_timestamp_ns: int = 0  # when the host received the frame's first packet (Unix time)
    fields: dict[str, np.ndarray] = field(default_factory=dict)

    @property
    def range(self) -> np.ndarray:
        return self.fields["RANGE"]

    @property
    def wall_clock_ns(self) -> int:
        """Absolute frame time: the sensor timestamp if it is wall-clock time (PTP),
        otherwise (time since sensor boot) the host receive time."""
        if self.timestamp_ns >= _YEAR_2000_NS:
            return self.timestamp_ns
        return self.host_timestamp_ns

    @classmethod
    def from_lidar_frame(cls, lf: core.LidarFrame) -> "Frame":
        ts = lf.timestamp
        valid = ts[ts != 0]
        return cls(
            frame_id=int(lf.frame_id),
            timestamp_ns=int(valid[0]) if valid.size else 0,
            host_timestamp_ns=int(lf.get_first_valid_packet_timestamp()),
            fields={name: lf.field(name).copy() for name in FIELDS if lf.has_field(name)},
        )


_YEAR_2000_NS = 946_684_800 * 10**9


def format_timestamptz(ns: int, tz: tzinfo = UTC) -> str:
    """Unix time in ns as a Postgres ``timestamptz`` literal (µs precision, explicit offset),
    e.g. ``2026-10-21 14:20:42.800342+00:00``."""
    dt = datetime.fromtimestamp(ns // 10**9, tz).replace(microsecond=ns // 1000 % 10**6)
    return dt.isoformat(sep=" ", timespec="microseconds")


def destagger(info: core.SensorInfo, arr: np.ndarray) -> np.ndarray:
    return core.destagger(info, np.ascontiguousarray(arr))


def azimuth_window_columns(start: int, end: int, width: int) -> np.ndarray:
    """Boolean mask (width,) of the columns (measurement ids) the sensor sends for
    ``azimuth_window`` [start, end] in millidegrees; start > end wraps through 0.

    Applies to *staggered* arrays, whose column index is the measurement id. The encoder
    angle decreases with the measurement id: id 0 is at 0, id 1 just below 360000."""
    angle = (width - np.arange(width)) % width * 360_000 / width
    if start <= end:
        return (angle >= start) & (angle <= end)
    return (angle >= start) | (angle <= end)


def write_ply(path: str, xyz: np.ndarray, values: dict[str, np.ndarray]) -> None:
    """Write points (N x 3, meters) plus per-point scalar fields as binary PLY."""
    dtype = [("x", "<f4"), ("y", "<f4"), ("z", "<f4")] + [(k.lower(), "<f4") for k in values]
    data = np.empty(len(xyz), dtype=dtype)
    data["x"], data["y"], data["z"] = xyz.T
    for k, v in values.items():
        data[k.lower()] = v
    header = (
        "ply\nformat binary_little_endian 1.0\n"
        f"element vertex {len(xyz)}\n"
        + "".join(f"property float {name}\n" for name, _ in dtype)
        + "end_header\n"
    )
    with open(path, "wb") as f:
        f.write(header.encode("ascii"))
        f.write(data.tobytes())
