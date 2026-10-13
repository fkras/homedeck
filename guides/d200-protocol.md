# D200 protocol: how Ulanzi Studio drives the deck

What the official app sends a Ulanzi D200 over USB, recovered from the app
itself, and how HomeDeck's communications layer (`src/homedeck/d200/`) follows
it. Every claim below cites where it comes from, so it can be checked.

**Source:** Ulanzi Studio **3.3.9.0**, `UlanziDeck.exe`
(SHA-256 `8c5580461eb44326d1fd05e21de3b435772ab0a5efff301ee355d304cb760f1d`,
image base `0x140000000`), plus its own logs from a D200 on firmware 5.1.7 →
5.3.6, hardware `SSD210V100`. Addresses below are for that build.

## How this was worked out

Two sources, both on the Windows machine with Ulanzi Studio installed.

**1. The app's logs.** `%APPDATA%\Ulanzi\UlanziDeck\logs\*.xlog` are Tencent
Mars logs, compressed but not encrypted (magic `0x09`). Decoded with
[`tools/re/decode_xlog.py`](../tools/re/decode_xlog.py), they turn out to be
debug logs: every line names the C++ file and function, and the device code
logs each frame:

```
[senddataqueue.h:174, sendToDeck][Sending data to deck: commandCode= 0 , protocolID= 12 ... strPath= ".../defProfile/D200/bin/02/01/bin.zip"
[UlanziDeck.cpp:5155, OnDeckFrameReceived][返回数据: "{ "SerialNumber": "02C51A015U3672946", "Dversion": "5.1.7", ... }"
[UlanziDeck.cpp:5115, OnDeckFrameReceived][Keytap: key:1 position:12 type:1 status:1
[UlanziDeck.cpp:5188, OnDeckFrameReceived][PROTOCOL_CODE_GETBASE set Brightness is 86
[UlanziDeck.cpp:5196, OnDeckFrameReceived][PROTOCOL_CODE_GETBASE set SleepTime is 0
[UlanziDeck.cpp:5229, OnDeckFrameReceived][COMMAND_CODE_D_RESPONSE machine answers commandCode:3,protocolID=3,SN=...
[UlanziDeck.cpp:6165, SendSmallWinInfoToDeck][SendSmallWinInfoToDeck : "1|58|70|23:44:53|2|12H|"
```

Across five days of logs: the app sent protocol IDs 1 (×9), 3 (×13) and 12
(×1); the deck sent `cc 3 / pid 3` (×16) and `cc 1 / pid 1` (×4, one per key
event); and the small-window string was logged ~39 000 times, once a second.

**2. The executable.** Static disassembly only (pefile + capstone; nothing run
or patched). The logging macros embed `__FUNCSIG__`, so 913 functions can be
named from the strings they reference, and Qt's moc tables give slot and
parameter names. [`tools/re/ulanzi_evidence.py`](../tools/re/ulanzi_evidence.py)
re-derives everything below from the exe in about 20 seconds:

```bash
pip install pefile capstone
python tools/re/ulanzi_evidence.py "C:\Program Files (x86)\Ulanzi Studio\UlanziDeck.exe"
```

## Framing

### The header is two one-byte fields, not a 16-bit command

Earlier notes (and strmdck) read bytes 2–3 as one big-endian "command ID".
They are two fields. The frame builder at `0x1404f8f70`:

```
1404f8fd6: mov  eax, 0x7c7c
1404f8fdb: mov  word ptr [rsp+0x20], ax       ; magic
1404f8fe0: mov  byte ptr [rsp+0x22], dl       ; commandCode
1404f8fe4: mov  byte ptr [rsp+0x23], r8b      ; protocolID
1404f8ff3: mov  dword ptr [rsp+0x24], ecx     ; length, little-endian
```

`SendDataQueue::sendToDeck(unsigned char, unsigned char, ...)` logs them as
`commandCode=` and `protocolID=`, and the receive path logs
`machine answers commandCode:%d,protocolID=%d`. The known "commands" decode
cleanly:

| Old reading | commandCode | protocolID | Meaning |
|:--|:--|:--|:--|
| `0x0101` | 1 (device event) | 1 | key press |
| `0x0303` | 3 (device reply) | 3 | device info |
| `0x000a` | 0 (host request) | 0x0a | brightness |

