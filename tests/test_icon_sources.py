"""Tests for icon source prefixes.

An `icon:` value is "<source>:<name>", and the prefix selects which layer class
fetches it. cbi: adds the custom-brand-icons pack (brand logos), fetched via
the Iconify API.
"""

import copy
import os

import jsonschema
import pytest
import yaml

from homedeck.enums import IconSource
from homedeck.icons import CustomBrandIconLayer, MaterialDesignIconLayer, PhosphorIconLayer
from homedeck.utils import deep_merge

SCHEMA_DIR = os.path.join('src', 'homedeck', 'yaml')


def layer(source, name, variant=None):
    return {
        'icon_source': source,
        'icon_name': name,
        'icon_variant': variant,
        'max_width': 196,
        'max_height': 196,
    }


class TestCustomBrandIcons:
    def test_source_is_registered(self):
        assert IconSource('cbi') is IconSource.CUSTOM_BRAND

    def test_download_url(self):
        icon = CustomBrandIconLayer(layer(IconSource.CUSTOM_BRAND, 'spotify'))

        assert icon.download_url == 'https://api.iconify.design/cbi/spotify.svg'

    def test_cached_under_its_own_directory(self):
        icon = CustomBrandIconLayer(layer(IconSource.CUSTOM_BRAND, 'spotify'))

        assert icon.original_file_path == os.path.join('.cache', 'icons', 'cbi', 'spotify.svg')

    def test_does_not_collide_with_other_packs(self):
        """A name present in several packs must cache separately."""
        cbi = CustomBrandIconLayer(layer(IconSource.CUSTOM_BRAND, 'spotify'))
        mdi = MaterialDesignIconLayer(layer(IconSource.MATERIAL_DESIGN, 'spotify'))

        assert cbi.original_file_path != mdi.original_file_path
        assert cbi.download_url != mdi.download_url


class TestExistingSourcesStillWork:
    def test_mdi(self):
        icon = MaterialDesignIconLayer(layer(IconSource.MATERIAL_DESIGN, 'lightbulb'))

        assert icon.download_url.endswith('/svg/lightbulb.svg')

    def test_phosphor_defaults_to_regular(self):
        icon = PhosphorIconLayer(layer(IconSource.PHOSPHOR, 'house'))

        assert '/regular/house.svg' in icon.download_url


class TestSchemaAcceptsPrefixes:
    def _validate(self, icon_value):
        with open(os.path.join(SCHEMA_DIR, 'configuration.base.yml'), encoding='utf-8') as fp:
            base = yaml.safe_load(fp)
        with open(os.path.join(SCHEMA_DIR, 'configuration.schema.yml'), encoding='utf-8') as fp:
            schema = yaml.safe_load(fp)

        config = {'pages': {'$root': {'buttons': [{'icon': icon_value, 'name': 'x'}]}}}
        jsonschema.validate(deep_merge(copy.deepcopy(base), config), schema)

    @pytest.mark.parametrize('icon', ['cbi:spotify', 'mdi:lightbulb', 'pi:house', 'local:x.png'])
    def test_accepted(self, icon):
        self._validate(icon)

    def test_unknown_prefix_rejected(self):
        with pytest.raises(jsonschema.ValidationError):
            self._validate('bogus:thing')
