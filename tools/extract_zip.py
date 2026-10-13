#!/usr/bin/env python3
"""Extract the ZIPs Ulanzi Studio sends to the deck from a USB capture.

    pip install scapy
    python3 tools/extract_zip.py capture.pcapng                 # page ZIPs (0x01)
    python3 tools/extract_zip.py capture.pcapng --protocol 0x09 # screensaver
    python3 tools/extract_zip.py capture.pcapng --protocol 0xe0 # result overlay
    python3 tools/extract_zip.py capture.pcapng --protocol all  # every ZIP-carrying frame

ZIPs span many USB packets: the first carries the 8-byte header (7c 7c,
command code, protocol ID, u32 length) plus 1016 bytes, the rest are raw
continuation data. This reassembles each one, writes it next to the capture as
<capture>-<protocol>-<n>.zip, and prints its file list and manifest.json.
See guides/d200-protocol.md.
"""
import argparse
import io
import json
import os
import sys
import zipfile

try:
    from scapy.all import rdpcap
except ImportError:
    sys.exit('scapy not installed: pip install scapy')

NAMES = {0x01: 'buttons', 0x09: 'screensaver', 0x0c: 'firmware', 0x0d: 'update', 0xe0: 'overlay'}


def usb_payloads(path):
    """Yield each USBPcap record's payload, stripping its header."""
    for packet in rdpcap(path):
        raw = bytes(packet)
        if len(raw) < 28:
            continue

        header_length = int.from_bytes(raw[0:2], 'little')
        yield raw[header_length:]


def extract(path, protocols):
    """Yield (protocol, packet index, data) for every host->device frame of
    those protocols."""
    payloads = list(usb_payloads(path))

    for index, payload in enumerate(payloads):
        # Host requests only: command code 0
        if payload[:2] != b'\x7c\x7c' or len(payload) < 8 or payload[2] != 0:
            continue
        protocol = payload[3]
        if protocol not in protocols:
            continue

        declared = int.from_bytes(payload[4:8], 'little')
        data = bytearray(payload[8:])
        cursor = index + 1
        while len(data) < declared and cursor < len(payloads):
            chunk = payloads[cursor]
            # Continuation packets are raw data; skip framed packets (the
            # deck's replies, or a small-window update squeezed in)
            if chunk and chunk[:2] != b'\x7c\x7c':
                data += chunk
            cursor += 1

        yield protocol, index, bytes(data[:declared])


def describe(data):
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        print('  not a valid ZIP - the capture may be truncated, or this is not a ZIP payload')
        print(f'  first bytes: {data[:64].hex()}')
        return

    for name in archive.namelist():
        print(f'  {name:48} {archive.getinfo(name).file_size:>8}')

    for name in archive.namelist():
        if name.endswith('.json'):
            print(f'\n  --- {name} ---')
            text = archive.read(name).decode('utf-8', errors='replace')
            try:
                text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
            except ValueError:
                pass
            print('  ' + text.replace('\n', '\n  '))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('capture')
    parser.add_argument('--protocol', default='0x01', help='protocol ID (e.g. 0x09, 0xe0) or "all"')
    args = parser.parse_args()

    protocols = set(NAMES) if args.protocol == 'all' else {int(args.protocol, 0)}
    base = os.path.splitext(args.capture)[0]

    found = 0
    for protocol, index, data in extract(args.capture, protocols):
        found += 1
        out = f'{base}-{NAMES.get(protocol, f"{protocol:02x}")}-{found}.zip'
        with open(out, 'wb') as fp:
            fp.write(data)
        print(f'\n0x{protocol:02x} at packet {index}: {len(data)} bytes -> {out}')
        describe(data)

    if not found:
        sys.exit(f'No frames with protocol {args.protocol} in that capture.')


if __name__ == '__main__':
    main()