### Frame layout and padding

The HID writer at `0x1404f9060` (`"Failed to hid_write="`):

```
1404f9101: lea  r14, [rdi+0xa]          ; total = length + 10
1404f910d: and  eax, 0x3ff
1404f9112: mov  ebx, 0x400
1404f9117: sub  rbx, rax                ; pad = 1024 - (total & 1023), always added
1404f91f1: mov  qword ptr [r15], rcx    ; 8-byte header
1404f91ff: call memcpy                  ; payload
1404f920a: mov  word ptr [rcx+8], di    ; two zero bytes after the payload
1404f921c: call memset(…, 0, pad)
```

```
offset 0   2  7c 7c
       2   1  commandCode   0 host request · 1 device event · 3 device reply
       3   1  protocolID
       4   4  payload length, little-endian
       8   n  payload
       8+n 2  00 00
              zeros to the next multiple of 1024 (a full extra 1024 if already aligned)
```

### Report ID 0 on every report, and the strmdck bug it explains

The same writer sends the frame in 1024-byte pieces, each copied after a zero
byte and written as 1025 bytes:

```
1404f925a: cmp  rax, 0x400                  ; chunk = min(1024, remaining)
1404f926e: mov  byte ptr [rsp+0xd0], 0      ; report ID 0
1404f9285: call memcpy(rsp+0xd1, chunk)
1404f928b: lea  eax, [rbx+1]                ; length = chunk + 1
1404f929c: call hid_write
```

Only the first report carries the header; the rest are raw continuation.

strmdck wrote bare 1024-byte chunks with no report ID. hidapi (and Linux usbhid
under it) treats the first byte of a write as the report ID and **strips it only
when it is `0x00`**. The header chunk starts with `0x7c`, so it went through
whole. But any continuation chunk whose first byte happened to be `0x00` lost
it, and the rest of the ZIP shifted by one byte. That is exactly the
"byte at 1016 + 1024·k must not be `0x00`" problem strmdck worked around by
re-zipping with a random `dummy.txt` until no chunk started badly. With the
report ID in place the problem cannot happen, and the workaround is gone.

### Reading

The reader thread at `0x1404f8bd0` (`"readLoop bytesRead="`):

```
1404f8c25: mov  r9d, 0x64                   ; 100 ms timeout
1404f8c2b: mov  r8d, 0x400                  ; 1024 bytes
1404f8c3d: call hid_read_timeout
1404f8c5e: cmp  byte ptr [rsp+0xb0], 0x7c   ; magic
1404f8c6c: cmp  byte ptr [rsp+0xb1], 0x7c
1404f8ca6: cmp  edi, 0x6400000              ; drop absurd lengths
```

Each report is one frame: there is no multi-report reassembly on input, so
everything the deck sends fits in 1016 bytes. A read error marks the device
disconnected (`设备读取错误`, "device read error", in the logs).

### No acknowledgements

`SendDataQueue::processQueue` waits on a condition variable with a 100 ms
timeout and sends queued frames one after another. It never waits for a reply.
The deck only answers the device-info request.

## Protocol IDs

Every call that sends a frame, from `ulanzi_evidence.py`. All use command code 0.

| ID | Name | Payload | Sent by | Notes |
|:--|:--|:--|:--|:--|
| `0x01` | BUTTONS | page ZIP | `acceptProfileZipResult` (ID from the request) | full grid |
| `0x03` | DEVICE_INFO | empty | `handleDeviceConnected`, … | deck replies `cc 3 / pid 3` with JSON |
| `0x04` | LOCK_SCREEN | empty | `SendScreenData(true)` | shows the screensaver; keys stop reporting |
| `0x05` | UNLOCK_SCREEN | empty | `SendScreenData(false)` | never sent by the app; dismisses it (tested) |
| `0x06` | SMALL_WINDOW | `mode\|cpu\|mem\|HH:MM:SS\|gpu\|12H\|date` | `SendSmallWinInfoToDeck` | once a second |
| `0x09` | SCREENSAVER | ZIP: the image file alone | slot `OnSetScreenPath` | captured; see below |
| `0x0a` | BRIGHTNESS | decimal string | `OnSetBrightness`, after device info | |
| `0x0b` | LABEL_STYLE | JSON | `handleDeviceConnected`, on request | |
| `0x0c` | FIRMWARE | `bin.zip` | `OnSendUpdateBin`, `setDeviceInfo` | **not implemented**, see below |
| `0x0d` | UPDATE_BUTTONS | partial ZIP | `acceptProfileZipResult` | only changed keys |
| `0x1a` | SLEEP_TIME | decimal minutes | `OnSetSleepTimeDeck`, after device info | 0 = never |
| `0xe0` | RESULT_OVERLAY | ZIP: `manifest.json` only | `sendExclamationMarkData` | flashes "!"; captured, see below |

