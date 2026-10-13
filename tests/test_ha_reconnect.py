"""Tests for recovering from a Home Assistant connection that goes away.

Home Assistant closes its websocket cleanly (code 1000) whenever it restarts or
updates. The websockets library ends `async for` over a connection quietly on a
clean close, so listen() used to just return - and asyncio.gather kept waiting
on the deck's other tasks forever. The deck stayed lit and drawn but deaf: no
state updates, every press raising ConnectionClosedOK, until restarted by hand.
"""

import asyncio
import json
import socket

import pytest
from PIL import Image
from homedeck.d200 import ButtonAction
from websockets.asyncio.server import serve

from homedeck.connecting import spinner_frames
from homedeck.home_assistant import HomeAssistantUnavailable, HomeAssistantWebSocket
from homedeck.homedeck import HomeDeck, run_alongside


async def fake_home_assistant(ws):
    ''' Authenticate, answer get_states, then close cleanly the way a
    restarting HA does. '''
    await ws.send(json.dumps({'type': 'auth_required'}))
    await ws.recv()
    await ws.send(json.dumps({'type': 'auth_ok'}))

    request = json.loads(await ws.recv())
    await ws.send(json.dumps({'id': request['id'], 'type': 'result', 'success': True, 'result': []}))
    await ws.close(1000)


async def test_listen_raises_when_home_assistant_closes_cleanly():
    async with serve(fake_home_assistant, '127.0.0.1', 0) as server:
        port = server.sockets[0].getsockname()[1]
        ha = HomeAssistantWebSocket(f'ws://127.0.0.1:{port}', 'token')

        await ha.connect()
        with pytest.raises(HomeAssistantUnavailable):
            await asyncio.wait_for(ha.listen(), timeout=5)


def unused_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


async def test_a_refused_connection_is_reported_as_unavailable():
    """What a restarting Home Assistant looks like from the deck. It must be
    the one exception the retry loop waits out, not a generic error that
    tears the deck down and dumps a traceback every few seconds."""
    ha = HomeAssistantWebSocket(f'ws://127.0.0.1:{unused_port()}', 'token')

    with pytest.raises(HomeAssistantUnavailable):
        await ha.connect()


async def forever():
    await asyncio.Event().wait()


class TestRunAlongside:
    async def test_returns_the_main_result_and_cancels_the_rest(self):
        background = asyncio.ensure_future(forever())

        async def main():
            return 'connected'

        async def waits_on_background():
            await background

        assert await run_alongside(main(), waits_on_background()) == 'connected'

        await asyncio.sleep(0)
        assert background.cancelled()

    async def test_a_failing_background_task_cancels_main(self):
        main = asyncio.ensure_future(forever())

        async def waits_on_main():
            await main

        async def fails():
            raise ConnectionError('gone')

        with pytest.raises(ConnectionError):
            await asyncio.wait_for(run_alongside(waits_on_main(), fails()), timeout=5)

        # Leaving it running is what piled up a hot-reload loop per reconnect
        await asyncio.sleep(0)
        assert main.cancelled()

    async def test_a_background_task_that_returns_ends_the_session(self):
        async def returns():
            return None

        with pytest.raises(RuntimeError, match='ended unexpectedly'):
            await asyncio.wait_for(run_alongside(forever(), returns()), timeout=5)


class FakeDevice:
    BUTTON_COUNT = 13
    ICON_WIDTH = 196

    def __init__(self, presses=()):
        self.calls = []
        self.presses = presses

    def set_buttons(self, buttons, *, update_only=False):
        self.calls.append((buttons, update_only))

    async def read_packet(self):
        for press in self.presses:
            yield press


def make_deck(device):
    deck = HomeDeck.__new__(HomeDeck)
    deck._device = device
    deck._configuration = None
    deck._current_page_element = object()
    deck._pages_stack = [('$root', 1), ('lights', 1)]
    return deck


class TestConnectingScreen:
    def test_frames_are_drawn_locally_at_icon_size(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        names = spinner_frames(196)

        assert len(set(names)) == len(names) > 1
        for name in names:
            with Image.open(tmp_path / '.cache' / 'icons' / '_generated' / name) as img:
                assert img.size == (196, 196)

    async def test_blanks_the_page_then_animates_one_button(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        device = FakeDevice()
        deck = make_deck(device)
        deck.CONNECTING_FRAME_INTERVAL = 0.01

        task = asyncio.ensure_future(deck._show_connecting())
        await asyncio.sleep(0.1)
        task.cancel()

        first, update_only = device.calls[0]
        assert not update_only
        assert len(first) == HomeDeck.EXTRA_SLOTS + FakeDevice.BUTTON_COUNT
        assert [i for i, button in first.items() if button] == [HomeDeck.CONNECTING_BUTTON]

        # Later frames only touch the spinner's button, and actually move
        later = device.calls[1:]
        assert later and all(update_only and list(buttons) == [HomeDeck.CONNECTING_BUTTON] for buttons, update_only in later)
        assert later[0][0][HomeDeck.CONNECTING_BUTTON]['icon'] != first[HomeDeck.CONNECTING_BUTTON]['icon']

        # The session that follows repaints from the root page
        assert deck._current_page_element is None
        assert deck._pages_stack == []

    async def test_presses_during_an_outage_are_dropped(self):
        presses = [ButtonAction(index=2, pressed=True, state=1), ButtonAction(index=2, pressed=False, state=1)]
        device = FakeDevice(presses)
        deck = make_deck(device)

        async def fail(*args):
            raise AssertionError('a press during an outage reached an action')

        deck._on_interacted = fail
        await deck._discard_presses()
