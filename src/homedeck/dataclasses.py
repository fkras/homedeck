from __future__ import annotations

import copy
from dataclasses import dataclass, field, fields
from typing import Dict, List, Optional, Tuple, Union

from deepdiff import DeepDiff
from strmdck.device import DeckDevice

from .enums import ButtonElementAction, IconSource
from .template import has_jinja_template
from .utils import (
    apply_presets,
    current_minutes_of_day,
    normalize_button_positions,
    normalize_hex_color,
    parse_time_of_day,
)

FONTS_MAP = {
    1: 'Source Han Sans SC',
    2: 'FZShuSong-Z01',
    3: 'DejaVu Sans',
    4: 'Bareona',
    5: 'Crimson Text',
    6: 'Magiera',
    7: 'Syke',
    8: 'Roboto',
}


@dataclass
class ScheduleConfig:
    ''' A time-of-day window that overrides brightness, e.g. dimming overnight. '''
    # Field is named "from_" because "from" is a Python keyword. MainConfig
    # remaps the YAML key "from" before constructing this.
    from_: str = field(default='00:00')
    to: str = field(default='00:00')
    brightness: Optional[int] = field(default=None)

    from_minutes: int = field(init=False, default=0)
    to_minutes: int = field(init=False, default=0)

    def __post_init__(self):
        self.from_minutes = parse_time_of_day(self.from_)
        self.to_minutes = parse_time_of_day(self.to)

    def contains(self, minutes: int) -> bool:
        ''' Whether a minutes-since-midnight value falls inside this window. '''
        if self.from_minutes == self.to_minutes:
            # Zero-length window never matches; use 00:00-24:00 for "always"
            return False

        if self.from_minutes < self.to_minutes:
            # Same-day window, e.g. 09:00 -> 17:00
            return self.from_minutes <= minutes < self.to_minutes

        # Window wraps midnight, e.g. 22:00 -> 06:00
        return minutes >= self.from_minutes or minutes < self.to_minutes


@dataclass
class SleepConfig:
    dim_brightness: Optional[int] = field(default=1)
    dim_timeout: Optional[int] = field(default=0)

    sleep_timeout: Optional[int] = field(default=0)

    # Time-of-day brightness windows. The first matching window wins.
    schedule: Optional[List[Dict]] = field(default_factory=lambda: [])

    def __post_init__(self):
        schedule = []
        for entry in self.schedule or []:
            if not isinstance(entry, dict):
                continue

            entry = dict(entry)
            # "from" is a reserved word in Python, map it onto "from_"
            if 'from' in entry:
                entry['from_'] = entry.pop('from')

            schedule.append(ScheduleConfig(**entry))

        self.schedule = schedule

    def active_schedule(self, minutes: Optional[int] = None) -> Optional[ScheduleConfig]:
        ''' The schedule window covering the given time, or None. '''
        if not self.schedule:
            return None

        if minutes is None:
            minutes = current_minutes_of_day()

        for entry in self.schedule:
            if entry.contains(minutes):
                return entry

        return None


@dataclass
class LabelStyleConfig:
    align: str = field(default='bottom')
    color: str = field(default='FFFFFF')
    font: int = field(default=1)
    show_title: bool = field(default=True)
    size: int = field(default=11)
    weight: int = field(default=80)

    font_name: Optional[str] = field(init=False)

    def __post_init__(self):
        self.color = normalize_hex_color(self.color)

        try:
            self.font_name = FONTS_MAP[self.font]
        except Exception:
            self.font_name = FONTS_MAP[1]


@dataclass
class PageButtonActionConfig:
    entity_id: str

    action: str
    data: Optional[object] = field(default_factory=lambda: {})

    def __post_init__(self):
        if isinstance(self.data, dict) and self.entity_id and 'entity_id' not in self.data:
            self.data['entity_id'] = self.entity_id


