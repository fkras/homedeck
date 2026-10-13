from __future__ import annotations

import asyncio
import copy
import itertools
import os
import sys
import time
import traceback
from dataclasses import asdict, dataclass
from typing import Dict, Optional

import psutil
import yaml
from dotenv import load_dotenv
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from websockets.exceptions import ConnectionClosed

from .configuration import Configuration
from .connecting import spinner_frames
from .d200 import ButtonAction, DeviceInfoEvent, SmallWindowMode, auto_connect
from .dataclasses import PageButtonConfig
from .elements import ButtonElement, InteractionType, PageElement
from .enums import SleepStatus
from .event_bus import EventName, event_bus
from .home_assistant import HomeAssistantUnavailable, HomeAssistantWebSocket
from .icons import CACHE_GENERATED_DIR
from .transitions import crossfade_frames, frame_count
from .utils import deep_merge, local_now

load_dotenv()
HA_HOST = os.getenv('HA_HOST')
HA_ACCESS_TOKEN = os.getenv('HA_ACCESS_TOKEN')


@dataclass
class Prediction:
    ''' A key fading to the state a press should lead to, until Home
    Assistant reports it. '''
    entity_id: str
    state: str
    expiry: asyncio.Task


class DeviceUnresponsiveError(RuntimeError):
    ''' Raised when the deck stops accepting writes and must be reconnected. '''


