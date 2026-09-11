"""A page must blank the slots it doesn't use.

Switching from a 13-button page to a 4-button one used to leave the previous
page's buttons visible in the remaining slots: generate() only emitted entries
for buttons that existed, so the deck never heard about the rest.
"""

from homedeck.elements import PageElement


class TestSlotPadding:
    def test_pads_to_the_deck_size(self):
        page = {0: None, 1: None, 2: None, 3: None}

        output = PageElement.generate(page, slot_count=13)

        assert sorted(output) == list(range(13))

    def test_unused_slots_are_explicitly_blank(self):
        page = {0: None, 1: None, 2: None, 3: None}

        output = PageElement.generate(page, slot_count=13)

        assert all(output[i] is None for i in range(4, 13))

    def test_a_full_page_is_unchanged(self):
        page = {i: None for i in range(13)}

        output = PageElement.generate(page, slot_count=13)

        assert sorted(output) == list(range(13))

    def test_no_padding_by_default(self):
        """Partial updates must stay partial - they only send what changed."""
        page = {0: None, 5: None}

        output = PageElement.generate(page)

        assert sorted(output) == [0, 5]

    def test_padding_never_overwrites_a_real_button(self):
        page = {12: None}

        output = PageElement.generate(page, slot_count=13)

        # Slot 12 is still present, and the rest are filled in around it
        assert 12 in output
        assert len(output) == 13


class TestFourteenthSlot:
    """The D200 has a 14th manifest slot that strmdck doesn't know about.

    A capture of the official Ulanzi app shows it writing 14 entries keyed
    "{col}_{row}", the last being "3_2" (index 13) with an empty icon. strmdck
    stops at BUTTON_COUNT = 13, so whatever the firmware drew in that slot -
    the Ulanzi branding behind the small window - was never cleared.
    """

    COLS = 5

    def _keys(self, slot_count):
        output = PageElement.generate({0: None}, slot_count=slot_count)
        return [f'{i % self.COLS}_{i // self.COLS}' for i in sorted(output)]

    def test_button_count_alone_misses_the_slot(self):
        assert '3_2' not in self._keys(13)

    def test_extra_slot_includes_it(self):
        from homedeck.homedeck import HomeDeck

        keys = self._keys(13 + HomeDeck.EXTRA_SLOTS)

        assert '3_2' in keys
        assert len(keys) == 14

    def test_matches_what_the_official_app_sends(self):
        from homedeck.homedeck import HomeDeck

        # Captured from the app's manifest.json
        app_slots = {
            '0_0', '0_1', '0_2', '1_0', '1_1', '1_2', '2_0', '2_1',
            '2_2', '3_0', '3_1', '3_2', '4_0', '4_1',
        }

        assert set(self._keys(13 + HomeDeck.EXTRA_SLOTS)) == app_slots
