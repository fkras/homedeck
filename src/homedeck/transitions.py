''' Cross-fading a key from one image to the next.

The D200 has no animation of its own that HomeDeck knows of, so a transition
is a few in-between images sent as ordinary partial updates. Each costs a
few KB over the deck's full-speed USB endpoint (~80 ms), which caps the rate
at roughly 10 frames a second. If the deck falls behind, the driver folds a
waiting update for a key into the next one, so stale frames are dropped and
the final image is never late.
'''
import os
import threading
from typing import List

from PIL import Image

from .icons import CACHE_GENERATED_DIR, stable_hash, touch_cache_file

# Seconds between frames: about what the USB link sustains for one key
FRAME_INTERVAL = 0.1
MAX_FRAMES = 20


def frame_count(duration: float, interval: float = FRAME_INTERVAL) -> int:
    ''' Steps in a fade of `duration` seconds, counting the final image. '''
    return max(1, min(MAX_FRAMES, round(duration / interval)))


def ease(t: float) -> float:
    ''' Smoothstep: starts and ends gently, like a dimmer. '''
    return t * t * (3 - 2 * t)


def crossfade_frames(old_icon: str, new_icon: str, steps: int, cache_dir: str = CACHE_GENERATED_DIR) -> List[str]:
    ''' Write the in-between images from `old_icon` to `new_icon` (file names
    in `cache_dir`) and return their names, in order. The final image is
    `new_icon` itself and isn't included.

    Frames are named after both ends, so toggling a light back and forth
    reuses them rather than drawing them again. '''
    if steps <= 1:
        return []

    key = stable_hash(f'{old_icon}>{new_icon}>{steps}')
    names = [f'fade-{key}-{i}.png' for i in range(1, steps)]
    paths = [os.path.join(cache_dir, name) for name in names]

    if all(os.path.exists(path) for path in paths):
        for path in paths:
            touch_cache_file(path)
        return names

    with Image.open(os.path.join(cache_dir, old_icon)) as a, Image.open(os.path.join(cache_dir, new_icon)) as b:
        end = b.convert('RGBA')
        start = a.convert('RGBA')
        if start.size != end.size:
            start = start.resize(end.size)

        for i, path in enumerate(paths, start=1):
            # Written aside and moved into place: a press and the drawing
            # ahead can make the same frame at once, and the driver must never
            # zip a half-written file
            tmp = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
            Image.blend(start, end, ease(i / steps)).save(tmp, 'PNG')
            os.replace(tmp, path)

    return names
