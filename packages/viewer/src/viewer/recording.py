"""Random access to the frames of an OSF recording."""

from pathlib import Path

from ouster.sdk import core, open_source

from .frame import Frame


class Recording:
    """An indexed recording; frames are decoded on demand, one at a time."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._src = open_source(str(self.path), sensor_idx=0)
        if not self._src.is_indexed:
            self._src.close()
            raise ValueError(f"{self.path.name} has no frame index (not a finished OSF file?)")
        self.info: core.SensorInfo = self._src.sensor_info[0]
        self._n = len(self._src)
        self._t0 = self.frame(0).timestamp_ns if self._n else 0
        # Span of the recording by sensor timestamps, including the last frame itself.
        last = self.seconds(self.frame(self._n - 1)) if self._n else 0.0
        self.duration_s = last + (1.0 / self.fps if self._n else 0.0)

    def __len__(self) -> int:
        return self._n

    @property
    def fps(self) -> int:
        return self.info.format.fps or 10

    def frame(self, index: int) -> Frame:
        if not 0 <= index < self._n:
            raise IndexError(index)
        return Frame.from_lidar_frame(self._src[index][0])

    def seconds(self, frame: Frame) -> float:
        """Time of ``frame`` relative to the start of the recording."""
        return (frame.timestamp_ns - self._t0) / 1e9

    def close(self) -> None:
        self._src.close()
