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

# (command code, protocol id) -> name. Byte 2 of the header is the command
# code (0 host request, 1 device event, 3 device reply), byte 3 the protocol.
# See guides/d200-protocol.md.
KNOWN = {
    (0, 0x01): 'SET_BUTTONS (page ZIP)',
    (0, 0x03): 'GET_DEVICE_INFO',
    (0, 0x04): 'LOCK_SCREEN',
    (0, 0x05): 'UNLOCK_SCREEN (never sent by the app)',
    (0, 0x06): 'SMALL_WINDOW',
    (0, 0x09): 'SCREENSAVER (ZIP)',
    (0, 0x0a): 'BRIGHTNESS',
    (0, 0x0b): 'LABEL_STYLE',
    (0, 0x0c): 'FIRMWARE (bin.zip)',
    (0, 0x0d): 'UPDATE_BUTTONS (partial ZIP)',
    (0, 0x1a): 'SLEEP_TIME (minutes, 0 = never)',
    (0, 0xe0): 'RESULT_OVERLAY (ZIP)',
    (1, 0x01): 'IN_KEY_EVENT',
    (1, 0x0b): 'IN_LABEL_STYLE_REQUEST',
    (3, 0x03): 'IN_DEVICE_INFO',
}


def scan(blob: bytes):
    """Yield (offset, command_id, length, payload) for each framed packet."""
    offset = 0
    while True:
        offset = blob.find(b'\x7c\x7c', offset)
        if offset == -1 or offset + 8 > len(blob):
            break

        command = (blob[offset + 2], blob[offset + 3])
        length = int.from_bytes(blob[offset + 4:offset + 8], 'little')

        # Sanity-check: ZIPs span many reports, but nothing is near 100 MB
        if command[0] in (0, 1, 3) and 0 <= length <= 0x6400000:
            yield offset, command, length, blob[offset + 8:offset + 8 + min(length, 1016)]

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
        print(f'@{offset:<8} cc={command[0]} pid=0x{command[1]:02x} len={length:<7} {name}')
        print(f'          hex   {binascii.hexlify(preview).decode()}')
        print(f'          ascii {printable}')
        print()

    print('=' * 60)
    print('Command summary:')
    for command, count in sorted(seen.items()):
        name = KNOWN.get(command, '*** UNKNOWN - INVESTIGATE ***')
        print(f'  cc={command[0]} pid=0x{command[1]:02x}  x{count:<5} {name}')


if __name__ == '__main__':
    main(sys.argv[1])

