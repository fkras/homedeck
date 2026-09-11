#!/usr/bin/env python3
"""Probe the unknown 0x001a command.

    systemctl stop homedeck-server
    $HD_PY tools/probe_001a.py
    systemctl start homedeck-server

A capture of the official Ulanzi app connecting to a D200 shows it sends an
undocumented command 0x001a with a single ASCII "0", every time, right after
setting brightness:

    0x000b  SET_LABEL_STYLE
    0x0303  IN_DEVICE_INFO
    0x000a  SET_BRIGHTNESS    86
    0x001a  ???               "0"     <- this
    0x0001  SET_BUTTONS       (ZIP)

A one-byte 0/1 payload looks like a toggle, and the screensaver is the obvious
candidate. This sends "0" and then "1" with a pause between so the effect can
be observed.

Only values the official app was seen to send ("0") plus its obvious counterpart
("1") are tried. Nothing here writes firmware or sweeps unknown command ids.
"""
import sys
import time

try:
    from strmdck.device_manager import auto_connect
    from strmdck.devices.ulanzi_d200 import PacketStruct
except ImportError:
    sys.exit('Run this with the venv python ($HD_PY)')

COMMAND = 0x001A


def send(device, value):
    packet = PacketStruct.build(dict(
        command_protocol=COMMAND,
        length=None,
        data=str(value).encode('utf-8'),
    ))
    device._hid_device.write(packet)


def main():
    device = auto_connect()
    if not device:
        sys.exit('No deck found. Stop homedeck-server first so it releases the device.')

    print('Connected.\n')
    print('Watch the deck, and note anything that changes - the screensaver')
    print('behaviour, the small window, or the Ulanzi branding.\n')

    for value in ('0', '1', '0'):
        print(f'  sending 0x{COMMAND:04x} with payload "{value}" ...')
        send(device, value)
        time.sleep(8)

    print('\nLeft at "0", which is what the official app sends.')
    print('Now restart HomeDeck:  systemctl start homedeck-server')
    device.close()


if __name__ == '__main__':
    main()
