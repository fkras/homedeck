# Reverse-engineering D200 commands from USB traffic

How to capture what the official Ulanzi app sends, identify an unknown command,
and add it to HomeDeck. Written for the firmware screensaver — which HomeDeck
currently cannot disable — but the method works for any missing feature.

## What we already know

The D200 speaks a simple framed protocol over USB HID. Every outgoing packet is
exactly **1024 bytes**:

| Offset | Size | Field |
|:--|:--|:--|
| 0 | 2 | Magic `7c 7c` (`\|\|`) |
| 2 | 2 | Command ID, **big-endian** |
| 4 | 4 | Payload length, **little-endian** |
| 8 | 1016 | Payload, zero-padded |

Real packets, for reference while scanning a capture:

```
set_brightness(80)   7c7c 000a 02000000 3830...     payload "80"
small_window         7c7c 0006 10000000 317c30...   payload "1|0|0|12:00:00|0"
set_buttons          7c7c 0001 04000000 504b0304... payload is a ZIP (PK..)
```

Known command IDs:

| ID | Direction | Meaning |
|:--|:--|:--|
| `0x0001` | out | Set all buttons (ZIP payload) |
| `0x0006` | out | Small-window data |
| `0x000a` | out | Brightness |
| `0x000b` | out | Label style (JSON) |
| `0x000d` | out | Partial button update |
| `0x0101` | in | Button press |
| `0x0303` | in | Device info |

So the screensaver command is very likely an unused low ID — `0x0002`–`0x0005`,
`0x0007`–`0x0009`, `0x000c`, or `0x000e`+ — carrying either JSON (like label
style) or an image/ZIP payload.

**The goal:** find the packet the app sends when you set or clear a screensaver,
then replay it.

## Choosing a capture method

You need to sniff USB while the **official Ulanzi app** talks to the deck. The
app runs on macOS and Windows only, which rules out capturing on the Orange Pi.

| Platform | Tool | Notes |
|:--|:--|:--|
| **Windows** | Wireshark + USBPcap | Easiest. USBPcap ships in the Wireshark installer. |
| **macOS** | Wireshark + `XHC20` interface | Needs Xcode's "Additional Tools" for the USB debug kext. Fiddlier. |
| **Linux** | `usbmon` + Wireshark | Cleanest capture, but the Ulanzi app doesn't run here. |

**Recommended: Windows.** It is by far the least painful. If you only have the
Mac, use the macOS route below.

---

## Method A — Windows (recommended)

### 1. Install

