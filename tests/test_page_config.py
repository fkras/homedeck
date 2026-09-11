"""Tests for PageConfig.

The schema documents an optional `name` on a page, so such a config passes
validation - but PageConfig.__init__ used to reject the keyword, which meant
HomeDeck crashed at startup on a configuration it had just called valid.
"""

import copy
import os

import jsonschema
import yaml

from homedeck.dataclasses import PageConfig
from homedeck.utils import deep_merge

SCHEMA_DIR = os.path.join('src', 'homedeck', 'yaml')


class TestPageName:
    def test_accepts_a_name(self):
        page = PageConfig(id='scenes', buttons=[], name='Scenes')

        assert page.name == 'Scenes'

    def test_name_is_optional(self):
        page = PageConfig(id='scenes', buttons=[])

        assert page.name is None


class TestExampleConfigs:
    """Every shipped example must both validate and actually load."""

    def _schema(self):
        with open(os.path.join(SCHEMA_DIR, 'configuration.schema.yml'), encoding='utf-8') as fp:
            return yaml.safe_load(fp)

    def _base(self):
        with open(os.path.join(SCHEMA_DIR, 'configuration.base.yml'), encoding='utf-8') as fp:
            return yaml.safe_load(fp)

    def test_examples_validate_and_construct(self):
        from homedeck.dataclasses import MainConfig

        class FakeDevice:
            ICON_WIDTH = 196
            ICON_HEIGHT = 196
            BUTTON_COUNT = 13

        examples = [f for f in os.listdir('assets') if f.endswith('.yml.example')]
        assert examples, 'no example configs found'

        for name in examples:
            with open(os.path.join('assets', name), encoding='utf-8') as fp:
                config = yaml.safe_load(fp)

            merged = deep_merge(copy.deepcopy(self._base()), config)

            # Valid per the schema...
            jsonschema.validate(merged, self._schema())

            # ...and actually constructible, which is a separate question
            main = MainConfig(**merged)
            main.post_setup(device=FakeDevice, all_states={})
