from __future__ import annotations

import os

import jsonschema
import jsonschema.exceptions
import yaml
from .d200 import UlanziD200 as DeckDevice

from .dataclasses import MainConfig
from .elements import PageElement


class Configuration:
    def __init__(self, *, device: DeckDevice, source_dict: dict, all_states: dict):
        self._device = device
        self._config_dict = source_dict

        self._is_valid = self._validate()
        if self._is_valid:
            self._post_process(all_states=all_states)

    def _validate(self):
        script_dir = os.path.dirname(os.path.realpath(__file__))
        with open(os.path.join(script_dir, 'yaml', 'configuration.schema.yml'), 'r', encoding='utf-8') as fp:
            try:
                jsonschema.validate(instance=self._config_dict, schema=yaml.safe_load(fp))
                return True
            except jsonschema.exceptions.ValidationError as e:
                print(e)

        return False

    def _post_process(self, all_states: dict):
        self._config = MainConfig(**self._config_dict)
        self._config.post_setup(device=self._device, all_states=all_states)

        self._page_elements = {}

        self._config_dict.setdefault('presets', {})

    def is_valid(self):
        return self._is_valid

    @property
    def brightness(self):
        return self._config.brightness

    @property
    def label_style(self):
        return self._config.label_style

    @property
    def sleep(self):
        return self._config.sleep

    @property
    def deck_sleep_timeout(self):
        return self._config.deck_sleep_timeout

    @property
    def alert_on_error(self):
        return self._config.alert_on_error

    @property
    def screensaver(self):
        return self._config.screensaver

    @property
    def system_buttons(self):
        return self._config.system_buttons

    @property
    def presets(self):
        return self._config_dict.get('presets', {})

    @property
    def page_elements(self):
        return self._page_elements

    def get_page_element(self, page_id: str) -> PageElement:
        if page_id in self._page_elements:
            return self._page_elements[page_id]

        page_element = PageElement(self._config.pages[page_id])
        self._page_elements[page_id] = page_element

        return page_element

    def has_page(self, page_id: str) -> bool:
        return page_id in self._config.pages

    def get_button(self, page_id: str, button_index: int):
        page = self.get_page_element(page_id)
        if page:
            return page.get_button_at(button_index)

        return None

    def __eq__(self, other: Configuration):
        # Only compare settings that are not `pages`
        return self._config == other._config
