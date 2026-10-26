"""Thin helpers around the sensor's HTTP API.

Everything here is read-only except :func:`set_operating_mode`, which is only
called to switch the operating mode (``recording.start`` / ``standby_after``,
``sensor start|stop``).
"""

import json
import math
import socket

from ouster.sdk import core, sensor


def _http(host: str, timeout_s: float) -> sensor.SensorHttp:
    # the SDK binding only accepts whole seconds
    return sensor.SensorHttp.create(host, max(1, math.ceil(timeout_s)))


def fetch_metadata(host: str, timeout_s: float) -> str:
    """Return the sensor metadata JSON (calibration, data format, config)."""
    return _http(host, timeout_s).metadata()


def fetch_config(host: str, timeout_s: float) -> dict:
    http = _http(host, timeout_s)
    return json.loads(http.get_config_params(active=True))


def set_operating_mode(host: str, mode: str) -> None:
    """Set operating_mode to NORMAL or STANDBY without touching anything else."""
    cfg = core.SensorConfig()
    cfg.operating_mode = core.OperatingMode.from_string(mode)
    sensor.set_config(host, cfg, persist=False, udp_dest_auto=False)


def local_ip_towards(host: str) -> str:
    """The local IP the OS would use to reach ``host`` (no packets are sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((host, 1))
        return s.getsockname()[0]


def describe(host: str, timeout_s: float) -> str:
    """Human-readable summary, including whether this machine will receive data."""
    info = core.SensorInfo(fetch_metadata(host, timeout_s))
    cfg = fetch_config(host, timeout_s)
    fmt = info.format
    local_ip = local_ip_towards(host)
    lines = [
        f"Sensor        {info.prod_line}  S/N {info.sn}  FW {info.fw_rev}",
        f"Mode          {cfg.get('lidar_mode')}  ({fmt.pixels_per_column} x "
        f"{fmt.columns_per_frame}, {fmt.fps} Hz)",
        f"Profile       {fmt.udp_profile_lidar}",
        f"Operating     {cfg.get('operating_mode')}",
        f"UDP dest      {cfg.get('udp_dest')}:{cfg.get('udp_port_lidar')}",
        f"This host     {local_ip}",
    ]
    if cfg.get("operating_mode") != "NORMAL":
        lines.append("! Sensor is not streaming. Start it with `recorder sensor start`.")
    if cfg.get("udp_dest") != local_ip:
        lines.append(
            "! udp_dest is not this host: `record` will not receive data here. "
            "Run it on the udp_dest host."
        )
    return "\n".join(lines)
