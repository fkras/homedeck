"""UlanziD200 against a fake hidapi handle: handshake, ordering, coalescing,
and failures that must surface instead of being swallowed."""

import asyncio
import json
import queue
import threading

import pytest

from homedeck.d200 import DeviceDisconnected, DeviceInfoEvent, KeyEvent, Protocol, SmallWindowMode, UlanziD200
from homedeck.d200.protocol import CommandCode, decode_report
from homedeck.d200.transport import HidTransport

DEVICE_INFO = json.dumps({
    'SerialNumber': '02C51A015U3672946', 'Dversion': '5.3.6', 'error': '0',
    'DeviceType': 'D200', 'HardwareVersion': 'SSD210V100',
}).encode()


def report(protocol, payload=b'', command_code=CommandCode.EVENT):
    data = b'\x7c\x7c' + bytes([command_code, protocol]) + len(payload).to_bytes(4, 'little') + payload
    return list(data.ljust(1024, b'\x00'))


class FakeHandle:
    """Stands in for a cython-hidapi device."""

    def __init__(self, *, answer_info=True):
        self.reports = []
        self.inbox = queue.Queue()
        self.answer_info = answer_info
        self.write_result = None
        self.read_error = None
        self.gate = threading.Event()
        self.gate.set()
        self.closed = False
        # hid_close() while a read or write is inside hidapi is a use-after-free
        self.active = 0
        self.closed_while_active = False

    # hidapi API
    def write(self, data):
        self.active += 1
        try:
            self.gate.wait(5)
            if self.write_result is not None:
                return self.write_result
            self.reports.append(bytes(data))
            frame = decode_report(bytes(data[1:]))
            if frame and frame.protocol == Protocol.DEVICE_INFO and self.answer_info:
                self.inbox.put(report(Protocol.DEVICE_INFO, DEVICE_INFO, CommandCode.REPLY))
            return len(data)
        finally:
            self.active -= 1

    def read(self, size, timeout_ms=0):
        self.active += 1
        try:
            if self.read_error:
                raise OSError(self.read_error)
            try:
                return self.inbox.get(timeout=timeout_ms / 1000)
            except queue.Empty:
                return []
        finally:
            self.active -= 1

    def get_product_string(self):
        return 'D200'

    def close(self):
        if self.active:
            self.closed_while_active = True
        self.closed = True

    def error(self):
        return 'fake error'

    # helpers
    def frames(self):
        out = []
        for data in self.reports:
            frame = decode_report(data[1:])
            if frame:
                out.append(frame)
        return out

    def protocols(self):
        return [f.protocol for f in self.frames()]


@pytest.fixture
def handle():
    return FakeHandle()


@pytest.fixture
def device(handle, tmp_path):
    dev = UlanziD200(HidTransport(handle), icon_dir=str(tmp_path))
    yield dev
    dev.close()


async def next_event(device, kind=object, timeout=2):
    async def wait():
        async for item in device.read_packet():
            if isinstance(item, kind):
                return item
    return await asyncio.wait_for(wait(), timeout)


class TestHandshake:
    async def test_asks_for_info_unlocks_then_disables_firmware_sleep(self, device, handle):
        info = await device.handshake(sleep_time=0)
        await device.flush(2)

        assert info.device_type == 'D200' and info.firmware_version == '5.3.6'
        assert handle.protocols() == [Protocol.DEVICE_INFO, Protocol.UNLOCK_SCREEN, Protocol.SLEEP_TIME]
        assert handle.frames()[2].payload == b'0'

    async def test_returns_none_when_the_deck_stays_quiet(self, tmp_path):
        handle = FakeHandle(answer_info=False)
        dev = UlanziD200(HidTransport(handle), icon_dir=str(tmp_path))
        try:
            assert await dev.handshake(timeout=0.2) is None
        finally:
            dev.close()

    async def test_reply_is_marked_solicited(self, device):
        await device.handshake()

        event = await next_event(device, DeviceInfoEvent)

        assert event.solicited


