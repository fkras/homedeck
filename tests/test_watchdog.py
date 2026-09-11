"""Tests for the unresponsive-device watchdog.

The D200's firmware screensaver can take the USB interface over completely:
button presses stop reaching the host and the device ignores writes, without
the USB device disappearing. strmdck swallows write errors, so HomeDeck would
otherwise keep sending keep-alives into a void forever.
"""

import time

import pytest

from homedeck.homedeck import DeviceUnresponsiveError, HomeDeck


class FakeHidDevice:
    """Stands in for the hidapi handle."""

    def __init__(self, *, raises=None, write_raises=None):
        self.raises = raises
        self.write_raises = write_raises
        self.writes = 0
        self.probes = 0

    def write(self, packet):
        self.writes += 1
        if self.write_raises:
            raise self.write_raises
        return len(packet)

    def get_product_string(self):
        self.probes += 1
        if self.raises:
            raise self.raises
        return 'Ulanzi Stream Controller D200'


class FakeDevice:
    """HomeDeck builds the small-window packet itself and writes it straight to
    the HID handle, so there is no keep_alive() on the device to stub."""

    _small_window_mode = None

    def __init__(self, hid_device=None, keep_alive_raises=None):
        self._hid_device = hid_device
        self.keep_alive_raises = keep_alive_raises

    @property
    def keep_alive_calls(self):
        # The small-window packet is the keep-alive
        return self._hid_device.writes if self._hid_device else 0


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


class TestSmallWindowMode:
    """The small window (button 13) cycles STATS -> CLOCK -> BACKGROUND.

    The deck reports a raw `state` byte for this button that is not a mode
    index - it alternates between values like 1 and 200 - so passing it
    straight to set_small_window_mode() only ever resolved to CLOCK and
    tapping appeared to do nothing.
    """

    def test_cycles_through_every_mode(self):
        from strmdck.devices.ulanzi_d200 import SmallWindowMode

        device = FakeSmallWindowDevice()
        deck = HomeDeck.__new__(HomeDeck)
        deck._device = device

        total = len(SmallWindowMode)
        seen = []
        for _ in range(total + 1):
            deck._cycle_small_window_mode()
            seen.append(device._small_window_mode)

        # Every mode is visited...
        assert set(seen[:total]) == set(SmallWindowMode)
        # ...and one more tap wraps back to where the cycle began
        assert seen[total] == seen[0]

    def test_pushes_the_change_to_the_device(self):
        """set_small_window_mode() only sets a variable; the deck must redraw."""
        device = FakeSmallWindowDevice()
        deck = HomeDeck.__new__(HomeDeck)
        deck._device = device

        deck._cycle_small_window_mode()

        assert device.restored == 1

    def test_survives_an_unknown_current_mode(self):
        device = FakeSmallWindowDevice()
        device._small_window_mode = None
        deck = HomeDeck.__new__(HomeDeck)
        deck._device = device

        deck._cycle_small_window_mode()   # must not raise

        assert device._small_window_mode is not None


class FakeSmallWindowDevice:
    def __init__(self):
        from strmdck.devices.ulanzi_d200 import SmallWindowMode

        self._small_window_mode = SmallWindowMode.CLOCK
        self.restored = 0

    def set_small_window_mode(self, mode):
        from strmdck.devices.ulanzi_d200 import SmallWindowMode

        try:
            self._small_window_mode = SmallWindowMode(mode)
        except Exception:
            self._small_window_mode = SmallWindowMode.CLOCK

    def restore_small_window(self):
        self.restored += 1
