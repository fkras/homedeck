from __future__ import annotations

import asyncio
import copy
import os
import sys
import time
import traceback
from dataclasses import asdict

import psutil
import yaml
from dotenv import load_dotenv
from strmdck.device import ButtonAction
from strmdck.device_manager import auto_connect
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .configuration import Configuration
from .elements import InteractionType, PageElement
from .enums import SleepStatus
from .event_bus import EventName, event_bus
from .home_assistant import HomeAssistantWebSocket
from .utils import deep_merge, local_now

load_dotenv()
HA_HOST = os.getenv('HA_HOST')
HA_ACCESS_TOKEN = os.getenv('HA_ACCESS_TOKEN')


class DeviceUnresponsiveError(RuntimeError):
    ''' Raised when the deck stops accepting writes and must be reconnected. '''


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

    # Clock format the small window renders: "12H" or "24H"
    CLOCK_FORMAT = '24H'

    # Slots beyond BUTTON_COUNT to blank explicitly. The D200's manifest has a
    # 14th entry ("3_2") that the official app writes and strmdck does not.
    # Set to 0 to send only the buttons strmdck knows about.
    EXTRA_SLOTS = 1

    # How often to check whether configuration.yml changed
    CONFIG_POLL_INTERVAL = 0.2
    # Settle time before reading a config file that was just written
    CONFIG_RELOAD_DEBOUNCE = 0.25

    def __init__(self, vendor_id: int = 0x2207, product_id: int = 0x0019):
        self._vendor_id = vendor_id
        self._product_id = product_id

        self._configuration_observer = None
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
        # await self._write_packet(b'\x01')  # Not sure what this is for
        self._device.set_label_style(asdict(configuration.label_style))

        self.page_go_to('$root', 1, append_stack=True)
        return True

    async def call_ha_service(self, *, domain: str, service: str, service_data: dict):
        try:
            await self._ha.call_service(domain=domain, service=service, service_data=service_data)
        except Exception:
            # A bare pass here hid the two failures that stop a press from ever
            # reaching Home Assistant: a websocket closed under us, and a
            # malformed action that raises before anything is sent.
            traceback.print_exc()

    def reload_current_page(self, *, force=False) -> bool:
        return self.reload_page(self._current_page_id, force=force)

    def force_reload_current_page(self) -> bool:
        return self.reload_page(self._current_page_id, force=True)

    def reload_page(self, page_id: str, *, force=False) -> bool:
        if not self._ha:
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
            # an empty icon. strmdck stops at 13, so whatever the firmware drew
            # in that slot - the Ulanzi branding behind the small window - is
            # never cleared. Sending it blank is what the official app does.
            slot_count = self._device.BUTTON_COUNT + self.EXTRA_SLOTS
            buttons = PageElement.generate(page.buttons, slot_count=slot_count)
            self._device.set_buttons(buttons)
        else:
            # Only update changed buttons
            buttons = PageElement.generate(page.changed_buttons)
            self._device.set_buttons(buttons, update_only=True)

        self._current_page_element = page
        return True

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
        while True:
            if not self._is_ready:
                break

            await asyncio.sleep(1)

            # Keep alive. The device can stop accepting writes without the USB
            # device disappearing - notably when the D200's own firmware
            # screensaver takes over, after which it ignores the host entirely.
            # strmdck swallows write errors, so probe the HID handle directly.
            if not self._device_is_alive():
                raise DeviceUnresponsiveError(
                    f'Device stopped responding for {self.DEVICE_TIMEOUT}s - reconnecting'
                )

            self._update_sleep_status()

    def _device_is_alive(self) -> bool:
        ''' Send the keep-alive and report whether the device still accepts it. '''
        try:
            self._send_small_window()
        except Exception as e:
            print('⚠️ keep_alive raised:', e)
            self._unresponsive_since = self._unresponsive_since or time.time()
        else:
            # strmdck writes asynchronously and hides failures, so a clean
            # return is not proof of life. Check the HID handle separately.
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
        clock area a second time every second and flicker visibly. Querying a
        string descriptor exercises the same USB path without touching the
        display.
        '''
        hid_device = getattr(self._device, '_hid_device', None)
        if not hid_device:
            return False

        try:
            hid_device.get_product_string()
        except Exception as e:
            print('⚠️ Device did not answer:', e)
            return False

        return True

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
            await button.trigger_action(self, interaction)

    def _send_small_window(self):
        ''' Send the small window's periodic update, with real statistics.

        strmdck's keep_alive() sends an empty dict, which defaults cpu, mem and
        gpu to zero, so the STATS view showed nothing but zeroes. Its payload
        builder is also short: a capture of the official Ulanzi app shows seven
        fields, not five -

            2|26|41|15:10:28|4|12H|          mode|cpu|mem|time|?|clock|weekday

        - and sending the five-field form disturbed rendering. Build the full
        payload here rather than going through strmdck.
        '''
        from strmdck.devices.ulanzi_d200 import CommandProtocol, PacketStruct

        stats = self._system_stats()
        now = local_now()

        mode = getattr(self._device, '_small_window_mode', None)
        mode_value = getattr(mode, 'value', 1)

        payload = '|'.join([
            str(mode_value),
            str(stats['cpu']),
            str(stats['mem']),
            now.strftime('%H:%M:%S'),
            str(stats['gpu']),
            self.CLOCK_FORMAT,
            '',                     # weekday, only populated for some modes
        ])

        packet = PacketStruct.build(dict(
            command_protocol=CommandProtocol.OUT_SET_SMALL_WINDOW_DATA.value,
            length=None,
            data=payload.encode('utf-8'),
        ))

        self._device._hid_device.write(packet)

    def _system_stats(self) -> dict:
        ''' CPU and memory percentages for the small window's STATS mode.

        strmdck's keep_alive() sends zeroes for all of these, so STATS showed
        nothing useful.

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

        The raw `state` byte the deck reports for this button is not a mode
        index - it alternates between values like 1 and 200 - so feeding it
        straight to set_small_window_mode() only ever resolved to CLOCK, and
        tapping appeared to do nothing. Track the mode ourselves instead, and
        push it to the device, which set_small_window_mode() alone does not do.
        '''
        from strmdck.devices.ulanzi_d200 import SmallWindowMode

        modes = list(SmallWindowMode)
        try:
            index = modes.index(self._device._small_window_mode)
        except (AttributeError, ValueError):
            index = 0

        next_mode = modes[(index + 1) % len(modes)]
        self._device.set_small_window_mode(next_mode.value)
        self._device.restore_small_window()

        print('🕐 Small window mode:', next_mode.name)

    def _reset(self):
        self._is_ready = False

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

    async def _setup(self):
        # Setup event bus
        event_bus.subscribe(EventName.DECK_RELOAD, self.reload_current_page)
        event_bus.subscribe(EventName.DECK_FORCE_RELOAD, self.force_reload_current_page)

        reconnect_delay = 3
        while True:
            self._reset()

            try:
                # Setup device
                device = None
                while True:
                    try:
                        device = auto_connect()
                        if device:
                            self._device = device
                            print('Device connected')
                            break

                        print('Could not find any device')
                        await asyncio.sleep(reconnect_delay)
                    except Exception as e:
                        try:
                            device.close()
                        except Exception:
                            pass

                        print('Could not open the device:', e)
                        await asyncio.sleep(reconnect_delay)

                # Setup Home Assistant
                async with self._ha.connect():
                    await self._ha.get_all_states()
                    self._ha.on_event('state_changed', self._ha_on_state_changed)
                    await self._ha.subscribe_events('state_changed')

                    self._is_ready = True
                    await asyncio.gather(
                        self._ha.listen(),
                        self._read_packets(),
                        self._keep_alive(),
                        self._setup_hot_reload(),
                    )
            except Exception:
                traceback.print_exc()

                # Crash app if error on startup
                if not self._is_ready:
                    sys.exit(1)

                self._is_ready = False
            finally:
                try:
                    self._device.close()
                except Exception:
                    pass

                try:
                    await self._ha.disconnect()
                except Exception:
                    pass

                await asyncio.sleep(reconnect_delay)

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
