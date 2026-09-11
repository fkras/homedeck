"""Tests for the brightness state machine (wake / dim / sleep + night schedule)."""

import time

from homedeck.dataclasses import SleepConfig
from homedeck.enums import SleepStatus
from homedeck.homedeck import HomeDeck


class FakeDevice:
    def __init__(self):
        self.brightness_calls = []

    def set_brightness(self, value):
        self.brightness_calls.append(value)


class FakeConfiguration:
    def __init__(self, brightness, sleep):
        self.brightness = brightness
        self.sleep = sleep


def make_deck(*, brightness=80, sleep=None, status=SleepStatus.WAKE):
    """Build a HomeDeck with just enough state for the brightness logic."""
    deck = HomeDeck.__new__(HomeDeck)
    deck._device = FakeDevice()
    deck._configuration = FakeConfiguration(brightness, sleep)
    deck._sleep_status = status
    deck._current_brightness = None
    deck._active_schedule_brightness = None
    deck._last_action_time = 0
    return deck


NIGHT = dict(dim_brightness=10, dim_timeout=30, sleep_timeout=0,
             schedule=[{'from': '22:00', 'to': '06:00', 'brightness': 10}])


class TestTargetBrightnessNoSchedule:
    def test_wake_uses_full_brightness(self):
        deck = make_deck(sleep=SleepConfig(dim_brightness=10, dim_timeout=30, sleep_timeout=300))
        assert deck._target_brightness() == 80

    def test_dim_uses_dim_brightness(self):
        deck = make_deck(sleep=SleepConfig(dim_brightness=10, dim_timeout=30, sleep_timeout=300),
                         status=SleepStatus.DIM)
        assert deck._target_brightness() == 10

    def test_sleep_is_off(self):
        deck = make_deck(sleep=SleepConfig(dim_brightness=10, dim_timeout=30, sleep_timeout=300),
                         status=SleepStatus.SLEEP)
        assert deck._target_brightness() == 0


class TestTargetBrightnessWithSchedule:
    def test_daytime_is_full_brightness(self):
        deck = make_deck(sleep=SleepConfig(**NIGHT))
        deck._active_schedule_brightness = None  # outside the night window
        assert deck._target_brightness() == 80

    def test_daytime_idle_stays_bright(self):
        """A wall-mounted deck must not dim in the middle of the day.

        dim_timeout still fires outside the night window, so without the
        schedule check the display would drop to dim_brightness at noon.
        """
        deck = make_deck(sleep=SleepConfig(**NIGHT), status=SleepStatus.DIM)
        deck._active_schedule_brightness = None
        assert deck._target_brightness() == 80

    def test_night_idle_is_dim_not_off(self):
        """The wall-mount requirement: dim overnight, never fully off."""
        deck = make_deck(sleep=SleepConfig(**NIGHT), status=SleepStatus.DIM)
        deck._active_schedule_brightness = 10
        assert deck._target_brightness() == 10

    def test_night_touch_brightens_for_readability(self):
        deck = make_deck(sleep=SleepConfig(**NIGHT), status=SleepStatus.WAKE)
        deck._active_schedule_brightness = 10
        # Pressing a button at night returns to the normal brightness
        assert deck._target_brightness() == 80

    def test_scheduled_level_is_used_verbatim(self):
        """The schedule's brightness wins over dim_brightness inside a window."""
        sleep = SleepConfig(dim_brightness=30, dim_timeout=30, sleep_timeout=0,
                            schedule=[{'from': '22:00', 'to': '06:00', 'brightness': 5}])
        deck = make_deck(sleep=sleep, status=SleepStatus.DIM)
        deck._active_schedule_brightness = 5
        assert deck._target_brightness() == 5

    def test_no_schedule_keeps_classic_idle_dimming(self):
        """Configs without a schedule must behave exactly as before."""
        deck = make_deck(sleep=SleepConfig(dim_brightness=10, dim_timeout=30, sleep_timeout=300),
                         status=SleepStatus.DIM)
        deck._active_schedule_brightness = None
        assert deck._target_brightness() == 10


class TestApplyBrightnessDeduplication:
    def test_no_redundant_device_writes(self):
        deck = make_deck(sleep=SleepConfig(dim_brightness=10, dim_timeout=30, sleep_timeout=300))

        deck._apply_brightness()
        deck._apply_brightness()
        deck._apply_brightness()

        # Only the first call reaches the device
        assert deck._device.brightness_calls == [80]

    def test_change_is_written(self):
        deck = make_deck(sleep=SleepConfig(dim_brightness=10, dim_timeout=30, sleep_timeout=300))
        deck._apply_brightness()

        deck._sleep_status = SleepStatus.DIM
        deck._apply_brightness()

        assert deck._device.brightness_calls == [80, 10]


class TestSleepTimeoutZeroNeverSleeps:
    def test_update_never_sleeps_when_timeout_disabled(self):
        deck = make_deck(sleep=SleepConfig(**NIGHT))
        deck._last_action_time = time.time() - 86400  # idle for a full day

        deck._update_sleep_status()

        assert deck._sleep_status != SleepStatus.SLEEP

    def test_update_dims_after_dim_timeout(self):
        deck = make_deck(sleep=SleepConfig(**NIGHT))
        deck._last_action_time = time.time() - 60  # past the 30s dim timeout

        deck._update_sleep_status()

        assert deck._sleep_status == SleepStatus.DIM


class TestWakeAndSleepTransitions:
    def test_wake_up_restores_brightness(self):
        deck = make_deck(sleep=SleepConfig(dim_brightness=10, dim_timeout=30, sleep_timeout=300),
                         status=SleepStatus.DIM)
        deck._wake_up()

        assert deck._sleep_status == SleepStatus.WAKE
        assert deck._device.brightness_calls[-1] == 80

    def test_sleep_turns_display_off(self):
        deck = make_deck(sleep=SleepConfig(dim_brightness=10, dim_timeout=30, sleep_timeout=300))
        deck._sleep()

        assert deck._sleep_status == SleepStatus.SLEEP
        assert deck._device.brightness_calls[-1] == 0
