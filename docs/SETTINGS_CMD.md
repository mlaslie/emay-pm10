# EMAY PM10 (USB) — "Set Device Parameters" packet layout

All EXE offsets refer to `Portable ECG Monitor.exe` in the `pm10_pc/app/app/` folder.

## Reverse-engineering summary

The containing function starts at **0x0043af60** (`push -1 / push 0x59a798 / push fs:[0]` SEH prologue). The call at **0x0043b320** is the "Set Device Parameters" send (confirmed via the `IDC_TXT_0083` "Set failed" string pushed immediately after at 0x0043b339).

Inside the function there is an `strcmp` against the ASCIIZ string `"PM10BLE"` at 0x0043b155…0x0043b18c against the member string at `[esi+0x25e0]`. The result governs which branch builds the packet:

- **BLE branch** (0x0043b192 … 0x0043b24f) — taken when the device name equals `PM10BLE`.
- **non-BLE branch** (0x0043b254 … 0x0043b2ff) — taken when it does not, i.e. the ordinary USB PM10.

The user's device is the ordinary USB PM10, so the non-BLE branch is the correct one. **The two branches produce different packets** (different opcode, different byte order, and the BLE version has an explicit 7-bit checksum while the USB version does not).

### Buffer address arithmetic

- 0x0043b312 `push 0x41` (length = 65) — reduces ESP by 4.
- 0x0043b314 `lea eax, [esp+0x34]` — **after** the push. So `eax = (ESP_pre_push - 4) + 0x34 = ESP_pre_push + 0x30`.
- All buffer writes earlier in the function use ESP == ESP_pre_push. Therefore a write to `[esp+0x31]` targets buf[1], `[esp+0x3e]` targets buf[14], etc. (The note in the task prompt that said `[esp+0x34+N]` was off by 4; the writes go one nibble higher into the buffer than that mapping suggested.)

Bytes buf[15]…buf[64] are never touched in this function. Rizin shows a `rep stosd` clear at 0x0043b13e–0x0043b147 (`ecx=0x10; lea edi,[esp+0x30]; rep stosd; stosb`) that zeros 65 bytes at buf before the branch, so **unwritten offsets are guaranteed zero**.

## Non-BLE (USB PM10) packet — byte-by-byte

Confidence: **H** = immediate written by a specific instruction; **M** = register-sourced but provenance traced to a specific member variable via a specific instruction; **L** = semantic label is a guess.

| off | bytes / value                                   | source instruction (address)                                   | semantic (confidence)                             |
|----:|--------------------------------------------------|----------------------------------------------------------------|----------------------------------------------------|
|   0 | 0x00                                             | from `rep stosd` @ 0x0043b13e                                  | HID report ID (H)                                  |
|   1 | 0x83                                             | `c6 44 24 31 83` @ 0x0043b2ee                                  | opcode "Set Device Parameters" (H)                 |
|   2 | 0x01                                             | `c6 44 24 32 01` @ 0x0043b2f6                                  | sub-op / constant (H)                              |
|   3 | `[esi+0x27f4]`                                   | `mov [esp+0x33], al` @ 0x0043b266 (al ← 0x0043b254)            | member @+0x27f4 — see notes (M/L)                  |
|   4 | `[esi+0x2078]`                                   | `mov [esp+0x34], cl` @ 0x0043b287 (cl ← 0x0043b260)            | member @+0x2078 (M/L)                              |
|   5 | `[esi+0x207c]`                                   | `mov [esp+0x35], dl` @ 0x0043b270 (dl ← 0x0043b25a)            | member @+0x207c — a 0/1 boolean (M/L)              |
|   6 | `[esi+0x2884]`                                   | `mov [esp+0x36], al` @ 0x0043b27a (al ← 0x0043b26a)            | member @+0x2884 (M/L)                              |
|   7 | `[esi+0x2074]`                                   | `mov [esp+0x37], cl` @ 0x0043b297 (cl ← 0x0043b28b)            | member @+0x2074 (M/L)                              |
|   8 | `([esi+0x2090] != 1) ? 1 : 0`                    | `setnz dl` @ 0x0043b291, `mov [esp+0x38], dl` @ 0x0043b29f     | boolean flag; inverted-1 encoding (M)              |
|   9 | `([esi+0x2098] == 1) ? 0x55 : 0`                 | `setnz cl; dec ecx; and ecx,0x55` (0x0043b29b-a9), `mov [esp+0x39], cl` @ 0x0043b2ac | boolean encoded as 0/0x55 (M)   |
|  10 | `[esi+0x20a8]` (low byte)                        | `mov dl,[esi+0x20a8]` @ 0x0043b2a3, `mov [esp+0x3a], dl` @ 0x0043b2bc | member @+0x20a8 (M/L)                        |
|  11 | `([esi+0x20a4] >> 7) & 0x7f`                     | `sar ecx,7; and cl,0x7f` (0x0043b2b6-b9), `mov [esp+0x3b], cl` @ 0x0043b2c6 | high 7 bits of 14-bit @+0x20a4 (H) |
|  12 | `[esi+0x20a4] & 0x7f`                            | `and dl,0x7f` @ 0x0043b2d0, `mov [esp+0x3c], dl` @ 0x0043b2d6 | low 7 bits of 14-bit @+0x20a4 (H)                  |
|  13 | `([esi+0x20a0] >> 7) & 0x7f`                     | `sar ecx,7; and cl,0x7f` (0x0043b2d3-e0), `mov [esp+0x3d], cl` @ 0x0043b2e6 | high 7 bits of 14-bit @+0x20a0 (H) |
|  14 | `[esi+0x20a0] & 0x7f`                            | `and dl,0x7f` @ 0x0043b2e3, `mov [esp+0x3e], dl` @ 0x0043b2fb | low 7 bits of 14-bit @+0x20a0 (H)                  |
| 15–64 | 0x00                                           | from `rep stosd` @ 0x0043b13e                                  | never written (H)                                  |

