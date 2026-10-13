"""The D200 wire protocol, checked against what Ulanzi Studio sends.

Expected bytes come from the official app: its HID writer (frame layout,
padding, report ID), its logs (payloads) and a USB capture. See
guides/d200-protocol.md.
"""

import io
import json
import zipfile
from datetime import datetime

import pytest

from homedeck.d200.protocol import (
    MAGIC,
    REPORT_SIZE,
    CommandCode,
    Protocol,
    SmallWindowMode,
    brightness_payload,
    build_manifest,
    build_page_zip,
    decode_report,
    encode_frame,
    frames_bytes,
    label_style_payload,
    parse_device_info,
    parse_key_event,
    sleep_time_payload,
    slot_key,
    small_window_date,
    small_window_payload,
)


class TestFraming:
    def test_brightness_frame_matches_the_capture(self):
        # Capture: 7c7c 000a 02000000 3836
        (report,) = encode_frame(Protocol.BRIGHTNESS, b'86')

        assert report[:1] == b'\x00'                       # report ID
        assert report[1:11] == bytes.fromhex('7c7c000a020000003836')
        assert report[11:13] == b'\x00\x00'                # trailing pair
        assert len(report) == 1 + REPORT_SIZE

    def test_every_report_carries_report_id_zero(self):
        reports = encode_frame(Protocol.BUTTONS, bytes(range(256)) * 20)

        assert all(r[0] == 0 and len(r) == 1 + REPORT_SIZE for r in reports)

    def test_only_the_first_report_has_a_header(self):
        payload = b'\x7c' * 3000
        reports = encode_frame(Protocol.BUTTONS, payload)

        stream = frames_bytes(reports)
        assert stream[:2] == MAGIC
        assert stream[8:8 + len(payload)] == payload
        assert int.from_bytes(stream[4:8], 'little') == len(payload)

    @pytest.mark.parametrize('length, reports', [
        (0, 1),
        (1014, 2),      # 8 + 1014 + 2 = 1024: the app still pads a whole report
        (1013, 1),
        (1015, 2),
        (2038, 3),      # 8 + 2038 + 2 = 2048
    ])
    def test_padding_follows_the_official_writer(self, length, reports):
        # malloc(len + 10), then pad by 0x400 - ((len + 10) & 0x3ff), always
        assert len(encode_frame(Protocol.BUTTONS, b'x' * length)) == reports

    def test_command_code_and_protocol_bytes(self):
        (report,) = encode_frame(0x1A, b'0', command_code=CommandCode.REQUEST)

        assert report[3] == CommandCode.REQUEST
        assert report[4] == 0x1A


class TestDecode:
    def test_key_event_report(self):
        report = bytes.fromhex('7c7c0101040000000106010100') + bytes(1011)

        frame = decode_report(report)

        assert frame.is_(Protocol.BUTTONS, CommandCode.EVENT)
        assert frame.payload == bytes([1, 6, 1, 1])

    def test_tolerates_a_leading_report_id(self):
        report = b'\x00' + bytes.fromhex('7c7c0303000000') + b'\x00'

        assert decode_report(report).protocol == Protocol.DEVICE_INFO

    def test_rejects_anything_without_magic(self):
        assert decode_report(bytes(64)) is None
        assert decode_report(b'\x7c') is None

    def test_rejects_absurd_lengths(self):
        report = MAGIC + b'\x03\x03' + (0x6400001).to_bytes(4, 'little')

        assert decode_report(report) is None


class TestKeyEvents:
    def test_press_and_release(self):
        # Log: "Keytap: key:1 position:12 type:1 status:1", then status:0
        press = parse_key_event(bytes([1, 12, 1, 1]))
        release = parse_key_event(bytes([1, 12, 1, 0]))

        assert press.index == 12 and press.pressed and press.state == 1
        assert release.index == 12 and not release.pressed

    def test_row_and_column(self):
        event = parse_key_event(bytes([1, 13, 1, 1]))

        assert (event.column, event.row) == (3, 2)      # the large tile, "3_2"

    def test_state_100_reads_as_1_like_the_app(self):
        assert parse_key_event(bytes([100, 0, 1, 1])).state == 1

    def test_short_payload(self):
        assert parse_key_event(b'\x01\x02') is None


class TestDeviceInfo:
    def test_parses_the_logged_reply(self):
        payload = (b'{\n    "SerialNumber": "02C51A015U3672946",\n    "Dversion": "5.3.6",\n'
                   b'    "error": "0",\n    "DeviceType": "D200",\n    "HardwareVersion": "SSD210V100"\n}\x00\x00')

        info = parse_device_info(payload)

        assert info.serial_number == '02C51A015U3672946'
        assert info.firmware_version == '5.3.6'
        assert info.device_type == 'D200'
        assert info.hardware_version == 'SSD210V100'

    def test_garbage(self):
        assert parse_device_info(b'not json') is None
        assert parse_device_info(b'[1, 2]') is None


