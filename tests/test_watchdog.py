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

    def __init__(self, *, raises=None):
        self.raises = raises
        self.writes = 0
        self.probes = 0

    def write(self, packet):
        self.writes += 1
        return len(packet)

    def get_product_string(self):
        self.probes += 1
        if self.raises:
            raise self.raises
        return 'Ulanzi Stream Controller D200'


class FakeDevice:
    def __init__(self, hid_device=None, keep_alive_raises=None):
        self._hid_device = hid_device
        self.keep_alive_raises = keep_alive_raises
        self.keep_alive_calls = 0

    def keep_alive(self):
        self.keep_alive_calls += 1
        if self.keep_alive_raises:
            raise self.keep_alive_raises


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

    def test_probe_draws_nothing(self):
        """The health check must not write a protocol packet.

        It used to re-send a small-window packet, which made the deck redraw
        the clock area twice a second and flicker.
        """
        hid = FakeHidDevice()
        deck = make_deck(FakeDevice(hid))

        deck._device_is_alive()

        assert hid.probes == 1
        assert hid.writes == 0


class TestUnresponsiveDevice:
    def test_failed_probe_starts_the_clock(self):
        deck = make_deck(FakeDevice(FakeHidDevice(raises=OSError('no such device'))))

        assert deck._device_is_alive() is True   # tolerated at first
        assert deck._unresponsive_since is not None

    def test_raising_keep_alive_starts_the_clock(self):
        deck = make_deck(FakeDevice(FakeHidDevice(), keep_alive_raises=OSError('boom')))

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
