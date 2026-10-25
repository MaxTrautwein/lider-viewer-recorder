"""Record a fixed number of frames from the sensor into an OSF file."""

import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
from ouster.sdk import core, osf

from staticmodel import StaticFilter, clear_pixels

from .receiver import UdpReceiver

log = logging.getLogger(__name__)

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|min)?\s*$")
_UNIT_S = {None: 1.0, "s": 1.0, "ms": 0.001, "m": 60.0, "min": 60.0}


def parse_duration(text: str, min_s: float, max_s: float) -> float:
    """Parse ``5``, ``5s``, ``500ms``, ``1m`` into seconds within [min_s, max_s]."""
    match = _DURATION_RE.match(text)
    if not match:
        raise ValueError(f"invalid duration {text!r} (examples: 5s, 500ms, 1m)")
    seconds = float(match.group(1)) * _UNIT_S[match.group(2)]
    if not min_s <= seconds <= max_s:
        raise ValueError(f"duration must be between {min_s:g} s and {max_s:g} s, got {seconds:g} s")
    return seconds


def frames_for_duration(seconds: float, fps: int) -> int:
    return max(1, math.ceil(round(seconds * fps, 6)))


def default_path(out_dir: str | Path, pattern: str, info: core.SensorInfo, n_frames: int) -> Path:
    """``out_dir`` / ``pattern`` with {serial}, {time} (local, strftime) and {frames} filled in."""
    return Path(out_dir) / pattern.format(serial=info.sn, time=datetime.now(), frames=n_frames)


@dataclass
class RecordResult:
    path: Path
    frames: int
    incomplete_frames: int
    seconds: float
    interrupted: bool
    static_fraction: float | None = None  # share of the points dropped as static

    @property
    def size_bytes(self) -> int:
        return self.path.stat().st_size


def record(
    receiver: UdpReceiver,
    n_frames: int,
    path: str | Path,
    no_data_timeout_s: float = 5.0,
    static: StaticFilter | None = None,
) -> RecordResult:
    """Write the next ``n_frames`` complete revolutions to ``path`` (OSF).

    The file is written as ``<path>.part`` and renamed when done, so a file with
    the final name is always complete. Ctrl+C stops early and keeps what was recorded.
    With ``static``, the points it discards (background of this sensor) are stored
    as "no return".
    """
    if n_frames < 1:
        raise ValueError("n_frames must be >= 1")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")

    writer = osf.Writer(str(part), receiver.info)
    written = incomplete = 0
    first_ts = last_ts = 0
    points = dropped = 0
    interrupted = False
    try:
        for lidar_frame in receiver.frames(no_data_timeout_s):
            complete = lidar_frame.complete()
            if written == 0 and not complete:
                continue  # recording started mid-revolution
            if static is not None:
                mask = static.discard_mask(lidar_frame)
                has_point = lidar_frame.field("RANGE") > 0
                points += int(np.count_nonzero(has_point))
                dropped += int(np.count_nonzero(mask & has_point))
                clear_pixels(lidar_frame, mask)
            writer.save(0, lidar_frame)
            ts = int(lidar_frame.timestamp[lidar_frame.get_first_valid_column()])
            first_ts = first_ts or ts
            last_ts = ts
            written += 1
            incomplete += not complete
            if written >= n_frames:
                break
    except KeyboardInterrupt:
        interrupted = True
        log.warning("interrupted, keeping %d recorded frames", written)
    finally:
        receiver.stop()
        writer.close()

    if written == 0:
        part.unlink(missing_ok=True)
        raise RuntimeError("no frames recorded")
    part.replace(path)
    period = 1.0 / (receiver.info.format.fps or 10)
    return RecordResult(
        path=path,
        frames=written,
        incomplete_frames=incomplete,
        seconds=(last_ts - first_ts) / 1e9 + period,
        interrupted=interrupted,
        static_fraction=dropped / max(points, 1) if static is not None else None,
    )
