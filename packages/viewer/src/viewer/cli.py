"""Command line entry point: ``viewer [path]`` and ``viewer config init``."""

import argparse
import logging
import sys

from . import config

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if argv[:1] == ["config"]:
        return _config_command(argv[1:])

    parser = argparse.ArgumentParser(
        prog="viewer",
        description="View Ouster lidar recordings (OSF files).",
        epilog=f"`viewer config init` creates {config.LOCAL} from the template.",
    )
    parser.add_argument("path", nargs="?", help="an .osf file or a folder of recordings")
    parser.add_argument(
        "--config", help=f"config file (default: first existing {config.LOCAL}, see README)"
    )
    args = parser.parse_args(argv)

    try:
        path = config.find(args.config)
        cfg = config.load(path)
    except config.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    log.info("config %s", path)

    from .window import run

    return run(args.path, cfg)


def _config_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="viewer config", description="Manage the config file.")
    parser.add_argument(
        "action", choices=["init"], help=f"init: create {config.LOCAL} from the template"
    )
    parser.add_argument("--user", action="store_true", help=f"write {config.user_path()} instead")
    parser.add_argument("--force", action="store_true", help="overwrite an existing file")
    args = parser.parse_args(argv)
    try:
        print(f"wrote {config.init(user=args.user, force=args.force)}")
    except config.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