@dataclass
class PageButtonConfig:
    entity_id: Optional[str] = None

    tap_action: Optional[PageButtonActionConfig] = None
    hold_action: Optional[PageButtonActionConfig] = None
    name: Optional[str] = None
    domain: Optional[str] = None
    visibility: Optional[Union[bool, str, None]] = True
    presets: Optional[Union[str | List[str]]] = None

    states: Optional[Dict[str, Dict]] = field(default_factory=lambda: {})
    is_dynamic: Optional[bool] = False

    material_you_color: Optional[str] = field(default=None, metadata={'icon': True, 'text_icon': True})
    material_you_scheme: Optional[str] = field(default=None, metadata={'icon': True, 'text_icon': True})

    # Icon fields
    icon_variant: Optional[str] = field(default=None, metadata={'icon': True})
    icon: Optional[str] = field(default=None, metadata={'icon': True})
    icon_size: Optional[int] = field(default=None, metadata={'icon': True})
    icon_size_mode: Optional[str] = field(default='cover', metadata={'icon': True})
    icon_padding: Optional[int] = field(default=None, metadata={'icon': True})
    icon_color: Optional[str] = field(default=None, metadata={'icon': True})
    icon_background_color: Optional[str] = field(default=None, metadata={'icon': True})
    icon_offset: Optional[Tuple[int, int]] = field(default_factory=lambda: (0, 0), metadata={'icon': True})
    icon_border_radius: Optional[int] = field(default=None, metadata={'icon': True})
    icon_border_width: Optional[int] = field(default=None, metadata={'icon': True})
    icon_border_color: Optional[str] = field(default=None, metadata={'icon': True})
    icon_brightness: Optional[int] = field(default=None, metadata={'icon': True})

    max_width: Optional[int] = field(default=0, metadata={'icon': True})
    max_height: Optional[int] = field(default=0, metadata={'icon': True})

    text: Optional[str] = field(default=None, metadata={'text_icon': True})
    text_color: Optional[str] = field(default=None, metadata={'text_icon': True})
    text_align: Optional[str] = field(default=None, metadata={'text_icon': True})
    text_font: Optional[str] = field(default=None, metadata={'text_icon': True})
    text_size: Optional[int] = field(default=None, metadata={'text_icon': True})
    text_offset: Optional[int] = field(default=None, metadata={'text_icon': True})

    additional_icons: Optional[List[Dict]] = field(default_factory=lambda: [])

    icon_source: Optional[IconSource] = field(init=False, default=None)
    icon_name: Optional[str] = field(init=False, default=None)

    def __post_init__(self):
        if self.tap_action:
            self.tap_action = PageButtonActionConfig(entity_id=self.entity_id, **self.tap_action)

        if self.hold_action:
            self.hold_action = PageButtonActionConfig(entity_id=self.entity_id, **self.hold_action)

        # Normalize presets
        if not self.presets:
            self.presets = []

        if self.presets and not isinstance(self.presets, list):
            self.presets = [self.presets]

    @staticmethod
    def transform(button: dict, *, device: 'DeckDevice', all_states: dict, presets_config={}, is_states=False):
        # Ignore null button
        if not button:
            return button

        if isinstance(button, str):
            if button == '$break':
                return button
            else:
                # Ignore unknown buttons
                return button

        if not is_states and presets_config and 'presets' not in button:
            default_style = None

            if 'tap_action' in button:
                action = button['tap_action']['action']
                if action == ButtonElementAction.PAGE_GO_TO.value:
                    default_style = '$page.go_to'

            # Set default presets
            default_style = default_style or '$default'
            button['presets'] = [default_style]

            entity_id = button.get('entity_id', '')
            domain = button.get('domain')
            if entity_id and not domain:
                domain = entity_id.split('.')[0]
                button['domain'] = domain

            # Set domain's style
            if domain:
                domain_style = f'${domain}'
                button['presets'].append(domain_style)

                if entity_id and (domain == 'binary_sensor' or domain == 'sensor'):
                    # Add device_class to binary_sensor
                    device_class = all_states.get(entity_id, {}).get('attributes', {}).get('device_class')
                    if device_class:
                        # Domain with device_class
                        domain_style += f'.{device_class}'
                        button['presets'].append(domain_style)

        if 'presets' in button:
            # Apply presets
            button = apply_presets(source=button, presets_config=presets_config)

        if not is_states:
            button.setdefault('icon_size', (device.ICON_WIDTH, device.ICON_HEIGHT))

            # Dimension
            button.setdefault('max_width', device.ICON_WIDTH)
            button.setdefault('max_height', device.ICON_HEIGHT)

            # Visibility
            if 'visibility' not in button:
                button['visibility'] = True

            # Check template string
            button['is_dynamic'] = 'states' in button or has_jinja_template(button)

            # Transform states
            if 'states' in button:
                for state in button['states']:
                    button['states'][state] = PageButtonConfig.transform(button['states'][state], device=device, all_states=all_states, presets_config=presets_config, is_states=True)

        if 'name' in button:
            button['name'] = str(button['name'])

        return button


