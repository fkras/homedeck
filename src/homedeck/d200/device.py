''' Asynchronous D200 device: what HomeDeck talks to.

Writes run on a dedicated thread in the order they were made, so the event
loop never blocks on USB - a page upload is a few hundred 1024-byte reports
and takes seconds. Writes that a newer one makes pointless (an older
brightness, a page that a newer page replaces) are dropped before they are
sent. Reads run on a second thread, like the official app's reader.

Failures are not swallowed: a failed write is recorded and is_responsive()
reports it, and a failed read ends read_packet() with DeviceDisconnected.
'''
from __future__ import annotations

import asyncio
import collections
import io
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Mapping, Optional, Union

from .protocol import (
    CommandCode,
    DeviceInfo,
    KeyEvent,
    Protocol,
    SmallWindowMode,
    brightness_payload,
    build_page_zip,
    label_style_payload,
    parse_device_info,
    parse_key_event,
    result_overlay_payload,
    screensaver_payload,
    sleep_time_payload,
    small_window_payload,
)
from .transport import D200_PRODUCT_ID, D200_VENDOR_ID, DeviceNotFound, HidTransport, TransportError

# HomeDeck and its tests know key events by strmdck's name
ButtonAction = KeyEvent

DEFAULT_ICON_DIR = os.path.join('.cache', 'icons', '_generated')


class DeviceDisconnected(TransportError):
    ''' The deck stopped answering reads; it needs reopening. '''


@dataclass
class DeviceInfoEvent:
    ''' The deck sent its device info. `solicited` is False when nothing asked
    for it - the firmware restarted, and what it showed is gone. '''
    info: DeviceInfo
    solicited: bool


@dataclass
class _Job:
    key: Optional[str]
    protocol: int
    payload: bytes = b''
    # Page jobs carry the buttons and build the ZIP on the writer thread
    buttons: Optional[Dict[int, Optional[dict]]] = None


class _Outbox:
    ''' Pending writes, in order, with stale ones dropped. '''

    # Only the latest of these matters
    LATEST_WINS = {'brightness', 'sleep_time', 'label_style', 'small_window', 'screensaver'}

    def __init__(self):
        self._jobs: List[_Job] = []
        self._cond = threading.Condition()
        self._busy = False
        self._closed = False

    def put(self, job: _Job):
        with self._cond:
            if self._closed:
                return

            if job.key in self.LATEST_WINS:
                for i, pending in enumerate(self._jobs):
                    if pending.key == job.key:
                        self._jobs[i] = job
                        break
                else:
                    self._jobs.append(job)
            elif job.key == 'page':
                # A full page replaces whatever pages were still waiting
                self._jobs = [j for j in self._jobs if j.key not in ('page', 'page_update')]
                self._jobs.append(job)
            elif job.key == 'page_update':
                # Fold into the newest waiting page or update: it hasn't been
                # sent yet, so sending it with these buttons is the same thing
                for pending in reversed(self._jobs):
                    if pending.key in ('page', 'page_update'):
                        pending.buttons.update(job.buttons)
                        break
                else:
                    self._jobs.append(job)
            else:
                self._jobs.append(job)

            self._cond.notify_all()

    def get(self) -> Optional[_Job]:
        with self._cond:
            while not self._jobs and not self._closed:
                self._cond.wait()

            if not self._jobs:
                return None

            self._busy = True
            return self._jobs.pop(0)

    def done(self):
        with self._cond:
            self._busy = False
            self._cond.notify_all()

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: not self._jobs and not self._busy, timeout)

    def close(self):
        with self._cond:
            self._closed = True
            self._jobs.clear()
            self._cond.notify_all()

    def pending(self) -> List[_Job]:
        with self._cond:
            return list(self._jobs)


