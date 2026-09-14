"""Waking from dim must repaint the whole page.

The D200's firmware drops the button images while the deck sits idle: the
screen stays lit and the clock keeps ticking, but every button goes black.
HomeDeck has no way to observe that, so it still believes the deck holds the
images it last sent.

That matters because a partial update only sends buttons whose *config*
changed. On a black screen it repaints one button and leaves the rest blank -
which is why pressing a button restored only that button, while entering a
sub-page and coming back (the one path that sends a full page) fixed
everything.

The repaint belongs on the wake transition only. Doing it on every Home
Assistant state change instead caused a full ~36KB redraw about once a minute.
"""

from homedeck.enums import SleepStatus
from homedeck.homedeck import HomeDeck


class RecordingDeck(HomeDeck):
    """Captures how reload_page was called, without touching a device."""

    def __init__(self):
        self.reload_calls = []
        self._current_page_id = '$root'
        self._sleep_status = SleepStatus.WAKE

    def reload_page(self, page_id, *, force=False):
        self.reload_calls.append((page_id, force))
        return True


class TestForceReload:
    def test_force_reload_asks_for_a_full_page(self):
        deck = RecordingDeck()

        deck.force_reload_current_page()

        assert deck.reload_calls == [('$root', True)]

    def test_plain_reload_does_not(self):
        deck = RecordingDeck()

        deck.reload_current_page()

        assert deck.reload_calls == [('$root', False)]


class TestStateChangeStaysPartial:
    """A Home Assistant state change must not force a full page.

    Forcing one here was tried and reverted: Home Assistant emits
    state_changed constantly (a sensor updating every minute is enough), and a
    deck sitting dimmed would repaint ~36KB each time. The images on the deck
    are whatever we last sent, so a partial update is correct. Recovering from
    the firmware blanking them is handled on wake instead.
    """

    def test_reload_is_not_forced(self):
        deck = RecordingDeck()
        deck._sleep_status = SleepStatus.DIM

        deck.reload_current_page()

        assert deck.reload_calls == [('$root', False)]
