# EMAY PM10 — custom ECG data access

Goal: pull ECG recordings off an older EMAY PM10 that the modern EMAY apps
no longer support.

## Findings so far (2026-07-08)

**Device identity.** The EMAY PM10 is a rebadged **Contec PM10** (Contec
Medical Systems is the OEM; also sold as EMAY EMG-10). FCC ID `2ABOGPM10` —
manuals and internal photos at https://fccid.io/2ABOGPM10. Single-lead ECG,
~30 s recordings, up to ~30 stored on-device, 150 Hz sample rate, 12-bit ADC,
±5 mV range, 0.05–40 Hz bandwidth (Contec datasheet).

**No prior art for this exact device.** No public protocol docs or
open-source clients for the PM10. Closest relatives:

- https://github.com/RigacciOrg/ecg-contec — parses Contec ECG90A `.ecg`
  files (Contec header conventions, int16 samples).
- https://github.com/tobac/cms50ew — Contec CMS50EW oximeter client;
  byte-framed request/response protocol over serial/SPP, likely the same
  protocol family.

**USB is charge-only on this unit.** Nothing PM10-like enumerates on the USB
bus while plugged in (`ioreg -p IOUSB`).

**Official Mac app.** `EMAY ECG HD.app` is installed; its data store is a
plain SQLite db at
`~/Library/Containers/com.emay.ecghd/Data/Documents/data/emay.db`
with tables `user`, `ecg_info` (time, duration, pulse, result, lead, device),
and `ecg_metadata` (`data` column = waveform, with crc32). Currently empty —
the app never managed to sync this device. If we ever get data flowing, this
schema is also a documented place to *put* it.

**Device confirmed.** Advertises as `PM107934`, address
`F1BD760F-8CD5-1184-C9C0-9E0FBF465404`. Manufacturer data (company ID
`0x4144`) = ASCII `"TA;DT150102"` — looks like a device-identity string,
not a placeholder.

**BT module: ISSC BT5050 in `5050_SPP` (Transparent SPP-over-BLE) profile.**
From Device Information Service (0x180A):
- Manufacturer: `ISSC`
- Model: `BT5050`
- Serial: `3481F43560ED`
- Hardware rev: `5050_SPP` — confirms Microchip/ISSC transparent-serial firmware
- Firmware rev: `2043032`, Software rev: `0000`

Vendor GATT service `49535343-fe7d-4ae5-8fa9-9fafd205e455` — the ISSC
transparent serial service. Characteristics (all `49535343-…`):
- `6daa-...19569aca69fe` (h49, read/write) — config; reads `001800180000004800`
- `aca3-...d85e28a60318` (h51, notify/write) — control/status pipe
- `1e4d-...23c647249616` (h54, all props) — main RX/TX data pipe
- `8841-...ecbe34729bb3` (h57, write/write-without-response) — bulk write
- `026e-...97daef17e26e` (h59, notify/write) — aux pipe

## Behaviour observed

- Powered on & idle: the device advertises briefly then powers off (~30s
  visibility per cycle).
- Connect, subscribe to all 3 notify chars, sit idle → **disconnects
  within ~3–20 s** with no data.
- Connect, subscribe, actively write to any characteristic (any bytes) →
  **connection stays alive** through 60+s. Writes act as keepalives.
- 60 s of write probes (ASCII "AT"/"$GET"/"TA?"/"DT?", raw sync bytes
  0x7D/0xAA/0x55/0xA7, Contec CMS50-style frames `7D 81 A?`,
  start-of-frame `AA 55`/`FA FA`/`55 AA`, echo of the mfr-data string) →
  **zero notify responses** on any of the 3 notify chars. Written to all 4
  write characteristics; every write succeeded at the BLE layer.
- Recording (fingers on pads) during probe window: no data streamed. This
  device appears to be **store-and-forward, not live-stream**.

## Tools in this repo

- `.venv/` — Python 3.13 + `bleak`
- `scan.py` — BLE scan, flags PM10-ish names: `.venv/bin/python scan.py 12`
- `gatt_dump.py` — connect + dump services/chars: `.venv/bin/python gatt_dump.py P031`
- `listen.py` — subscribe to notify char, log all traffic (read-only):
  `.venv/bin/python listen.py P031 60`

- `poke.py` — connect + probe the write pipes with candidate hello packets:
  `.venv/bin/python poke.py PM10`

## Windows PC software — retrieved and analyzed

Downloaded the Contec-hosted installer `Portable ECG Monitor-20210316 V1.4.7.exe`
(v1.4.7, 2021-03-16, 2.87 MB) from
`https://www.dlsoftw.com/software/03H9G164.zip` — Contec's public
download portal, index code `03H9G164`. Inno Setup 5.3.9 installer;
extracted to `pm10_pc/app/app/`.

