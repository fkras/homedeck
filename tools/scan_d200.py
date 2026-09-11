#!/usr/bin/env python3
"""Scan a USB capture for Ulanzi D200 protocol packets.

Reads the capture as raw bytes and finds every framed packet, so it works on
pcap, pcapng and plain dumps without needing a pcap parser.

    python3 tools/scan_d200.py screensaver.pcapng

Commands reported as UNKNOWN are the interesting ones - see
guides/reverse-engineering-usb.md.
"""
import sys
import binascii
from collections import Counter

KNOWN = {
    0x0001: 'SET_BUTTONS',
    0x0006: 'SET_SMALL_WINDOW_DATA',
    0x000a: 'SET_BRIGHTNESS',
    0x000b: 'SET_LABEL_STYLE',
    0x000d: 'PARTIALLY_UPDATE_BUTTONS',
    0x0101: 'IN_BUTTON',
    0x0303: 'IN_DEVICE_INFO',
}


def scan(blob: bytes):
    """Yield (offset, command_id, length, payload) for each framed packet."""
    offset = 0
    while True:
        offset = blob.find(b'\x7c\x7c', offset)
        if offset == -1 or offset + 8 > len(blob):
            break

        command = int.from_bytes(blob[offset + 2:offset + 4], 'big')
        length = int.from_bytes(blob[offset + 4:offset + 8], 'little')

        # Sanity-check: the payload can't exceed the 1016-byte frame body
        if 0 < length <= 1016:
            yield offset, command, length, blob[offset + 8:offset + 8 + length]

        offset += 2


def main(path):
    # Read the file as raw bytes: works for pcap, pcapng and plain dumps
    blob = open(path, 'rb').read()

    seen = Counter()
    for offset, command, length, payload in scan(blob):
        seen[command] += 1
        name = KNOWN.get(command, '*** UNKNOWN ***')
        preview = payload[:48]
        printable = ''.join(chr(b) if 32 <= b < 127 else '.' for b in preview)
        print(f'@{offset:<8} cmd=0x{command:04x} len={length:<5} {name}')
        print(f'          hex   {binascii.hexlify(preview).decode()}')
        print(f'          ascii {printable}')
        print()

    print('=' * 60)
    print('Command summary:')
    for command, count in sorted(seen.items()):
        name = KNOWN.get(command, '*** UNKNOWN - INVESTIGATE ***')
        print(f'  0x{command:04x}  x{count:<5} {name}')


if __name__ == '__main__':
    main(sys.argv[1])

