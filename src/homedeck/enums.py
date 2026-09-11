from enum import Enum


class ButtonElementAction(Enum):
    PAGE_BACK = '$page.back'
    PAGE_PREVIOUS = '$page.previous'
    PAGE_NEXT = '$page.next'
    PAGE_GO_TO = '$page.go_to'


class InteractionType(Enum):
    TAP = 'tap'
    HOLD = 'hold'


class IconSource(Enum):
    BLANK = 'blank'
    LOCAL = 'local'
    URL = 'url'
    TEXT = 'text'
    MATERIAL_DESIGN = 'mdi'
    PHOSPHOR = 'pi'
    CUSTOM_BRAND = 'cbi'


class PhosphorIconVariant:
    THIN = 'thin'
    LIGHT = 'light'
    REGULAR = 'regular'
    BOLD = 'bold'
    FILL = 'fill'
    DUOTONE = 'duotone'


class SleepStatus:
    WAKE = 'wake'
    DIM = 'dim'
    SLEEP = 'sleep'


class MaterialYouScheme:
    PRIMARY = 'primary'
    ON_PRIMARY = 'on-primary'
    PRIMARY_CONTAINER = 'primary-container'
    ON_PRIMARY_CONTAINER = 'on-primary-container'
    SECONDARY = 'secondary'
    ON_SECONDARY = 'on-secondary'
    SECONDARY_CONTAINER = 'secondary-container'
    ON_SECONDARY_CONTAINER = 'on-secondary-container'
    TERTIARY = 'tertiary'
    ON_TERTIARY = 'on-tertiary'
    TERTIARY_CONTAINER = 'tertiary-container'
    ON_TERTIARY_CONTAINER = 'on-tertiary-container'
    ERROR = 'error'
    ON_ERROR = 'on-error'
    ERROR_CONTAINER = 'error-container'
    ON_ERROR_CONTAINER = 'on-error-container'
    BACKGROUND = 'background'
    ON_BACKGROUND = 'on-background'
    SURFACE = 'surface'
    ON_SURFACE = 'on-surface'
    SURFACE_VARIANT = 'surface-variant'
    ON_SURFACE_VARIANT = 'on-surface-variant'
    OUTLINE = 'outline'
    OUTLINE_VARIANT = 'outline-variant'
    SHADOW = 'shadow'
    SCRIM = 'scrim'
    INVERSE_SURFACE = 'inverse-surface'
    INVERSE_ON_SURFACE = 'inverse-on-surface'
    INVERSE_PRIMARY = 'inverse-primary'
