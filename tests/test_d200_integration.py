"""HomeDeck driving a real UlanziD200 over a fake HID handle."""

import asyncio
import os

import pytest

import homedeck.homedeck as homedeck_module
from homedeck.d200 import Protocol, UlanziD200
from homedeck.d200.protocol import CommandCode
from homedeck.d200.transport import HidTransport
from homedeck.enums import SleepStatus
from homedeck.homedeck import HomeDeck
from tests.test_d200_device import DEVICE_INFO, FakeHandle, report

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def handle():
    return FakeHandle()


@pytest.fixture
def device(handle, tmp_path):
    dev = UlanziD200(HidTransport(handle), icon_dir=str(tmp_path))
    yield dev
    dev.close()


def bare_deck(device):
    deck = HomeDeck.__new__(HomeDeck)
    deck._vendor_id = UlanziD200.USB_VENDOR_ID
    deck._product_id = UlanziD200.USB_PRODUCT_ID
    deck._device = device
    return deck


class TestConnect:
    async def test_connect_runs_the_official_handshake(self, handle, tmp_path, monkeypatch):
        opened = {}

        def fake_auto_connect(vendor_id, product_id, **kwargs):
            opened.update(kwargs, vendor_id=vendor_id, product_id=product_id)
            return UlanziD200(HidTransport(handle), icon_dir=str(tmp_path))

        monkeypatch.setattr(homedeck_module, 'auto_connect', fake_auto_connect)
        deck = bare_deck(None)
        deck._device = None

        await deck._connect_device()
        try:
            await deck._device.flush(2)

            assert opened['vendor_id'] == 0x2207 and opened['product_id'] == 0x0019
            assert opened['clock_24h'] is (HomeDeck.CLOCK_FORMAT == '24H')
            assert deck._device.device_info.serial_number == '02C51A015U3672946'
            assert handle.protocols() == [Protocol.DEVICE_INFO, Protocol.UNLOCK_SCREEN, Protocol.SLEEP_TIME]
            assert handle.frames()[2].payload == b'0'
        finally:
            deck._device.close()