class UlanziD200:
    BUTTON_COUNT = 13
    BUTTON_ROWS = 3
    BUTTON_COLS = 5
    ICON_WIDTH = 196
    ICON_HEIGHT = 196

    # The large tile, "3_2" in the manifest
    SMALL_WINDOW_INDEX = 13

    DECK_NAME = 'Ulanzi Stream Controller D200'
    USB_VENDOR_ID = D200_VENDOR_ID
    USB_PRODUCT_ID = D200_PRODUCT_ID

    # A device-info frame this soon after asking for one answers the request
    SOLICITED_WINDOW = 3.0

    def __init__(self, transport: HidTransport, *, icon_dir: str = DEFAULT_ICON_DIR, clock_24h: bool = True,
                 now: Callable[[], datetime] = datetime.now):
        self._transport = transport
        self._icon_dir = icon_dir
        self.clock_24h = clock_24h
        self._now = now

        self.device_info: Optional[DeviceInfo] = None
        self._info_event = threading.Event()
        self._info_requested_at = 0.0

        # What was last asked for, to skip repeats and to restore after the
        # firmware restarts
        self._brightness: Optional[int] = None
        self._sleep_time: Optional[int] = None
        self._label_style: Optional[bytes] = None
        self._small_window_mode = SmallWindowMode.CLOCK
        self._small_window_stats = {'cpu': 0, 'mem': 0, 'gpu': 0}

        self._write_failed_since: Optional[float] = None
        self.last_error: Optional[BaseException] = None
        self._read_error: Optional[BaseException] = None

        self._events = collections.deque(maxlen=256)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wakeup: Optional[asyncio.Event] = None

        self._closing = False
        self._outbox = _Outbox()
        self._writer = threading.Thread(target=self._writer_main, name='d200-writer', daemon=True)
        self._reader = threading.Thread(target=self._reader_main, name='d200-reader', daemon=True)
        self._writer.start()
        self._reader.start()

    # -- lifecycle ----------------------------------------------------------

    @classmethod
    def open(cls, vendor_id: int = D200_VENDOR_ID, product_id: int = D200_PRODUCT_ID, *,
             path: Optional[Union[str, bytes]] = None, **kwargs) -> 'UlanziD200':
        return cls(HidTransport.open(vendor_id, product_id, path=path), **kwargs)

    async def handshake(self, *, sleep_time: int = 0, timeout: float = 1.0) -> Optional[DeviceInfo]:
        ''' Ask for the device info, as the official app does on connect, then
        make sure the deck is listening: dismiss a lock screen left over from
        before (keys don't report while it's up) and set the firmware sleep
        timer. Returns the info, or None if the deck didn't answer in time. '''
        info = await self.request_device_info(timeout=timeout)
        self.unlock_screen()
        self.set_sleep_time(sleep_time, force=True)
        return info

    def close(self):
        ''' Drop unsent writes and release the device. Both threads are let
        out of hidapi before the handle is freed. '''
        self._closing = True
        self._outbox.close()
        self._transport.abort()
        for thread in (self._writer, self._reader):
            if thread is not threading.current_thread():
                thread.join(timeout=2)
        self._transport.close()

    @property
    def closed(self) -> bool:
        return self._closing

    # -- health -------------------------------------------------------------

    @property
    def write_failed_since(self) -> Optional[float]:
        return self._write_failed_since

    def is_responsive(self) -> bool:
        ''' Whether the deck is taking writes and answering. Draws nothing. '''
        if self._closing or self._read_error or self._write_failed_since is not None:
            return False

        return self._transport.probe()

    async def flush(self, timeout: Optional[float] = None) -> bool:
        ''' Wait until every write made so far has been sent. '''
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._outbox.wait_idle, timeout)

    # -- settings -----------------------------------------------------------

    def set_brightness(self, brightness: int, force=False):
        brightness = max(0, min(100, int(brightness)))
        if not force and brightness == self._brightness:
            return

        self._brightness = brightness
        self._put('brightness', Protocol.BRIGHTNESS, brightness_payload(brightness))

    def set_sleep_time(self, minutes: int, force=False):
        ''' The deck's sleep-time setting, in minutes (0x1a). Ulanzi Studio
        sends it on every connect, 0 by default, and offers 5, 15, 30, 60 and
        120. It doesn't stop the firmware locking onto its screensaver when
        the host goes quiet - only host traffic and unlock_screen() do. '''
        payload = sleep_time_payload(minutes)
        if not force and minutes == self._sleep_time:
            return

        self._sleep_time = int(minutes)
        self._put('sleep_time', Protocol.SLEEP_TIME, payload)

    def set_label_style(self, label_style: Mapping, force=False):
        ''' Accepts HomeDeck's label_style (align, color, font_name,
        show_title, size, weight). '''
        payload = label_style_payload(
            align=label_style.get('align', 'bottom'),
            color=label_style.get('color', 'FFFFFF'),
            font_name=label_style.get('font_name') or 'Roboto',
            show_title=label_style.get('show_title', True),
            size=label_style.get('size', 10),
            weight=label_style.get('weight', 80),
        )
        if not force and payload == self._label_style:
            return

        self._label_style = payload
        self._put('label_style', Protocol.LABEL_STYLE, payload)

    # -- small window -------------------------------------------------------

    @property
    def small_window_mode(self) -> SmallWindowMode:
        return self._small_window_mode

    def set_small_window_mode(self, mode):
        try:
            self._small_window_mode = SmallWindowMode(mode)
        except ValueError:
            self._small_window_mode = SmallWindowMode.CLOCK

    def set_small_window_data(self, *, cpu: int = 0, mem: int = 0, gpu: int = 0, now: Optional[datetime] = None,
                              date: Optional[str] = None):
        ''' Redraw the small window. Sent once a second, it is also the
        keep-alive. '''
        self._small_window_stats = {'cpu': cpu, 'mem': mem, 'gpu': gpu}
        payload = small_window_payload(self._small_window_mode, cpu=cpu, mem=mem, gpu=gpu, now=now or self._now(),
                                       clock_24h=self.clock_24h, date=date)
        self._put('small_window', Protocol.SMALL_WINDOW, payload)

    def restore_small_window(self):
        self.set_small_window_data(**self._small_window_stats)

    def keep_alive(self):
        self.restore_small_window()

    # -- buttons ------------------------------------------------------------

    def set_buttons(self, buttons: Mapping[int, Optional[dict]], *, update_only=False):
        ''' Draw `buttons` ({index: {'name', 'icon'} or None}).

        A full page (the default) is the whole grid; slots it leaves out keep
        what they showed. update_only sends just these keys. '''
        buttons = {int(i): (dict(b) if b else None) for i, b in buttons.items()}
        if update_only:
            self._outbox.put(_Job('page_update', Protocol.UPDATE_BUTTONS, buttons=buttons))
        else:
            self._outbox.put(_Job('page', Protocol.BUTTONS, buttons=buttons))

    # -- other commands -----------------------------------------------------

    async def request_device_info(self, timeout: float = 1.0) -> Optional[DeviceInfo]:
        self._info_event.clear()
        self._info_requested_at = time.monotonic()
        self._put(None, Protocol.DEVICE_INFO)

        loop = asyncio.get_running_loop()
        if not await loop.run_in_executor(None, self._info_event.wait, timeout):
            return None
        return self.device_info

    def show_alert(self, index: int):
        ''' Flash the deck's built-in "!" on a key, as Ulanzi Studio does when
        an action fails. '''
        self._put(None, Protocol.RESULT_OVERLAY, result_overlay_payload(index))

    def set_screensaver(self, image: Union[str, bytes], *, filename: Optional[str] = None):
        ''' Upload the firmware screensaver image: a path or the file's bytes.

        Ulanzi Studio sends a JPEG (1220x708 in the capture); anything that
        isn't one is converted. Ulanzi's own default is a 260x260 PNG logo in
        System/Images/ScreenSave. '''
        if isinstance(image, str):
            filename = filename or os.path.basename(image)
            with open(image, 'rb') as fp:
                image = fp.read()

        if image[:3] != b'\xff\xd8\xff':
            from PIL import Image
            with Image.open(io.BytesIO(image)) as img:
                buf = io.BytesIO()
                img.convert('RGB').save(buf, 'JPEG', quality=90)
            image = buf.getvalue()
            filename = os.path.splitext(filename or 'screensaver')[0] + '.jpg'

        self._put('screensaver', Protocol.SCREENSAVER, screensaver_payload(image, filename or 'screensaver.jpg'))

    def lock_screen(self):
        ''' Show the screensaver image. While locked the deck reports no key
        presses; unlock_screen() ends it. '''
        self._put(None, Protocol.LOCK_SCREEN)

    def unlock_screen(self):
        ''' Dismiss the lock screen so keys report again. '''
        self._put(None, Protocol.UNLOCK_SCREEN)

    def send_raw(self, protocol: int, payload: Union[bytes, str] = b''):
        ''' Send any frame. For experiments with what isn't wrapped yet. '''
        if protocol == Protocol.FIRMWARE:
            raise ValueError('firmware updates are not supported here; use Ulanzi Studio')

        if isinstance(payload, str):
            payload = payload.encode('utf-8')
        self._put(None, protocol, payload)

    # -- events -------------------------------------------------------------

    async def read_packet(self):
        ''' Yield KeyEvent (ButtonAction) and DeviceInfoEvent as they arrive.
        Raises DeviceDisconnected when the deck stops answering. '''
        loop = asyncio.get_running_loop()
        if self._loop is not loop or self._wakeup is None:
            self._loop = loop
            self._wakeup = asyncio.Event()

        while True:
            while self._events:
                item = self._events.popleft()
                if isinstance(item, BaseException):
                    raise item
                yield item

            if self._read_error:
                raise DeviceDisconnected(str(self._read_error))
            if self._closing:
                return

            self._wakeup.clear()
            if not self._events:
                await self._wakeup.wait()

    # -- threads ------------------------------------------------------------

    def _put(self, key: Optional[str], protocol: int, payload: bytes = b''):
        self._outbox.put(_Job(key, protocol, payload))

    def _post(self, item):
        self._events.append(item)
        loop, wakeup = self._loop, self._wakeup
        if loop is not None and wakeup is not None:
            try:
                loop.call_soon_threadsafe(wakeup.set)
            except RuntimeError:
                # The loop that was reading has gone; the next reader drains
                pass

    def _writer_main(self):
        while True:
            job = self._outbox.get()
            if job is None:
                return

            try:
                payload = job.payload
                if job.buttons is not None:
                    payload = build_page_zip(job.buttons, icon_dir=self._icon_dir)
                self._transport.send(job.protocol, payload)
            except Exception as e:
                if not self._closing:
                    self.last_error = e
                    if self._write_failed_since is None:
                        self._write_failed_since = time.time()
                        print(f'⚠️ D200 write failed: {e}')
            else:
                self._write_failed_since = None
            finally:
                self._outbox.done()

    def _reader_main(self):
        while not self._closing:
            try:
                frame = self._transport.read_frame()
            except TransportError as e:
                if not self._closing:
                    self._read_error = e
                    self._post(DeviceDisconnected(str(e)))
                return

            if frame is not None:
                try:
                    self._handle_frame(frame)
                except Exception as e:  # never let one bad frame stop reading
                    print('⚠️ D200 could not handle frame:', frame, e)

    def _handle_frame(self, frame):
        if frame.is_(Protocol.BUTTONS, CommandCode.EVENT):
            event = parse_key_event(frame.payload)
            if event:
                self._post(event)
        elif frame.protocol == Protocol.DEVICE_INFO:
            info = parse_device_info(frame.payload)
            if not info:
                return

            solicited = time.monotonic() - self._info_requested_at < self.SOLICITED_WINDOW
            self.device_info = info
            self._info_event.set()
            # The app re-sends both after every device info (its
            # PROTOCOL_CODE_GETBASE step); a restarted firmware has forgotten them
            if self._brightness is not None:
                self._put('brightness', Protocol.BRIGHTNESS, brightness_payload(self._brightness))
            if self._sleep_time is not None:
                self._put('sleep_time', Protocol.SLEEP_TIME, sleep_time_payload(self._sleep_time))
            self._post(DeviceInfoEvent(info=info, solicited=solicited))
        elif frame.is_(Protocol.LABEL_STYLE, CommandCode.EVENT):
            # The deck asking for the label style; answer as the app does
            if self._label_style is not None:
                self._put('label_style', Protocol.LABEL_STYLE, self._label_style)


def auto_connect(vendor_id: int = D200_VENDOR_ID, product_id: int = D200_PRODUCT_ID, **kwargs) -> Optional[UlanziD200]:
    ''' Open the first D200, or return None if there isn't one. Raises
    TransportError if one is there but can't be opened. '''
    try:
        return UlanziD200.open(vendor_id, product_id, **kwargs)
    except DeviceNotFound:
        return None
