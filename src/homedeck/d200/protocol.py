''' Wire protocol of the Ulanzi D200, as spoken by the official Ulanzi Studio.

Everything here is pure - no I/O - so it can be tested without a deck.
See guides/d200-protocol.md for where each detail comes from.

Framing
-------
Every message is a frame:

    offset  size  field
    0       2     magic 7c 7c
    2       1     command code   (0 = host request, 1 = device event, 3 = device reply)
    3       1     protocol id    (what the frame is about, see Protocol)
    4       4     payload length, little-endian
    8       n     payload
    8+n     2     00 00
                  zero padding up to the next multiple of 1024

The frame is written as consecutive 1024-byte HID output reports, each
prefixed with report ID 0. Only the first report carries the header; the rest
are raw continuation. Frames from the device always fit one 1024-byte report.
'''
from __future__ import annotations

import io
import json
import os
import struct
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Dict, Iterable, List, Mapping, Optional, Union

MAGIC = b'\x7c\x7c'
HEADER_SIZE = 8
REPORT_SIZE = 1024
REPORT_ID = 0
# The official reader drops frames that claim more than this
MAX_FRAME_LENGTH = 0x6400000


class CommandCode(IntEnum):
    ''' Byte 2 of the header: who is talking and why. '''
    REQUEST = 0     # host -> device; everything the host sends
    EVENT = 1       # device -> host; key presses, label style requests
    REPLY = 3       # device -> host; answers to DEVICE_INFO


class Protocol(IntEnum):
    ''' Byte 3 of the header.

    Names follow the slot or function in UlanziDeck.exe that sends them.
    '''
    # Host -> device: ZIP of manifest.json + icons for the whole grid.
    # Device -> host (command code EVENT): a key press or release.
    BUTTONS = 0x01
    # Host -> device, empty payload: ask for the device info JSON.
    # Device -> host (command code REPLY): the JSON.
    DEVICE_INFO = 0x03
    # Host -> device, empty payload. Shows the screensaver image, and the deck
    # stops reporting key presses until unlocked. Sent when the PC locks.
    LOCK_SCREEN = 0x04
    # Host -> device, empty payload. Dismisses the lock screen; keys report
    # again (tested on firmware 5.3.6). The app computes it but never sends
    # it - it re-sends the page instead.
    UNLOCK_SCREEN = 0x05
    # Host -> device: "mode|cpu|mem|HH:MM:SS|gpu|12H/24H|date", once a second.
    SMALL_WINDOW = 0x06
    # Host -> device: ZIP holding the screensaver image file (one JPEG in
    # every capture). When the firmware shows it isn't confirmed.
    SCREENSAVER = 0x09
    # Host -> device: decimal string, 0-100.
    BRIGHTNESS = 0x0A
    # Host -> device: label style JSON.
    # Device -> host (command code EVENT): asks for the label style.
    LABEL_STYLE = 0x0B
    # Host -> device: firmware image (bin.zip). Deliberately not exposed.
    FIRMWARE = 0x0C
    # Host -> device: ZIP like BUTTONS, only the keys that changed.
    UPDATE_BUTTONS = 0x0D
    # Host -> device: decimal string, minutes before the firmware blanks the
    # screen. "0" disables it. The app offers 0, 5, 15, 30, 60, 120.
    SLEEP_TIME = 0x1A
    # Host -> device: ZIP whose manifest names a key; the firmware flashes its
    # built-in "!" on it. Ulanzi Studio sends it when an action fails.
    RESULT_OVERLAY = 0xE0


class SmallWindowMode(IntEnum):
    ''' What the large tile (key 13, "3_2") shows.

    Values are the radio button IDs in SystemSmallWindowsEditor.
    '''
    STATS = 0           # radio_cpu: CPU / RAM / GPU percentages
    CLOCK = 1           # radio_time1
    BACKGROUND = 2      # radio_img: whatever the page ZIP draws in 3_2
    KNOB = 100          # radio_knob: dial devices only
    CLOCK_2 = 200       # radio_time2: date and weekday
    CLOCK_3 = 201       # radio_time3: weekday
    CLOCK_4 = 202       # radio_time4: date
    CLOCK_5 = 203       # radio_time5