class TestFirmwareRestart:
    async def test_unsolicited_info_restores_brightness_and_sleep(self, device, handle):
        device.set_brightness(70)
        device.set_sleep_time(0)
        await device.flush(2)
        handle.reports.clear()
        device.SOLICITED_WINDOW = 0

        handle.inbox.put(report(Protocol.DEVICE_INFO, DEVICE_INFO, CommandCode.REPLY))
        event = await next_event(device, DeviceInfoEvent)
        await device.flush(2)

        assert not event.solicited
        frames = {f.protocol: f.payload for f in handle.frames()}
        assert frames == {Protocol.BRIGHTNESS: b'70', Protocol.SLEEP_TIME: b'0'}

    async def test_label_style_request_is_answered(self, device, handle):
        device.set_label_style({'align': 'top', 'color': '00FF00', 'font_name': 'Roboto', 'size': 9})
        await device.flush(2)
        handle.reports.clear()

        handle.inbox.put(report(Protocol.LABEL_STYLE, b'', CommandCode.EVENT))
        for _ in range(50):
            await asyncio.sleep(0.02)
            if handle.reports:
                break
        await device.flush(2)

        (frame,) = handle.frames()
        assert frame.protocol == Protocol.LABEL_STYLE
        assert json.loads(frame.payload)['Align'] == 'top'


class TestKeys:
    async def test_key_events_reach_read_packet(self, device, handle):
        handle.inbox.put(report(Protocol.BUTTONS, bytes([1, 6, 1, 1])))
        handle.inbox.put(report(Protocol.BUTTONS, bytes([1, 6, 1, 0])))

        press = await next_event(device, KeyEvent)
        release = await next_event(device, KeyEvent)

        assert (press.index, press.pressed) == (6, True)
        assert (release.index, release.pressed) == (6, False)

    async def test_events_before_anyone_reads_are_kept(self, device, handle):
        handle.inbox.put(report(Protocol.BUTTONS, bytes([1, 2, 1, 1])))
        await asyncio.sleep(0.2)

        assert (await next_event(device, KeyEvent)).index == 2


class TestWrites:
    async def test_settings_are_deduplicated(self, device, handle):
        device.set_brightness(50)
        await device.flush(2)
        device.set_brightness(50)
        device.set_brightness(50, force=True)
        await device.flush(2)

        assert handle.protocols() == [Protocol.BRIGHTNESS, Protocol.BRIGHTNESS]

    async def test_newest_brightness_wins_while_queued(self, device, handle):
        handle.gate.clear()                  # hold the writer
        device.send_raw(Protocol.LOCK_SCREEN)
        await asyncio.sleep(0.05)
        for level in (10, 20, 30):
            device.set_brightness(level)
        handle.gate.set()
        await device.flush(2)

        frames = handle.frames()
        assert [f.protocol for f in frames] == [Protocol.LOCK_SCREEN, Protocol.BRIGHTNESS]
        assert frames[1].payload == b'30'

    async def test_a_new_page_replaces_queued_pages_and_updates(self, device, handle):
        handle.gate.clear()
        device.send_raw(Protocol.LOCK_SCREEN)
        await asyncio.sleep(0.05)
        device.set_buttons({0: {'name': 'old'}})
        device.set_buttons({0: {'name': 'older update'}}, update_only=True)
        device.set_buttons({0: {'name': 'new'}, 1: None})
        handle.gate.set()
        await device.flush(2)

        assert handle.protocols() == [Protocol.LOCK_SCREEN, Protocol.BUTTONS]

    async def test_updates_fold_into_a_queued_page(self, device, handle, tmp_path):
        import io
        import zipfile

        handle.gate.clear()
        device.send_raw(Protocol.LOCK_SCREEN)
        await asyncio.sleep(0.05)
        device.set_buttons({0: {'name': 'a'}, 1: {'name': 'b'}})
        device.set_buttons({1: {'name': 'B'}}, update_only=True)
        handle.gate.set()
        await device.flush(2)

        page = handle.frames()[-1]
        # The ZIP spans several reports; rebuild it from the raw stream
        stream = b''.join(r[1:] for r in handle.reports[1:])
        length = int.from_bytes(stream[4:8], 'little')
        with zipfile.ZipFile(io.BytesIO(stream[8:8 + length])) as zf:
            manifest = json.loads(zf.read('manifest.json'))

        assert page.protocol == Protocol.BUTTONS
        assert manifest['0_0']['ViewParam'][0]['Text'] == 'a'
        assert manifest['1_0']['ViewParam'][0]['Text'] == 'B'

    async def test_small_window_uses_the_current_mode(self, device, handle):
        device.set_small_window_mode(SmallWindowMode.STATS)
        device.set_small_window_data(cpu=12, mem=41, gpu=3)
        await device.flush(2)

        (frame,) = handle.frames()
        fields = frame.payload.decode().split('|')
        assert fields[0] == '0' and fields[1:3] == ['12', '41'] and fields[4] == '3'
        assert fields[5] == '24H'

    async def test_unknown_mode_falls_back_to_clock(self, device):
        device.set_small_window_mode(999)

        assert device.small_window_mode == SmallWindowMode.CLOCK

    def test_firmware_is_refused(self, device):
        with pytest.raises(ValueError):
            device.send_raw(Protocol.FIRMWARE, b'PK')