**Critical revelation: the PC software has NO Bluetooth code at all**
(no BLE UUIDs, no `BluetoothAPIs`, no `BluetoothLE*`). Contradicts the
earlier research summary. **Transport is USB-HID only.** The DLL
`UsbDll.dll` imports `HidD_GetHidGuid`, `HidD_GetAttributes`, and
`HidD_SetNumInputBuffers` — a stock Windows HID client.

Key exports from `UsbDll.dll` (name-mangled MSVC):
- `CUsbFilter::OpenUsb(WORD vid, WORD pid, WORD reportSize, HWND, void*, int)`
- `CUsbFilter::SendToUsb(BYTE* buf, int len)`
- `CUsbFilter::CloseUsb()`
- `CUsbFilter::set_hevent(void*)`, `set_hwnd(HWND)` — used to notify the
  main app window via `SendMessageW` when a HID input report arrives.

### VID / PID / report size (extracted from disassembly)

Two call sites in the EXE (`0x432ea8` and `0x437630`) invoke `OpenUsb`
with identical constants:
- **VID = `0x4444`** (17476)
- **PID = `0x5555`** (21845)
- HID report size = `0x200` (512 bytes)
- 5th arg = HWND (main window), 6th arg = 128 (probably input-buffer count)

These are not USB-IF-registered VIDs — placeholder/generic values.

### Commands (partial — full extraction running in background)

29 distinct call sites to the send wrapper `fcn.00401a0f`. One example
decoded so far (site `0x4334c7` in `fcn.00433370`):
- 65-byte packet, byte 0=0x00, byte 1=0x81, byte 2=0x01
- 1000 ms wait timeout for response

Full protocol catalog being written to `PROTOCOL.md`.

## Current blocker

**Device does not enumerate as USB-HID on this Mac.** `hidscan.py` sees
33 HID devices but none matching VID 0x4444 / PID 0x5555 or bearing
"PM10"/"EMAY"/"Contec" strings, plugged in or not. Root cause almost
certainly one of:

1. **Charge-only USB cable**. Many "USB" cables shipped with medical
   devices only wire power pins. Try a known-good data cable.
2. **PC-mode not activated on device.** Some Contec devices only expose
   USB-HID when explicitly placed into "PC connect" mode — usually by
   holding a specific button while powering on, or selecting a menu
   option.

Once the device shows up under VID 0x4444 in `hidscan.py`, the
Python-side implementation is straightforward.

## Next steps

1. **User: get device visible on USB-HID.** Try (a) a different USB
   data cable; (b) holding each button while pressing power-on until
   the LCD shows a "PC" / "USB" mode; (c) any menu option that mentions
   PC / computer / connect.
2. **Wait for `PROTOCOL.md`** — background agent extracting all 29
   command byte patterns from disassembly, plus the response-format
   handling and handshake sequence.
3. Implement `client.py` — Python HID client using `hidapi`, replay
   handshake, download a stored recording, decode samples, plot & CSV.
4. Fallback if 1 fails: BLE path via archived Android APK sniff (older
   plan, still valid but harder).

## Session update — first successful download commands ✅

Full USB-HID protocol proven working:
- `hidapi.open(0x4444, 0x5555)` → `mfr='Nuvoton' product='HID Transfer'`
- HID report format: 65-byte writes (`[0x00 report_id] + 64 payload`), 64-byte reads.
- Framing: opcode has bit 7 set, params are 7-bit clean, checksum = `sum & 0x7F`.

**Response opcode mapping** (empirically observed):
- `0x8X` → `0xFX`
- `0x9X` → `0xEX`
- `0xBX` → `0xCX`
- `0xFA` → `0xDA`, `0xFC` → `0xDC`

**Confirmed working commands**:
| Send | Response | Meaning |
|---|---|---|
| `81 01` | `f1 01 00 00 0f 5e 01 01 01 7a 10 …` | Info (fw/config) |
| `82 02` | `f2 02` | Get datetime |
| `82 02 YY MM DD HH MM SS 00 ck` | echo | Set datetime |
| `83 01 YY MM DD HH MM SS ck` | echo | Set datetime v2 |
| `84 04` | `f4 04 01` | **Prepare download** — returns record count in 3rd byte |
| `85` | `f5` | Patient info (bare) |
| `B0 02 idx ck` | `c0 …` | Delete record ⚠️ destructive |
| `FE` | (silent close) | Disconnect |

**Silent commands (need more state or different form)**: `0x90`, `0x95`, `0x96`, `0x97`, `0x98`.

## Windows-binary deep-dive findings

- The connect flow in `fcn.00433370` sends commands in code order:
  `0x81 0x01` (info) → `0x82 0x02` (getdate) → `0x82 0x02 +date` (setdate) →
  **`0x84 0x04`** (prep) → `0x85` (patient info) → `0x90 0x03` (stream) →
  **LIST** (opcode `0x95` or `0x97` based on `neg edx; sbb dl,dl; and dl,0x02; add dl,0x95`) →
  `0xFE` (disconnect).