### There is NO checksum byte in the USB variant

Between the last write at 0x0043b2fb and the `call fcn.00401a0f` at 0x0043b320 there is only `mov [0x5f1da4], ecx; mov edx,[esi+0x2600]; push edx; call ResetEvent; push 0x41; lea eax,[esp+0x34]; push 0x3e8; push eax; mov ecx, esi; call fcn.00401a0f`. **No `call fcn.00401dac` (checksum helper) appears in this branch.** The BLE branch (0x0043b246) does compute one and stores it at buf[14], with count=0x0d and start=[esp+0x31] (buf[1]) — but the USB PM10 code path emits the packet raw.

## Semantic mapping — what is uncertain

Cross-referenced with the parser of the *response* to `0x81` (device → PC "Get Device Parameters") at 0x0042ec6a–0x0042eced, the incoming byte offsets map 1:1 to member fields, mirroring the outgoing layout:

| response offset | assigns to `[ebp+…]` | write instr addr |
|---:|---|---|
| resp[0x2a] | `+0x27f4` | 0x0042ec7a |
| resp[0x2b] | `+0x2078` | 0x0042ec86 |
| resp[0x2c] | `+0x207c` (0/1 via `cmp cl,1`) | 0x0042eca5 |
| resp[0x2d] | `+0x2884` | 0x0042ecab |
| resp[0x2e] | `+0x2074` | 0x0042ecbf |
| resp[0x33] | `+0x2090` (0/1 via `cmp dl,bl`) | 0x0042eccc |
| resp[0x34] | `+0x2098` (0/1 via `cmp cl,bl`) | 0x0042ecec |
| resp[0x59] | `+0x20a8` | 0x0042ed13 |
| resp[0x5a,0x5b] | `+0x20a4` (14-bit big-endian) | 0x0042ed39 |
| resp[0x5c,0x5d] | `+0x20a0` (14-bit big-endian) | 0x0042ed3f |

The response format is the same shape as the sent one (bytes 3..14 in the request → bytes 0x2a..0x5d in the response with the same order and identical encoding of the two 14-bit fields).

### What I could confirm about semantics

- **buf[11..12] and buf[13..14] are two 14-bit values** stored big-endian (high 7 bits then low 7 bits). Constructor at 0x0042d51d–0x0042d527 sets `[esi+0x20a4] = 60` (0x3c) and `[esi+0x20a0] = 100` (0x64). The response parser also range-checks them to `[25, 300]`, falling back to 60 and 100 respectively. **These are almost certainly the HR-low and HR-high alarm thresholds, not the sample time** — 60/100 bpm is the classic normal-HR window, and sample time on the PM10 is not a duration in that range.
- **The default sample time = 30 (0x1e) is stored at a stack local (`mov byte [esp+0x3c], 0x1e` at 0x0042d4e6)**. It is not one of the `[esi+0x20xx]` fields sampled by this packet in an obvious way. Sample time may be sent by a *different* command (`0x83 0x02`? or a separate opcode) — this needs a separate investigation before you attempt to "set defaults" for it.
- **Which of buf[3..7] is language / filter / heartbeat is not resolved by disassembly alone**. The five candidate fields (`+0x27f4`, `+0x2078`, `+0x207c`, `+0x2884`, `+0x2074`) receive their values from the *response* parser and are copied straight back out into the request — nowhere in the function containing 0x0043b320 do they get overwritten from dialog controls. That means the actual GetDlgItem→member wire-up lives in the settings dialog's `OnOK`/`DoDataExchange`, which is a **different function** than the one that builds the packet. Without decoding that dialog too, I cannot honestly label buf[3..7].

## Safe procedure — strongly recommended