From the deck:

| cc / pid | Payload | Handled by |
|:--|:--|:--|
| 1 / `0x01` | 4 bytes: key, position, type, status | key event |
| 3 / `0x03` | device info JSON | `ParsingJson` → `setDeviceInfo` |
| 1 / `0x0b` | — | the deck asking for the label style; the app answers with `0x0b` |

While the PC is locked, `SendDataToDeck` lets only 4 and 5 through once the
deck shows the lock screen, and never sends 12 (firmware). After a send, the
queue `remove()`s the temp file for 1, `0x0d` and `0xe0`.

The slots that send `0x09`, `0x0a`, `0x1a` and `0x0c` are unnamed functions,
but the moc dispatcher at `0x1404c7f40` calls them as consecutive cases in the
same order as the moc method table lists `OnSetScreenPath(strPath)`,
`OnSetBrightness(iVolumn)`, `OnSetSleepTimeDeck`, `OnSetUpdateBin`,
`OnSetUpdateApp`, `OnSendUpdateBin` (two cases, from a default argument).

## The things that matter for HomeDeck

### `0x1a` is the sleep-time setting, and nobody was sending it

[guides/reverse-engineering-usb.md](reverse-engineering-usb.md) found an unknown
`0x001a "0"` in a capture, right after brightness. In `OnDeckFrameReceived`,
after every device-info frame:

```
14015b921: lea  rdx, "PROTOCOL_CODE_GETBASE set Brightness is"
14015b963: mov  r8b, 0xa                    ; SendSmallWinInfoToDeck(0, 0x0a, brightness)
14015b9c1: lea  rdx, "PROTOCOL_CODE_GETBASE set SleepTime is"
14015ba03: mov  r8b, 0x1a                   ; SendSmallWinInfoToDeck(0, 0x1a, sleepTime)
```

The settings slot `OnSetSleepTimeDeck` sends the same ID (`0x14015f5e0`,
`QString::number(minutes)`). The value is the
checked radio button's ID in the settings dialog, set up at `0x1400ff841`:

```
addButton(rBtn_sleepTime1,   5)
addButton(rBtn_sleepTime2,  15)
addButton(rBtn_sleepTime3,  30)
addButton(rBtn_sleepTime4,  60)
addButton(rBtn_sleepTime5, 120)
addButton(rBtn_timeClose,    0)      ; xor r8d, r8d
```