# Icon fields for calculating unique ID in Icon._calculate_id()
ICON_FIELDS = [field.name for field in fields(PageButtonConfig) if field.metadata and field.metadata.get('icon')]
TEXT_ICON_FIELDS = [field.name for field in fields(PageButtonConfig) if field.metadata and field.metadata.get('text_icon')]


@dataclass(init=False)
class PageConfig:
    id: str
    buttons: List[PageButtonConfig]  # Input is `str`
    buttons_raw: Dict = None

    name: Optional[str] = None
    button_positions: Optional[Dict[str, Dict]] = field(default_factory=lambda: {})

    def __init__(self, id: str, buttons: dict, button_positions: dict = {}, name: str = None):
        self.id = id
        self.buttons = []
        self.name = name
        self.button_positions = button_positions

        # Set `buttons` string to buttons_raw
        self.buttons_raw = copy.deepcopy(buttons)

    def post_setup(self, *, device: 'DeckDevice', main_config: MainConfig, all_states: dict, presets_config={}):
        # Merge button positions
        self.button_positions = normalize_button_positions(self.button_positions or {})
        # TODO: FIX this
        # self.button_positions = deep_merge(main_config.button_positions, self.button_positions)

        # Transform button_raws
        for index, button in enumerate(self.buttons_raw):
            self.buttons_raw[index] = PageButtonConfig.transform(button, device=device, all_states=all_states, presets_config=presets_config)


@dataclass
class SystemButtonConfig:
    button: Dict
    position: Optional[int] = 0


@dataclass
class MainConfig:
    brightness: int = field(default=100)
    label_style: LabelStyleConfig = None
    sleep: SleepConfig = None

    pages: Dict[str, PageConfig] = field(default_factory=lambda: {})
    presets: Dict[str, Dict] = field(default_factory=lambda: {})

    system_buttons: Dict[str, Dict] = field(default_factory=lambda: {})

    def __post_init__(self):
        if self.label_style:
            self.label_style = LabelStyleConfig(**self.label_style)

        if self.sleep:
            self.sleep = SleepConfig(**self.sleep)

            # Limit sleep.dim_brightness <= brightness
            self.sleep.dim_brightness = min(self.sleep.dim_brightness, self.brightness)

    def post_setup(self, device: DeckDevice, all_states: dict):
        # System buttons
        system_keys = list(self.system_buttons.keys())
        for key in system_keys:
            value = self.system_buttons[key]
            if value.get('button'):
                value['button'] = PageButtonConfig.transform(value['button'], device=device, all_states=all_states, presets_config=self.presets)

            self.system_buttons[ButtonElementAction(key)] = SystemButtonConfig(**value)
            del self.system_buttons[key]

        # Setup pages
        for page_id, page_value in self.pages.items():
            page_config = PageConfig(id=page_id, **page_value)
            page_config.post_setup(device=device, main_config=self, all_states=all_states, presets_config=self.presets)

            self.pages[page_id] = page_config

    def __eq__(self, other: MainConfig):
        same = self.brightness == other.brightness and self.label_style == other.label_style and self.sleep == other.sleep and not DeepDiff(self.presets, other.presets)
        if not same:
            return False

        # Compage pages
        diff = DeepDiff(self.pages, other.pages)
        return not diff
