from datetime import datetime

import pytest

from homedeck.dataclasses import ScheduleConfig, SleepConfig
from homedeck.utils import current_minutes_of_day, parse_time_of_day


def minutes(hours, mins=0):
    return hours * 60 + mins


class TestParseTimeOfDay:
    @pytest.mark.parametrize('value,expected', [
        ('00:00', 0),
        ('06:00', minutes(6)),
        ('22:00', minutes(22)),
        ('23:59', minutes(23, 59)),
        ('24:00', minutes(24)),
        ('6:30', minutes(6, 30)),
        (' 22:00 ', minutes(22)),
    ])
    def test_valid(self, value, expected):
        assert parse_time_of_day(value) == expected

    @pytest.mark.parametrize('value', ['25:00', '22:60', 'nonsense', '2200', '', '24:30'])
    def test_invalid_falls_back_to_midnight(self, value):
        assert parse_time_of_day(value) == 0

    def test_none(self):
        assert parse_time_of_day(None) == 0


class TestYamlTimeQuoting:
    """Times must be quoted in YAML.

    YAML 1.1 reads an unquoted 22:00 as the sexagesimal integer 1320, while
    06:00 stays a string. The schema rejects the integer form, which is what
    keeps a mis-quoted config from silently dimming at the wrong hour — so any
    tool that generates this YAML has to quote the values.
    """

    def test_unquoted_time_becomes_an_int(self):
        import yaml

        assert yaml.safe_load('to: 22:00')['to'] == 1320
        assert yaml.safe_load("to: '22:00'")['to'] == '22:00'

    def test_int_minutes_are_still_parsed_sanely(self):
        # 1320 happens to be exactly 22:00 in minutes, so if one ever reaches
        # the parser it is interpreted consistently rather than as garbage.
        assert parse_time_of_day(1320) == minutes(22)
        assert parse_time_of_day(360) == minutes(6)

    def test_int_is_clamped_to_a_day(self):
        assert parse_time_of_day(99999) == minutes(24)
        assert parse_time_of_day(-5) == 0


class TestScheduleWindow:
    def test_same_day_window(self):
        entry = ScheduleConfig(from_='09:00', to='17:00')

        assert not entry.contains(minutes(8, 59))
        assert entry.contains(minutes(9))       # inclusive start
        assert entry.contains(minutes(12))
        assert not entry.contains(minutes(17))  # exclusive end
        assert not entry.contains(minutes(20))

    def test_window_wrapping_midnight(self):
        """The wall-mount case: dim from 22:00 through 06:00."""
        entry = ScheduleConfig(from_='22:00', to='06:00')

        # Evening, inside
        assert entry.contains(minutes(22))
        assert entry.contains(minutes(23, 30))
        # Across midnight, still inside
        assert entry.contains(minutes(0))
        assert entry.contains(minutes(3))
        assert entry.contains(minutes(5, 59))
        # Morning, outside
        assert not entry.contains(minutes(6))
        assert not entry.contains(minutes(12))
        assert not entry.contains(minutes(21, 59))

    def test_zero_length_window_never_matches(self):
        entry = ScheduleConfig(from_='12:00', to='12:00')
        for hour in range(24):
            assert not entry.contains(minutes(hour))

    def test_full_day_window(self):
        entry = ScheduleConfig(from_='00:00', to='24:00')
        for hour in range(24):
            assert entry.contains(minutes(hour))


class TestSleepConfigSchedule:
    def test_maps_from_keyword(self):
        """'from' is a Python keyword, so YAML's key is remapped to 'from_'."""
        config = SleepConfig(
            dim_brightness=10,
            dim_timeout=30,
            sleep_timeout=0,
            schedule=[{'from': '22:00', 'to': '06:00', 'brightness': 5}],
        )

        assert len(config.schedule) == 1
        entry = config.schedule[0]
        assert entry.from_minutes == minutes(22)
        assert entry.to_minutes == minutes(6)
        assert entry.brightness == 5

    def test_active_schedule_selects_matching_window(self):
        config = SleepConfig(
            dim_brightness=10,
            dim_timeout=30,
            sleep_timeout=0,
            schedule=[{'from': '22:00', 'to': '06:00', 'brightness': 5}],
        )

        assert config.active_schedule(minutes(23)).brightness == 5
        assert config.active_schedule(minutes(2)).brightness == 5
        assert config.active_schedule(minutes(12)) is None

    def test_first_matching_window_wins(self):
        config = SleepConfig(
            dim_brightness=10,
            dim_timeout=30,
            sleep_timeout=0,
            schedule=[
                {'from': '22:00', 'to': '06:00', 'brightness': 5},
                {'from': '00:00', 'to': '24:00', 'brightness': 90},
            ],
        )

        # Overlapping at 23:00 -> the earlier entry wins
        assert config.active_schedule(minutes(23)).brightness == 5
        # Only the catch-all matches at noon
        assert config.active_schedule(minutes(12)).brightness == 90

    def test_empty_schedule(self):
        config = SleepConfig(dim_brightness=10, dim_timeout=30, sleep_timeout=0)
        assert config.schedule == []
        assert config.active_schedule(minutes(23)) is None

    def test_brightness_is_optional(self):
        config = SleepConfig(
            dim_brightness=10,
            dim_timeout=30,
            sleep_timeout=0,
            schedule=[{'from': '22:00', 'to': '06:00'}],
        )

        assert config.active_schedule(minutes(23)).brightness is None


class TestCurrentMinutesOfDay:
    def test_uses_supplied_datetime(self):
        assert current_minutes_of_day(datetime(2026, 1, 1, 22, 30)) == minutes(22, 30)
        assert current_minutes_of_day(datetime(2026, 1, 1, 0, 0)) == 0

    def test_defaults_to_now(self):
        assert 0 <= current_minutes_of_day() < minutes(24)
