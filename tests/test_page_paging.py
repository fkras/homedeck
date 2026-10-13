"""Splitting a page over sub-pages must not strand buttons.

render_buttons inserts the system buttons as it pages: Back on the first
sub-page of a non-root page, Previous on later ones, Next when a sub-page
overflows. The Next check used the button count from before that pass's own
Back or Previous went in, so a sub-page they pushed over the edge got no Next
key - a non-root page with exactly 13 buttons put the 13th on a second
sub-page that nothing on the deck leads to.
"""

import random

from homedeck.dataclasses import PageConfig, SystemButtonConfig
from homedeck.elements import PageElement
from homedeck.enums import ButtonElementAction as Action

PER_PAGE = 13
MAX_SUB_PAGES = 8


def system_buttons(back=1, previous=2, next=3):
    return {
        Action.PAGE_BACK: SystemButtonConfig(button={'name': 'BACK'}, position=back),
        Action.PAGE_PREVIOUS: SystemButtonConfig(button={'name': 'PREV'}, position=previous),
        Action.PAGE_NEXT: SystemButtonConfig(button={'name': 'NEXT'}, position=next),
    }


def sub_pages(entries, *, is_sub_page, buttons=None):
    ''' Every sub-page as a list of 13 names: '-' for an empty key. '''
    pages = []
    for page_number in range(1, MAX_SUB_PAGES + 1):
        element = PageElement(PageConfig(id='p', buttons=entries))
        element.render_buttons(
            system_buttons=buttons or system_buttons(),
            page_number=page_number,
            is_sub_page=is_sub_page,
            buttons_per_page=PER_PAGE,
            all_states={},
        )
        raws = element.button_raws
        pages.append([(raws.get(i) or {}).get('name', '-') for i in range(PER_PAGE)])

    # Drop the empty sub-pages past the end
    while len(pages) > 1 and all(name == '-' for name in pages[-1]):
        pages.pop()
    return pages


def reachable(pages):
    ''' Sub-pages the deck can get to: each through a Next key on the one before. '''
    count = 1
    while count < len(pages) and 'NEXT' in pages[count - 1]:
        count += 1
    return count


def named(n):
    return [{'name': f'b{i}'} for i in range(n)]


class TestNoStrandedButtons:
    def test_a_full_sub_page_gets_a_next_key(self):
        pages = sub_pages(named(13), is_sub_page=True)

        assert pages[0][:4] == ['BACK', 'b0', 'NEXT', 'b1']
        assert pages[1][:3] == ['b11', 'PREV', 'b12']
        assert reachable(pages) == len(pages) == 2

    def test_the_root_page_is_unchanged(self):
        # No Back key on the root page, so 13 buttons fit on one sub-page
        pages = sub_pages(named(13), is_sub_page=False)

        assert len(pages) == 1
        assert pages[0] == [f'b{i}' for i in range(13)]

    def test_a_sub_page_that_fits_is_unchanged(self):
        pages = sub_pages(named(8), is_sub_page=True)

        assert len(pages) == 1
        assert pages[0][:9] == ['BACK'] + [f'b{i}' for i in range(8)]

    def test_previous_pushing_a_later_sub_page_over_gets_a_next_key(self):
        # 25 root buttons: Next on 1 makes 26 keys, Previous on 2 pushes the
        # last onto a third sub-page
        pages = sub_pages(named(25), is_sub_page=False)

        assert reachable(pages) == len(pages) == 3

    def test_random_pages_place_every_button_once_where_the_deck_can_reach(self):
        rng = random.Random(7)
        for _ in range(400):
            entries = []
            for i in range(rng.randrange(40)):
                r = rng.random()
                if r < 0.12:
                    entries.append(None)
                elif r < 0.2:
                    entries.append({'name': f'b{i}', 'visibility': 'gone'})
                else:
                    entries.append({'name': f'b{i}'})
            buttons = system_buttons(back=rng.randrange(14), previous=rng.randrange(14), next=1 + rng.randrange(13))
            is_sub_page = rng.random() < 0.5

            pages = sub_pages(entries, is_sub_page=is_sub_page, buttons=buttons)
            shown = [name for page in pages[:reachable(pages)] for name in page if name.startswith('b')]
            expected = [e['name'] for e in entries if e and e.get('visibility') != 'gone']

            assert shown == expected, (entries, pages)


class TestNullsKeepTheirPlace:
    def test_an_empty_key_after_a_gone_button_is_still_a_key(self):
        # The gone button is skipped; the null and the button after it move
        # up with the rest, rather than the null keeping its raw index
        entries = [{'name': 'a', 'visibility': 'gone'}, None, {'name': 'c'}]

        element = PageElement(PageConfig(id='p', buttons=entries))
        element.render_buttons(system_buttons=system_buttons(), page_number=1, is_sub_page=False, buttons_per_page=PER_PAGE, all_states={})

        raws = element.button_raws
        assert sorted(raws) == [0, 1]
        assert raws[0] is None
        assert raws[1]['name'] == 'c'
