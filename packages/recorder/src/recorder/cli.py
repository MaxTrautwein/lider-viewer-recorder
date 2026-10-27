"""Command line entry point: ``recorder <command> ...``."""

import argparse
import logging
import sys

from . import config

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="recorder", description="Record Ouster lidar data to OSF files (headless)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config", help=f"config file (default: first existing {config.LOCAL}, see README)"
    )
    common.add_argument("--sensor", help="sensor hostname or IP (default: sensor.host)")

    p = sub.add_parser(
        "record",
        parents=[common],
        help="record N frames or a duration to an OSF file (run on the udp_dest host)",
    )
    amount = p.add_mutually_exclusive_group(required=True)
    amount.add_argument("-n", "--frames", type=int, help="number of frames (>= 1)")
    amount.add_argument(
        "-d", "--duration", help="recording length, e.g. 5, 5s, 1500ms, 1m (limits: config)"
    )
    out = p.add_mutually_exclusive_group()
    out.add_argument("-o", "--output", help="output file (.osf)")
    out.add_argument(
        "--out-dir", help="directory for auto-named files (default: recording.out_dir)"
    )
    p.add_argument("--port", type=int, help="local UDP port for lidar data (default: udp.port)")
    p.add_argument(
        "--start",
        action=argparse.BooleanOptionalAction,
        help="switch the sensor to NORMAL if it is in STANDBY (default: recording.start)",
    )
    p.add_argument(
        "--standby-after",
        action=argparse.BooleanOptionalAction,
        help="put the sensor back to STANDBY afterwards if --start switched it on "
        "(default: recording.standby_after)",
    )

    p = sub.add_parser("sensor", parents=[common], help="show sensor info or start/stop it")
    p.add_argument("action", choices=["info", "start", "stop"])

    p = sub.add_parser("config", help="manage the config file")
    p.add_argument(
        "action", choices=["init"], help=f"init: create {config.LOCAL} from the template"
    )
    p.add_argument("--user", action="store_true", help=f"write {config.user_path()} instead")
    p.add_argument("--force", action="store_true", help="overwrite an existing file")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        if args.command == "config":
            print(f"wrote {config.init(user=args.user, force=args.force)}")
            return 0
        path = config.find(args.config)
        cfg = config.load(path)
    except config.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    log.info("config %s", path)

    host = args.sensor or cfg.sensor.host
    if not host:
        parser.error(f"no sensor: set sensor.host in {path} or pass --sensor")

    from . import sensor as sensor_api

    timeout = cfg.sensor.http_timeout_s
    if args.command == "sensor":
        if args.action != "info":
            sensor_api.set_operating_mode(host, "NORMAL" if args.action == "start" else "STANDBY")
        print(sensor_api.describe(host, timeout))
        return 0

    return _record(args, cfg, host, parser, sensor_api)


def _record(
    args: argparse.Namespace,
    cfg: config.Config,
    host: str,
    parser: argparse.ArgumentParser,
    sensor_api,
) -> int:
    from .receiver import NoDataError, UdpReceiver
    from .record import default_path, frames_for_duration, parse_duration, record

    rec_cfg = cfg.recording
    if args.frames is not None and args.frames < 1:
        parser.error("--frames must be >= 1")
    try:
        seconds = (
            parse_duration(args.duration, rec_cfg.min_duration_s, rec_cfg.max_duration_s)
            if args.duration
            else None
        )
    except ValueError as e:
        parser.error(str(e))
    start = rec_cfg.start if args.start is None else args.start
    standby_after = rec_cfg.standby_after if args.standby_after is None else args.standby_after
    timeout = cfg.sensor.http_timeout_s

    started = False
    if start and sensor_api.fetch_config(host, timeout).get("operating_mode") != "NORMAL":
        log.info("switching %s to NORMAL operating mode", host)
        sensor_api.set_operating_mode(host, "NORMAL")
        started = True

    receiver = UdpReceiver(
        sensor_api.fetch_metadata(host, timeout),
        port=args.port or cfg.udp.port,
        bind=cfg.udp.bind,
        receive_buffer_bytes=cfg.udp.receive_buffer_bytes,
    )
    fps = receiver.info.format.fps or 10
    n_frames = args.frames if seconds is None else frames_for_duration(seconds, fps)
    path = args.output or default_path(
        args.out_dir or rec_cfg.out_dir, rec_cfg.filename, receiver.info, n_frames
    )
    log.info(
        "recording %d frames (%.1f s at %d Hz) from %s on UDP port %d -> %s",
        n_frames, n_frames / fps, fps, host, receiver.port, path,
    )  # fmt: skip

    try:
        result = record(receiver, n_frames, path, cfg.udp.no_data_timeout_s)
    except NoDataError as e:
        print(f"error: {e}\n\n{sensor_api.describe(host, timeout)}", file=sys.stderr)
        return 2
    finally:
        if started and standby_after:
            log.info("switching %s back to STANDBY", host)
            sensor_api.set_operating_mode(host, "STANDBY")

    print(
        f"{result.path}\n"
        f"  frames: {result.frames}"
        + (f" ({result.incomplete_frames} incomplete)" if result.incomplete_frames else "")
        + f"\n  duration: {result.seconds:.2f} s\n"
        f"  size: {result.size_bytes / 1e6:.1f} MB"
        + ("\n  (interrupted)" if result.interrupted else "")
    )
    return 130 if result.interrupted else 0


if __name__ == "__main__":
    sys.exit(main())