- **Critical branch at `0x00433a80`** (`jnz`): the packet form depends on whether
  the device model string at `[esi+0x25e0]` equals `"PM10BLE"` (ASCII compare at
  `0x00433bc5`, `0x00433e4f`, `0x00433f9a`).
  - **BLE-match branch**: sends `[0x00, 0x84, 0x04, 0, …]` (2-byte payload).
  - **Non-BLE branch** (our device — no "PM10BLE" in model): sends
    `[0x00, bl, 0x00, state_bytes[3..14], zeros, ck@buf[63]]`
    — opcode `bl` (register value from earlier control-flow), subop `0x00`,
    bytes 3-14 populated from `[esi+0x2074..0x2884]` (patient/session state),
    and 62-byte checksum at `buf[63]`.

- Our device is definitely on the non-BLE branch: model string field
  `[esi+0x25e0]` is never populated from any command's response we can see, so
  it stays as zeros → first byte compare (`0x00` vs `P`) mismatches immediately.

- **RE agent got 0x84 0x04 wrong**: it's the "prepare download" command,
  not "start recording". Our device's response `f4 04 01` = ack + record count.

- The list-command opcode is `0x95` or `0x97`, NOT `0x96` (the RE agent's
  guess). `0x96` is the "request specific record header" command used inside
  the download loop at `0x00430e77`.

## Current blocker

The read-side commands (`0x84 0x04`, `0x96 <idx>`, `0x98 <lo> <hi> …`) accept
the byte-format we've synthesized but return single-packet acknowledgements
only; the app never receives a multi-packet record dump. Most likely causes:

1. **Non-BLE state prerequisite** — the app's non-BLE branch sends a
   different sequence (12 state bytes packed into the 0x85 send) that we
   haven't yet reproduced faithfully. Without those bytes, the device may
   silently reject record queries.
2. Session-state expiration between commands (drain() waits may exceed a
   device timeout).

## Accidental destruction ⚠️

During the exploration sweep in `probe_records.py`, the following commands
were sent that likely caused data loss:
- Bare `0xB0` byte scans → interpreted as delete-record with junk indices.
- Bare `0x82` byte scans → interpreted as set-datetime with zero fields;
  device clock is now showing 2138-07-08 (year value corrupted to 138).

Fix rule going forward: NEVER probe unknown opcode families with bare
single-byte sweeps; only send commands whose semantics we know.

## Recovery — CONFIRMED WORKING (2026-07-08)

The device was successfully restored from Chinese UI + broken recording state
via a single `0x83 0x01` "Set Device Parameters" packet.

### Confirmed non-BLE (USB) settings-write packet layout

Send site: EXE 0x0043b320 (non-BLE branch entered at 0x0043b254).

```
wire[0]  = 0x00        HID report ID
wire[1]  = 0x83        opcode: SET_DEVICE_PARAMETERS
wire[2]  = 0x01        sub-op
wire[3]  = language    0=Chinese, 1=English    ← CONFIRMED buf[3]
wire[4]  = filter_on   0=off, 1=on             ← CONFIRMED buf[4]
wire[5]  = 1           (unknown but safe = 1)  buf[5] = [esi+0x207c]
wire[6]  = 1           (unknown but safe = 1)  buf[6] = [esi+0x2884]
wire[7]  = 1           (unknown but safe = 1)  buf[7] = [esi+0x2074]
wire[8]  = 1           computed flag           buf[8]
wire[9]  = 0           computed flag           buf[9]
wire[10] = 0           (unknown but safe = 0)  buf[10] = [esi+0x20a8]
wire[11] = (hr_low  >> 7) & 0x7F   ← HR-low  hi 7 bits  buf[11]
wire[12] = hr_low  & 0x7F          ← HR-low  lo 7 bits  buf[12]
wire[13] = (hr_high >> 7) & 0x7F   ← HR-high hi 7 bits  buf[13]
wire[14] = hr_high & 0x7F          ← HR-high lo 7 bits  buf[14]
wire[15..64] = 0
NO CHECKSUM (USB branch omits it — verified in disassembly).
```

Default HR-low = 60 bpm, HR-high = 100 bpm.

Device replies `f3 01 <8 bytes of setting echoes> 00 …` (65 bytes) on success.

### Response opcode summary (extended empirical map)

| Send opcode | Response opcode | Purpose |
|---|---|---|
| 0x80          | 0xF0 | ping |
| 0x81 0x01     | 0xF1 | get info (small — 11 bytes) |
| 0x82 0x02     | 0xF2 | get/set datetime |
| 0x83 0x00     | 0xF3 | get datetime v2 |
| **0x83 0x01** | **0xF3** | **SET_DEVICE_PARAMETERS (restore command)** |
| 0x84 0x04     | 0xF4 | prepare download; returns record count |
| 0x85          | 0xF5 | patient info |
| 0x90          | 0xE0 | state trigger; puts device in USB-connect mode |
| 0xA0 lo hi    | 0xD0 | download record; multi-packet response |
| 0xB0 0x02 idx | 0xC0 | delete record — ⚠️ destructive |
| 0xFE          | (silent) | disconnect |
