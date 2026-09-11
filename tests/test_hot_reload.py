"""Tests for configuration hot-reload.

The deck watches assets/configuration.yml and reloads when it changes. Saves
arrive from an SSH editor or from the Home Assistant add-on, which does a plain
overwrite; editors often write a temp file and rename it over the target
instead, which arrives as a move rather than a modify.
"""

import os

from homedeck.homedeck import HomeDeck


class FakeEvent:
    def __init__(self, src_path=None, dest_path=None):
        self.src_path = src_path
        self.dest_path = dest_path


class FakeDeck:
    def __init__(self):
        self._need_reload_all = False


def make_handler():
    deck = FakeDeck()
    return HomeDeck.ConfigurationFileChangeHandler(deck), deck


CONFIG = os.path.abspath(os.path.join('assets', 'configuration.yml'))
OTHER = os.path.abspath(os.path.join('assets', 'something-else.yml'))


class TestEventsThatTriggerReload:
    def test_modified(self):
        """What the Home Assistant add-on's save produces."""
        handler, deck = make_handler()

        handler.on_modified(FakeEvent(src_path=CONFIG))

        assert deck._need_reload_all is True

    def test_created(self):
        handler, deck = make_handler()

        handler.on_created(FakeEvent(src_path=CONFIG))

        assert deck._need_reload_all is True

    def test_moved_onto_the_config(self):
        """Atomic saves rename a temp file over the target."""
        handler, deck = make_handler()

        handler.on_moved(FakeEvent(src_path=CONFIG + '.tmp', dest_path=CONFIG))

        assert deck._need_reload_all is True

    def test_bytes_paths(self):
        """Some platforms deliver paths as bytes."""
        handler, deck = make_handler()

        handler.on_modified(FakeEvent(src_path=CONFIG.encode()))

        assert deck._need_reload_all is True

    def test_relative_path_is_normalised(self):
        handler, deck = make_handler()

        handler.on_modified(FakeEvent(src_path=os.path.join('assets', 'configuration.yml')))

        assert deck._need_reload_all is True


class TestEventsThatDoNot:
    def test_other_file_modified(self):
        handler, deck = make_handler()

        handler.on_modified(FakeEvent(src_path=OTHER))

        assert deck._need_reload_all is False

    def test_move_away_from_the_config(self):
        """Renaming the config elsewhere isn't a new config to load."""
        handler, deck = make_handler()

        handler.on_moved(FakeEvent(src_path=CONFIG, dest_path=OTHER))

        assert deck._need_reload_all is False

    def test_missing_path(self):
        handler, deck = make_handler()

        handler.on_modified(FakeEvent(src_path=None))

        assert deck._need_reload_all is False


class TestRapidSaves:
    def test_a_second_save_is_never_lost(self):
        """The regression this suite exists for.

        The old handler ignored any event within 1s of the previous one, so a
        quick follow-up save was dropped entirely and the deck kept showing the
        stale config. Every save must leave the deck flagged as dirty.
        """
        handler, deck = make_handler()

        handler.on_modified(FakeEvent(src_path=CONFIG))
        deck._need_reload_all = False          # simulate the reload consuming it
        handler.on_modified(FakeEvent(src_path=CONFIG))   # immediate second save

        assert deck._need_reload_all is True

    def test_repeated_events_stay_flagged(self):
        handler, deck = make_handler()

        for _ in range(5):
            handler.on_modified(FakeEvent(src_path=CONFIG))

        assert deck._need_reload_all is True
