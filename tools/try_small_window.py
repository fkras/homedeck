#!/usr/bin/env python3
"""Send small-window payloads to the D200 to explore its modes.

Stop HomeDeck first so the two don't fight over the device:

    systemctl stop homedeck-server
    $HD_PY tools/try_small_window.py

A USB capture of the official Ulanzi app shows it sends more fields than
strmdck implements, and a mode value strmdck doesn't know about:

    strmdck:  2|26|41|15:10:28              mode|cpu|mem|time|gpu
    Ulanzi:   2|26|41|15:10:28|4|12H|       two extra fields
    Ulanzi:   201|10|41|15:10:36|2|12H|Fri  mode 201, plus a weekday

This sends each variant with a pause between so you can watch the screen and
see which one changes the big button.
"""
import sys
import time
from datetime import datetime

try:
    from strmdck.device_manager import auto_connect
    from strmdck.devices.ulanzi_d200 import CommandProtocol, PacketStruct
except ImportError:
    sys.exit('Run this with the venv python ($HD_PY)')


def send(device, payload, note):
    packet = PacketStruct.build(dict(
        command_protocol=CommandProtocol.OUT_SET_SMALL_WINDOW_DATA.value,
        length=None,
        data=payload.encode('utf-8'),
    ))
    device._hid_device.write(packet)
    print(f'  {payload:38}  {note}')


def main():
    device = auto_connect()
    if not device:
        sys.exit('No deck found. Is HomeDeck still running and holding it?')

    print('Connected. Watch the big button after each line.\n')

    now = datetime.now()
    clock = now.strftime('%H:%M:%S')
    weekday = now.strftime('%a')

    variants = [
        (f'0|12|41|{clock}|0|12H|', 'mode 0   - stats?'),
        (f'1|12|41|{clock}|0|12H|', 'mode 1   - clock?'),
        (f'2|12|41|{clock}|0|12H|', 'mode 2   - what the app sends most'),
        (f'201|12|41|{clock}|0|12H|{weekday}', 'mode 201 - what the app sent for the other view'),
        (f'200|12|41|{clock}|0|12H|{weekday}', 'mode 200 - guess, 201 minus one'),
        (f'202|12|41|{clock}|0|12H|{weekday}', 'mode 202 - guess, 201 plus one'),
    ]

    for payload, note in variants:
        send(device, payload, note)
        time.sleep(4)

    print('\nDone. Note which line changed the display, then Ctrl+C and report back.')
    device.close()


if __name__ == '__main__':
    main()
