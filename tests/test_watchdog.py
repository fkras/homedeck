"""Tests for the unresponsive-device watchdog.

The D200 can stop taking writes without the USB device disappearing - seen
when its firmware screensaver took over. Writes run on the device's own
thread, so HomeDeck asks the device whether they are failing rather than
trusting that a call returned.
"""

import time

import pytest

from homedeck.d200 import SmallWindowMode
from homedeck.homedeck import DeviceUnresponsiveError, HomeDeck


class FakeHidDevice:
    """What the device knows about the USB side: whether its writes fail and
    whether a descriptor read answers."""

    def __init__(self, *, raises=None, write_raises=None):
        self.raises = raises
        self.write_raises = write_raises
        self.writes = 0
        self.probes = 0

    def write(self):
        self.writes += 1
        if self.write_raises:
            raise self.write_raises

    def probe(self):
        self.probes += 1
        if self.raises:
            raise self.raises
        return True


class FakeDevice:
    """The parts of UlanziD200 the watchdog uses."""

    def __init__(self, hid_device=None, *, write_failing=False):
        self._hid = hid_device
        self.write_failing = write_failing
        self.last_error = None

    def set_small_window_data(self, **kwargs):
        if self._hid is None:
            raise OSError('device is closed')
        self._hid.write()

    def is_responsive(self):
        if self._hid is None or self.write_failing:
            return False
        return self._hid.probe()

    unlocks = 0

    def unlock_screen(self):
        self.unlocks += 1

    @property
    def keep_alive_calls(self):
        # The small-window packet is the keep-alive
        return self._hid.writes if self._hid else 0


def make_deck(device):
    deck = HomeDeck.__new__(HomeDeck)
    deck._device = device
    deck._unresponsive_since = None
    return deck


class TestHealthyDevice:
    def test_alive_when_the_device_answers(self):
        deck = make_deck(FakeDevice(FakeHidDevice()))

        assert deck._device_is_alive() is True
        assert deck._unresponsive_since is None

    def test_keep_alive_is_actually_sent(self):
        device = FakeDevice(FakeHidDevice())
        deck = make_deck(device)

        deck._device_is_alive()

        assert device.keep_alive_calls == 1

    def test_probe_adds_no_extra_draw(self):
        """The health check must not send a second small-window packet.

        It used to re-send one on top of the keep-alive, which made the deck
        redraw the clock area twice a second and flicker. Exactly one write per
        tick is correct.
        """
        hid = FakeHidDevice()
        deck = make_deck(FakeDevice(hid))

        deck._device_is_alive()

        assert hid.writes == 1      # the keep-alive itself, nothing more
        assert hid.probes == 1      # liveness uses a descriptor read


class TestUnresponsiveDevice:
    def test_failed_probe_starts_the_clock(self):
        deck = make_deck(FakeDevice(FakeHidDevice(raises=OSError('no such device'))))

        assert deck._device_is_alive() is True   # tolerated at first
        assert deck._unresponsive_since is not None

    def test_a_failing_write_starts_the_clock(self):
        deck = make_deck(FakeDevice(FakeHidDevice(write_raises=OSError('boom'))))

        assert deck._device_is_alive() is True
        assert deck._unresponsive_since is not None

    def test_a_write_failing_on_the_device_thread_starts_the_clock(self):
        """The call returns fine; the failure shows up in is_responsive()."""
        deck = make_deck(FakeDevice(FakeHidDevice(), write_failing=True))

        assert deck._device_is_alive() is True
        assert deck._unresponsive_since is not None

    def test_missing_hid_handle_is_not_alive(self):
        deck = make_deck(FakeDevice(hid_device=None))

        assert deck._device_is_alive() is True
        assert deck._unresponsive_since is not None

    def test_gives_up_after_the_timeout(self):
        """This is the screensaver case: sustained silence, so reconnect."""
        deck = make_deck(FakeDevice(FakeHidDevice(raises=OSError('gone'))))

        deck._device_is_alive()
        # Backdate past the timeout instead of sleeping
        deck._unresponsive_since = time.time() - (HomeDeck.DEVICE_TIMEOUT + 1)

        assert deck._device_is_alive() is False

    def test_recovers_before_the_timeout(self):
        """A brief hiccup must not trigger a reconnect."""
        hid = FakeHidDevice(raises=OSError('busy'))
        deck = make_deck(FakeDevice(hid))

        deck._device_is_alive()
        assert deck._unresponsive_since is not None

        # Device starts answering again
        hid.raises = None
        assert deck._device_is_alive() is True
        assert deck._unresponsive_since is None