Because the device is already in a damaged state and a wrong 0x83 packet could persist bad settings, **do not build a "set defaults" packet from scratch**. Instead:

1. Send the "get parameters" query (opcode 0x81 — needs a separate reverse of the request side; the response layout above is authoritative).
2. Read the 65-byte response, splice in *only* the fields you know how to encode safely (the two HR thresholds and buf[2]=0x01 opcode), leave every other byte at whatever the device just told you, and send it back.

If you are certain a byte in buf[3..7] is what you want to change (e.g. from experimentation), overwrite only that one byte. Do NOT invent defaults for buf[3..7] and hope.

## Python constructor

```python
def cmd_set_device_parameters(
    hr_low_bpm: int = 60,           # HIGH confidence: this is [esi+0x20a4], sent as buf[11..12]
    hr_high_bpm: int = 100,         # HIGH confidence: this is [esi+0x20a0], sent as buf[13..14]
    f_27f4: int = 0,                # buf[3]  TODO(uncertain): copy from a fresh "get" response
    f_2078: int = 0,                # buf[4]  TODO(uncertain): copy from a fresh "get" response
    f_207c: int = 0,                # buf[5]  TODO(uncertain): 0/1 flag. Copy from "get".
    f_2884: int = 0,                # buf[6]  TODO(uncertain): copy from a fresh "get" response
    f_2074: int = 0,                # buf[7]  TODO(uncertain): copy from a fresh "get" response
    f_2090_bool: bool = False,      # buf[8]  encoded as: (val != 1) ? 1 : 0  TODO(uncertain semantic)
    f_2098_bool: bool = False,      # buf[9]  encoded as: (val == 1) ? 0x55 : 0  TODO(uncertain semantic)
    f_20a8: int = 0,                # buf[10] TODO(uncertain): single byte. Constructor default is 0xFF.
) -> bytes:
    """Build the 65-byte HID output report for the USB (non-BLE) PM10.

    Layout is documented in SETTINGS_CMD.md.  DO NOT call this to "restore defaults"
    without first snapshotting the current settings via the 0x81 "get" query and
    verifying which byte controls which setting on the device.  The mapping of
    buf[3..7] to user-facing settings (language / filter / heart-beat sound / etc.)
    is NOT resolved by the packet-build code alone.
    """
    if not (25 <= hr_low_bpm <= 300):
        raise ValueError("hr_low_bpm out of device range 25..300")
    if not (25 <= hr_high_bpm <= 300):
        raise ValueError("hr_high_bpm out of device range 25..300")

    buf = bytearray(65)          # HID report ID 0x00 followed by 64 payload bytes
    buf[0]  = 0x00               # HID report id (LOW: HID stack strips this)
    buf[1]  = 0x83               # opcode  (HIGH)
    buf[2]  = 0x01               # sub-op / constant  (HIGH)
    buf[3]  = f_27f4 & 0xFF
    buf[4]  = f_2078 & 0xFF
    buf[5]  = f_207c & 0xFF
    buf[6]  = f_2884 & 0xFF
    buf[7]  = f_2074 & 0xFF
    buf[8]  = 1 if not f_2090_bool else 0     # (val != 1) ? 1 : 0
    buf[9]  = 0x55 if f_2098_bool else 0x00   # (val == 1) ? 0x55 : 0
    buf[10] = f_20a8 & 0xFF
    buf[11] = (hr_low_bpm  >> 7) & 0x7F       # HIGH: big-endian 14-bit
    buf[12] =  hr_low_bpm        & 0x7F       # HIGH
    buf[13] = (hr_high_bpm >> 7) & 0x7F       # HIGH
    buf[14] =  hr_high_bpm       & 0x7F       # HIGH
    # buf[15..64] remain zero (matches rep stosd clear at 0x0043b13e)
    return bytes(buf)
```

## Confidence tally (65 bytes)

- **HIGH** (55 bytes) — buf[0], buf[1], buf[2], buf[11], buf[12], buf[13], buf[14], and all 50 tail bytes buf[15..64].
- **MEDIUM** (10 bytes) — buf[3], buf[4], buf[5], buf[6], buf[7], buf[8], buf[9], buf[10] — provenance to a specific `[esi+X]` member is verified, but the mapping of that member to a user-visible setting is not resolved in this function.
- **LOW** (0 bytes) — no field was labelled purely by guess.

## Checksum specification

- **USB (non-BLE) branch: no checksum byte.** No call to `fcn.00401dac`/`sub_43ff00` occurs between the last data write and the send.
- BLE branch (informational, not used): `sum(buf[1..14)) & 0x7F` written to buf[14]. Callee is `__thiscall` (`ecx=this=esi`), stack args `push count=0x0d; push start=&buf[1]`, invoked at 0x0043b246, result byte at 0x0043b24b.
