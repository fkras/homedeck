#!/usr/bin/env python3
"""Ask Home Assistant why a button press does nothing.

    $HD_PY tools/ha_probe.py                              # audit the config
    $HD_PY tools/ha_probe.py light.toggle light.kuzhina   # fire one call

With no arguments it reads assets/configuration.yml, resolves presets the way
the deck does, and checks every button against the live instance: entity
missing, service unknown, or a service whose domain doesn't match the entity's.
That last one is the quiet failure - light.toggle on a switch.* entity is a
perfectly valid config that Home Assistant refuses at call time.

With a service and an entity it sends exactly the call_service message the deck
sends and prints Home Assistant's raw reply, error included - the reply the
deck receives and discards.
"""
import asyncio
import json
import os
import sys

import yaml
from dotenv import load_dotenv

try:
    import websockets
except ImportError:
    sys.exit('websockets not installed - run this with the venv python ($HD_PY)')

load_dotenv()
HA_HOST = (os.getenv('HA_HOST') or '').rstrip('/')
HA_ACCESS_TOKEN = os.getenv('HA_ACCESS_TOKEN')

CONFIG_PATH = os.path.join('assets', 'configuration.yml')

# Actions the deck handles itself; they never reach Home Assistant.
LOCAL_ACTIONS = ('$page.',)


class Session:
    def __init__(self, ws):
        self._ws = ws
        self._id = 0

    async def request(self, message: dict) -> dict:
        self._id += 1
        message['id'] = self._id
        await self._ws.send(json.dumps(message))

        # Skip anything that isn't the answer to this message.
        while True:
            data = json.loads(await self._ws.recv())
            if data.get('type') == 'result' and data.get('id') == self._id:
                return data


async def open_session(ws):
    await ws.recv()  # auth_required
    await ws.send(json.dumps({'type': 'auth', 'access_token': HA_ACCESS_TOKEN}))
    data = json.loads(await ws.recv())
    if data.get('type') != 'auth_ok':
        sys.exit(f'Authentication failed: {data}')

    print(f'Authenticated to {HA_HOST} (Home Assistant {data.get("ha_version", "?")})')
    return Session(ws)


def resolve_presets(button: dict, presets: dict, _seen=None) -> dict:
    """Flatten a button's presets chain, nearest definition winning."""
    _seen = _seen or set()
    names = button.get('presets') or []
    if isinstance(names, str):
        names = [names]

    resolved = {}
    for name in names:
        if name in _seen or name not in presets:
            continue

        _seen.add(name)
        parent = presets[name] or {}
        resolved.update(resolve_presets(parent, presets, _seen))
        resolved.update(parent)

    resolved.update(button)
    return resolved


def iter_buttons(config: dict):
    presets = config.get('presets') or {}
    for page_id, page in (config.get('pages') or {}).items():
        for index, button in enumerate(page.get('buttons') or []):
            if not isinstance(button, dict):
                continue

            yield page_id, index, resolve_presets(button, presets)


async def audit(session: Session):
    try:
        with open(CONFIG_PATH, encoding='utf-8') as fp:
            config = yaml.safe_load(fp.read())
    except OSError as e:
        sys.exit(f'Could not read {CONFIG_PATH}: {e}')

    states = await session.request({'type': 'get_states'})
    entities = {state['entity_id'] for state in states['result']}
    print(f'{len(entities)} entities exist in Home Assistant')

    services = await session.request({'type': 'get_services'})
    services = services['result']

    problems = 0
    checked = 0
    for page_id, index, button in iter_buttons(config):
        entity_id = button.get('entity_id')
        action = (button.get('tap_action') or {}).get('action')
        where = f'{page_id}[{index}] {button.get("name") or entity_id or action}'

        if not action or action.startswith(LOCAL_ACTIONS):
            continue

        checked += 1

        if entity_id and entity_id not in entities:
            print(f'  MISSING ENTITY  {where}: {entity_id} does not exist')
            problems += 1
            continue

        if '.' not in action:
            print(f'  BAD ACTION      {where}: {action} is not domain.service')
            problems += 1
            continue

        domain, service = action.split('.', 1)
        if domain not in services or service not in services[domain]:
            print(f'  MISSING SERVICE {where}: {action} is not a service')
            problems += 1
            continue

        # homeassistant.* accepts any domain; everything else expects its own.
        if entity_id and domain != 'homeassistant' and entity_id.split('.')[0] != domain:
            print(f'  DOMAIN MISMATCH {where}: {action} on {entity_id} - use {entity_id.split(".")[0]}.{service}')
            problems += 1
            continue

        if not entity_id:
            print(f'  NO ENTITY       {where}: {action} has no entity_id to act on')
            problems += 1

    print(f'\nChecked {checked} Home Assistant button(s), {problems} problem(s)')
    return 1 if problems else 0


async def call(session: Session, action: str, entity_id: str):
    if '.' not in action:
        sys.exit(f'{action} is not domain.service')

    domain, service = action.split('.', 1)

    before = await session.request({'type': 'get_states'})
    before = {s['entity_id']: s['state'] for s in before['result']}
    if entity_id not in before:
        print(f'Warning: {entity_id} does not exist in Home Assistant')
    else:
        print(f'{entity_id} is currently {before[entity_id]!r}')

    message = {
        'type': 'call_service',
        'domain': domain,
        'service': service,
        'service_data': {'entity_id': entity_id},
    }
    print(f'\n-> {json.dumps(message)}')
    reply = await session.request(message)
    print(f'<- {json.dumps(reply)}')

    if not reply.get('success'):
        print(f'\nREFUSED: {reply.get("error", {}).get("message")}')
        return 1

    # A successful call_service only means Home Assistant accepted it. An
    # integration that is offline still reports success, so read the state back.
    await asyncio.sleep(2)
    after = await session.request({'type': 'get_states'})
    after = {s['entity_id']: s['state'] for s in after['result']}
    now = after.get(entity_id)

    print(f'\nAccepted. {entity_id} is now {now!r}')
    if entity_id in before and before[entity_id] == now:
        print('State did not change - Home Assistant ran the service but the '
              'device did not respond (offline integration, or a service that '
              'is not a toggle).')
        return 1

    return 0


async def main():
    if not HA_HOST or not HA_ACCESS_TOKEN:
        sys.exit('HA_HOST and HA_ACCESS_TOKEN must be set in .env')

    args = sys.argv[1:]
    if len(args) not in (0, 2):
        sys.exit(__doc__)

    async with websockets.connect(f'{HA_HOST}/api/websocket') as ws:
        session = await open_session(ws)

        if args:
            return await call(session, args[0], args[1])

        return await audit(session)


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