class TestFailures:
    async def test_failed_write_marks_the_device_unresponsive(self, device, handle):
        assert device.is_responsive()

        handle.write_result = -1
        device.set_brightness(40)
        await device.flush(2)

        assert not device.is_responsive()
        assert device.write_failed_since is not None

        # and recovers once writes go through again
        handle.write_result = None
        device.set_brightness(41)
        await device.flush(2)
        assert device.is_responsive()

    async def test_read_error_ends_read_packet(self, device, handle):
        handle.read_error = 'device disconnected'

        with pytest.raises(DeviceDisconnected):
            await next_event(device)

        assert not device.is_responsive()

    async def test_close_is_quiet(self, handle, tmp_path):
        dev = UlanziD200(HidTransport(handle), icon_dir=str(tmp_path))
        dev.close()

        assert handle.closed
        assert not handle.closed_while_active
        assert dev.last_error is None
        assert not dev.is_responsive()

    async def test_close_waits_for_a_write_in_flight(self, handle, tmp_path):
        """Freeing the handle under a hid_write() would crash the process."""
        dev = UlanziD200(HidTransport(handle), icon_dir=str(tmp_path))
        handle.gate.clear()                          # a report is stuck in write()
        dev.set_buttons({i: {'name': 'x' * 50} for i in range(13)})
        await asyncio.sleep(0.1)
        assert handle.active

        closer = threading.Thread(target=dev.close)
        closer.start()
        await asyncio.sleep(0.3)
        assert not handle.closed                     # still waiting on the write

        handle.gate.set()
        closer.join(5)

        assert handle.closed
        assert not handle.closed_while_active
        assert len(handle.reports) == 1              # the rest of the frame was dropped


class TestOverlayScreensaverLock:
    async def test_show_alert(self, device, handle):
        import io
        import zipfile

        device.show_alert(4)
        await device.flush(2)

        (frame,) = handle.frames()
        assert frame.protocol == Protocol.RESULT_OVERLAY
        with zipfile.ZipFile(io.BytesIO(frame.payload)) as zf:
            assert list(json.loads(zf.read('manifest.json'))) == ['4_0']

    async def test_screensaver_png_is_converted_to_jpeg(self, device, handle):
        import io
        import zipfile
        from PIL import Image

        png = io.BytesIO()
        Image.new('RGB', (40, 20), 'red').save(png, 'PNG')
        device.set_screensaver(png.getvalue(), filename='mine.png')
        await device.flush(5)

        stream = b''.join(r[1:] for r in handle.reports)
        length = int.from_bytes(stream[4:8], 'little')
        assert stream[3] == Protocol.SCREENSAVER
        with zipfile.ZipFile(io.BytesIO(stream[8:8 + length])) as zf:
            assert zf.namelist() == ['mine.jpg']
            assert zf.read('mine.jpg')[:3] == b'\xff\xd8\xff'

    async def test_screensaver_jpeg_passes_through(self, device, handle, tmp_path):
        import io
        import zipfile

        path = tmp_path / 'pic.jpg'
        path.write_bytes(b'\xff\xd8\xff' + b'jpeg' * 10)
        device.set_screensaver(str(path))
        await device.flush(5)

        (frame,) = handle.frames()
        with zipfile.ZipFile(io.BytesIO(frame.payload)) as zf:
            assert zf.read('pic.jpg') == path.read_bytes()

    async def test_lock_and_unlock(self, device, handle):
        device.lock_screen()
        device.unlock_screen()
        await device.flush(2)

        assert handle.protocols() == [Protocol.LOCK_SCREEN, Protocol.UNLOCK_SCREEN]
        assert all(f.payload == b'' for f in handle.frames())
