"""Cross-fading a key when its look changes (`transition`).

A fade is in-between images sent as ordinary partial updates, ending on the
real image. What matters on the deck: keys without a transition still switch
at once, the final image is always the last thing sent, and a full page (a
page change, waking up) stops a fade so its frames can't paint over it.
"""

import asyncio
import os

from PIL import Image

from homedeck import homedeck as homedeck_module
from homedeck.enums import SleepStatus
from homedeck.homedeck import HomeDeck
from homedeck.transitions import crossfade_frames, frame_count


class FakeDevice:
    def __init__(self):
        self.sent = []

    def set_buttons(self, buttons, *, update_only=False):
        self.sent.append((dict(buttons), update_only))


class Element:
    def __init__(self, transition=0.0):
        self.transition = transition


class FadingDeck(HomeDeck):
    """Just enough of HomeDeck to send keys, without a device or Home Assistant."""

    def __init__(self):
        self._device = FakeDevice()
        self._sleep_status = SleepStatus.WAKE


def stub_frames(monkeypatch):
    def frames(old, new, steps):
        return [f'{old}>{new}#{i}' for i in range(1, steps)]
    monkeypatch.setattr(homedeck_module, 'crossfade_frames', frames)


class TestFrameCount:
    def test_about_ten_a_second(self):
        assert frame_count(0.8) == 8

    def test_at_least_the_final_image(self):
        assert frame_count(0.01) == 1

    def test_capped(self):
        assert frame_count(5) == 20


class TestCrossfadeFrames:
    def test_blends_from_the_old_image_to_the_new(self, tmp_path):
        Image.new('RGBA', (4, 4), (0, 0, 0, 255)).save(tmp_path / 'off.png')
        Image.new('RGBA', (4, 4), (200, 100, 0, 255)).save(tmp_path / 'on.png')

        names = crossfade_frames('off.png', 'on.png', 4, cache_dir=str(tmp_path))

        assert len(names) == 3
        reds = [Image.open(tmp_path / n).getpixel((0, 0))[0] for n in names]
        assert 0 < reds[0] < reds[1] < reds[2] < 200

    def test_reuses_frames_it_already_drew(self, tmp_path):
        Image.new('RGBA', (4, 4), (0, 0, 0, 255)).save(tmp_path / 'off.png')
        Image.new('RGBA', (4, 4), (255, 255, 255, 255)).save(tmp_path / 'on.png')

        first = crossfade_frames('off.png', 'on.png', 3, cache_dir=str(tmp_path))
        os.remove(tmp_path / 'off.png')  # would fail if it drew them again
        again = crossfade_frames('off.png', 'on.png', 3, cache_dir=str(tmp_path))

        assert first == again

    def test_a_single_step_needs_no_frames(self, tmp_path):
        assert crossfade_frames('a.png', 'b.png', 1, cache_dir=str(tmp_path)) == []


class TestSendChanged:
    async def test_a_key_without_transition_switches_at_once(self):
        deck = FadingDeck()
        deck._shown_keys()[0] = {'name': 'Lamp', 'icon': 'off.png'}

        deck._send_changed({0: {'name': 'Lamp', 'icon': 'on.png'}}, {0: Element(0)})

        assert deck._device.sent == [({0: {'name': 'Lamp', 'icon': 'on.png'}}, True)]

    async def test_a_key_with_transition_fades_and_ends_on_the_real_image(self, monkeypatch):
        stub_frames(monkeypatch)
        deck = FadingDeck()
        deck._shown_keys()[0] = {'name': 'Lamp', 'icon': 'off.png'}

        deck._send_changed({0: {'name': 'Lamp', 'icon': 'on.png'}}, {0: Element(0.3)})
        await asyncio.sleep(0.5)

        icons = [sent[0][0]['icon'] for sent in deck._device.sent]
        assert icons == ['off.png>on.png#1', 'off.png>on.png#2', 'on.png']
        assert all(update_only for _, update_only in deck._device.sent)
        assert deck._shown_keys()[0]['icon'] == 'on.png'

    async def test_other_keys_are_not_held_up(self, monkeypatch):
        stub_frames(monkeypatch)
        deck = FadingDeck()
        deck._shown_keys().update({0: {'icon': 'off.png'}, 1: {'icon': 'a.png'}})

        deck._send_changed({0: {'icon': 'on.png'}, 1: {'icon': 'b.png'}}, {0: Element(0.3), 1: Element(0)})

        assert deck._device.sent[0] == ({1: {'icon': 'b.png'}}, True)
        await asyncio.sleep(0.5)

    async def test_nothing_to_fade_from_switches_at_once(self, monkeypatch):
        stub_frames(monkeypatch)
        deck = FadingDeck()

        deck._send_changed({0: {'icon': 'on.png'}}, {0: Element(0.5)})

        assert deck._device.sent == [({0: {'icon': 'on.png'}}, True)]

    async def test_a_newer_change_fades_from_where_the_last_one_got_to(self, monkeypatch):
        stub_frames(monkeypatch)
        deck = FadingDeck()
        deck._shown_keys()[0] = {'icon': 'off.png'}

        deck._send_changed({0: {'icon': 'on.png'}}, {0: Element(0.4)})
        await asyncio.sleep(0.05)
        deck._send_changed({0: {'icon': 'off.png'}}, {0: Element(0.4)})
        await asyncio.sleep(0.6)

        icons = [sent[0][0]['icon'] for sent in deck._device.sent]
        assert icons[0] == 'off.png>on.png#1'
        assert icons[1].startswith('off.png>on.png#1>off.png')
        assert icons[-1] == 'off.png'
        assert 'on.png' not in icons

    async def test_cancelling_stops_the_frames(self, monkeypatch):
        stub_frames(monkeypatch)
        deck = FadingDeck()
        deck._shown_keys()[0] = {'icon': 'off.png'}

        deck._send_changed({0: {'icon': 'on.png'}}, {0: Element(1)})
        await asyncio.sleep(0.05)
        deck._cancel_transitions()
        sent = len(deck._device.sent)
        await asyncio.sleep(0.3)

        assert len(deck._device.sent) == sent