async def run_alongside(main, *background):
    ''' Run `main` with `background` coroutines alongside it, and return its
    result.

    The background coroutines are meant to last as long as `main` does: if one
    raises, or returns, first, `main` is cancelled and that error raised.
    Whatever is still running at the end is cancelled.

    asyncio.gather does neither: it kept waiting while the others ran, so a
    task that returned took no session down with it, and when one raised the
    others kept running and piled up on every reconnect. '''
    tasks = [asyncio.ensure_future(coro) for coro in (main, *background)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task is not tasks[0]:
                task.result()
                raise RuntimeError(f'{task.get_coro().__qualname__} ended unexpectedly')

        return tasks[0].result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class HomeDeck:
    class ConfigurationFileChangeHandler(FileSystemEventHandler):
        def __init__(self, deck: HomeDeck):
            self._deck = deck
            self._file_path = os.path.abspath(os.path.join('assets', 'configuration.yml'))

        def _is_config(self, path) -> bool:
            if not path:
                return False

            # Paths arrive as str or bytes depending on the platform
            if isinstance(path, bytes):
                path = path.decode('utf-8', errors='replace')

            return os.path.abspath(path) == self._file_path

        def _mark_dirty(self):
            # Just flag it. The reload loop coalesces rapid saves, so unlike a
            # guard here that swallows them, a second save moments after the
            # first is never lost.
            self._deck._need_reload_all = True

        def on_modified(self, event):
            if self._is_config(event.src_path):
                self._mark_dirty()

        def on_created(self, event):
            if self._is_config(event.src_path):
                self._mark_dirty()

        def on_moved(self, event):
            # Editors and atomic writers save to a temp file then rename it
            # over the target, which arrives as a move, not a modify.
            if self._is_config(getattr(event, 'dest_path', None)):
                self._mark_dirty()

    # Coalesce bursts of Home Assistant state_changed events into one redraw
    STATE_CHANGE_DEBOUNCE = 0.1

    # Reconnect after the device has ignored writes for this many seconds
    DEVICE_TIMEOUT = 10

    # Seconds between unlocks sent in case the deck locked itself
    UNLOCK_INTERVAL = 30

    # Wait between attempts to open the deck or reach Home Assistant
    RECONNECT_DELAY = 3
    # While Home Assistant is away: the button the spinner is drawn on (the
    # middle of the grid), its label, and how long each frame stays up
    CONNECTING_BUTTON = 7
    CONNECTING_LABEL = 'Connecting'
    CONNECTING_FRAME_INTERVAL = 0.25

    # Clock format the small window renders: "12H" or "24H"
    CLOCK_FORMAT = '24H'

    # What tapping the small window steps through. The deck has four more
    # clock styles (SmallWindowMode.CLOCK_2 to CLOCK_5).
    SMALL_WINDOW_MODES = (SmallWindowMode.STATS, SmallWindowMode.CLOCK, SmallWindowMode.BACKGROUND)

    # Slots beyond BUTTON_COUNT to blank explicitly. The D200's manifest has a
    # 14th entry ("3_2", the large tile) that the official app writes blank.
    # Set to 0 to send only the 13 keys.
    EXTRA_SLOTS = 1

    # Service calls still waiting on Home Assistant's answer, remembered so a
    # refusal can flag the key that made it
    PENDING_SERVICE_KEYS = 64

    # How often to check whether configuration.yml changed
    CONFIG_POLL_INTERVAL = 0.2
    # Settle time before reading a config file that was just written
    CONFIG_RELOAD_DEBOUNCE = 0.25

    def __init__(self, vendor_id: int = None, product_id: int = None):
        # DECK_VENDOR_ID / DECK_PRODUCT_ID (hex) cover a firmware update that
        # changes the USB IDs; tools/diagnose_device.py prints the new ones
        self._vendor_id = vendor_id or int(os.getenv('DECK_VENDOR_ID') or '2207', 16)
        self._product_id = product_id or int(os.getenv('DECK_PRODUCT_ID') or '0019', 16)

        self._configuration_observer = None
        # Set once the first session is up. Unlike _is_ready, _reset() never
        # clears it, so a failed reconnect retries instead of exiting.
        self._has_started = False
        script_dir = os.path.dirname(os.path.realpath(__file__))
        with open(os.path.join(script_dir, 'yaml', 'configuration.base.yml'), 'r') as fp:
            self._base_configuration_dict = yaml.safe_load(fp.read())

    async def connect(self, retries: int = -1):
        await self._setup()

    def reload_all(self) -> bool:
        if not self._ha:
            return False

        self._need_reload_all = False

        try:
            with open(os.path.join('assets', 'configuration.yml'), 'r', encoding='utf-8') as fp:
                configuration_dict = yaml.safe_load(fp.read())
                configuration_dict = deep_merge(copy.deepcopy(self._base_configuration_dict), configuration_dict)

                new_configuration = Configuration(device=self._device, source_dict=configuration_dict, all_states=self._ha.all_states)

            if not new_configuration or not new_configuration.is_valid():
                # Crash app if the configuration file is invalid on startup
                if not self._configuration:
                    sys.exit(1)

                return

            # Check configuration changed
            print('✅ Configuration changed!')
            self._configuration = new_configuration
            # Force brightness to be re-pushed against the new configuration
            self._current_brightness = None
            self._wake_up()
        except Exception:
            traceback.print_exc()
            return False

        configuration = self._configuration
        self._device.set_label_style(asdict(configuration.label_style))
        self._device.set_sleep_time(configuration.deck_sleep_timeout)
        self._apply_screensaver(configuration.screensaver)

        self.page_go_to('$root', 1, append_stack=True)
        return True

    def _apply_screensaver(self, path):
        ''' Upload the configured screensaver image if it changed. It is ~100KB
        and the deck keeps it, so it isn't re-sent on every config reload. '''
        if not path:
            return

        full_path = os.path.join('assets', path)
        try:
            stamp = (full_path, os.path.getmtime(full_path), getattr(self._device, 'device_info', None))
        except OSError:
            print(f'⚠️ screensaver {full_path} not found')
            return

        if stamp == getattr(self, '_screensaver_sent', None):
            return

        try:
            self._device.set_screensaver(full_path)
        except Exception as e:
            print(f'⚠️ Could not use {full_path} as the screensaver: {e}')
            return
        self._screensaver_sent = stamp

    async def call_ha_service(self, *, domain: str, service: str, service_data: dict, key_index: int = None):
        # A fading key starts its fade now, together with the light, rather
        # than once Home Assistant reports the new state back
        self._predict_press(key_index, domain, service, service_data)

        try:
            message_id = await self._ha.call_service(domain=domain, service=service, service_data=service_data)
        except Exception:
            # A bare pass here hid the two failures that stop a press from ever
            # reaching Home Assistant: a websocket closed under us, and a
            # malformed action that raises before anything is sent.
            traceback.print_exc()
            self._alert_key(key_index)
            self._drop_prediction(key_index)
            return

        if key_index is not None and isinstance(message_id, int):
            # Home Assistant answers later; _on_ha_command_failed flags the key
            # if it refuses the call
            self._pending_service_keys[message_id] = key_index
            while len(self._pending_service_keys) > self.PENDING_SERVICE_KEYS:
                self._pending_service_keys.pop(next(iter(self._pending_service_keys)))

    def _on_ha_command_failed(self, message_id, error):
        key_index = self._pending_service_keys.pop(message_id, None)
        self._alert_key(key_index)
        self._drop_prediction(key_index)

    def _alert_key(self, key_index):
        ''' Flash the deck's "!" on the key whose action failed. '''
        if key_index is None or not self._device or not getattr(self, '_configuration', None):
            return
        if self._configuration.alert_on_error:
            self._device.show_alert(key_index)

    def reload_current_page(self, *, force=False) -> bool:
        return self.reload_page(self._current_page_id, force=force)

    def force_reload_current_page(self) -> bool:
        return self.reload_page(self._current_page_id, force=True)

    def reload_page(self, page_id: str, *, force=False) -> bool:
        # Not while the connecting spinner is up: a late redraw (a debounced
        # state change, an icon finishing its download) would paint over it
        if not self._is_ready:
            return False

        is_sub_page = self._current_page_id != '$root'
        page = self._configuration.get_page_element(page_id)
        changed = page.render_buttons(system_buttons=self._configuration.system_buttons, page_number=self._current_page_number, is_sub_page=is_sub_page, buttons_per_page=self._device.BUTTON_COUNT, all_states=self._ha.all_states)

        # Don't render the same page
        if not force and self._current_page_element == page and not changed:
            return

        if force or self._current_page_element != page:
            # Update full page. Pad to the deck's key count so slots this page
            # doesn't use are explicitly blanked rather than left showing the
            # previous page's buttons.
            #
            # One past BUTTON_COUNT: a capture of the official app shows it
            # writes 14 manifest entries, the last being "3_2" (index 13) with
            # an empty icon. Without it, whatever the firmware drew in that
            # slot - the Ulanzi branding behind the small window - is never
            # cleared. Sending it blank is what the official app does.
            slot_count = self._device.BUTTON_COUNT + self.EXTRA_SLOTS
            buttons = PageElement.generate(page.buttons, slot_count=slot_count)
            # A fade still running would paint its frames over the new page
            self._cancel_transitions()
            self._clear_predictions()
            self._device.set_buttons(buttons)
            self._shown_keys().clear()
            self._shown_keys().update(buttons)
        else:
            # Only update changed buttons
            buttons = PageElement.generate(page.changed_buttons)
            self._send_changed(buttons, page.changed_buttons)

        self._current_page_element = page
        self._schedule_draw_ahead()
        return True

    # -- transitions --------------------------------------------------------

    def _shown_keys(self) -> Dict[int, Optional[dict]]:
        ''' What each key shows now, as sent: {'name', 'icon'} or None. '''
        return self.__dict__.setdefault('_shown', {})

    def _transition_tasks(self) -> Dict[int, asyncio.Task]:
        return self.__dict__.setdefault('_transitions', {})

    def _transition_targets(self) -> Dict[int, dict]:
        ''' What each running fade ends on. '''
        return self.__dict__.setdefault('_fading_to', {})

    def _send_changed(self, buttons: Dict[int, Optional[dict]], elements: Dict[int, ButtonElement], *, predicted=False):
        ''' Send changed keys, cross-fading the ones whose button has a
        `transition` from the image the key shows now. '''
        immediate = {}
        for index, button in buttons.items():
            if not predicted and self._held_by_prediction(index):
                continue

            # Already fading to it, or showing it: the press predicted it
            if button is not None and (
                self._transition_targets().get(index) == button
                or (index not in self._transition_tasks() and self._shown_keys().get(index) == button)
            ):
                continue

            element = elements.get(index)
            old = self._shown_keys().get(index)
            fade = (
                element is not None and element.transition > 0
                and button and old
                and button.get('icon') and old.get('icon')
                and button['icon'] != old['icon']
            )
            if fade and self._start_transition(index, old, button, element.transition):
                continue

            self._cancel_transition(index)
            immediate[index] = button

        if immediate:
            self._device.set_buttons(immediate, update_only=True)
            self._shown_keys().update(immediate)

    def _start_transition(self, index: int, old: dict, new: dict, duration: float) -> bool:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False

        self._cancel_transition(index)
        self._transition_tasks()[index] = loop.create_task(self._run_transition(index, old, new, duration))
        self._transition_targets()[index] = new
        return True

    async def _run_transition(self, index: int, old: dict, new: dict, duration: float):
        steps = frame_count(duration)
        try:
            # Drawn off the event loop: a dozen PNGs is noticeable on an SBC
            frames = await asyncio.get_running_loop().run_in_executor(None, crossfade_frames, old['icon'], new['icon'], steps)
        except Exception as e:
            print(f'⚠️ Could not draw the transition for key {index}: {e}')
            frames = []

        # The label switches with the first frame; only the image fades
        for name in frames:
            frame = {**new, 'icon': name}
            self._device.set_buttons({index: frame}, update_only=True)
            self._shown_keys()[index] = frame
            await asyncio.sleep(duration / steps)

        self._device.set_buttons({index: new}, update_only=True)
        self._shown_keys()[index] = new
        if self._transition_tasks().get(index) is asyncio.current_task():
            del self._transition_tasks()[index]
            self._transition_targets().pop(index, None)

    def _cancel_transition(self, index: int):
        task = self._transition_tasks().pop(index, None)
        self._transition_targets().pop(index, None)
        if task:
            task.cancel()

    def _cancel_transitions(self):
        for task in self._transition_tasks().values():
            task.cancel()
        self._transition_tasks().clear()
        self._transition_targets().clear()

    # -- fading on the press --------------------------------------------------
    #
    # A light that dims in starts as soon as Home Assistant commands it, but
    # Home Assistant only reports the new state once the integration does -
    # from a few hundred ms to seconds later for a cloud one. Waiting for that
    # put the key's fade well behind the light's. So a press on a fading key
    # predicts the state its action leads to and fades there at once; the
    # report then finds the key already there.

    # How long the predicted look waits for Home Assistant to report the state
    PREDICTION_TIMEOUT = 6

    # Actions whose result a press can predict, and the state they leave
    # (None: the opposite of the current one)
    PREDICTABLE_SERVICES = {'toggle': None, 'turn_on': 'on', 'turn_off': 'off'}

    def _predictions(self) -> Dict[int, 'Prediction']:
        return self.__dict__.setdefault('_predicted', {})

    def _look_in_state(self, element: ButtonElement, entity_id: str, state: str):
        ''' How a key would look with its entity in `state`: ({'name', 'icon'},
        its ButtonElement), or None if it would be hidden, gone or has no image
        yet. Resolved as render_buttons resolves it. '''
        states = dict(self._ha.all_states)
        states[entity_id] = {**(states.get(entity_id) or {}), 'state': state}

        resolved = PageElement.resolve_button(copy.deepcopy(element.source), states)
        if any(PageElement.visibility_of(resolved)):
            return None

        predicted = ButtonElement(PageButtonConfig(**copy.deepcopy(resolved)))
        button = PageElement.generate({0: predicted}, log=False)[0]
        if not button or not button.get('icon'):
            return None

        return button, predicted

    def _predict_press(self, index: Optional[int], domain: str, service: str, service_data) -> bool:
        ''' Start a fading key's fade on the press, toward the state its action
        will leave the entity in. Does nothing when that can't be known. '''
        page_id = getattr(self, '_current_page_id', None)
        if index is None or service not in self.PREDICTABLE_SERVICES or page_id is None or not getattr(self, '_configuration', None):
            return False

        element = self._configuration.get_button(page_id, index)
        if element is None or element.transition <= 0 or not element.source or element.transition_trigger != 'press':
            return False

        entity_id = element.source.get('entity_id')
        targets = service_data.get('entity_id') if isinstance(service_data, dict) else None
        # Only the key's own entity, through a domain that acts on it: Home
        # Assistant silently ignores light.toggle on a switch
        if not entity_id or targets not in (entity_id, [entity_id]):
            return False
        if domain not in (entity_id.split('.')[0], 'homeassistant'):
            return False

        # A second press before Home Assistant reported the first toggles
        # from where the first one is heading
        pending = self._predictions().get(index)
        current = pending.state if pending else (self._ha.all_states.get(entity_id) or {}).get('state')
        if current not in ('on', 'off'):
            return False

        target = self.PREDICTABLE_SERVICES[service] or ('off' if current == 'on' else 'on')
        if target == current:
            return False

        look = self._look_in_state(element, entity_id, target)
        if not look:
            return False

        button, predicted = look
        self._clear_prediction(index)
        expiry = asyncio.get_running_loop().create_task(self._expire_prediction(index))
        self._predictions()[index] = Prediction(entity_id=entity_id, state=target, expiry=expiry)
        self._send_changed({index: button}, {index: predicted}, predicted=True)
        return True

    def _held_by_prediction(self, index: int) -> bool:
        ''' Whether an update for this key should wait: a press predicted a
        state Home Assistant hasn't reported yet. A report of it confirms the
        prediction. '''
        prediction = self._predictions().get(index)
        if not prediction:
            return False

        if (self._ha.all_states.get(prediction.entity_id) or {}).get('state') == prediction.state:
            self._clear_prediction(index)
            return False

        return True

    async def _expire_prediction(self, index: int):
        await asyncio.sleep(self.PREDICTION_TIMEOUT)
        prediction = self._predictions().get(index)
        if prediction and prediction.expiry is asyncio.current_task():
            # Home Assistant never reported it: show the key as it is
            del self._predictions()[index]
            self._show_actual(index)

    def _drop_prediction(self, index: Optional[int]):
        ''' The action failed: undo the key's predicted look now. '''
        if index is not None and self._predictions().get(index):
            self._clear_prediction(index)
            self._show_actual(index)

    def _actual_look(self, index: int):
        ''' The key as Home Assistant last reported it: (button, element). '''
        page = getattr(self, '_current_page_element', None)
        element = page.buttons.get(index) if page else None
        return PageElement.generate({index: element}, log=False)[index], element

    def _show_actual(self, index: int):
        button, element = self._actual_look(index)
        self._send_changed({index: button}, {index: element} if element else {}, predicted=True)

    def _clear_prediction(self, index: int):
        prediction = self._predictions().pop(index, None)
        if prediction and prediction.expiry is not asyncio.current_task():
            prediction.expiry.cancel()

    def _clear_predictions(self):
        for index in list(self._predictions()):
            self._clear_prediction(index)

    def _schedule_draw_ahead(self):
        task = self.__dict__.get('_draw_ahead_task')
        if task:
            task.cancel()
        try:
            self._draw_ahead_task = asyncio.get_running_loop().create_task(self._draw_ahead(self._current_page_element))
        except RuntimeError:
            pass

    async def _draw_ahead(self, page: PageElement):
        ''' Draw what pressing a fading key will need - its look in the other
        state and the frames both ways - so the first press doesn't wait on
        the drawing either. Each is drawn once and cached on disk. '''
        # After the page itself is on its way
        await asyncio.sleep(0.5)

        loop = asyncio.get_running_loop()
        for index, element in list(page.buttons.items()):
            if self._current_page_element is not page:
                return
            if element is None or element.transition <= 0 or not element.source or index in self._transition_tasks():
                continue

            entity_id = element.source.get('entity_id')
            state = (self._ha.all_states.get(entity_id) or {}).get('state') if entity_id else None
            shown = self._shown_keys().get(index)
            if state not in ('on', 'off') or not shown or not shown.get('icon'):
                continue

            try:
                other = self._look_in_state(element, entity_id, 'off' if state == 'on' else 'on')
                if other:
                    button, other_element = other
                    await loop.run_in_executor(None, crossfade_frames, shown['icon'], button['icon'], frame_count(other_element.transition))
                    await loop.run_in_executor(None, crossfade_frames, button['icon'], shown['icon'], frame_count(element.transition))
            except Exception as e:
                print(f'⚠️ Could not draw ahead for key {index}: {e}')

            await asyncio.sleep(0)

    async def _read_packets(self):
        button_index = None
        button_state = None

        is_holding = False
        hold_threshold = 0.5
        hold_timer = None

        press_index = -1
        press_time = 0

        def set_timeout(callback, delay):
            async def wrapper():
                await asyncio.sleep(delay)
                await callback()

            return asyncio.create_task(wrapper())

        async def hold_timer_callback():
            nonlocal is_holding, press_time, hold_timer

            current_time = time.time()
            diff_time = current_time - press_time
            if diff_time >= hold_threshold:
                is_holding = True
                hold_timer = None

                await self._on_interacted(InteractionType.HOLD, button_index, button_state)

        async for command in self._device.read_packet():
            if isinstance(command, DeviceInfoEvent):
                if not command.solicited:
                    # Nothing asked: the firmware restarted and lost what it
                    # showed. The device restores brightness and its sleep
                    # timer itself; the picture is ours to repaint.
                    print('⚠️ Deck restarted - repainting')
                    self._device.restore_small_window()
                    self.force_reload_current_page()
                continue

            if isinstance(command, ButtonAction):
                self._last_action_time = time.time()

                button_index = command.index
                button_state = command.state

                # Clear hold_timer
                if hold_timer:
                    hold_timer.cancel()
                    hold_timer = None

                sleep_config = self._configuration.sleep
                if sleep_config and self._sleep_status != SleepStatus.WAKE:
                    if self._sleep_status == SleepStatus.DIM:
                        self._wake_up()
                        # The firmware drops the button images while idle - the
                        # screen stays lit and the clock keeps ticking, but the
                        # buttons go black. HomeDeck can't see that, so a later
                        # partial update (which only sends buttons whose config
                        # changed) leaves them blank. Repaint the whole page.
                        self.force_reload_current_page()
                    elif self._sleep_status == SleepStatus.SLEEP:
                        # Only wake the device up on releasing button
                        if not is_holding and not command.pressed:
                            # Turn the backlight on first so the deck feels
                            # responsive, then repaint behind it.
                            self._wake_up()
                            # Reload small window
                            self._device.restore_small_window()
                            # Reload page
                            self.force_reload_current_page()

                        # Don't accept current action
                        is_holding = False
                        continue

                if command.pressed:
                    press_time = time.time()
                    is_holding = False

                    # Setup hold_timer
                    hold_timer = set_timeout(hold_timer_callback, hold_threshold)

                    if press_index != button_index:
                        press_index = button_index
                else:
                    if not is_holding:
                        await self._on_interacted(InteractionType.TAP, button_index, button_state)

                    is_holding = False

    async def _keep_alive(self):
        tick = 0
        while True:
            await asyncio.sleep(1)
            tick += 1

            # Keep alive. The device can stop accepting writes without the USB
            # device disappearing; reconnect if it does.
            if not self._device_is_alive():
                raise DeviceUnresponsiveError(
                    f'Device stopped responding for {self.DEVICE_TIMEOUT}s - reconnecting'
                )

            # The firmware locks itself onto its screensaver when the host goes
            # quiet for about two minutes, and while locked it reports no key
            # presses - with every write still succeeding, so the check above
            # can't see it. The small window sent each tick keeps that from
            # happening; this undoes it if a stall let it happen anyway.
            # Unlocking an unlocked deck changes nothing on screen.
            if tick % self.UNLOCK_INTERVAL == 0:
                self._device.unlock_screen()

            self._update_sleep_status()

    def _device_is_alive(self) -> bool:
        ''' Send the keep-alive and report whether the device still accepts it. '''
        try:
            self._send_small_window()
        except Exception as e:
            print('⚠️ keep_alive raised:', e)
            self._unresponsive_since = self._unresponsive_since or time.time()
        else:
            # Writes happen on the device's own thread, so a clean return is
            # not proof of life. Ask the device how its writes are going.
            if self._hid_is_responsive():
                self._unresponsive_since = None
                return True

            self._unresponsive_since = self._unresponsive_since or time.time()

        # Tolerate brief hiccups; only give up once the device has been
        # unresponsive for a sustained period.
        elapsed = time.time() - self._unresponsive_since
        if elapsed >= self.DEVICE_TIMEOUT:
            return False

        print(f'⚠️ Device not responding ({elapsed:.0f}s)')
        return True

    def _hid_is_responsive(self) -> bool:
        ''' Check the device still answers, without drawing anything.

        Deliberately does NOT send a protocol packet. An earlier version
        re-sent a small-window packet here, which made the deck redraw the
        clock area a second time every second and flicker visibly. The device
        reports whether its writes are failing and reads a string descriptor,
        which exercises the same USB path without touching the display.
        '''
        if not self._device:
            return False

        try:
            responsive = self._device.is_responsive()
        except Exception as e:
            print('⚠️ Device did not answer:', e)
            return False

        if not responsive:
            print('⚠️ Device did not answer:', getattr(self._device, 'last_error', None) or 'no reply')
        return responsive

    def _update_sleep_status(self):
        ''' Re-evaluate idle timeouts and the time-of-day schedule once per tick. '''
        if not self._configuration:
            return

        # A schedule change must take effect even while the device is asleep or
        # has never been touched, so it is evaluated before the early returns.
        schedule_brightness = self._scheduled_brightness()
        if schedule_brightness != self._active_schedule_brightness:
            self._active_schedule_brightness = schedule_brightness
            # Re-apply brightness for the new window at the current sleep status
            self._apply_brightness()

        if self._sleep_status == SleepStatus.SLEEP or self._last_action_time <= 0:
            return

        sleep_config = self._configuration.sleep
        if not sleep_config:
            return

        diff = time.time() - self._last_action_time
        if sleep_config.sleep_timeout > 0 and diff > sleep_config.sleep_timeout:
            self._sleep()
        elif sleep_config.dim_timeout > 0 and self._sleep_status != SleepStatus.DIM and diff > sleep_config.dim_timeout:
            # Dim device
            self._sleep_status = SleepStatus.DIM
            self._apply_brightness()

    def _scheduled_brightness(self):
        ''' Brightness forced by the active time-of-day window, if any. '''
        sleep_config = self._configuration.sleep if self._configuration else None
        if not sleep_config:
            return None

        entry = sleep_config.active_schedule()
        if not entry:
            return None

        # An entry without an explicit brightness falls back to dim_brightness
        return entry.brightness if entry.brightness is not None else sleep_config.dim_brightness

    def _target_brightness(self):
        ''' The brightness the device should currently show. '''
        configuration = self._configuration
        if self._sleep_status == SleepStatus.SLEEP:
            return 0

        awake = configuration.brightness
        sleep_config = configuration.sleep
        scheduled = self._active_schedule_brightness

        # Pressing a button always restores the normal brightness, day or night.
        if self._sleep_status != SleepStatus.DIM:
            return awake

        if scheduled is not None:
            # Inside a scheduled window (e.g. overnight): idle down to the
            # scheduled level rather than the generic dim level.
            return scheduled

        if not sleep_config:
            return awake

        if sleep_config.schedule:
            # A schedule is configured but we're outside every window, so this
            # is "daytime": stay at full brightness instead of idling down.
            # Without this a wall-mounted deck would dim all day long.
            return awake

        # No schedule at all: classic idle dimming.
        return sleep_config.dim_brightness

    def _apply_brightness(self):
        brightness = self._target_brightness()
        if brightness == self._current_brightness:
            return

        self._current_brightness = brightness
        self._device.set_brightness(brightness)

    def _wake_up(self):
        self._sleep_status = SleepStatus.WAKE
        self._last_action_time = time.time()
        self._active_schedule_brightness = self._scheduled_brightness()
        self._apply_brightness()

    def _sleep(self):
        # Sleep device
        self._sleep_status = SleepStatus.SLEEP
        self._last_action_time = time.time()
        self._apply_brightness()

    async def _on_interacted(self, interaction: InteractionType, index: int, state: object):
        print('👆', interaction.value, index, state)

        # Small window button
        if index == 13:
            if interaction == InteractionType.TAP:
                self._cycle_small_window_mode()
            elif interaction == InteractionType.HOLD:
                # Sleep
                self._sleep()
                # Wait for a bit
                await asyncio.sleep(0.2)
                # Restore to the previous mode
                self._device.restore_small_window()
            return

        button = self._configuration.get_button(self._current_page_id, index)
        if button:
            await button.trigger_action(self, interaction, index)

    def _send_small_window(self):
        ''' Send the small window's periodic update, with real statistics.

        The official app sends all seven fields once a second -

            1|5|35|15:10:28|0|12H|           mode|cpu|mem|time|gpu|clock|date

        - and this is HomeDeck's keep-alive as well.
        '''
        stats = self._system_stats()
        self._device.set_small_window_data(cpu=stats['cpu'], mem=stats['mem'], gpu=stats['gpu'], now=local_now())

    def _system_stats(self) -> dict:
        ''' CPU and memory percentages for the small window's STATS mode.

        There is no temperature field. Every field the official app sends is
        accounted for - mode, cpu, mem, time, gpu, clock format, weekday - and
        in a capture its "gpu" value ranged 0-5 alongside CPU figures of 6-26,
        which is a utilisation percentage, not degrees. Writing a temperature
        there would render as a bogus GPU percentage, so this reports 0 for
        GPU exactly as the app does on a machine without one. Use
        _cpu_temperature() and a Home Assistant sensor if you want the Pi's
        temperature on a button.
        '''
        stats = {'cpu': 0, 'mem': 0, 'gpu': 0}

        try:
            # Non-blocking: percentage since the previous call, which the
            # one-second keep-alive loop makes meaningful.
            stats['cpu'] = int(psutil.cpu_percent())
            stats['mem'] = int(psutil.virtual_memory().percent)
        except Exception:
            pass

        return stats

    def _cpu_temperature(self):
        ''' CPU temperature in whole degrees Celsius, or None. '''
        try:
            sensors = psutil.sensors_temperatures()
        except Exception:
            sensors = {}

        # Prefer a recognisable CPU sensor, else take the first reading going
        for key in ('cpu_thermal', 'coretemp', 'k10temp', 'soc_thermal', 'cpu-thermal'):
            readings = sensors.get(key)
            if readings:
                return int(readings[0].current)

        for readings in sensors.values():
            if readings:
                return int(readings[0].current)

        # DietPi on Orange Pi doesn't always expose a psutil sensor, so fall
        # back to the raw thermal zone (millidegrees).
        try:
            with open('/sys/class/thermal/thermal_zone0/temp', 'r') as fp:
                return int(int(fp.read().strip()) / 1000)
        except Exception:
            return None

    def _cycle_small_window_mode(self):
        ''' Step the small window to the next mode and redraw it.

        The `state` byte the deck reports for this button alternates between
        values like 1 and 200 - which match the CLOCK and CLOCK_2 mode IDs, so
        it looks like the firmware's own idea of the mode, not the one we last
        sent. Step through our own list and push the result, which
        set_small_window_mode() alone does not do.
        '''
        modes = list(self.SMALL_WINDOW_MODES)
        try:
            index = modes.index(self._device.small_window_mode)
        except (AttributeError, ValueError):
            index = 0

        next_mode = modes[(index + 1) % len(modes)]
        self._device.set_small_window_mode(next_mode.value)
        self._device.restore_small_window()

        print('🕐 Small window mode:', next_mode.name)

    def _reset(self):
        self._is_ready = False
        self._cancel_transitions()
        self._clear_predictions()
        self._shown_keys().clear()

        if hasattr(self, '_device') and self._device:
            self._device.close()
        self._device = None

        self._ha = HomeAssistantWebSocket(HA_HOST, HA_ACCESS_TOKEN)

        self._current_page_element = None
        self._pages_stack = []

        self._need_reload_all = True
        self._configuration = None

        self._sleep_status = SleepStatus.WAKE
        self._last_action_time = time.time()
        self._current_brightness = None
        self._active_schedule_brightness = None
        self._unresponsive_since = None

        # Debounce timer for Home Assistant state changes
        self._ha_reload_timer = None
        self._pending_service_keys = {}

    async def _setup(self):
        # Setup event bus
        event_bus.subscribe(EventName.DECK_RELOAD, self.reload_current_page)
        event_bus.subscribe(EventName.DECK_FORCE_RELOAD, self.force_reload_current_page)

        while True:
            self._reset()

            try:
                await self._connect_device()
                await self._run_home_assistant()
            except Exception:
                traceback.print_exc()

                # Crash app if error on startup. Only the first one: _reset()
                # clears _is_ready on every reconnect, so checking that here
                # made any failed reconnect exit the whole process.
                if not self._has_started:
                    sys.exit(1)
            finally:
                self._is_ready = False

                try:
                    self._device.close()
                except Exception:
                    pass

                try:
                    await self._ha.disconnect()
                except Exception:
                    pass

                await asyncio.sleep(self.RECONNECT_DELAY)

    async def _connect_device(self):
        device = None
        while True:
            try:
                device = auto_connect(
                    self._vendor_id,
                    self._product_id,
                    icon_dir=CACHE_GENERATED_DIR,
                    clock_24h=self.CLOCK_FORMAT == '24H',
                    now=local_now,
                )
                if device:
                    self._device = device
                    # What the official app does first: ask who it is talking
                    # to. The handshake also unlocks a deck that locked onto
                    # its screensaver while nothing was talking to it, and
                    # sends the sleep-time setting; reload_all() applies the
                    # configured value once loaded.
                    info = await device.handshake(sleep_time=0)
                    if info:
                        print(f'Device connected: {info.device_type} {info.serial_number}, firmware {info.firmware_version}')
                    else:
                        print('Device connected (it did not report its info)')
                    return

                print('Could not find any device')
            except Exception as e:
                try:
                    device.close()
                except Exception:
                    pass

                print('Could not open the device:', e)

            await asyncio.sleep(self.RECONNECT_DELAY)

    async def _run_home_assistant(self):
        ''' Keep a Home Assistant session running on the open deck.

        Home Assistant going away - a restart, an update, the network - is
        waited out here, with the deck left open and a spinner on it. This only
        returns by raising, for trouble that needs the deck itself reopened.
        '''
        while True:
            await run_alongside(
                self._connect_home_assistant(),
                self._show_connecting(),
                self._discard_presses(),
                self._keep_alive(),
            )

            try:
                self._ha.on_event('state_changed', self._ha_on_state_changed)
                await self._ha.subscribe_events('state_changed')

                self._is_ready = True
                self._has_started = True
                # Reload against the fresh states. That repaints the whole
                # page over the spinner, and drops any page left over from
                # before the outage.
                self._need_reload_all = True

                await run_alongside(
                    self._ha.listen(),
                    self._read_packets(),
                    self._keep_alive(),
                    self._setup_hot_reload(),
                )
            except (HomeAssistantUnavailable, ConnectionClosed) as e:
                print('⚠️ Lost Home Assistant:', e)
            finally:
                self._is_ready = False
                try:
                    await self._ha.disconnect()
                except Exception:
                    pass

    async def _connect_home_assistant(self):
        ''' Connect to Home Assistant, retrying until it answers. '''
        while True:
            self._ha = HomeAssistantWebSocket(HA_HOST, HA_ACCESS_TOKEN)
            self._ha.on_command_failed = self._on_ha_command_failed
            self._pending_service_keys = {}
            try:
                await self._ha.connect()
                return
            except HomeAssistantUnavailable as e:
                # Expected while it restarts, so one line rather than a
                # traceback every few seconds
                print(f'⚠️ Home Assistant unavailable ({e}) - retrying in {self.RECONNECT_DELAY}s')

            await asyncio.sleep(self.RECONNECT_DELAY)

    async def _show_connecting(self):
        ''' Animate a spinner on the deck until cancelled. '''
        self._cancel_transitions()
        self._clear_predictions()
        self._shown_keys().clear()
        frames = spinner_frames(self._device.ICON_WIDTH)
        slot_count = self._device.BUTTON_COUNT + self.EXTRA_SLOTS
        # The page that was up is being replaced, and the session that follows
        # starts again from the root page
        self._current_page_element = None
        self._pages_stack = []

        for frame in itertools.count():
            button = {'icon': frames[frame % len(frames)], 'name': self.CONNECTING_LABEL}
            if frame == 0:
                # Blank every other slot, as a full page does
                buttons = dict.fromkeys(range(slot_count))
                buttons[self.CONNECTING_BUTTON] = button
                self._device.set_buttons(buttons)
            else:
                self._device.set_buttons({self.CONNECTING_BUTTON: button}, update_only=True)

            await asyncio.sleep(self.CONNECTING_FRAME_INTERVAL)

    async def _discard_presses(self):
        ''' Read and drop button presses while Home Assistant is away.

        Unread presses wait in the HID buffer, so without this every press made
        during an outage would fire its action once the connection came back.
        A press still wakes a dimmed deck, so the spinner can be seen.
        '''
        async for command in self._device.read_packet():
            if isinstance(command, ButtonAction) and command.pressed and self._configuration:
                self._wake_up()

    async def _ha_on_state_changed(self, _):
        # Don't redraw a screen nobody can see
        if self._sleep_status == SleepStatus.SLEEP:
            return

        # Home Assistant often emits a burst of state_changed events at once
        # (e.g. a scene turning on six lights). Coalesce them into one redraw.
        if self._ha_reload_timer:
            self._ha_reload_timer.cancel()

        async def debounced_reload():
            try:
                await asyncio.sleep(self.STATE_CHANGE_DEBOUNCE)
                self._ha_reload_timer = None

                # A partial update is enough: the images on the deck are
                # whatever we last sent. Forcing a full page here would repaint
                # ~36KB on every state_changed event Home Assistant emits,
                # which for a deck sitting dimmed is a redraw a minute.
                self.reload_current_page()
            except asyncio.CancelledError:
                # Superseded by a newer event; the newer timer will redraw
                pass

        self._ha_reload_timer = asyncio.create_task(debounced_reload())

    async def _setup_hot_reload(self):
        print('Setting up hot reload')

        if not self._configuration_observer:
            event_handler = self.ConfigurationFileChangeHandler(self)
            observer = Observer()
            observer.schedule(event_handler, path='./assets', recursive=False)

            observer.start()
            self._configuration_observer = observer

        while True:
            if self._need_reload_all:
                # Let a burst of events settle so a file still being written
                # is read once, complete, rather than half-way through.
                await asyncio.sleep(self.CONFIG_RELOAD_DEBOUNCE)
                self.reload_all()

            await asyncio.sleep(self.CONFIG_POLL_INTERVAL)

    def page_go_to(self, page_id: str, page_number: int = 1, append_stack=True):
        if not self._configuration.has_page(page_id):
            print('Invalid page:', page_id)
            return

        if append_stack:
            self._pages_stack.append((page_id, page_number))

        self._current_page_id = page_id
        self._current_page_number = page_number
        self.reload_current_page()

    def page_go_back(self):
        # Remove current page
        if self._pages_stack:
            self._pages_stack.pop()

        # Get last page
        target_page, page_number = self._pages_stack[-1] if self._pages_stack else ('$root', 1)
        print(target_page, page_number)
        self.page_go_to(target_page, page_number=page_number, append_stack=False)

    def page_go_previous(self):
        # Update page number in stack
        target_page, page_number = self._pages_stack[-1]
        page_number = max(1, self._current_page_number - 1)
        self._pages_stack[-1] = (target_page, page_number)

        self._current_page_number = page_number
        self.reload_current_page()

    def page_go_next(self):
        # Update page number in stack
        target_page, page_number = self._pages_stack[-1]
        page_number = self._current_page_number + 1
        self._pages_stack[-1] = (target_page, page_number)

        self._current_page_number = page_number
        self.reload_current_page()
