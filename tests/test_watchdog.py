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

    def __init__(self, *, write_result=64, raises=None):
        self.write_result = write_result
        self.raises = raises
        self.writes = 0

    def write(self, packet):
        self.writes += 1
        if self.raises:
            raise self.raises
        return self.write_result


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
    def test_alive_when_writes_succeed(self):
        deck = make_deck(FakeDevice(FakeHidDevice(write_result=1024)))

        assert deck._device_is_alive() is True
        assert deck._unresponsive_since is None

    def test_alive_when_write_returns_none(self):
        # Some hidapi bindings return None rather than a byte count
        deck = make_deck(FakeDevice(FakeHidDevice(write_result=None)))

        assert deck._device_is_alive() is True

    def test_keep_alive_is_actually_sent(self):
        device = FakeDevice(FakeHidDevice())
        deck = make_deck(device)

        deck._device_is_alive()

        assert device.keep_alive_calls == 1


class TestUnresponsiveDevice:
    def test_negative_write_starts_the_clock(self):
        """hidapi returns -1 when the device stops accepting writes."""
        deck = make_deck(FakeDevice(FakeHidDevice(write_result=-1)))

        assert deck._device_is_alive() is True   # tolerated at first
        assert deck._unresponsive_since is not None

    def test_raising_write_starts_the_clock(self):
        deck = make_deck(FakeDevice(FakeHidDevice(raises=OSError('write error'))))

        assert deck._device_is_alive() is True
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
        deck = make_deck(FakeDevice(FakeHidDevice(write_result=-1)))

        deck._device_is_alive()
        # Backdate past the timeout instead of sleeping
        deck._unresponsive_since = time.time() - (HomeDeck.DEVICE_TIMEOUT + 1)

        assert deck._device_is_alive() is False

    def test_recovers_before_the_timeout(self):
        """A brief hiccup must not trigger a reconnect."""
        hid = FakeHidDevice(write_result=-1)
        deck = make_deck(FakeDevice(hid))

        deck._device_is_alive()
        assert deck._unresponsive_since is not None

        # Device starts answering again
        hid.write_result = 64
        assert deck._device_is_alive() is True
        assert deck._unresponsive_since is None


class TestKeepAliveLoop:
    @pytest.mark.asyncio
    async def test_raises_so_the_supervisor_reconnects(self):
        """_setup() catches this and runs its reconnect path."""
        deck = make_deck(FakeDevice(FakeHidDevice(write_result=-1)))
        deck._is_ready = True
        deck._configuration = None
        deck._unresponsive_since = time.time() - (HomeDeck.DEVICE_TIMEOUT + 1)

        with pytest.raises(DeviceUnresponsiveError):
            await deck._keep_alive()


class TestKeepAlivePacket:
    def test_builds_a_valid_packet(self):
        deck = make_deck(FakeDevice(FakeHidDevice()))

        packet = deck._keep_alive_packet()

        assert packet[:2] == b'\x7c\x7c'   # protocol magic
        assert len(packet) == 1024
