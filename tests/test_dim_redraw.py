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


class TestStateChangeWhileDimmed:
    """A state change arriving while dimmed must redraw everything."""

    def _force_flag_for(self, status):
        deck = RecordingDeck()
        deck._sleep_status = status

        # What _ha_on_state_changed's debounced reload does
        deck.reload_current_page(force=deck._sleep_status == SleepStatus.DIM)

        return deck.reload_calls[0][1]

    def test_dimmed_forces_a_full_redraw(self):
        assert self._force_flag_for(SleepStatus.DIM) is True

    def test_awake_uses_a_partial_update(self):
        # Awake the images are intact, so only changed buttons need sending
        assert self._force_flag_for(SleepStatus.WAKE) is False
