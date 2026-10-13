#!/usr/bin/env python3
"""Talk to a D200 by hand through HomeDeck's communications layer.

Stop HomeDeck first so the two don't fight over the device:

    systemctl stop homedeck
    $HD_PY tools/d200_cli.py info
    systemctl start homedeck

Commands:

    info                      device type, serial, firmware
    brightness 0-100
    sleep MINUTES             the deck's sleep-time setting (0x1a; 0/5/15/30/60/120 in the app)
    small-window MODE         stats, clock, background, clock2..clock5, or a number
    small-window-tour         show every small-window mode for a few seconds each
    lock                      show the screensaver; keys stop reporting until unlock
    unlock                    dismiss it
    alert KEY                 flash the deck's "!" on a key (0-12)
    screensaver IMAGE         upload the firmware screensaver image
    listen [SECONDS]          print key presses (default 30s)
    raw PROTOCOL [PAYLOAD]    send any frame, e.g. "raw 0x0a 50" (firmware refused)

See guides/d200-protocol.md for what each one is.
"""
import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

try:
    from homedeck.d200 import D200_SMALL_WINDOW_MODES, DeviceInfoEvent, KeyEvent, SmallWindowMode, TransportError, UlanziD200
except ImportError as e:
    sys.exit(f'Run this with the venv python ($HD_PY): {e}')

MODE_NAMES = {
    'stats': SmallWindowMode.STATS,
    'clock': SmallWindowMode.CLOCK,
    'background': SmallWindowMode.BACKGROUND,
    'clock2': SmallWindowMode.CLOCK_2,
    'clock3': SmallWindowMode.CLOCK_3,
    'clock4': SmallWindowMode.CLOCK_4,
    'clock5': SmallWindowMode.CLOCK_5,
}


def parse_mode(value: str) -> SmallWindowMode:
    if value.lower() in MODE_NAMES:
        return MODE_NAMES[value.lower()]
    return SmallWindowMode(int(value, 0))


async def run(args):
    vendor = int(os.getenv('DECK_VENDOR_ID') or '2207', 16)
    product = int(os.getenv('DECK_PRODUCT_ID') or '0019', 16)
    try:
        device = UlanziD200.open(vendor, product)
    except TransportError as e:
        sys.exit(f'Could not open the deck ({e}). Is HomeDeck still running?')

    try:
        if args.command == 'info':
            info = await device.request_device_info(timeout=2)
            if not info:
                sys.exit('The deck did not answer.')
            for key, value in info.raw.items():
                print(f'  {key:16} {value}')

        elif args.command == 'brightness':
            device.set_brightness(int(args.value))

        elif args.command == 'sleep':
            device.set_sleep_time(int(args.value))

        elif args.command == 'small-window':
            device.set_small_window_mode(parse_mode(args.value))
            device.set_small_window_data(cpu=12, mem=34, gpu=5)

        elif args.command == 'small-window-tour':
            for mode in D200_SMALL_WINDOW_MODES:
                print(f'  {mode.value:>3}  {mode.name}')
                device.set_small_window_mode(mode)
                end = time.time() + 4
                while time.time() < end:
                    device.set_small_window_data(cpu=12, mem=34, gpu=5)
                    await asyncio.sleep(1)

        elif args.command == 'lock':
            device.lock_screen()
            print("Locked. Keys won't report until: d200_cli.py unlock")

        elif args.command == 'unlock':
            device.unlock_screen()

        elif args.command == 'alert':
            device.show_alert(int(args.value))

        elif args.command == 'screensaver':
            device.set_screensaver(args.value)
            print('Uploading (a few seconds for a large image)...')

        elif args.command == 'listen':
            print(f'Press keys for {args.value or 30}s...')

            async def listen():
                async for event in device.read_packet():
                    if isinstance(event, KeyEvent):
                        what = 'down' if event.pressed else 'up  '
                        print(f'  key {event.index:2} ({event.column}_{event.row}) {what} state={event.state} type={event.kind}')
                    elif isinstance(event, DeviceInfoEvent):
                        print(f'  device info (solicited={event.solicited}): {event.info.raw}')

            try:
                await asyncio.wait_for(listen(), float(args.value or 30))
            except asyncio.TimeoutError:
                pass

        elif args.command == 'raw':
            device.send_raw(int(args.value, 0), (args.payload or '').encode())

        await device.flush(10)
    finally:
        device.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=['info', 'brightness', 'sleep', 'small-window', 'small-window-tour', 'lock', 'unlock', 'alert', 'screensaver', 'listen', 'raw'])
    parser.add_argument('value', nargs='?')
    parser.add_argument('payload', nargs='?')
    args = parser.parse_args()

    if args.command in ('brightness', 'sleep', 'small-window', 'alert', 'screensaver', 'raw') and args.value is None:
        parser.error(f'{args.command} needs a value')

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    asyncio.run(run(args))


if __name__ == '__main__':
    main()
