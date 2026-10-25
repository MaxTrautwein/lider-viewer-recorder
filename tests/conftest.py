import socket

import pytest
from fake_sensor import load_info

from recorder.receiver import UdpReceiver


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def make_receiver():
    """A new receiver on a free port (a receiver records only once: record() stops it)."""
    meta, _ = load_info()
    return lambda: UdpReceiver(meta, port=free_udp_port(), bind="127.0.0.1")


@pytest.fixture
def receiver(make_receiver):
    return make_receiver()
