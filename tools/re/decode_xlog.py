#!/usr/bin/env python3
"""Decode Ulanzi Studio's logs to text.

    python3 tools/re/decode_xlog.py %APPDATA%/Ulanzi/UlanziDeck/logs/UlanziDeck_20261012.xlog out.txt

The logs are Tencent Mars xlog files. Ulanzi Studio writes the variant with
magic 0x09 (deflate, no encryption), so no key is needed. They are debug
builds of the log: every line carries the C++ file, line and function, and the
device code logs each frame it sends and receives:

    senddataqueue.h:174, sendToDeck][Sending data to deck: commandCode= 0 , protocolID= 1 ...
    UlanziDeck.cpp:5115, OnDeckFrameReceived][Keytap: key:1 position:12 type:1 status:1

Block layout: magic(1) seq(2) begin_hour(1) end_hour(1) length(4, LE)
crypt_key(64), then `length` bytes, then 0x00.
"""
import struct
import sys
import zlib

NO_COMPRESS = {0x03, 0x06, 0x08}
COMPRESS = {0x04, 0x05, 0x07, 0x09}
HEADER = 1 + 2 + 1 + 1 + 4 + 64


def decode(blob: bytes) -> bytes:
    out = []
    i = 0
    while i + HEADER <= len(blob):
        magic = blob[i]
        if magic not in NO_COMPRESS | COMPRESS:
            i += 1
            continue

        (length,) = struct.unpack_from('<I', blob, i + 5)
        start = i + HEADER
        end = start + length
        if end >= len(blob) or blob[end] != 0x00:
            i += 1
            continue

        data = blob[start:end]
        if magic in COMPRESS:
            try:
                data = zlib.decompressobj(-zlib.MAX_WBITS).decompress(data)
            except zlib.error as e:
                data = f'[block at {i}: {e}]\n'.encode()

        out.append(data)
        i = end + 1

    return b''.join(out)


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)

    with open(sys.argv[1], 'rb') as fp:
        text = decode(fp.read())
    with open(sys.argv[2], 'wb') as fp:
        fp.write(text)
    print(f'{sys.argv[1]}: {len(text)} bytes of text')


if __name__ == '__main__':
    main()
