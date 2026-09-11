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
