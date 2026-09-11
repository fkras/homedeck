#!/usr/bin/env python3
"""Extract a SET_BUTTONS ZIP from a USB capture.

    pip install scapy
    python3 tools/extract_zip.py firstcapture.pcapng [out.zip]

The deck receives its button layout as a ZIP sent over many USB packets: the
first carries the 8-byte protocol header plus 1016 bytes, and the rest are raw
continuation data. tools/scan_d200.py skips it because the declared length
exceeds one frame, so this reassembles it instead.

The ZIP holds the button images and a manifest.json keyed by "{col}_{row}",
which is the clearest description of the deck's layout available.
"""
import io
import json
import sys
import zipfile

try:
    from scapy.all import rdpcap
except ImportError:
    sys.exit('scapy not installed: pip install scapy')

SET_BUTTONS = 0x0001


def usb_payloads(path):
    """Yield each USBPcap record's payload, stripping its header."""
    for packet in rdpcap(path):
        raw = bytes(packet)
        if len(raw) < 28:
            continue

        header_length = int.from_bytes(raw[0:2], 'little')
        yield raw[header_length:]


def extract(path):
    payloads = list(usb_payloads(path))

    for index, payload in enumerate(payloads):
        if payload[:2] != b'\x7c\x7c':
            continue
        if int.from_bytes(payload[2:4], 'big') != SET_BUTTONS:
            continue

        declared = int.from_bytes(payload[4:8], 'little')
        print(f'SET_BUTTONS at packet {index}, {declared} bytes')

        data = bytearray(payload[8:])
        cursor = index + 1
        while len(data) < declared and cursor < len(payloads):
            chunk = payloads[cursor]
            # Continuation packets are raw data; a new framed packet ends it
            if chunk and chunk[:2] != b'\x7c\x7c':
                data += chunk
            cursor += 1

        return bytes(data[:declared])

    return None


def main():
    path = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else 'buttons.zip'

    data = extract(path)
    if not data:
        sys.exit('No SET_BUTTONS transfer found in that capture.')

    with open(out, 'wb') as fp:
        fp.write(data)
    print(f'wrote {out} ({len(data)} bytes)\n')

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        sys.exit('Reassembled data is not a valid ZIP - the capture may be truncated.')

    for name in archive.namelist():
        print(f'  {name:48} {archive.getinfo(name).file_size:>8}')

    if 'manifest.json' in archive.namelist():
        manifest = json.loads(archive.read('manifest.json'))
        print(f'\nmanifest slots ({len(manifest)}): {sorted(manifest)}')
        for key in sorted(manifest):
            icon = manifest[key]['ViewParam'][0].get('Icon', '')
            print(f'  {key}  {"(blank)" if not icon else icon.split("/")[-1]}')


if __name__ == '__main__':
    main()
