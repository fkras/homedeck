''' The spinner shown while Home Assistant is unreachable.

Drawn locally with Pillow rather than through an icon source: the usual
sources download over the network, which is exactly what may be down, and
nothing here depends on the configuration, which needs Home Assistant's states
before it can load.
'''

import os
from typing import List

from PIL import Image, ImageDraw

from .icons import CACHE_GENERATED_DIR

FRAME_COUNT = 12
# Home Assistant's brand blue on a faint track
ARC_COLOR = (65, 189, 245, 255)
TRACK_COLOR = (255, 255, 255, 40)
ARC_SWEEP = 100


def spinner_frames(size: int, frame_count: int = FRAME_COUNT) -> List[str]:
    ''' Draw the spinner's frames into the generated-icon cache, where the
    device driver picks icons up from, and return their file names. '''
    os.makedirs(CACHE_GENERATED_DIR, exist_ok=True)

    names = []
    for frame in range(frame_count):
        name = f'homedeck-connecting-{size}-{frame}.png'
        path = os.path.join(CACHE_GENERATED_DIR, name)
        if not os.path.exists(path):
            _draw_frame(size, 360 * frame / frame_count).save(path, 'PNG')

        names.append(name)

    return names


def _draw_frame(size: int, start: float) -> Image.Image:
    # Drawn at 4x and scaled down so the arc's edges are anti-aliased
    scale = 4
    canvas = size * scale
    width = canvas // 14
    margin = canvas // 4
    box = (margin, margin, canvas - margin, canvas - margin)

    img = Image.new('RGBA', (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse(box, outline=TRACK_COLOR, width=width)
    # Pillow measures angles clockwise from 3 o'clock; start from 12
    draw.arc(box, start - 90, start - 90 + ARC_SWEEP, fill=ARC_COLOR, width=width)

    return img.resize((size, size), Image.LANCZOS)