# Modes a D200 can show (KNOB is for the dial)
D200_SMALL_WINDOW_MODES = (
    SmallWindowMode.STATS,
    SmallWindowMode.CLOCK,
    SmallWindowMode.BACKGROUND,
    SmallWindowMode.CLOCK_2,
    SmallWindowMode.CLOCK_3,
    SmallWindowMode.CLOCK_4,
    SmallWindowMode.CLOCK_5,
)

# Firmware sleep timeouts the official app offers, in minutes. 0 = never.
SLEEP_TIME_CHOICES = (0, 5, 15, 30, 60, 120)


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Frame:
    command_code: int
    protocol: int
    payload: bytes = b''

    def is_(self, protocol: Protocol, command_code: CommandCode) -> bool:
        return self.protocol == protocol and self.command_code == command_code


def encode_frame(protocol: int, payload: Union[bytes, str] = b'', *, command_code: int = CommandCode.REQUEST) -> List[bytes]:
    ''' Encode one frame as the HID output reports to write, in order.

    Each report is 1025 bytes: report ID 0, then 1024 bytes of frame.

    The leading report ID matters. hidapi, and Linux usbhid underneath it,
    treat the first byte as the report ID and strip it only when it is 0x00.
    Writing bare 1024-byte chunks - as strmdck did - sends the header chunk
    intact (it starts with 0x7c) but eats the first byte of any continuation
    chunk that happens to start with 0x00, shifting the rest of the ZIP.
    '''
    if isinstance(payload, str):
        payload = payload.encode('utf-8')

    header = MAGIC + bytes([command_code & 0xFF, protocol & 0xFF]) + struct.pack('<I', len(payload))
    body = header + payload + b'\x00\x00'
    # Same arithmetic as the official writer: always pad, so a body that is
    # already a multiple of 1024 gains one extra empty report
    body += b'\x00' * (REPORT_SIZE - (len(body) % REPORT_SIZE))

    prefix = bytes([REPORT_ID])
    return [prefix + body[i:i + REPORT_SIZE] for i in range(0, len(body), REPORT_SIZE)]


def decode_report(report: Union[bytes, bytearray, List[int]]) -> Optional[Frame]:
    ''' Decode one input report into a Frame, or None if it isn't one. '''
    data = bytes(report)
    # hidapi strips report ID 0 on input, but tolerate it being left in
    if data[:1] == b'\x00' and data[1:3] == MAGIC:
        data = data[1:]

    if len(data) < HEADER_SIZE or data[:2] != MAGIC:
        return None

    length = struct.unpack_from('<I', data, 4)[0]
    if length > MAX_FRAME_LENGTH:
        return None

    return Frame(command_code=data[2], protocol=data[3], payload=data[HEADER_SIZE:HEADER_SIZE + length])


# ---------------------------------------------------------------------------
# Device -> host payloads
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KeyEvent:
    ''' A key press or release.

    The payload is four bytes, logged by the app as
    "Keytap: key:%d position:%d type:%d status:%d".
    '''
    index: int          # position: row * 5 + column; 13 is the large tile
    pressed: bool       # status: 1 pressed, 0 released
    state: int          # "key": 1 for normal keys; for the large tile, the
                        # small-window mode the firmware is showing
    kind: int = 1       # "type": 1 in every capture

    @property
    def row(self) -> int:
        return self.index // 5

    @property
    def column(self) -> int:
        return self.index % 5


def parse_key_event(payload: bytes) -> Optional[KeyEvent]:
    if len(payload) < 4:
        return None

    state, index, kind, status = payload[0], payload[1], payload[2], payload[3]
    # The app maps 100 to 1 for D200 and D200H before acting on it
    if state == 100:
        state = 1

    return KeyEvent(index=index, pressed=status == 1, state=state, kind=kind)