class TestKeepAliveLoop:
    @pytest.mark.asyncio
    async def test_raises_so_the_supervisor_reconnects(self):
        """_setup() catches this and runs its reconnect path."""
        deck = make_deck(FakeDevice(FakeHidDevice(raises=OSError('gone'))))
        deck._is_ready = True
        deck._configuration = None
        deck._unresponsive_since = time.time() - (HomeDeck.DEVICE_TIMEOUT + 1)

        with pytest.raises(DeviceUnresponsiveError):
            await deck._keep_alive()


class TestUnlock:
    async def test_keep_alive_unlocks_every_interval(self, monkeypatch):
        """The deck locks itself after ~2 minutes without host traffic and then
        reports no presses; the keep-alive loop unlocks it periodically."""
        import asyncio as real_asyncio
        import homedeck.homedeck as module

        device = FakeDevice(FakeHidDevice())
        deck = make_deck(device)
        deck._configuration = None
        ticks = []

        async def fast_sleep(_):
            ticks.append(1)
            if len(ticks) > HomeDeck.UNLOCK_INTERVAL * 2:
                raise real_asyncio.CancelledError
        monkeypatch.setattr(module.asyncio, 'sleep', fast_sleep)

        with pytest.raises(real_asyncio.CancelledError):
            await deck._keep_alive()

        assert device.unlocks == 2


class TestSmallWindowMode:
    """The small window (button 13) cycles STATS -> CLOCK -> BACKGROUND.

    The deck reports a raw `state` byte for this button that is not the mode
    we last sent - it alternates between values like 1 and 200 - so HomeDeck
    steps through its own list instead.
    """

    def test_cycles_through_every_mode(self):
        device = FakeSmallWindowDevice()
        deck = HomeDeck.__new__(HomeDeck)
        deck._device = device

        modes = HomeDeck.SMALL_WINDOW_MODES
        total = len(modes)
        seen = []
        for _ in range(total + 1):
            deck._cycle_small_window_mode()
            seen.append(device.small_window_mode)

        # Every mode is visited...
        assert set(seen[:total]) == set(modes)
        # ...and one more tap wraps back to where the cycle began
        assert seen[total] == seen[0]

    def test_pushes_the_change_to_the_device(self):
        """set_small_window_mode() only sets a variable; the deck must redraw."""
        device = FakeSmallWindowDevice()
        deck = HomeDeck.__new__(HomeDeck)
        deck._device = device

        deck._cycle_small_window_mode()

        assert device.restored == 1

    def test_survives_a_mode_outside_the_cycle(self):
        device = FakeSmallWindowDevice()
        device.small_window_mode = SmallWindowMode.CLOCK_3
        deck = HomeDeck.__new__(HomeDeck)
        deck._device = device

        deck._cycle_small_window_mode()   # must not raise

        assert device.small_window_mode in HomeDeck.SMALL_WINDOW_MODES


class FakeSmallWindowDevice:
    def __init__(self):
        self.small_window_mode = SmallWindowMode.CLOCK
        self.restored = 0

    def set_small_window_mode(self, mode):
        try:
            self.small_window_mode = SmallWindowMode(mode)
        except ValueError:
            self.small_window_mode = SmallWindowMode.CLOCK

    def restore_small_window(self):
        self.restored += 1
