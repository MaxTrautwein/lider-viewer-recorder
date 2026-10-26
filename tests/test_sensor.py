from recorder import sensor as sensor_api


class FakeHttp:
    timeouts = []

    @classmethod
    def create(cls, host, timeout_sec):
        assert type(timeout_sec) is int
        cls.timeouts.append(timeout_sec)
        return cls()

    def metadata(self):
        return "{}"

    def get_config_params(self, active):
        return "{}"


def test_http_timeout_is_whole_seconds(monkeypatch):
    monkeypatch.setattr(sensor_api.sensor, "SensorHttp", FakeHttp)
    sensor_api.fetch_metadata("h", 10.0)
    sensor_api.fetch_config("h", 2.5)
    assert FakeHttp.timeouts == [10, 3]