@dataclass(frozen=True)
class DeviceInfo:
    serial_number: str = ''
    firmware_version: str = ''
    device_type: str = ''
    hardware_version: str = ''
    error: str = ''
    raw: dict = field(default_factory=dict, compare=False)


def parse_device_info(payload: bytes) -> Optional[DeviceInfo]:
    ''' {"SerialNumber": ..., "Dversion": "5.3.6", "error": "0",
    "DeviceType": "D200", "HardwareVersion": "SSD210V100"} '''
    text = payload.split(b'\x00', 1)[0].decode('utf-8', errors='replace').strip()
    try:
        data = json.loads(text)
    except ValueError:
        return None

    if not isinstance(data, dict):
        return None

    return DeviceInfo(
        serial_number=str(data.get('SerialNumber', '')),
        firmware_version=str(data.get('Dversion', '')),
        device_type=str(data.get('DeviceType', '')),
        hardware_version=str(data.get('HardwareVersion', '')),
        error=str(data.get('error', '')),
        raw=data,
    )


# ---------------------------------------------------------------------------
# Host -> device payloads
# ---------------------------------------------------------------------------

def brightness_payload(brightness: int) -> bytes:
    return str(max(0, min(100, int(brightness)))).encode('ascii')


def sleep_time_payload(minutes: int) -> bytes:
    minutes = int(minutes)
    if minutes < 0:
        raise ValueError(f'sleep time must be >= 0 minutes, got {minutes}')

    return str(minutes).encode('ascii')


def label_style_payload(*, align: str = 'bottom', color: Union[int, str] = 0xFFFFFF, font_name: str = 'Roboto',
                        show_title: bool = True, size: int = 10, weight: int = 80) -> bytes:
    ''' {"Align":"bottom","Color":16777215,"FontName":"...","ShowTitle":true,"Size":10,"Weight":80} '''
    if isinstance(color, str):
        color = int(color.lstrip('#'), 16)

    style = {
        'Align': align,
        'Color': int(color),
        'FontName': font_name,
        'ShowTitle': bool(show_title),
        'Size': int(size),
        'Weight': int(weight),
    }
    return json.dumps(style, separators=(',', ':')).encode('utf-8')


def small_window_date(mode: int, now: datetime, *, date_format: str = '%d/%m/%Y', weekday_format: str = '%a') -> str:
    ''' The seventh small-window field. Only the clock styles that show a date
    fill it; the official app sends an empty string for every other mode. '''
    if mode == SmallWindowMode.CLOCK_2:
        return f'{now.strftime(date_format)} {now.strftime(weekday_format)}'
    if mode == SmallWindowMode.CLOCK_3:
        return now.strftime(weekday_format)
    if mode == SmallWindowMode.CLOCK_4:
        return now.strftime(date_format)
    return ''


def small_window_payload(mode: int, *, cpu: int = 0, mem: int = 0, gpu: int = 0, now: Optional[datetime] = None,
                         clock_24h: bool = True, date: Optional[str] = None) -> bytes:
    ''' "mode|cpu|mem|HH:MM:SS|gpu|24H|date" - all seven fields, as the app
    sends them. `date` defaults to what the app would show for `mode`. '''
    now = now or datetime.now()
    mode = int(mode)
    if date is None:
        date = small_window_date(mode, now)

    fields = [
        str(mode),
        str(int(cpu)),
        str(int(mem)),
        now.strftime('%H:%M:%S'),
        str(int(gpu)),
        '24H' if clock_24h else '12H',
        date,
    ]
    return '|'.join(fields).encode('utf-8')


# ---------------------------------------------------------------------------
# Page ZIP
# ---------------------------------------------------------------------------

GRID_COLUMNS = 5
GRID_ROWS = 3


