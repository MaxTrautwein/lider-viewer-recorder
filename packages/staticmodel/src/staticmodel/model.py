"""Per-pixel background model of a fixed sensor and the static/dynamic decision.

For every pixel of the *staggered* range image (H x W, as the sensor delivers it) the model
keeps up to ``MODES`` typical ranges ("modes", e.g. a door open and closed) with the number of
frames each was seen in. A point is static when its range is close to a mode that was seen in
at least ``min_fraction`` of all frames fed into the model.
"""

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from ouster.sdk import core

MODES = 3
FORMAT_VERSION = 1


@dataclass(frozen=True)
class StaticParams:
    """When a point counts as static."""

    abs_tolerance_m: float = 0.2  # max distance from a mode ...
    rel_tolerance: float = 0.01  # ... or this fraction of the range, whichever is larger
    min_fraction: float = 0.3  # a mode must be seen in at least this fraction of frames


DEFAULT_PARAMS = StaticParams()


def model_path(pattern: str | Path, serial: int | str) -> Path:
    """``pattern`` with ``{serial}`` filled in (one model file per sensor)."""
    return Path(str(pattern).format(serial=serial)).expanduser()


class StaticModel:
    def __init__(
        self,
        serial: str,
        prod_line: str,
        shape: tuple[int, int],
        build: StaticParams = DEFAULT_PARAMS,
    ) -> None:
        self.serial = serial
        self.prod_line = prod_line
        self.shape = shape
        self.build = build  # tolerances used to assign ranges to modes while building
        self.center_mm = np.zeros((MODES, *shape), np.float32)
        self.hits = np.zeros((MODES, *shape), np.uint32)
        self.frames = 0
        self.sources: list[dict] = []
        self.created = self.updated = _now()

    @classmethod
    def new(cls, info: core.SensorInfo, build: StaticParams = DEFAULT_PARAMS) -> "StaticModel":
        fmt = info.format
        return cls(
            str(info.sn), info.prod_line, (fmt.pixels_per_column, fmt.columns_per_frame), build
        )

    # ---- sensor compatibility ------------------------------------------------

    def mismatch(self, info: core.SensorInfo) -> str | None:
        """Why this model cannot be used with ``info``'s data, or None if it can."""
        if str(info.sn) != self.serial:
            return f"model is for sensor S/N {self.serial}, data is from S/N {info.sn}"
        shape = (info.format.pixels_per_column, info.format.columns_per_frame)
        if shape != self.shape:
            return (
                f"model is for {self.shape[0]} x {self.shape[1]} frames, "
                f"data has {shape[0]} x {shape[1]} (other lidar_mode?)"
            )
        return None

    def check(self, info: core.SensorInfo) -> None:
        if reason := self.mismatch(info):
            raise ValueError(reason)

    # ---- building --------------------------------------------------------------

    def update(self, range_mm: np.ndarray) -> None:
        """Feed one frame's (staggered) RANGE in millimeters into the model."""
        r = self._ranges(range_mm)
        tol = np.maximum(self.build.abs_tolerance_m * 1000, self.build.rel_tolerance * r)
        dist = np.abs(self.center_mm - r)
        dist[self.hits == 0] = np.inf
        nearest, nearest_dist = _argmin_modes(dist)
        matched = (r > 0) & (nearest_dist <= tol)
        # A range that matches no mode replaces the least seen mode.
        slot = np.where(matched, nearest, _argmin_modes(self.hits)[0])
        for k in range(MODES):
            sel = (r > 0) & (slot == k)
            hits = np.where(matched, self.hits[k] + 1, 1)
            center = self.center_mm[k]
            new_center = np.where(matched, center + (r - center) / hits, r)
            self.center_mm[k] = np.where(sel, new_center, center)
            self.hits[k] = np.where(sel, hits, self.hits[k])
        self.frames += 1
        self.updated = _now()

    def add_source(self, name: str, frames: int, first: int, last: int) -> None:
        """Note which recording (frame indices first..last) the last ``frames`` came from."""
        self.sources.append(
            {"recording": name, "frames": frames, "first": first, "last": last, "added": _now()}
        )

    # ---- classification -----------------------------------------------------------

    def static_mask(self, range_mm: np.ndarray, params: StaticParams) -> np.ndarray:
        """Boolean H x W: pixels whose point matches the background (range 0: False)."""
        r = self._ranges(range_mm)
        tol = np.maximum(params.abs_tolerance_m * 1000, params.rel_tolerance * r)
        close = np.abs(self.center_mm - r) <= tol
        return (r > 0) & (close & self._qualifies(params)).any(axis=0)

    def frame_mask(self, frame: core.LidarFrame, params: StaticParams) -> np.ndarray:
        """Pixels of a LidarFrame that can be dropped: the first return is static and the
        second return (dual return profiles) is missing or static too."""
        mask = self.static_mask(frame.field("RANGE"), params)
        if frame.has_field("RANGE2"):
            r2 = frame.field("RANGE2")
            mask &= (r2 == 0) | self.static_mask(r2, params)
        return mask

    def background_range(self, params: StaticParams) -> np.ndarray:
        """Staggered H x W uint32 range of each pixel's most seen qualifying mode (0: none)."""
        hits = np.where(self._qualifies(params), self.hits, 0)
        best = hits.argmax(axis=0)
        center = np.take_along_axis(self.center_mm, best[None], 0)[0]
        return np.where(hits.max(axis=0) > 0, np.rint(center), 0).astype(np.uint32)

    def _qualifies(self, params: StaticParams) -> np.ndarray:
        return (self.hits > 0) & (self.hits >= params.min_fraction * self.frames)

    def _ranges(self, range_mm: np.ndarray) -> np.ndarray:
        if range_mm.shape != self.shape:
            raise ValueError(f"frame shape {range_mm.shape} does not match model {self.shape}")
        return range_mm.astype(np.float32)

    # ---- file ---------------------------------------------------------------------

    def save(self, path: str | Path) -> None:
        """Write as compressed ``.npz`` (via a temporary file, so a crash keeps the old one)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "format": FORMAT_VERSION,
            "serial": self.serial,
            "prod_line": self.prod_line,
            "shape": list(self.shape),
            "build": asdict(self.build),
            "frames": self.frames,
            "sources": self.sources,
            "created": self.created,
            "updated": self.updated,
        }
        tmp = path.with_name(path.name + ".part")
        with open(tmp, "wb") as f:
            np.savez_compressed(
                f, center_mm=self.center_mm, hits=self.hits, meta=np.array(json.dumps(meta))
            )
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str | Path) -> "StaticModel":
        with np.load(path, allow_pickle=False) as data:
            try:
                meta = json.loads(str(data["meta"]))
                if meta.get("format") != FORMAT_VERSION:
                    raise ValueError(f"unsupported format {meta.get('format')!r}")
                model = cls(
                    meta["serial"],
                    meta["prod_line"],
                    tuple(meta["shape"]),
                    StaticParams(**meta["build"]),
                )
                model.center_mm = data["center_mm"].astype(np.float32)
                model.hits = data["hits"].astype(np.uint32)
                model.frames = int(meta["frames"])
                model.sources = list(meta["sources"])
                model.created, model.updated = meta["created"], meta["updated"]
            except KeyError as e:
                raise ValueError(f"{path}: not a static model (missing {e})") from None
        expected = (MODES, *model.shape)
        if model.center_mm.shape != expected or model.hits.shape != expected:
            raise ValueError(f"{path}: array shapes do not match the model")
        return model


@dataclass(frozen=True)
class StaticFilter:
    """What a recorder drops: static points and, with ``drop_no_return``, the near-IR /
    signal values of pixels without a return (half of a typical frame's data)."""

    model: StaticModel
    params: StaticParams = DEFAULT_PARAMS
    drop_no_return: bool = True

    def discard_mask(self, frame: core.LidarFrame) -> np.ndarray:
        mask = self.model.frame_mask(frame, self.params)
        if self.drop_no_return:
            no_return = frame.field("RANGE") == 0
            if frame.has_field("RANGE2"):
                no_return &= frame.field("RANGE2") == 0
            mask |= no_return
        return mask


def clear_pixels(frame: core.LidarFrame, mask: np.ndarray) -> None:
    """Set every pixel field of ``frame`` to 0 ("no return") where ``mask`` is True."""
    for name in frame.fields:
        if frame.field_class(name) == core.FieldClass.PIXEL_FIELD:
            frame.field(name)[mask] = 0


def _argmin_modes(a: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``a.argmin(axis=0)`` and ``a.min(axis=0)``; much faster than numpy's argmin over the
    short, non-contiguous mode axis."""
    best = np.zeros(a.shape[1:], np.intp)
    value = a[0]
    for k in range(1, len(a)):
        better = a[k] < value
        best[better] = k
        value = np.where(better, a[k], value)
    return best, value


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
