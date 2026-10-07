"""Tests for recovering from a Home Assistant connection that goes away.

Home Assistant closes its websocket cleanly (code 1000) whenever it restarts or
updates. The websockets library ends `async for` over a connection quietly on a
clean close, so listen() used to just return - and asyncio.gather kept waiting
on the deck's other tasks forever. The deck stayed lit and drawn but deaf: no
state updates, every press raising ConnectionClosedOK, until restarted by hand.
"""

import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from homedeck.home_assistant import HomeAssistantWebSocket
from homedeck.homedeck import run_until_first_exit


async def fake_home_assistant(ws):
    ''' Authenticate, then close cleanly the way a restarting HA does. '''
    await ws.send(json.dumps({'type': 'auth_required'}))
    await ws.recv()
    await ws.send(json.dumps({'type': 'auth_ok'}))
    await ws.close(1000)


async def test_listen_raises_when_home_assistant_closes_cleanly():
    async with serve(fake_home_assistant, '127.0.0.1', 0) as server:
        port = server.sockets[0].getsockname()[1]
        ha = HomeAssistantWebSocket(f'ws://127.0.0.1:{port}', 'token')

        async with ha.connect():
            with pytest.raises(ConnectionError):
                await asyncio.wait_for(ha.listen(), timeout=5)


async def forever():
    await asyncio.Event().wait()


async def test_a_failing_task_cancels_the_others():
    survivor = asyncio.ensure_future(forever())

    async def fails():
        raise ConnectionError('gone')

    async def waits_on_survivor():
        await survivor

    with pytest.raises(ConnectionError):
        await asyncio.wait_for(run_until_first_exit(fails(), waits_on_survivor()), timeout=5)

    # Leaving it running is what piled up a hot-reload loop per reconnect
    await asyncio.sleep(0)
    assert survivor.cancelled()


async def test_a_task_that_returns_ends_the_session():
    async def returns():
        return None

    with pytest.raises(RuntimeError, match='ended unexpectedly'):
        await asyncio.wait_for(run_until_first_exit(returns(), forever()), timeout=5)
