#!/usr/bin/env python3
"""Search the custom-brand-icons pack for an icon name.

    python3 tools/find_icon.py hue
    python3 tools/find_icon.py spotify

Names are not always what you would guess - "philips-hue" does not exist, for
instance - so search before putting one in your configuration.

Use the result as `icon: cbi:<name>`.
"""
import json
import sys
import urllib.request

COLLECTION_URL = 'https://api.iconify.design/collection?prefix=cbi'


def icon_names():
    # The API rejects requests without a User-Agent
    request = urllib.request.Request(COLLECTION_URL, headers={'User-Agent': 'homedeck'})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.load(response)

    # Names appear under "uncategorized", or grouped under "categories"
    names = list(data.get('uncategorized', []))
    for group in data.get('categories', {}).values():
        names.extend(group)

    # Aliases map an old spelling to the real name; both resolve
    names.extend(data.get('aliases', {}))

    return sorted(set(names))


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)

    term = sys.argv[1].lower()
    names = icon_names()
    matches = [n for n in names if term in n.lower()]

    if not matches:
        print(f'No match for {term!r} among {len(names)} icons.')
        return

    print(f'{len(matches)} match(es) for {term!r}:\n')
    for name in matches:
        print(f'  icon: cbi:{name}')


if __name__ == '__main__':
    main()
