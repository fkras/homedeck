"""Tests for the system-stats helpers.

These collect CPU, memory and temperature for the small window's STATS mode.
They are not wired into the keep-alive yet: a capture of the official Ulanzi
app shows its small-window payload carries more fields than strmdck builds, and
sending the short form disturbs rendering. See
guides/reverse-engineering-usb.md.
"""

import builtins

import psutil

from homedeck.homedeck import HomeDeck


def make_deck():
    return HomeDeck.__new__(HomeDeck)


# psutil.sensors_temperatures() is Linux-only and absent on macOS, so these
# tests add it with raising=False rather than requiring it to already exist.
class FakeReading:
    def __init__(self, current):
        self.current = current


class TestSystemStats:
    def test_reports_real_cpu_and_memory(self):
        stats = make_deck()._system_stats()

        assert 0 <= stats['cpu'] <= 100
        assert 0 < stats['mem'] <= 100
        assert all(isinstance(v, int) for v in stats.values())

    def test_survives_psutil_failing(self, monkeypatch):
        monkeypatch.setattr(psutil, 'cpu_percent', lambda *a, **k: 1 / 0)

        stats = make_deck()._system_stats()

        assert stats['cpu'] == 0        # falls back rather than crashing
        assert 'mem' in stats


class TestCpuTemperature:
    def test_prefers_a_known_cpu_sensor(self, monkeypatch):
        monkeypatch.setattr(psutil, 'sensors_temperatures',
                            lambda: {'acpitz': [FakeReading(30.0)],
                                     'cpu_thermal': [FakeReading(48.7)]},
                            raising=False)

        assert make_deck()._cpu_temperature() == 48

    def test_falls_back_to_any_sensor(self, monkeypatch):
        monkeypatch.setattr(psutil, 'sensors_temperatures',
                            lambda: {'something_else': [FakeReading(41.2)]},
                            raising=False)

        assert make_deck()._cpu_temperature() == 41

    def test_reads_the_thermal_zone_when_psutil_has_nothing(self, monkeypatch, tmp_path):
        """DietPi on the Orange Pi exposes no psutil sensors."""
        monkeypatch.setattr(psutil, 'sensors_temperatures', lambda: {}, raising=False)

        zone = tmp_path / 'temp'
        zone.write_text('52341\n')       # millidegrees

        real_open = builtins.open

        def fake_open(path, *args, **kwargs):
            if path == '/sys/class/thermal/thermal_zone0/temp':
                return real_open(zone, *args, **kwargs)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, 'open', fake_open)

        assert make_deck()._cpu_temperature() == 52

    def test_returns_none_when_unavailable(self, monkeypatch):
        monkeypatch.setattr(psutil, 'sensors_temperatures', lambda: {}, raising=False)

        real_open = builtins.open

        def fake_open(path, *args, **kwargs):
            if path == '/sys/class/thermal/thermal_zone0/temp':
                raise FileNotFoundError(path)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, 'open', fake_open)

        assert make_deck()._cpu_temperature() is None

    def test_temperature_lands_in_the_gpu_field(self, monkeypatch):
        monkeypatch.setattr(psutil, 'sensors_temperatures',
                            lambda: {'cpu_thermal': [FakeReading(55.0)]},
                            raising=False)

        assert make_deck()._system_stats()['gpu'] == 55