class TestPayloads:
    def test_brightness(self):
        assert brightness_payload(86) == b'86'
        assert brightness_payload(250) == b'100'
        assert brightness_payload(-3) == b'0'

    def test_sleep_time(self):
        assert sleep_time_payload(0) == b'0'
        assert sleep_time_payload(15) == b'15'
        with pytest.raises(ValueError):
            sleep_time_payload(-1)

    def test_label_style_matches_the_capture(self):
        payload = label_style_payload(align='bottom', color='FFFFFF', font_name='Source Han Sans SC',
                                      show_title=True, size=10, weight=80)

        assert json.loads(payload) == {
            'Align': 'bottom', 'Color': 16777215, 'FontName': 'Source Han Sans SC',
            'ShowTitle': True, 'Size': 10, 'Weight': 80,
        }
        assert b' ' not in payload.replace(b'Source Han Sans SC', b'')

    def test_small_window_matches_the_logs(self):
        # Log: SendSmallWinInfoToDeck : "1|5|35|HH:MM:SS|0|12H|"
        now = datetime(2026, 10, 9, 15, 10, 28)

        payload = small_window_payload(SmallWindowMode.CLOCK, cpu=5, mem=35, gpu=0, now=now, clock_24h=False)

        assert payload == b'1|5|35|15:10:28|0|12H|'

    def test_small_window_has_seven_fields(self):
        payload = small_window_payload(SmallWindowMode.STATS, now=datetime(2026, 1, 1))

        assert payload.count(b'|') == 6

    @pytest.mark.parametrize('mode, expected', [
        (SmallWindowMode.CLOCK_2, '09/10/2026 Fri'),
        (SmallWindowMode.CLOCK_3, 'Fri'),
        (SmallWindowMode.CLOCK_4, '09/10/2026'),
        (SmallWindowMode.CLOCK_5, ''),
        (SmallWindowMode.CLOCK, ''),
        (SmallWindowMode.STATS, ''),
        (SmallWindowMode.BACKGROUND, ''),
    ])
    def test_date_field_per_mode(self, mode, expected):
        assert small_window_date(mode, datetime(2026, 10, 9, 12, 0)) == expected

    def test_capture_with_weekday(self):
        # Capture: 201|10|41|15:10:36|2|12H|Fri
        payload = small_window_payload(201, cpu=10, mem=41, gpu=2, now=datetime(2026, 10, 9, 15, 10, 36), clock_24h=False)

        assert payload == b'201|10|41|15:10:36|2|12H|Fri'


class TestPageZip:
    def test_slot_keys(self):
        assert [slot_key(i) for i in (0, 4, 5, 12, 13)] == ['0_0', '4_0', '0_1', '2_2', '3_2']

    def test_manifest(self):
        manifest = build_manifest({0: {'name': 'Lamp', 'icon': 'a.png'}, 13: None})

        assert manifest == {
            '0_0': {'State': 0, 'ViewParam': [{'Text': 'Lamp', 'Icon': 'icons/a.png'}]},
            '3_2': {'State': 0, 'ViewParam': [{}]},
        }

    def test_zip_contains_manifest_and_icons(self, tmp_path):
        (tmp_path / 'a.png').write_bytes(b'PNGDATA')

        data = build_page_zip({0: {'name': 'Lamp', 'icon': 'a.png'}, 1: None}, icon_dir=str(tmp_path))

        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert zf.read('icons/a.png') == b'PNGDATA'
            manifest = json.loads(zf.read('manifest.json'))
        assert manifest['0_0']['ViewParam'][0]['Icon'] == 'icons/a.png'
        assert manifest['1_0'] == {'State': 0, 'ViewParam': [{}]}

    def test_missing_icon_sends_the_key_without_one(self, tmp_path):
        data = build_page_zip({0: {'name': 'Lamp', 'icon': 'gone.png'}}, icon_dir=str(tmp_path))

        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            manifest = json.loads(zf.read('manifest.json'))
            assert zf.namelist() == ['manifest.json']
        assert manifest['0_0']['ViewParam'][0] == {'Text': 'Lamp'}

    def test_no_continuation_report_starts_with_magic(self, tmp_path, monkeypatch):
        import homedeck.d200.protocol as protocol

        calls = []
        real = protocol._continuation_starts_with_magic

        def first_time_unlucky(data):
            calls.append(data)
            return len(calls) == 1 or real(data)

        monkeypatch.setattr(protocol, '_continuation_starts_with_magic', first_time_unlucky)

        data = build_page_zip({0: {'name': 'x'}}, icon_dir=str(tmp_path))

        assert len(calls) == 2              # rebuilt once, with padding
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert 'pad.txt' in zf.namelist()


class TestOverlayAndScreensaver:
    def test_overlay_matches_the_obs_capture(self):
        # Capture (OBS action, OBS closed): {"0_2": {"KeyType": 1, "Progress": 100, "State": 1}}
        from homedeck.d200.protocol import result_overlay_payload

        with zipfile.ZipFile(io.BytesIO(result_overlay_payload(10))) as zf:
            assert zf.namelist() == ['manifest.json']
            assert json.loads(zf.read('manifest.json')) == {'0_2': {'KeyType': 1, 'Progress': 100, 'State': 1}}

    def test_screensaver_is_the_file_alone_under_its_name(self):
        # Capture / ScreenFiles.zip: one entry, "bliss 600dpi.jpg"
        from homedeck.d200.protocol import screensaver_payload

        with zipfile.ZipFile(io.BytesIO(screensaver_payload(b'\xff\xd8\xffJPEG', 'some/dir/bliss 600dpi.jpg'))) as zf:
            assert zf.namelist() == ['bliss 600dpi.jpg']
            assert zf.read('bliss 600dpi.jpg') == b'\xff\xd8\xffJPEG'
