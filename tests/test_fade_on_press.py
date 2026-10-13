"""A fading key fades on the press, not on Home Assistant's report.

A light with its own fade starts dimming as soon as Home Assistant commands
it; Home Assistant reports the new state when the integration does, from a
few hundred ms to seconds later. Fading the key on that report left it well
behind the light. So a press on a fading key (transition_trigger: press, the
default) predicts the state its action leads to and fades there at once.
"""

import asyncio

from homedeck import homedeck as homedeck_module
from homedeck.enums import SleepStatus
from homedeck.homedeck import HomeDeck

ENTITY = 'light.lamp'
OFF = {'name': 'Lamp', 'icon': 'off.png'}
ON = {'name': 'Lamp', 'icon': 'on.png'}


class FakeDevice:
    def __init__(self):
        self.sent = []

    def set_buttons(self, buttons, *, update_only=False):
        self.sent.append(dict(buttons))

    def show_alert(self, index):
        pass

    def icons(self, index=0):
        return [b[index]['icon'] for b in self.sent if index in b]


class Element:
    def __init__(self, transition=0.3, trigger=None, entity_id=ENTITY):
        self.transition = transition
        self.transition_trigger = trigger or 'press'
        self.source = {'entity_id': entity_id}


class FakeConfig:
    alert_on_error = False

    def __init__(self, element):
        self.element = element

    def get_button(self, page_id, index):
        return self.element if index == 0 else None


class FakeHA:
    def __init__(self, state='off', entity_id=ENTITY):
        self.all_states = {entity_id: {'entity_id': entity_id, 'state': state, 'attributes': {}}}
        self.release = asyncio.Event()

    async def call_service(self, *, domain, service, service_data):
        # Home Assistant answers once the test says so
        await self.release.wait()
        return 7


class PressDeck(HomeDeck):
    def __init__(self, element, state='off', entity_id=ENTITY):
        self._device = FakeDevice()
        self._configuration = FakeConfig(element)
        self._ha = FakeHA(state, entity_id)
        self._current_page_id = '$root'
        self._sleep_status = SleepStatus.WAKE
        self._pending_service_keys = {}
        self._shown_keys()[0] = OFF if state == 'off' else ON

    def _look_in_state(self, element, entity_id, state):
        return ({'off': OFF, 'on': ON}[state], Element(element.transition, element.transition_trigger))

    def _actual_look(self, index):
        state = self._ha.all_states[ENTITY]['state']
        return ({'off': OFF, 'on': ON}[state], Element())


def stub_frames(monkeypatch):
    monkeypatch.setattr(homedeck_module, 'crossfade_frames', lambda old, new, steps: [f'{old}>{new}#{i}' for i in range(1, steps)])


async def press(deck, service='toggle', domain='light', data=None):
    ''' Tap key 0; returns the call, still waiting for Home Assistant. '''
    task = asyncio.ensure_future(deck.call_ha_service(
        domain=domain, service=service, service_data={'entity_id': ENTITY} if data is None else data, key_index=0,
    ))
    await asyncio.sleep(0.05)
    return task


class TestFadesOnThePress:
    async def test_the_fade_starts_before_home_assistant_answers(self, monkeypatch):
        stub_frames(monkeypatch)
        deck = PressDeck(Element())

        call = await press(deck)

        assert deck._device.icons()[:1] == ['off.png>on.png#1']
        deck._ha.release.set()
        await call

    async def test_the_report_finds_it_already_there(self, monkeypatch):
        stub_frames(monkeypatch)
        deck = PressDeck(Element())

        call = await press(deck)
        deck._ha.release.set()
        await call
        deck._ha.all_states[ENTITY]['state'] = 'on'
        deck._send_changed({0: ON}, {0: Element()})     # the state_changed redraw
        await asyncio.sleep(0.4)

        assert deck._device.icons() == ['off.png>on.png#1', 'off.png>on.png#2', 'on.png']
        assert not deck._predictions()

    async def test_an_update_before_the_report_doesnt_undo_it(self, monkeypatch):
        stub_frames(monkeypatch)
        deck = PressDeck(Element())

        call = await press(deck)
        deck._send_changed({0: OFF}, {0: Element()})    # e.g. an attribute changed, state still off
        await asyncio.sleep(0.4)

        assert deck._device.icons()[-1] == 'on.png'
        deck._ha.release.set()
        await call

    async def test_a_second_press_toggles_from_where_the_first_is_heading(self, monkeypatch):
        stub_frames(monkeypatch)
        deck = PressDeck(Element())

        first = await press(deck)
        second = await press(deck)
        await asyncio.sleep(0.4)

        assert deck._device.icons()[-1] == 'off.png'
        assert deck._predictions()[0].state == 'off'
        deck._ha.release.set()
        await asyncio.gather(first, second)


class TestFallsBack:
    async def test_a_refused_action_fades_back(self, monkeypatch):
        stub_frames(monkeypatch)
        deck = PressDeck(Element())

        call = await press(deck)
        deck._ha.release.set()
        await call
        deck._on_ha_command_failed(7, {'code': 'not_found'})
        await asyncio.sleep(0.4)

        assert deck._device.icons()[-1] == 'off.png'
        assert not deck._predictions()

    async def test_no_report_in_time_fades_back(self, monkeypatch):
        stub_frames(monkeypatch)
        monkeypatch.setattr(HomeDeck, 'PREDICTION_TIMEOUT', 0.1)
        deck = PressDeck(Element())

        call = await press(deck)
        deck._ha.release.set()
        await call
        await asyncio.sleep(0.6)

        assert deck._device.icons()[-1] == 'off.png'

    async def test_a_page_change_drops_it(self, monkeypatch):
        stub_frames(monkeypatch)
        deck = PressDeck(Element())

        call = await press(deck)
        deck._clear_predictions()                       # what a full page does

        assert not deck._predictions()
        deck._ha.release.set()
        await call


class TestOnlyWhenTheResultIsKnown:
    async def assert_no_prediction(self, deck, **kwargs):
        call = await press(deck, **kwargs)
        assert deck._device.sent == []
        deck._ha.release.set()
        await call

    async def test_transition_trigger_state_waits_for_the_report(self, monkeypatch):
        stub_frames(monkeypatch)
        await self.assert_no_prediction(PressDeck(Element(trigger='state')))

    async def test_a_key_without_a_fade(self):
        await self.assert_no_prediction(PressDeck(Element(transition=0)))

    async def test_an_action_on_another_entity(self):
        await self.assert_no_prediction(PressDeck(Element()), data={'entity_id': 'light.other'})

    async def test_a_wrong_domain_action_that_home_assistant_ignores(self):
        await self.assert_no_prediction(PressDeck(Element()), domain='switch')

    async def test_an_action_that_doesnt_toggle(self):
        await self.assert_no_prediction(PressDeck(Element()), service='turn_on_soon')

    async def test_turning_on_what_is_on(self):
        await self.assert_no_prediction(PressDeck(Element(), state='on'), service='turn_on')

    async def test_an_unavailable_entity(self):
        await self.assert_no_prediction(PressDeck(Element(), state='unavailable'))

    async def test_homeassistant_toggle_is_predicted(self, monkeypatch):
        stub_frames(monkeypatch)
        deck = PressDeck(Element())

        call = await press(deck, domain='homeassistant')

        assert deck._device.icons()[:1] == ['off.png>on.png#1']
        deck._ha.release.set()
        await call
