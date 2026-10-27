from datetime import UTC
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from recorder import cli as recorder_cli
from recorder import config as recorder_config
from viewer import config as viewer_config

CONFIGS = [recorder_config, viewer_config]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Run in an empty directory with an empty user config dir."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


def recorder_data() -> dict:
    return yaml.safe_load(recorder_config.template_text())


def test_recorder_template_loads():
    cfg = recorder_config.parse(recorder_config.template_text())
    assert cfg.sensor.host == ""
    assert cfg.udp.port is None
    assert cfg.udp.no_data_timeout_s == 120.0
    assert cfg.recording.out_dir == Path("recordings")
    assert (cfg.recording.min_duration_s, cfg.recording.max_duration_s) == (1.0, 60.0)
    assert cfg.recording.start is True
    assert cfg.recording.standby_after is True


def test_viewer_template_loads():
    cfg = viewer_config.parse(viewer_config.template_text())
    assert cfg.timezone is UTC
    assert cfg.recordings_dir is None


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["udp"].pop("bind"), "missing key udp.bind"),
        (lambda d: d["sensor"].update(hots="x"), "unknown key sensor.hots"),
        (lambda d: d["udp"].update(port="7502"), "udp.port: expected int or null"),
        (lambda d: d["recording"].update(start=1), "recording.start: expected bool"),
        (lambda d: d["udp"].update(receive_buffer_bytes=True), "expected int"),
        (lambda d: d.update(sensor=None), "sensor: expected a mapping"),
        (lambda d: d["recording"].update(filename="{sn}.osf"), "recording.filename"),
        (lambda d: d["recording"].update(min_duration_s=90), "min_duration_s <= max"),
        (lambda d: d.update(static={"model": None}), "unknown key static"),  # removed key
    ],
)
def test_recorder_rejects(change, message):
    data = recorder_data()
    change(data)
    with pytest.raises(recorder_config.ConfigError, match=message):
        recorder_config.parse(yaml.safe_dump(data), "x.yaml")


def test_recorder_accepts_int_for_float():
    data = recorder_data()
    data["sensor"]["http_timeout_s"] = 3
    assert recorder_config.parse(yaml.safe_dump(data)).sensor.http_timeout_s == 3.0


def test_viewer_timezone():
    cfg = viewer_config.parse("timezone: Europe/Berlin\nrecordings_dir: ~/rec\n")
    assert cfg.timezone == ZoneInfo("Europe/Berlin")
    assert cfg.recordings_dir == Path("~/rec").expanduser()
    with pytest.raises(viewer_config.ConfigError, match="unknown time zone"):
        viewer_config.parse("timezone: Mars/Olympus\nrecordings_dir: null\n")
    with pytest.raises(viewer_config.ConfigError, match="unknown key static_model"):  # removed
        viewer_config.parse("timezone: UTC\nrecordings_dir: null\nstatic_model: null\n")


@pytest.mark.parametrize("cfg", CONFIGS)
def test_missing_file_lists_search_paths(cfg, tmp_path):
    with pytest.raises(cfg.ConfigError) as e:
        cfg.find()
    assert str(tmp_path / cfg.LOCAL) in str(e.value)
    assert str(tmp_path / "xdg" / "lidar" / cfg.LOCAL) in str(e.value)
    assert f"{cfg.NAME} config init" in str(e.value)
    with pytest.raises(cfg.ConfigError, match="not found"):
        cfg.find("nope.yaml")


@pytest.mark.parametrize("cfg", CONFIGS)
def test_search_order(cfg, tmp_path):
    user = cfg.init(user=True)
    assert user == tmp_path / "xdg" / "lidar" / cfg.LOCAL
    assert cfg.find() == user
    local = cfg.init()
    assert local == tmp_path / cfg.LOCAL
    assert cfg.find() == local
    assert cfg.find(user) == user


@pytest.mark.parametrize("cfg", CONFIGS)
def test_init(cfg):
    path = cfg.init()
    assert path.read_text() == cfg.template_text()
    with pytest.raises(cfg.ConfigError, match="already exists"):
        cfg.init()
    path.write_text("changed")
    cfg.init(force=True)
    assert path.read_text() == cfg.template_text()
    cfg.load(path)


def test_recorder_cli_without_config(capsys):
    assert recorder_cli.main(["sensor", "info"]) == 1
    assert "recorder config init" in capsys.readouterr().err


def test_recorder_cli_requires_host(capsys):
    assert recorder_cli.main(["config", "init"]) == 0
    with pytest.raises(SystemExit):
        recorder_cli.main(["record", "-n", "1"])
    assert "sensor.host" in capsys.readouterr().err
