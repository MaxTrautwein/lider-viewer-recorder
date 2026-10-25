"""YAML configuration: ``viewer-local.yaml``, created from the packaged template."""

import os
import types
import typing
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, tzinfo
from importlib import resources
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

NAME = "viewer"
TEMPLATE = f"{NAME}.yaml"
LOCAL = f"{NAME}-local.yaml"


class ConfigError(Exception):
    """The configuration file is missing or invalid."""


@dataclass(frozen=True)
class Config:
    timezone: tzinfo
    recordings_dir: Path | None
    static_model: str | None  # path pattern with {serial}


def template_text() -> str:
    return resources.files(__package__).joinpath(TEMPLATE).read_text(encoding="utf-8")


def user_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "lidar" / LOCAL


def search_paths() -> list[Path]:
    return [Path.cwd() / LOCAL, user_path()]


def find(explicit: str | Path | None = None) -> Path:
    """The config file to use: ``explicit`` if given, else the first existing search path."""
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        return path
    for path in search_paths():
        if path.is_file():
            return path
    searched = "".join(f"\n  {p}" for p in search_paths())
    raise ConfigError(
        f"no {LOCAL} found, searched:{searched}\n"
        f"Create one from the template with `{NAME} config init` (or `--user`) and edit it."
    )


def load(path: str | Path) -> Config:
    path = Path(path)
    return parse(path.read_text(encoding="utf-8"), str(path))


def parse(text: str, source: str = "<string>") -> Config:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"{source}: invalid YAML: {e}") from None
    try:
        cfg = _build(Config, data, "")
        _validate(cfg)
    except ConfigError as e:
        raise ConfigError(f"{source}: {e}") from None
    return cfg


def init(user: bool = False, force: bool = False) -> Path:
    """Write the template to the local (or user) config path and return that path."""
    path = user_path() if user else Path.cwd() / LOCAL
    if path.exists() and not force:
        raise ConfigError(f"{path} already exists (use --force to overwrite)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(template_text(), encoding="utf-8")
    return path


def _validate(cfg: Config) -> None:
    if cfg.static_model is not None:
        try:
            cfg.static_model.format(serial="0")
        except (KeyError, IndexError, ValueError) as e:
            raise ConfigError(
                f"static_model: invalid pattern {cfg.static_model!r} ({e!r}); "
                "placeholder is {serial}"
            ) from None


def _build(cls: type, data: object, where: str):
    if not isinstance(data, dict):
        raise ConfigError(f"{where or 'top level'}: expected a mapping, got {data!r}")
    names = [f.name for f in fields(cls)]
    if unknown := [k for k in data if k not in names]:
        raise ConfigError(f"unknown key {_key(where, unknown[0])}")
    if missing := [k for k in names if k not in data]:
        raise ConfigError(f"missing key {_key(where, missing[0])}")
    hints = typing.get_type_hints(cls)
    return cls(**{n: _convert(hints[n], data[n], _key(where, n)) for n in names})


def _convert(tp: type, value: object, key: str):
    if is_dataclass(tp):
        return _build(tp, value, key)
    allowed = typing.get_args(tp) if isinstance(tp, types.UnionType) else (tp,)
    if value is None and type(None) in allowed:
        return None
    for t in allowed:
        if t is tzinfo and isinstance(value, str):
            return _timezone(value, key)
        if t is Path and isinstance(value, str):
            return Path(value).expanduser()
        if type(value) is t:
            return value
    expected = " or ".join("null" if t is type(None) else t.__name__ for t in allowed)
    raise ConfigError(f"{key}: expected {expected}, got {value!r}")


def _timezone(name: str, key: str) -> tzinfo:
    if name.upper() == "UTC":
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"{key}: unknown time zone {name!r} (e.g. UTC, Europe/Berlin)") from None


def _key(where: str, name: str) -> str:
    return f"{where}.{name}" if where else name