def slot_key(index: int) -> str:
    ''' Manifest key for a key index: "{column}_{row}". Index 13 is "3_2". '''
    return f'{index % GRID_COLUMNS}_{index // GRID_COLUMNS}'


def build_manifest(buttons: Mapping[int, Optional[dict]]) -> Dict[str, dict]:
    ''' Manifest for {index: {'name': ..., 'icon': ...} or None}.

    `icon` is the file name inside the ZIP's icons/ folder; a None button is
    sent as an explicit blank so the slot is cleared.
    '''
    manifest = {}
    for index, button in buttons.items():
        view = {}
        if button:
            if button.get('name'):
                view['Text'] = button['name']
            if button.get('icon'):
                view['Icon'] = f'icons/{button["icon"]}'

        manifest[slot_key(int(index))] = {'State': 0, 'ViewParam': [view]}

    return manifest


def _continuation_starts_with_magic(data: bytes) -> bool:
    # The first report carries 1016 bytes of payload after the header, the
    # rest 1024 each
    first = REPORT_SIZE - HEADER_SIZE
    return any(data[i:i + 2] == MAGIC for i in range(first, len(data), REPORT_SIZE))


def build_page_zip(buttons: Mapping[int, Optional[dict]], *, icon_dir: str) -> bytes:
    ''' ZIP for BUTTONS / UPDATE_BUTTONS: manifest.json plus icons/<file>.

    Icons are read from `icon_dir`. A missing icon file sends the key without
    an image rather than failing the page.
    '''
    buttons = dict(buttons)
    icons = {}
    for button in buttons.values():
        name = button.get('icon') if button else None
        if not name or name in icons:
            continue

        try:
            with open(os.path.join(icon_dir, name), 'rb') as fp:
                icons[name] = fp.read()
        except OSError:
            icons[name] = None

    for index, button in buttons.items():
        if button and button.get('icon') and icons.get(button['icon']) is None:
            buttons[index] = {k: v for k, v in button.items() if k != 'icon'}

    manifest = json.dumps(build_manifest(buttons), sort_keys=True, separators=(',', ':'), indent=2)

    padding = b''
    while True:
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
            if padding:
                zf.writestr('pad.txt', padding)
            zf.writestr('manifest.json', manifest)
            for name, data in icons.items():
                if data is not None:
                    zf.writestr(f'icons/{name}', data)

        data = output.getvalue()
        # The official app doesn't guard against this and nothing suggests the
        # firmware resyncs on magic mid-frame. It costs one cheap check, so a
        # continuation report never starts with what looks like a header.
        if not _continuation_starts_with_magic(data) or len(padding) > 64:
            return data

        padding += b' '


def result_overlay_payload(index: int, *, state: int = 1, progress: int = 100, key_type: int = 1) -> bytes:
    ''' ZIP for RESULT_OVERLAY: the deck flashes its built-in "!" on a key.

    From a capture of Ulanzi Studio (an OBS action with OBS closed):
    {"0_2": {"KeyType": 1, "Progress": 100, "State": 1}}, no images. On
    firmware 5.3.6 every State, Progress and KeyType tried showed the same
    "!", so the values the app sends are kept. There is no check mark. '''
    manifest = {slot_key(int(index)): {'KeyType': int(key_type), 'Progress': int(progress), 'State': int(state)}}
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('manifest.json', json.dumps(manifest, separators=(',', ':')))
    return output.getvalue()


def screensaver_payload(image: bytes, filename: str = 'screensaver.jpg') -> bytes:
    ''' ZIP for SCREENSAVER: the image file alone, under its own name.

    Byte-identical to the ScreenFiles.zip Ulanzi Studio keeps in
    %APPDATA%/Ulanzi/UlanziDeck and sends: one 1220x708 JPEG. '''
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(os.path.basename(filename), image)
    return output.getvalue()


def frames_bytes(reports: Iterable[bytes]) -> bytes:
    ''' Join reports back into the frame stream (without report IDs). '''
    return b''.join(report[1:] for report in reports)
