#!/usr/bin/env python3
"""Report what HomeDeck can see of the connected deck.

    $HD_PY tools/diagnose_device.py

Lists every HID device, flags which ones HomeDeck would match, and says why a
deck is or isn't being picked up. Useful after a firmware update, which can
change the USB product ID and make auto_connect() find nothing even though the
hardware is healthy.
"""
import sys

try:
    import hid
except ImportError:
    sys.exit('hidapi not installed - run this with the venv python ($HD_PY)')

try:
    from strmdck.device_manager import DEVICE_MAP
except ImportError:
    sys.exit('strmdck not installed - run this with the venv python ($HD_PY)')

ULANZI_VENDOR_ID = 0x2207


def main():
    devices = list(hid.enumerate())
    print(f'{len(devices)} HID device(s) visible\n')

    known = set(DEVICE_MAP)
    print('HomeDeck matches exactly these (vendor, product) pairs:')
    for vendor, product in sorted(known):
        print(f'  0x{vendor:04x}:0x{product:04x}  {DEVICE_MAP[(vendor, product)].__name__}')
    print()

    matched = []
    ulanzi = []

    for device in devices:
        vendor = device['vendor_id']
        product = device['product_id']
        pair = (vendor, product)

        if pair in known:
            matched.append(device)
        if vendor == ULANZI_VENDOR_ID:
            ulanzi.append(device)

    print('--- Ulanzi devices on the bus (vendor 0x2207) ---')
    if not ulanzi:
        print('  none found\n')
    for device in ulanzi:
        status = 'MATCHES HomeDeck' if (device['vendor_id'], device['product_id']) in known else '*** NOT MATCHED ***'
        print(f"  0x{device['vendor_id']:04x}:0x{device['product_id']:04x}  "
              f"iface={device.get('interface_number')}  "
              f"usage_page=0x{device.get('usage_page', 0):04x}  "
              f"path={(device.get('path') or b'').decode(errors='replace')}")
        print(f"      product={device.get('product_string')!r} "
              f"manufacturer={device.get('manufacturer_string')!r}")
        print(f"      -> {status}")
    print()

    print('=' * 60)
    if matched:
        print(f'✅ HomeDeck should connect ({len(matched)} matching interface(s)).')
        print('   If it still fails, the device is found but not responding -')
        print('   check the logs for a connect error.')
    elif ulanzi:
        print('❌ A Ulanzi device is present but its product ID is NOT one')
        print('   HomeDeck knows, so auto_connect() returns None.')
        print()
        print('   This is what a firmware update looks like. To try the new ID,')
        print('   edit the venv copy of strmdck:')
        print()
        print('     strmdck/devices/ulanzi_d200.py  ->  USB_PRODUCT_ID = 0x....')
        print()
        print('   Use the product ID printed above.')
    else:
        print('❌ No Ulanzi device on the USB bus at all.')
        print('   Check the cable (some USB-C cables are power-only), try the')
        print('   other port, and confirm with: lsusb')


if __name__ == '__main__':
    main()
