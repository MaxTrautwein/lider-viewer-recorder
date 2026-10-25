"""Receive lidar packets from a sensor over UDP and batch them into frames."""

import socket
import threading
import time
from collections.abc import Iterator

import numpy as np
from ouster.sdk import core

RECV_TIMEOUT_S = 0.5


class NoDataError(RuntimeError):
    """No lidar packets arrived within the timeout."""


class UdpReceiver:
    """Receives the sensor's lidar packets and yields complete revolutions.

    The sensor's configuration is never changed: packets are received on
    whatever port the sensor already sends to (its ``udp_dest`` must be this host).
    """

    def __init__(
        self,
        metadata_json: str,
        port: int | None = None,
        bind: str = "0.0.0.0",
        receive_buffer_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        self.metadata_json = metadata_json
        self.info = core.SensorInfo(metadata_json)
        self.port = port or self.info.config.udp_port_lidar or 7502
        self.bind = bind
        self.receive_buffer_bytes = receive_buffer_bytes
        self.packets_received = 0
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def frames(self, no_data_timeout_s: float = 5.0) -> Iterator[core.LidarFrame]:
        """Yield frames as they complete.

        The same LidarFrame object is reused for every frame, so consumers must
        be done with it (or copy it) before requesting the next one.
        Raises :class:`NoDataError` if no lidar packet arrives for ``no_data_timeout_s``.
        """
        pf = core.PacketFormat(self.info)
        size = pf.lidar_packet_size
        fmt = self.info.format
        lidar_frame = core.LidarFrame(
            fmt.pixels_per_column,
            fmt.columns_per_frame,
            core.get_field_types(self.info),
            fmt.columns_per_packet,
        )
        batcher = core.FrameBatcher(self.info)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.receive_buffer_bytes)
        sock.bind((self.bind, self.port))
        sock.settimeout(RECV_TIMEOUT_S)
        buf = bytearray(65536)
        last_packet = time.monotonic()
        try:
            while not self._stop.is_set():
                try:
                    n = sock.recv_into(buf)
                except TimeoutError:
                    if time.monotonic() - last_packet > no_data_timeout_s:
                        raise NoDataError(
                            f"no lidar packets on UDP port {self.port} "
                            f"for {no_data_timeout_s:.0f} s"
                        ) from None
                    continue
                if n != size:
                    continue  # not a lidar packet for this data format
                last_packet = time.monotonic()
                packet = core.LidarPacket(pf)
                packet.buf[:] = np.frombuffer(buf, np.uint8, n)
                packet.host_timestamp = time.time_ns()
                self.packets_received += 1
                if batcher.batch(packet, lidar_frame):
                    yield lidar_frame
        finally:
            sock.close()