Download [Wireshark](https://www.wireshark.org/download.html) and **tick USBPcap**
during installation. Reboot afterwards — USBPcap installs a driver.

### 2. Find the deck

Plug the D200 into the Windows machine and open Device Manager. Under **Human
Interface Devices**, find the D200 and note which USB root hub it sits on
(Properties → Details → Parent).

### 3. Capture

1. Start Wireshark **as Administrator**.
2. Pick the `USBPcap` interface for that root hub.
3. Start the capture.
4. In the Ulanzi app, **set a screensaver**. Then, if the app allows it, change
   or clear it.
5. Stop the capture and save as `screensaver.pcapng`.

Keep the capture short and do one action at a time — it makes the diff obvious.

### 4. Filter to the interesting packets

In Wireshark's display filter:

```
usb.transfer_type == 0x03 && usb.endpoint_address.direction == 0
```

That is interrupt transfers, host→device. Then look for the `7c7c` magic. A
more direct filter, if your Wireshark version supports payload slicing:

```
usb.capdata[0:2] == 7c:7c
```

Click a packet, expand **Leftover Capture Data**, and read bytes 3–4 — that is
the command ID.

---

## Method B — macOS

### 1. Enable USB capture

Install Wireshark, then the **Additional Tools for Xcode** package from
[developer.apple.com/download/all](https://developer.apple.com/download/all/)
(search "Additional Tools"). Inside, run the USB kext installer from the
Hardware folder, then reboot.

Verify a USB interface appears:

```bash
ifconfig | grep XHC
```

### 2. Capture

```bash
# Start capture on the USB bus (XHC20 is typical; use what ifconfig shows)
sudo tcpdump -i XHC20 -w ~/screensaver.pcapng
```

Set the screensaver in the Ulanzi app, then `Ctrl+C`.

> If `XHC20` doesn't exist, run `wireshark` as root and look for interfaces
> named `XHC*` or `usbmon*`. macOS numbering varies by machine.

---

## Analysing the capture

Wireshark's UI is fine for browsing, but a script is faster for finding every
framed packet at once. [`tools/scan_d200.py`](../tools/scan_d200.py) scans a
capture for the `7c7c` magic and decodes each command:

```bash
python3 tools/scan_d200.py screensaver.pcapng
```

It has no dependencies — it reads the capture as raw bytes, so it works on pcap,
pcapng and plain dumps alike. Output looks like this:

```
@3264     cmd=0x0004 len=55    *** UNKNOWN ***
          hex   7b22456e61626c65223a20747275652c20224475726174696f6e...
          ascii {"Enable": true, "Duration": 300, "Image": "save

============================================================
Command summary:
  0x0001  x1     SET_BUTTONS
  0x0004  x1     *** UNKNOWN - INVESTIGATE ***
  0x0006  x1     SET_SMALL_WINDOW_DATA
  0x000a  x1     SET_BRIGHTNESS
  0x000b  x1     SET_LABEL_STYLE
```

**What you're looking for:** a command ID marked `*** UNKNOWN ***` that appears
only in the screensaver capture. Check the payload:

- **Starts with `{`** → JSON, like `set_label_style`. Easiest case: read the
  field names directly and you'll likely see the setting you want.
- **Starts with `PK\x03\x04`** → a ZIP, like `set_buttons`. The image is inside.
- **Starts with `\x89PNG` or `\xff\xd8`** → a raw PNG or JPEG.
- **Short and numeric** → a mode or toggle, like `set_brightness`.

### Narrowing it down

If several unknown commands show up, capture twice and diff:

```bash
python3 tools/scan_d200.py idle.pcapng       > idle.txt      # app open, nothing changed
python3 tools/scan_d200.py screensaver.pcapng > saver.txt    # screensaver set
diff idle.txt saver.txt
```

Whatever appears only in the second capture is your command.

## Testing a candidate

Once you have a command ID and payload, send it yourself. **Stop HomeDeck first**
so the two don't fight over the device:

```bash
systemctl stop homedeck        # or homedeck-server
```

```python
#!/usr/bin/env python3
"""Send one raw command to the D200."""
from strmdck.device_manager import auto_connect
from strmdck.devices.ulanzi_d200 import PacketStruct

COMMAND = 0x0000        # <- the unknown ID from your capture
PAYLOAD = b''           # <- the payload bytes, or b'' to try clearing

device = auto_connect()
assert device, 'No deck found'

packet = PacketStruct.build(dict(
    command_protocol=COMMAND,
    length=None,
    data=PAYLOAD,
))
device._hid_device.write(packet)
print(f'Sent 0x{COMMAND:04x} with {len(PAYLOAD)} byte payload')
```

Run it with the venv's Python:

```bash
$HD_PY send_raw.py
```

If the payload is JSON, try variations — an empty string, `{"Enable":false}`,
a zero duration — mirroring the field names you saw in the capture.

> [!WARNING]
> Send only command IDs you actually observed the official app send. Sweeping
> through unknown IDs with made-up payloads can write to firmware areas you
> didn't intend, and there is no documented recovery path for a bricked D200.

## Adding it to HomeDeck

Once a command works reliably, wire it in. `strmdck` is a separate package, so
either fork it or add a small override in this repo.

**1. Add the command ID** in `strmdck/devices/ulanzi_d200.py`:

```python
class CommandProtocol(Enum):
    ...
    OUT_SET_SCREENSAVER = 0x000X    # whatever you found
```

**2. Add a method** on `UlanziD200Device`:

```python
def set_screensaver(self, enabled: bool):
    packet = PacketStruct.build(dict(
        command_protocol=CommandProtocol.OUT_SET_SCREENSAVER.value,
        length=None,
        data=json.dumps({'Enable': enabled}).encode('utf-8'),
    ))
    self._write_packet(packet)
```

**3. Call it from HomeDeck** — the natural place is `reload_all()` in
`src/homedeck/homedeck.py`, next to `set_label_style()`, so it is reapplied on
every config load:

```python
self._device.set_label_style(asdict(configuration.label_style))
self._device.set_screensaver(False)
```

Then expose it as a config key (`screensaver: false`) in
`configuration.base.yml` and `configuration.schema.yml`, following how
`brightness` is wired up.

## Capturing the first-connection handshake

The default screen (Ulanzi logo, name and URL) is what the deck ships with. The
official app replaces it the first time it talks to the device — so the command
that writes it is sent **at connection time**, not when you change a setting.
A capture taken while clicking around in the app will never contain it.

To catch it, the capture has to be running *before* the deck is plugged in:

1. Close the Ulanzi app completely.
2. Unplug the D200.
3. Start the Wireshark/USBPcap capture on the root hub the deck will use.
4. Plug the deck in. Wait for Windows to enumerate it (a few seconds).
5. **Now** open the Ulanzi app and let it connect. Do nothing else.
6. Stop the capture as soon as the deck's screen changes.

That window — enumeration through first draw — is where the interesting
traffic is. Scan it:

```bash
python3 tools/scan_d200.py firstconnect.pcapng
```

Expect a much larger capture than a settings change produces. Look for:

- **`0x0001` (SET_BUTTONS) with a ZIP payload** early on — that is the app
  replacing the default buttons, and it is the same command HomeDeck already
  uses.
- **Any command ID not in the known list**, especially one carrying image data
  (`\x89PNG`, `\xff\xd8`) or a second ZIP. That is the candidate for whatever
  owns the background layer.

If the only thing the app sends is `0x0001`, that is itself a useful result: it
means the default screen is replaced by ordinary button data, and the reason
HomeDeck does not clear it is something about *how* it writes them rather than
a missing command.

## What the first-connection capture showed

A capture of the official app connecting to a D200 (firmware 5.3.6, hardware
SSD210V100) produced this handshake, all host -> device:

```
0x000b  SET_LABEL_STYLE   {"Align":"bottom","Color":16777215,...}
0x0303  IN_DEVICE_INFO    {"SerialNumber":...,"Dversion":"5.3.6","DeviceType":"D200"}
0x000a  SET_BRIGHTNESS    86
0x001a  *** UNKNOWN ***   "0"
0x0001  SET_BUTTONS       284473-byte ZIP
0x0006  SET_SMALL_WINDOW  2|12|41|15:56:09|1|12H|
```

Two findings.

**`0x001a` is a new command**, payload a single ASCII `"0"`, sent right after
brightness on every connection. A one-byte 0/1 value looks like a toggle. It is
the best current candidate for the screensaver or background switch, but this
has not been confirmed - sending it is untested.

**The deck has 14 slots, not 13.** The app's `manifest.json` is keyed
`"{col}_{row}"` and contains:

```
0_0 0_1 0_2   1_0 1_1 1_2   2_0 2_1 2_2   3_0 3_1 3_2   4_0 4_1
```

That is 14 entries. HomeDeck's index-to-key mapping produces only 13 and never
emits `3_2`, so whatever the firmware last drew there stays on screen. In the
app's own manifest `3_2` is the single entry with an empty icon - it blanks
that slot explicitly.

Use [`tools/extract_zip.py`](../tools/extract_zip.py) to pull the ZIP out of a
capture and read its manifest:

```bash
python3 tools/extract_zip.py firstcapture.pcapng default.zip
```

## If the capture comes up empty

Some settings are written **once to firmware** and not re-sent on every launch.
If you see no unknown command:

- Capture while **changing** the screensaver, not just while the app is open.
- Capture the app's **first connection** to the deck — start the capture, then
  plug the deck in.
- Check whether the app talks over a **second HID interface**. The D200 exposes
  more than one (`/dev/hidraw0` and `hidraw1` on Linux); `strmdck` uses only one
  of them, and the config channel may be the other.

That last point is the most likely explanation if everything else looks normal,
and it is worth checking early:

```bash
# On the Orange Pi, see every interface the deck exposes
ls -l /dev/hidraw*
cat /sys/class/hidraw/hidraw*/device/uevent | grep -i hid_name
```