class TestReloadAll:
    async def test_deck_sleep_timeout_comes_from_the_configuration(self, device, handle, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        os.makedirs('assets')
        with open(os.path.join('assets', 'configuration.yml'), 'w') as fp:
            fp.write('deck_sleep_timeout: 15\npages:\n  $root:\n    buttons: []\n')
        with open(os.path.join(REPO, 'src', 'homedeck', 'yaml', 'configuration.base.yml')) as fp:
            import yaml
            base = yaml.safe_load(fp)

        deck = bare_deck(device)
        deck._ha = type('HA', (), {'all_states': {}})()
        deck._base_configuration_dict = base
        deck._configuration = None
        deck._is_ready = False
        deck._pages_stack = []
        deck._sleep_status = SleepStatus.WAKE
        deck._current_brightness = None

        assert deck.reload_all()
        await device.flush(2)

        frames = {f.protocol: f.payload for f in handle.frames()}
        assert frames[Protocol.SLEEP_TIME] == b'15'
        assert Protocol.LABEL_STYLE in frames
        assert frames[Protocol.BRIGHTNESS] == str(base['brightness']).encode()


class TestFirmwareRestart:
    async def test_unsolicited_device_info_repaints(self, device, handle):
        deck = bare_deck(device)
        repaints = []
        deck.force_reload_current_page = lambda: repaints.append(True)
        device.SOLICITED_WINDOW = 0

        handle.inbox.put(report(Protocol.DEVICE_INFO, DEVICE_INFO, CommandCode.REPLY))
        task = asyncio.ensure_future(deck._read_packets())
        try:
            for _ in range(100):
                await asyncio.sleep(0.02)
                if repaints:
                    break
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert repaints == [True]

    async def test_solicited_device_info_does_not(self, device, handle):
        deck = bare_deck(device)
        repaints = []
        deck.force_reload_current_page = lambda: repaints.append(True)

        await device.request_device_info()
        task = asyncio.ensure_future(deck._read_packets())
        await asyncio.sleep(0.2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert repaints == []


class FakeHA:
    def __init__(self, *, raises=None):
        self.raises = raises
        self.next_id = 7
        self.on_command_failed = None

    async def call_service(self, **kwargs):
        if self.raises:
            raise self.raises
        self.next_id += 1
        return self.next_id


class FakeConfig:
    def __init__(self, alert_on_error=True):
        self.alert_on_error = alert_on_error

    def get_button(self, page_id, index):
        # No fading keys here, so no press predicts a look
        return None


def alert_keys(handle):
    import io
    import json
    import zipfile

    keys = []
    for frame in handle.frames():
        if frame.protocol == Protocol.RESULT_OVERLAY:
            with zipfile.ZipFile(io.BytesIO(frame.payload)) as zf:
                keys += list(json.loads(zf.read('manifest.json')))
    return keys


def alert_deck(device, ha, alert_on_error=True):
    deck = bare_deck(device)
    deck._ha = ha
    deck._configuration = FakeConfig(alert_on_error)
    deck._pending_service_keys = {}
    return deck


class TestAlertOnError:
    async def test_refused_call_flags_its_key(self, device, handle):
        ha = FakeHA()
        deck = alert_deck(device, ha)

        await deck.call_ha_service(domain='light', service='toggle', service_data={}, key_index=6)
        await deck.call_ha_service(domain='light', service='toggle', service_data={}, key_index=2)
        deck._on_ha_command_failed(8, {'code': 'not_found'})      # the first call
        await device.flush(2)

        assert alert_keys(handle) == ['1_1']

    async def test_a_call_that_never_went_out_flags_its_key(self, device, handle):
        deck = alert_deck(device, FakeHA(raises=ConnectionError('closed')))

        await deck.call_ha_service(domain='light', service='toggle', service_data={}, key_index=12)
        await device.flush(2)

        assert alert_keys(handle) == ['2_2']

    async def test_can_be_switched_off(self, device, handle):
        deck = alert_deck(device, FakeHA(raises=ConnectionError('closed')), alert_on_error=False)

        await deck.call_ha_service(domain='light', service='toggle', service_data={}, key_index=12)
        await device.flush(2)

        assert alert_keys(handle) == []

    async def test_unknown_failures_are_ignored(self, device, handle):
        deck = alert_deck(device, FakeHA())

        deck._on_ha_command_failed(999, {'code': 'x'})
        await device.flush(2)

        assert alert_keys(handle) == []

    async def test_pending_calls_are_bounded(self, device):
        deck = alert_deck(device, FakeHA())

        for _ in range(HomeDeck.PENDING_SERVICE_KEYS + 10):
            await deck.call_ha_service(domain='light', service='toggle', service_data={}, key_index=0)

        assert len(deck._pending_service_keys) == HomeDeck.PENDING_SERVICE_KEYS


class TestScreensaverConfig:
    async def test_uploaded_once_until_the_file_changes(self, device, handle, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        os.makedirs('assets')
        image = tmp_path / 'assets' / 'saver.jpg'
        image.write_bytes(b'\xff\xd8\xff' + b'a' * 100)
        deck = bare_deck(device)

        deck._apply_screensaver('saver.jpg')
        deck._apply_screensaver('saver.jpg')
        await device.flush(5)
        assert handle.protocols() == [Protocol.SCREENSAVER]

        image.write_bytes(b'\xff\xd8\xff' + b'b' * 100)
        os.utime(image, (1, 1))
        deck._apply_screensaver('saver.jpg')
        await device.flush(5)
        assert handle.protocols() == [Protocol.SCREENSAVER, Protocol.SCREENSAVER]

    async def test_missing_file_is_not_fatal(self, device, handle, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        deck = bare_deck(device)

        deck._apply_screensaver('nope.jpg')
        deck._apply_screensaver(None)
        await device.flush(2)

        assert handle.protocols() == []
