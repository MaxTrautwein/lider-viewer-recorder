import json

import numpy as np
import pytest
from fake_sensor import OBSTACLE, load_info, room_frame
from ouster.sdk import core

from staticmodel import StaticFilter, StaticModel, StaticParams, clear_pixels, model_path

PARAMS = StaticParams()


def obstacle_mask(info: core.SensorInfo) -> np.ndarray:
    """Staggered boolean mask of the obstacle pixels."""
    fmt = info.format
    block = np.zeros((fmt.pixels_per_column, fmt.columns_per_frame), np.uint8)
    block[OBSTACLE] = 1
    return core.destagger(info, block, inverse=True).astype(bool)


@pytest.fixture
def info():
    return load_info()[1]


@pytest.fixture
def room_model(info):
    model = StaticModel.new(info)
    for fid in range(5):
        model.update(room_frame(info, fid).field("RANGE"))
    return model


def test_room_is_static(info, room_model):
    rng = room_frame(info, 9).field("RANGE")
    assert room_model.static_mask(rng, PARAMS)[rng > 0].all()


def test_obstacle_is_dynamic(info, room_model):
    rng = room_frame(info, 9, obstacle=True).field("RANGE")
    static = room_model.static_mask(rng, PARAMS)
    obstacle = obstacle_mask(info)
    assert not static[obstacle].any()
    assert static[~obstacle & (rng > 0)].all()


def test_no_return_is_never_static(info, room_model):
    rng = np.zeros(room_model.shape, np.uint32)
    assert not room_model.static_mask(rng, PARAMS).any()


def test_tolerance(info):
    model = StaticModel.new(info)
    model.update(np.full(model.shape, 10_000, np.uint32))
    near = np.full(model.shape, 10_150, np.uint32)
    assert model.static_mask(near, StaticParams(abs_tolerance_m=0.2)).all()
    assert not model.static_mask(near, StaticParams(abs_tolerance_m=0.1)).any()
    # Relative tolerance wins at long range: 2 % of 10 m = 20 cm.
    assert model.static_mask(near, StaticParams(abs_tolerance_m=0.1, rel_tolerance=0.02)).all()


def test_min_fraction_and_modes(info):
    """A pixel that alternates between two ranges (door open / closed) keeps both as modes;
    a range seen only once in many frames does not count."""
    model = StaticModel.new(info)
    a, b, rare = (np.full(model.shape, mm, np.uint32) for mm in (5_000, 8_000, 2_000))
    for i in range(10):
        model.update(a if i % 2 else b)
    model.update(rare)
    assert model.frames == 11
    assert model.static_mask(a, PARAMS).all()
    assert model.static_mask(b, PARAMS).all()
    assert not model.static_mask(rare, PARAMS).any()
    assert not model.static_mask(a, StaticParams(min_fraction=0.6)).any()


def test_running_mean(info):
    model = StaticModel.new(info)
    for mm in (10_000, 10_100, 10_200):
        model.update(np.full(model.shape, mm, np.uint32))
    assert np.allclose(model.background_range(PARAMS), 10_100)


def test_background_range(info, room_model):
    rng = room_frame(info, 0).field("RANGE")
    np.testing.assert_allclose(room_model.background_range(PARAMS), rng, atol=1)
    assert not StaticModel.new(info).background_range(PARAMS).any()


def test_enhance_and_round_trip(info, room_model, tmp_path):
    room_model.add_source("a.osf", 5, 0, 4)
    path = tmp_path / "models" / "m.npz"
    room_model.save(path)
    assert not path.with_name("m.npz.part").exists()

    loaded = StaticModel.load(path)
    assert (loaded.serial, loaded.shape, loaded.frames) == (str(info.sn), (128, 1024), 5)
    np.testing.assert_array_equal(loaded.hits, room_model.hits)
    np.testing.assert_array_equal(loaded.center_mm, room_model.center_mm)

    loaded.update(room_frame(info, 7, obstacle=True).field("RANGE"))
    loaded.add_source("b.osf", 1, 3, 3)
    loaded.save(path)
    again = StaticModel.load(path)
    assert again.frames == 6
    assert [s["recording"] for s in again.sources] == ["a.osf", "b.osf"]


def test_load_rejects_other_files(tmp_path):
    path = tmp_path / "x.npz"
    np.savez(path, a=np.zeros(3))
    with pytest.raises(ValueError, match="not a static model"):
        StaticModel.load(path)
    np.savez(path, meta=np.array(json.dumps({"format": 99})))
    with pytest.raises(ValueError, match="unsupported format"):
        StaticModel.load(path)


def test_check_sensor(info):
    model = StaticModel.new(info)
    model.check(info)
    other = StaticModel(str(info.sn + 1), info.prod_line, model.shape)
    with pytest.raises(ValueError, match="S/N"):
        other.check(info)
    wide = StaticModel(str(info.sn), info.prod_line, (128, 2048))
    assert "lidar_mode" in wide.mismatch(info)
    with pytest.raises(ValueError, match="shape"):
        wide.update(np.zeros((128, 1024), np.uint32))


def test_clear_pixels(info, room_model):
    frame = room_frame(info, 1, obstacle=True)
    mask = room_model.frame_mask(frame, PARAMS)
    clear_pixels(frame, mask)
    for name in ("RANGE", "REFLECTIVITY", "SIGNAL", "NEAR_IR"):
        assert not frame.field(name)[mask].any()
        assert frame.field(name)[obstacle_mask(info)].all()


def test_model_path():
    assert model_path("models/{serial}_static.npz", 122) == model_path("models/122_static.npz", 0)


def test_filter_drop_no_return(info, room_model):
    frame = room_frame(info, 1, obstacle=True)
    frame.field("RANGE")[:, :10] = 0  # no return, but NEAR_IR / SIGNAL still set
    keep = StaticFilter(room_model, drop_no_return=False).discard_mask(frame)
    drop = StaticFilter(room_model).discard_mask(frame)
    assert not keep[:, :10].any() and drop[:, :10].all()
    assert (drop & ~keep).sum() == 128 * 10