So it is a time in minutes, 0 meaning off, and the app sends it on every
connect, `0` by default. strmdck never sent it. What it controls on the deck
wasn't observed: it is **not** what makes the deck lock onto its screensaver
(see [below](#lock-screen-and-screensaver) — that happened with Ulanzi Studio
having just sent `0`).

**HomeDeck now:** sends `0` during the connect handshake, then whatever
`deck_sleep_timeout` (default `0`) in the configuration says.

### The connect handshake

`handleDeviceConnected` → `SendDataToDeck(0, 0x03)` asks for the device info.
The reply goes through `ParsingJson` and `setDeviceInfo`, which compares the
firmware version and pushes `bin.zip` if it is older. That is the 5.1.7 → 5.3.6
update in the logs. The `GETBASE` step then re-sends brightness and sleep time.
A capture of a first connection shows the order:
`0x0b` label style → `0x03` info → `0x0a` → `0x1a` → `0x01` page → `0x06`.

**HomeDeck now:** `UlanziD200.handshake()` asks for the info (it gets serial,
firmware and hardware version), unlocks the deck and sends the sleep time. Like the app, it
re-sends brightness and sleep time after *every* device-info frame. One that
arrives unasked means the firmware restarted, so HomeDeck also repaints.

### Key events

`OnDeckFrameReceived` when `protocolID == 1 && commandCode == 1`:

```
Keytap: key:%d position:%d type:%d status:%d      ; payload[0], [1], [2], [3]
row = position / columns, column = position % columns
column 3, row 2 -> the large tile
D200 / D200H: key 100 is treated as 1
```

`status` is 1 for press and 0 for release. strmdck required `type == 1` and
failed to parse anything else; HomeDeck now accepts any value.

### Small window

`SendSmallWinInfoToDeck` builds seven fields (`0x140163c90`): mode, CPU, RAM,
time, GPU, `"24H"`/`"12H"`, and a date field. strmdck sent the first five. The
modes are the radio button IDs in `SystemSmallWindowsEditor`:

| ID | Widget | Shows |
|:--|:--|:--|
| 0 | `radio_cpu` | CPU / RAM / GPU |
| 1 | `radio_time1` | clock |
| 2 | `radio_img` | the image the page ZIP puts in `3_2` |
| 100 | `radio_knob` | dial devices only |
| 200 | `radio_time2` | clock, date and weekday |
| 201 | `radio_time3` | clock and weekday |
| 202 | `radio_time4` | clock and date |
| 203 | `radio_time5` | clock |

The date field (`0x1400b3280`): 200 → `date weekday`, 201 → weekday, 202 →
date, anything else empty. That matches the capture `201|10|41|15:10:36|2|12H|Fri`.

### Label style

`{"Align":"bottom","Color":16777215,"FontName":"…","ShowTitle":true,"Size":10,"Weight":80}`,
compact JSON. The deck can ask for it (`cc 1 / pid 0x0b`); the app answers, and
so does HomeDeck now.

### Lock screen and screensaver

`SendScreenData(bool)` computes `protocolID = (bool ^ 1) + 4`. All four callers
(`handleScreenLocked`, `handlePowerBroadcast`, `handleNativeEvent`,
`OnDeckFrameReceived`) pass `true`, so the app only ever sends `0x04`, and on
unlock `SendUnlockedData` sends the page ZIP again instead of a `0x05`.

Tested on the deck (firmware 5.3.6, Ulanzi Studio closed):

- `0x04` fills the screen with the screensaver image. **While it is up the deck
  reports no key presses**, and pressing keys does not dismiss it. That is the
  "deck goes deaf until unplugged" behaviour HomeDeck's watchdog was written
  for. Writes still succeed, so a write-based health check can't see it.
- `0x05` dismisses it: the keys come back and presses reach the host again.
- The deck sends nothing when it locks.

**The deck locks itself when the host goes quiet.** With Ulanzi Studio closed
(having last sent sleep time `0`) and nothing else talking to it, the deck
went to the locked screensaver within about two minutes of the last frame.
With the small-window update (`0x06`) sent once a second, as both Ulanzi Studio
and HomeDeck do, it stayed unlocked for the whole four-minute test and key
presses kept arriving. Sending `0x05` to a deck that is already unlocked
changes nothing on screen.

The image comes from `0x09`. A capture of Ulanzi Studio applying a screensaver
holds one `0x09` frame of 89 217 bytes: a ZIP with a single entry, the picked
file under its own name (`bliss 600dpi.jpg`, a 1220×708 JPEG). The app keeps
the same ZIP as `%APPDATA%\Ulanzi\UlanziDeck\ScreenFiles.zip` (byte for byte
identical to the captured payload) next to a `ScreenFiles\` folder holding the
image. No other command went with it. Uploading a different image this way and
sending `0x04` showed the new image. Ulanzi's default is the 260×260 logo in
`System/Images/ScreenSave/20240329-144314.png`.

**HomeDeck now:** the once-a-second small-window update keeps the deck from
locking; `handshake()` sends `0x05`, so a deck that locked while HomeDeck was
stopped starts listening again; and the keep-alive loop sends `0x05` every 30
seconds in case a stall let it lock. `UlanziD200.lock_screen()`, `unlock_screen()` and
`set_screensaver(image)` are available, and the `screensaver:` config key
uploads an image from `assets/` when it changes.

### Result overlay: the "!"

A capture of an OBS plugin action pressed while OBS was closed holds one `0xe0`
frame, 184 bytes: a ZIP with only this `manifest.json`:

```json
{"0_2": {"KeyType": 1, "Progress": 100, "State": 1}}
```

The firmware draws the symbol itself; the images under `Version/RunResult/`
are not sent. The slot is fed by `onActionExecState(std::shared_ptr<StateData>)`
(state at `+0x40`, progress at `+0x44`), and the plugin API exposes `showAlert`
but no `showOk`.

Tried on the deck: State 0–5, Progress 0–100 and KeyType 0–5 all flash the same
"!" briefly on the named key. On this firmware it is an alert and nothing else;
there is no check mark.

**HomeDeck now:** `UlanziD200.show_alert(index)`. HomeDeck calls it for the key
whose Home Assistant action failed (a call Home Assistant refuses, matched by
message id, or one that never went out) unless `alert_on_error: false`.

## Not implemented

- **Firmware (`0x0c`).** It writes the deck's firmware, and a bad image has no
  documented recovery. `send_raw()` refuses it. Use Ulanzi Studio for updates.
- **USB IDs.** `0x2207:0x0019` is what strmdck used and what HomeDeck has
  been opening. No hard-coded VID/PID turned up in the app. Set
  `DECK_VENDOR_ID` / `DECK_PRODUCT_ID` if a firmware update changes them.

The deck exposes two HID interfaces: 0, the vendor one ("ulanzi", usage page
`0x0C`) that carries this protocol, and 1, a keyboard ("HID Interface", usage
page `0x01`). On Windows the keyboard enumerates first, so taking the first
match, as `hid_open()` and strmdck do, opens the wrong one. HomeDeck picks
interface 0 explicitly.

Anything else can be sent by hand with `send_raw()` or
[`tools/d200_cli.py`](../tools/d200_cli.py) `raw`.

## Tested on the deck

On a D200 (firmware 5.3.6) attached to Windows, through `homedeck.d200`:
device info request and reply; a 151 KB page ZIP over 148 reports, drawn
intact (the report-ID fix, without strmdck's re-zip workaround); `show_alert`;
`set_screensaver` with a PNG converted to JPEG; `lock_screen` / `unlock_screen`;
key events while unlocked and none while locked; the idle lock after ~2 minutes
of host silence, and none with the once-a-second small-window update. Not yet run on the Orange Pi's
Linux `hidraw`.

## Using the layer

```python
from homedeck.d200 import SmallWindowMode, UlanziD200

deck = UlanziD200.open()                    # 0x2207:0x0019
info = await deck.handshake(sleep_time=0)   # DeviceInfo(serial_number=..., firmware_version='5.3.6', ...)
deck.set_brightness(80)
deck.set_label_style({'align': 'bottom', 'color': 'FFFFFF', 'font_name': 'Roboto', 'size': 10})
deck.set_buttons({0: {'name': 'Lamp', 'icon': 'lamp.png'}, 13: None})
deck.set_small_window_mode(SmallWindowMode.CLOCK_2)
deck.set_small_window_data(cpu=12, mem=40)
deck.show_alert(10)                         # the deck's "!" on key 10
deck.set_screensaver('assets/saver.jpg')
deck.lock_screen(); deck.unlock_screen()

async for event in deck.read_packet():      # KeyEvent, DeviceInfoEvent
    ...
```

- Writes go to a dedicated thread, in order. A page upload is a few hundred
  reports and takes seconds over the deck's full-speed endpoint, so the event
  loop is never blocked by it.
- A write that a newer one makes pointless is dropped before it is sent: an
  older brightness, small-window or label style, or a page a newer page replaces.
  A partial update folds into a page still waiting to go.
- Failures are reported, not swallowed. A failed write makes `is_responsive()`
  false until a write succeeds again; a failed read ends `read_packet()` with
  `DeviceDisconnected`.

On the deck itself, with HomeDeck stopped:

```bash
$HD_PY tools/d200_cli.py info
$HD_PY tools/d200_cli.py sleep 0
$HD_PY tools/d200_cli.py small-window-tour
$HD_PY tools/d200_cli.py listen
```
