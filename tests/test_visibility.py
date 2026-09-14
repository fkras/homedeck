"""Tests for blank and hidden buttons.

There are two ways to leave a slot empty and they differ:

- `hidden` / `false` / a bare null entry -> the slot stays, drawn blank
- `gone` -> the button is skipped entirely and later buttons shift up

The schema used `oneOf` for `visibility`, but 'hidden' and 'gone' match both
the Visibility enum and the plain-string branch; `oneOf` requires exactly one
match, so the documented keywords were rejected as invalid.
"""

import copy
import os

import jsonschema
import pytest
import yaml

from homedeck.configuration import Configuration
from homedeck.utils import deep_merge

SCHEMA_DIR = os.path.join('src', 'homedeck', 'yaml')


class FakeDevice:
    ICON_WIDTH = 196
    ICON_HEIGHT = 196
    BUTTON_COUNT = 13


def build(buttons_yaml):
    with open(os.path.join(SCHEMA_DIR, 'configuration.base.yml'), encoding='utf-8') as fp:
        base = yaml.safe_load(fp)
    with open(os.path.join(SCHEMA_DIR, 'configuration.schema.yml'), encoding='utf-8') as fp:
        schema = yaml.safe_load(fp)

    config = yaml.safe_load('pages:\n  $root:\n    buttons:\n' + buttons_yaml)
    merged = deep_merge(copy.deepcopy(base), config)
    jsonschema.validate(merged, schema)
    return merged


def names(merged):
    """Render the page and report what landed in each slot."""
    configuration = Configuration(device=FakeDevice, source_dict=merged, all_states={})
    page = configuration.get_page_element('$root')
    page.render_buttons(
        system_buttons=configuration.system_buttons,
        page_number=1,
        is_sub_page=False,
        buttons_per_page=13,
        all_states={},
    )
    raws = page.button_raws
    return [raws[i].get('name') if raws.get(i) else None for i in sorted(raws)][:3]


class TestBlankSlot:
    @pytest.mark.parametrize('entry', [
        '      - null\n',
        '      - {}\n',
        '      - {name: B, visibility: hidden}\n',
        '      - {name: B, visibility: false}\n',
    ])
    def test_leaves_the_slot_in_place(self, entry):
        merged = build('      - name: A\n' + entry + '      - name: C\n')

        assert names(merged) == ['A', None, 'C']


class TestGone:
    def test_removes_the_slot_and_shifts_the_rest_up(self):
        """Unlike 'hidden', the slot is not kept - the page is one shorter."""
        merged = build('      - name: A\n'
                       '      - {name: B, visibility: gone}\n'
                       '      - name: C\n')

        assert names(merged) == ['A', 'C']


class TestVisible:
    def test_shows_the_button(self):
        merged = build('      - name: A\n'
                       '      - {name: B, visibility: visible}\n'
                       '      - name: C\n')

        assert names(merged) == ['A', 'B', 'C']
