import asyncio
import json
import logging
from contextlib import asynccontextmanager

import websockets

logging.basicConfig(level=logging.INFO)


class HomeAssistantWebSocket:
    def __init__(self, host: str, token: str):
        self._host = host.rstrip('/')
        self._token = token
        self._ws = None
        self._message_id = 1
        self._event_listeners = {}
        self._lock = asyncio.Lock()
        self._should_reconnect = True

        self._states = {}

        self._callbacks = {}
        self._error_callbacks = {}

    @asynccontextmanager
    async def connect(self) -> websockets.WebSocketClientProtocol:
        ws_url = f'{self._host}/api/websocket'

        async with websockets.connect(ws_url, ping_timeout=5) as ws:
            self._ws = ws
            await self._authenticate()
            yield ws

    async def disconnect(self):
        await self._ws.close()

    async def get_entity_state(self, entity_id: str):
        if entity_id in self._states:
            return self._states[entity_id]

        return None

    async def _on_state_changed(self, data):
        try:
            self._states[data['entity_id']] = data['new_state']

            if self._callback:
                await self._callback()
        except Exception as e:
            print('_on_state_changed', e)

    @property
    def all_states(self):
        return self._states

    async def _authenticate(self):
        response = await self._ws.recv()
        auth_message = json.dumps({
            'type': 'auth',
            'access_token': self._token,
        })
        await self._ws.send(auth_message)
        response = await self._ws.recv()

        data = json.loads(response)
        if data.get('type') != 'auth_ok':
            raise Exception('Authentication failed!')

        logging.info('Authenticated successfully.')

    async def send_message(self, message: dict, callback=None):
        async with self._lock:
            message['id'] = self._message_id
            if callback:
                self._callbacks[message['id']] = callback

            self._message_id += 1
            # Logged after the id is assigned, so a failure reported by
            # listen() ("Message 7 failed") can be traced back to the command
            # that caused it.
            logging.info('send_message: ' + str(message))
            await self._ws.send(json.dumps(message))

            return message['id']

    async def send_message_sync(self, message: dict):
        logging.info('send_message (sync): ' + str(message))
        async with self._lock:
            message['id'] = self._message_id
            self._message_id += 1
            await self._ws.send(json.dumps(message))

            while True:
                data = json.loads(await self._ws.recv())
                if data['type'] == 'result':
                    return data['result']

    async def call_service(self, *, domain: str, service: str, service_data: dict = None):
        return await self.send_message({
            'type': 'call_service',
            'domain': domain,
            'service': service,
            'service_data': service_data or {}
        })

    async def get_state(self, entity_id: str):
        return await self.send_message({
            'type': 'get_states',
            'entity_id': entity_id
        })

    async def get_all_states(self, callback=None):
        states = await self.send_message_sync({'type': 'get_states'})
        logging.info('Received all_states')
        self._states = {state['entity_id']: state for state in states}

    async def turn_on(self, entity_id: str):
        return await self.call_service('homeassistant', 'turn_on', {
            'entity_id': entity_id,
        })

    async def turn_off(self, entity_id: str):
        return await self.call_service('homeassistant', 'turn_off', {
            'entity_id': entity_id,
        })

    async def subscribe_events(self, event_type: str):
        return await self.send_message({
            'type': 'subscribe_events',
            'event_type': event_type,
        })

    async def listen(self):
        async for message in self._ws:
            data = json.loads(message)
            # logging.info(f'Received: {message}')

            # A refused command comes back as a result with success: false and
            # no 'result' key. Most callers - call_service among them - send
            # without a callback, so such a result matched neither branch below
            # and was dropped in silence: a service call Home Assistant refused
            # (unknown entity, service's domain not matching the entity's,
            # token without permission) looked exactly like one that worked.
            if data.get('type') == 'result' and not data.get('success', True):
                logging.error('Message %s failed: %s', data.get('id'), data.get('error'))

            if 'id' in data and data['id'] in self._callbacks:
                if 'result' not in data:
                    continue

                callback = self._callbacks.pop(data['id'])
                if callback:
                    await callback(data['result'])
            elif data.get('type') == 'event' and data.get('event', {}).get('event_type') in self._event_listeners:
                event = data['event']
                event_type = event['event_type']
                event_data = event['data']
                # Update state
                if event_type == 'state_changed':
                    self._states[event_data['entity_id']] = event_data['new_state']

                for callback in self._event_listeners[event_type]:
                    await callback(event_data)

    def on_event(self, event_type: str, callback):
        if event_type not in self._event_listeners:
            self._event_listeners[event_type] = []

        self._event_listeners[event_type].append(callback)
