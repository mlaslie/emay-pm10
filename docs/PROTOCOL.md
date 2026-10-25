# EMAY / Contec PM10 — USB-HID Wire Protocol (reverse-engineered)

Static analysis only, macOS host, `Portable ECG Monitor.exe` (32-bit MFC PE, ~2.7 MB)
and `UsbDll.dll` (~28 KB). Read-only via `rizin` and `strings`. No binary was executed.

VID/PID: **0x4444 / 0x5555** (opened by `CUsbFilter::OpenUsb(0x4444,0x5555,0x200,...)`
at EXE 0x00432ea8 and 0x00437630).

---

## 1. Transport frame

### 1.1 Buffer sizes

`CUsbFilter::OpenUsb` is called with the third argument `0x200` (=512), which is
the internal HID-report buffer size the DLL allocates. **All actual TX packets
sent are exactly 65 bytes (0x41).** The first byte is the Windows HID report ID
(always `0x00`), followed by 64 bytes of payload.

Every one of the ~29 `SendToUsb` call sites in the EXE pushes `0x41` as the
length argument to the send wrapper (see the summary table in §3).

### 1.2 Write path

`fcn.00401a0f` is a `jmp` stub that resolves to `sub_4366a0`:

```
0x004366a0  mov  eax, [esp+0x0c]    ; length
0x004366a4  mov  edx, [esp+0x04]    ; buffer pointer
0x004366a8  push eax
0x004366a9  push edx
0x004366aa  add  ecx, 0x2168        ; ecx = &CDialog::m_UsbFilter
0x004366b0  call sym.imp.UsbDll.dll_CUsbFilter::SendToUsb   ; -> 0x5fc500
0x004366b6  dec  eax
0x004366b7  neg  eax
0x004366b9  sbb  eax, eax
0x004366bb  inc  eax
0x004366bc  ret  0xc                ; cleans 3 stack args (buf, wait_ms, len)
```

So the C++ wrapper signature is
`int __thiscall wrap(this, uint8_t* buf, int wait_ms_ignored, int len)`.
The `wait_ms` (always pushed as `0x3e8`, i.e. 1000 ms) is discarded; the DLL's
`SendToUsb` is a WriteFile+GetOverlappedResult that blocks until complete.

### 1.3 DLL SendToUsb (UsbDll.dll @ 0x10001b00)

```
sub  esp, 0x14                       ; OVERLAPPED on stack, hEvent[0]=0
mov  edx, [esp+0x1c]                 ; len
mov  esi, ecx                        ; this
push &overlapped                     ; lpOverlapped
mov  ecx, [esi+0x10]                 ; write handle
push 0                               ; lpBytesWritten
mov  eax, [esp+0x24]                 ; buf
push edx                             ; nNumberOfBytesToWrite = len (always 65)
push eax                             ; lpBuffer
push ecx                             ; hFile
call WriteFile
...
GetOverlappedResult(hFile, &overlapped, &bytes, TRUE)
```

The DLL uses two file handles (`OpenUsb` calls `CreateFileW` twice on the same
device path with different access masks): `[esi+0x0c]` is the read handle,
`[esi+0x10]` is the write handle.

### 1.4 Read path — DLL ReadThread (UsbDll.dll @ 0x10001a00)

```
lea  ecx, [esi+0x48]                 ; OVERLAPPED
push ecx
push 0                               ; lpBytesRead
push 0x28a                           ; nNumberOfBytesToRead = 650
push [esi+0x70]                      ; lpBuffer  (buffer @ this+0x70, 0x400 bytes)
push [esi+0x0c]                      ; read handle
call ReadFile
...
WaitForSingleObject(hEvent, INFINITE)
GetOverlappedResult(...) -> bytes    ; store dword at [esi+0x470]
if (hwnd) SendMessageW(hwnd, 0x464, 0, bytes);
else      SetEvent(hevent);
```

**RX packet size is 650 bytes (0x28a).** This is the raw HID input-report
capacity the DLL asks the driver for; the actual returned length is passed to
the EXE as `lParam` of a `WM_USER+0x64 == 0x464` (=1124) message.

The EXE registers its notify HWND via `CUsbFilter::set_hwnd`, so every input
report generates one `SendMessageW(hwnd, 0x464, 0, bytesRead)`; if no HWND is
registered, an event handle set via `set_hevent` is signalled instead.

### 1.5 Packet layout (TX side)

```
byte 0    : HID report ID       (always 0x00)
byte 1    : command opcode      (see §3)
byte 2    : sub-opcode / count  (command-specific)
byte 3..N : parameter bytes
byte N+1  : checksum            (present on some commands, see §1.6)
byte ...  : zero padding to 65 bytes
```

### 1.6 Checksum

Helper `fcn.00401dac` -> `sub_43ff00`:

```
0x0043ff00  mov  edx, [esp+8]       ; count
0x0043ff04  xor  al, al
0x0043ff06  xor  ecx, ecx
loop:
0x0043ff12  mov  bl, [ecx+esi*1]
0x0043ff15  add  al, bl
0x0043ff17  inc  ecx
0x0043ff1a  jl   loop
0x0043ff1e  and  al, 0x7f           ; 7-bit sum
0x0043ff20  ret  8
```

So the checksum is
`ck = (sum(bytes[start..start+count])) & 0x7F`.
It is always computed over the region **starting at buffer offset 1** (i.e. the
opcode and its parameters, not the HID report ID) and stored **immediately
after** the covered bytes. Commands that do not call `0x401dac` (e.g. the
plain "connect" and "disconnect" packets) leave every byte after the opcode
zero, and the device tolerates that.

Concretely, the pattern seen at every send-site is one of:

```
;; no-checksum command (e.g. 0x81 0x01)
mov byte [buf+1], OP
mov byte [buf+2], SUB
push 0x41 ; push 0x3e8 ; push &buf ; call 0x401a0f

;; checksummed command (e.g. 0x96 <param> ..)
mov byte [buf+1], OP
mov byte [buf+2..], params
lea eax, [buf+1]
push N                                ; number of bytes covered
push eax
call 0x401dac                         ; -> al = (sum & 0x7f)
mov byte [buf+1+N], al                ; checksum trailing byte
push 0x41 ; push 0x3e8 ; push &buf ; call 0x401a0f
```

---

## 2. Response (RX) format — undetermined

The 650-byte read buffer is delivered to the EXE via `WM_USER+0x64` messages
(msg = 0x464). I was **not able to statically locate a single handler** that
`switch()`es on an opcode in the RX buffer: neither `cmp <mem>, 0x81` nor
`cmp <mem>, 0x83` etc. appear in the EXE, and there is no MFC message-map
entry for 0x464 that can be located with the `81 f9 64 04` or `3d 64 04` byte
patterns.

That strongly suggests one of:

1. The RX dispatcher indirects through a virtual dispatch or a jump table that
   `rizin`'s static analysis did not recover. A dynamic trace would find it in
   seconds; static analysis will not.
2. The read buffer is treated as a byte stream reassembled across many
   `WM_USER 0x464` events (record downloads must be — a 30-second lead II at
   150 sps × 12 bits ≈ 6.75 KB spans dozens of HID reports).

What IS known statically:

- Each `SendMessageW(hwnd, 0x464, 0, N)` gives the EXE `N` bytes at
  `[this+0x70]` inside the CUsbFilter object. There is no explicit framing
  byte the DLL prepends or strips; the buffer is whatever the device emitted.
- The DLL never inspects the RX bytes.
- The `set_hevent` code path shows the DLL can also be driven synchronously:
  send a command, `WaitForSingleObject(hEvent, 1000)`, then read
  `CUsbFilter::GetBufLenth()` = `[this+0x470]` and process `[this+0x70]`.
  This is the shape a Python re-implementation should use.

**Concrete facts about RX packet size that CAN be asserted from the DLL:**

- One `ReadFile` request = up to 650 bytes.
- Windows HID stack will not deliver more than one input report per ReadFile,
  so the *device's* HID input-report size is ≤ 650 bytes. Most likely 64 or 65
  bytes (matching TX), with the DLL just requesting a large-enough buffer.
- Multi-packet responses ARE required: strings like `HelpEN.chm` are 30-s ECG
  records, and the record download commands (§3, opcodes `0xb3/0xb5/0xfb/0xfc`)
  push tens of bytes and expect the device to stream back.

The exact framing (start byte, length, CRC) is **not determinable statically
from the EXE without executing it or capturing USB traffic.**

---

## 3. Command catalog

Column key:
- **Site**: address of the `call fcn.00401a0f` (= the send point).
- **Opcode / bytes**: contents of `buf[1..]` before the wrapper is called.
- **cks bytes**: `N` passed to `fcn.00401dac`; absent = no checksum computed.
- **Notes**: nearest IDC_TXT_* label or datetime/utility calls giving semantic hints.

All packets are 65 bytes total (report ID 0x00 + 64 payload). All timeouts are
1000 ms. Every entry cites the exact EXE offset of the byte-write instructions.

### 3.1 Fixed 2-byte handshake commands (no checksum)

| Site | Buf offsets → values | Meaning (inferred) |
|---|---|---|
| 0x004334c7 | `buf[1]=0x81` @ 0x0043349a; `buf[2]=0x01` @ 0x0043349e | **GET_DEVICE_INFO / connect**. Surrounded by `IDC_TXT_0016` @ 0x004333b1 and `IDC_TXT_0017` @ 0x004333fe. First real command in the "connect" dialog flow. |
| 0x00433673 | `buf[1]=0x82` @ 0x00433657; `buf[2]=0x02` @ 0x0043365c | **GET_DATETIME** (or trigger sync). Adjacent to `IDC_TXT_0019/0020`. |
| 0x00433aa0 | branch A: `buf[1]=0x84, buf[2]=0x04` @ 0x00433a82/0x00433a86 · branch B: `buf[1]=0x00, buf[2]=0x00` @ 0x00433a8c/0x00433a8f | **START_RECORD (0x84 0x04)** vs a "clear/nop" alternative. IDC_TXT_0021. Preceded by `WaitForSingleObject(1000)`. |
| 0x0043417b | `buf[1]=0xFE` @ 0x00434153 | **DISCONNECT / close session** (0xFE). Around IDC_TXT_0023. |
| 0x00433ed2 (partial) | `buf[1]=0x90` @ 0x00433e89; `buf[2]=0x03` @ 0x00433e97 (then modified by cksum path); | **START_LIVE_STREAM 0x90 0x03** – see §3.3 for full form. IDC_TXT_0024. |
| 0x00433d5f | `buf[1]=0x85`, `buf[2]=0x01` @ 0x00433c90/0x00433d33 followed by many small writes; two `0x401dac` calls | **WRITE_PATIENT_INFO 0x85 0x01** – long variable-length ASCII payload assembled from `_wtoi`/string helpers, then 13-byte and 62-byte checksums. |
| 0x00436b12c | (see 0x0043672c) | ... |
| 0x0043672c | `buf[1]=0x80` @ 0x00436712 | **BLE / handshake ping 0x80** (single byte, no sub-op). |
| 0x0043bc12 | `buf[1]=0xFE` @ 0x0043bbf0 | Second DISCONNECT site (different UI path — cancel from IDC_TXT_0014 page). |
| 0x0043c542 | `buf[1]=0xFE` @ 0x0043c53d (via `[esp+0x1d]`) | Third DISCONNECT site (function that has no other setup). |

### 3.2 Data-carrying commands with 2-byte checksum

Sum is over `buf[1..2]`; result stored at `buf[3]`.

| Site | Packet | Notes |
|---|---|---|
| 0x00430e77 | `[0x00, 0x96, 0x01, ck, 00..]` — writes @ 0x00430e4a/0x00430e52, ck @ 0x00430e70 | **REQUEST_NEXT_BLOCK 0x96 0x01**. Inside a `tmp.dat` writer loop (record download). |
| 0x00439e7b | `[0x00, 0x96, <lo>, ck, 00..]` — writes @ 0x00439e53/0x00439e5a | **REQUEST_RECORD_INDEX 0x96 <recordId low>**. `<lo>` = low byte of a `CTime`/index value from `mov [ebp-0x12e], al`. |
| 0x00434078 | `[0x00, <dl>, 0x01, ck2, ...]` – two cksums (`0x401dac` count=2 @ 0x0043403d, count=6 @ 0x00434056). Behavior depends on a `wcscmp(fw, "1.01")` earlier @ 0x00433fe2. | **FIRMWARE-DEPENDENT GET_LIST**: old fw sends the 2-byte variant, new fw the 6-byte variant. Same struct as 0x0043ba59 below. |
| 0x0043ba59 | Same double-`0x401dac` pattern (count 2 @ 0x0043ba1b, count 6 @ 0x0043ba35). Preceded by wcscmp(fw,"1.01") @ 0x0043b9ba. | Sibling of 0x00434078. |
| 0x0043b81e | `[0x00, 0x90, cl, ck, ...]` writes @ 0x0043b7e3 (`buf[2]=0x03` under pushes, i.e. offset 2 before overwrite) / 0x0043b7ed (ck) / 0x0043b7f9 (`buf[2]=cl`) | **RESET_LIVE_STREAM 0x90 <mode>** — mode byte assembled from a `fcn.00401492(-1)` call. |

### 3.3 Data-carrying commands with 6-byte checksum

Sum is over `buf[1..6]`; result stored at `buf[7]`.

| Site | Packet |
|---|---|
| 0x00430ede | `[0x00, 0x98, <dl>, <al&0x4f>, 0x01, 0, 0, ck, 0..]` — writes @ 0x00430ea3/0eab/0eb2/0eb9; ck @ 0x00430ed7. `dl` = `[ebp+0x288c] & 0x7f`, `al` = `[ebp+0x288c] >> 7 & 0x4f`. **SEEK_TO_OFFSET (0x98)** — 14-bit block offset encoded across two 7-bit-clean bytes. |
| 0x00433ed2 | `[0x00, 0x90, 0x03, cl, ck, ..]` — 0x90 @ 0x00433e89, 0x03 @ 0x00433e97, ck sums 2 bytes, then a further `call ebx` before final send. **START_LIVE_STREAM 0x90 0x03**. |
| 0x00438731 | `[0x00, 0x98, cl, al, 0x00, ck, ..]` @ 0x0043863b/0x00438660/0x0043866d/ck 0x00438678, then re-preps as `[0x00, 0xa0, cl, al, ...]` @ 0x00438683/0x0043869e/0x004386a4. Both branches then send. Values from `_wtoi` @ 0x00438643 & 0x0043868b. **Read specific record by index — sub_op 0x98 followed by 0xa0**. |
| 0x00438f87 | `[0x00, 0x98, al, cl, dl, 0x00, ck, ..]` — 0x98 @ 0x00438f3a; al,cl,dl @ 0x00438f4b/0x00438f5a/0x00438f66; ck @ 0x00438f81. **DELETE_RECORD 0x98 <id>** (bytes derived from `_wtoi(sprintf("%d", ...))`). |
| 0x00439898 | `[0x00, 0x96, al, ck, ..]` writes at 0x00439847/0x00439853 (2-byte cksum path) **then** overwritten to `[0x00, 0xa0, dl, al, ..]` @ 0x00439861/0x0043987c/0x00439882 (no second cksum). **RESET/PING with 0xa0** — both opcodes live in same function; only the second reaches the wire. |
| 0x0043aabc | `[0x00, 0x99, 0x02, 0, al, 0, ck, ..]` @ 0x0043aa6c/0x0043aa71/0x0043aa76; ck sums 6 @ 0x0043aa7a; then overwritten to `[0x00, 0xb0, 0x02, cl, al, ..]` @ 0x0043aa87/0x0043aa94/0x0043aa99/0x0043aa9d. **DELETE / TAG RECORD 0xb0 0x02** (final on-wire opcode is 0xb0). |
| 0x0043adea | `[0x00, 0x99, al, 0, 0, 0, ck, ..]` @ 0x0043ada5/0x0043adaa, ck @ 0x0043adb3; then overwritten to `[0x00, 0xb0, dl, ..]` @ 0x0043adbf/0x0043adcb. Sibling of 0x0043aabc. |
| 0x0043b320 | `[0x00, 0x83, 0x01, ..date-time bytes.., ck]` @ 0x0043b2ee/0x0043b2f6 (0x83, 0x01 at offsets 1,2), then further writes assembling 5..6 more bytes (year/month/day/hour/min/sec) that were pre-loaded at [esp+0x33..0x3d]; second cksum at 0x0043b23d/0x0043b242 sets `buf[9]=0x85` at post-push offsets. Reads as **SET_ALARM / SET_DATETIME_v2 (0x83 0x01)** with encoded date-time similar to §3.4. |
| 0x0043b81e | (already listed above) |

### 3.4 Long checksummed commands (data upload)

| Site | Packet |
|---|---|
| 0x00433944 | `[0x00, 0x82, 0x02, YY, MM, DD, HH, MM, SS, XX, ck, 0..]`. Nine byte-writes each fed by `CTime::GetYear` / `GetMonth` / `GetDay` / `GetHour` / `GetMinute` / `GetSecond` at 0x00433877..0x004338b8 (initial `bl=0` at buf+1, then overwritten to 0x82 at 0x004338d8, sub-op 0x02 at 0x004338d5-region). `0x401dac` count=9 @ 0x004338cb, result at `buf[10]` = `[ebp-0x6a]` @ 0x004338d0. **SET_DATETIME 0x82 0x02**. |
| 0x0043db14 | `[0x00, 0xB8, 0x00, 0x00, 0x00, 0x00, 0x38, 0..]` — 0xB8 @ 0x0043daf5, `'8' (0x38)` @ 0x0043dafb. No cksum call reached. Padded zeros. **PRINT_LABEL_A / MODE_SWITCH 0xB8** followed by literal '8' (ASCII digit). |
| 0x0043de37 | `[0x00, 0xFC, ..2-byte tag.., ck, ..]` @ 0x0043dd75 (0xFC), 0x0043dd3f (`0x401dac` count = **arbitrary** — value in edx uncertain, the sum is 3 bytes); ck @ 0x0043dd44. **FORMAT / ERASE 0xFC**. |
| 0x0043e1b3 | `[0x00, 0xB5, ..49-byte payload assembled by fcn.5544e2 (str8→bytes with `stosw`/`stosb`).., ck]`. `0x401dac` count = **0x31 (49)** @ 0x0043e185. Buffer at `[esp+0x48]`. **RECORD_UPLOAD_PART 0xB5** – single 65-byte HID output report carrying ≤49 bytes of ECG payload. |
| 0x0043e4de | `[0x00, 0xFB, ..53-byte payload from same 5544e2 helper.., ck]`. `0x401dac` count = **0x35 (53)** @ 0x0043e3fc (implicit; the site does the sum via helper). **RECORD_UPLOAD_END 0xFB** – 53-byte tail. |
| 0x0043f531 | Two-arm packet: arm 1 sets `buf[1]=0xB3, buf[2]=cl` @ 0x0043f474/0x0043f482, uses 2 `repne scasb` string helpers, calls `0x401dac` on wcslen-derived count; arm 2 sets `buf[1]=0x84, buf[2]=0x01` @ 0x0043f4da/0x0043f4df, calls `0x401cd5` (a wrapper). **RECORD_HEADER 0xB3** vs **CONFIRM 0x84 0x01** (branched by fw version). |
| 0x0043f8b1 | Buffer at `[esp+0x04]`. Only clear byte-write in the trailer is `[esp+0x11]=0xA0` @ 0x0043f8ac (offset 0x0d after the pushes). The command byte in `buf[1]` is filled by `fcn.4026ad`/`fcn.401514` upstream (not statically decoded). **Uncertain — likely 0xA0 with ≥13-byte payload.** |
| 0x0044017f | `[0x00, 0xBF, YY_lo, YY_hi, MM, DD, HH, MI, SS, ms_lo, ms_hi, ...]` — 0xBF @ 0x004400b4; subsequent bytes at 0x004400bd..0x00440137 are the components of a `SYSTEMTIME` structure fetched at 0x00440090; the packet ends with `fcn.4012ee` (a helper) instead of `0x401dac`. **SET_DEVICE_TIME_v2 0xBF** — the "new" datetime-set path, uses PC `GetSystemTime` directly. |

### 3.5 Grouping by opcode (first payload byte)

- **0x80** – ping/hello (0x0043672c, single byte).
- **0x81 0x01** – get device info (0x004334c7). *First command after `OpenUsb` on the "Connect Device" screen.*
- **0x82 0x02** – get/set date-time, 9-byte payload (0x00433673 quick form, 0x00433944 full form).
- **0x83 0x01** – set alarm / v2 date-time write (0x0043b320).
- **0x84 0x01 / 0x84 0x04** – recording control / confirm (0x00433aa0 start, 0x0043f531 confirm).
- **0x85 0x01** – write patient info (0x00433d5f).
- **0x90 0x03** – start live ECG stream (0x00433ed2); 0x0043b81e reconfigures it.
- **0x96 <id>** – request record header by index (0x00430e77, 0x00439e7b, 0x00439898-arm-A).
- **0x98 <lo> <hi> 0x01** – request record data block at 14-bit offset (0x00430ede, 0x00438731-arm-A, 0x00438f87).
- **0x99 0x02** – tag/mark record (arm-A in 0x0043aabc, 0x0043adea; overwritten to 0xB0 before send).
- **0xA0 <lo> <hi>** – switch mode / follow-up query (0x00438731-arm-B, 0x00439898-arm-B, 0x0043f8b1 uncertain).
- **0xB0 0x02 …** – delete record by index (0x0043aabc, 0x0043adea, final-arm).
- **0xB3 …** – record upload header (0x0043f531 arm-A).
- **0xB5 <49 bytes>** – ECG payload chunk (0x0043e1b3).
- **0xB8 '8'** – mode switch / language? (0x0043db14).
- **0xBF <SYSTEMTIME>** – set device time v2 (0x0044017f).
- **0xFB <53 bytes>** – payload tail (0x0043e4de).
- **0xFC …** – erase / format (0x0043de37).
- **0xFE** – disconnect (0x0043417b, 0x0043bc12, 0x0043c542).

Interpretation of **high bit set** on the opcode: every command byte has bit 7 = 1
(0x80..0xFF). Combined with the 7-bit checksum and the `_wtoi`/`sar 7`/`and
0x4f` bit-splitting seen at 0x00430e83..0x00430e9e, this device follows a
**MIDI-like framing** — the "command" byte is the only byte with the top bit
set; parameter bytes are always ASCII-safe or `<0x80`, so a receiver can
resync by looking for any byte with bit 7 set.

---

## 4. Handshake sequence

Both `CUsbFilter::OpenUsb` sites (0x00432ea8 and 0x00437630) return without
sending anything themselves. `OpenUsb` merely calls `SetupDiGetClassDevsW` +
`SetupDiEnumDeviceInterfaces`, matches VID/PID/UsagePage via
`HidD_GetAttributes`, then opens the two `CreateFileW` handles (read-only and
write-only, both with `FILE_FLAG_OVERLAPPED`) and starts the read thread.

The first `SendToUsb` on the "connect" UI page is at **0x004334c7**, sending
`[0x00, 0x81, 0x01, 0x00, ...]` — 65 bytes. The dialog then walks through:

1. **`[0x00, 0x81, 0x01]`** — get device info. Around IDC_TXT_0016 (probably
   "connecting…"). Site 0x004334c7.
2. **`[0x00, 0x82, 0x02]`** — get/query device date-time. Site 0x00433673.
3. Optionally **`[0x00, 0x82, 0x02, YY, MM, DD, HH, MM, SS, XX, ck]`** — write
   PC's date-time. Site 0x00433944.
4. Then a list-query goes out: either the 2-byte-cksum form or 6-byte-cksum
   form depending on `wcscmp(firmware, "1.01")` at 0x00433fe2. Site 0x00434078.
5. Downloads use **`0x96 <idx>`** for headers and **`0x98 <lo> <hi> 0x01`** for
   data blocks, with the 14-bit block offset encoded as two 7-bit bytes.
6. When the user leaves the screen, one of the three **`0xFE`** disconnect
   packets is sent (0x0043417b / 0x0043bc12 / 0x0043c542).

---

## 5. Data format — undetermined statically

No obvious integer-scaling constant (150, 250, 500, 0.05, 1.2207, etc.) is
present in `.rdata` in a form I could confidently attribute to ECG sample
decoding. Neither `strings` nor byte pattern searches for common ECG
conversion factors (`0x66666666`, `0x3c23d70a` = 0.005f, `0x3d4ccccd` = 0.05f)
turned up hits inside the code sections that follow a `WM 0x464` handler.

Reasonable guesses based on the wire framing (see §3.4):

- Records upload as a header (0xB3) + N × 65-byte data chunks (0xB5 with 49
  data bytes each) + tail (0xFB with 53 data bytes).
- If a 30 s × 150 Hz single-lead record is 4 500 samples × 12 bits ≈ 6 750 bytes,
  that's ≈ 138 × 49-byte chunks. Plausible.
- 12-bit big-endian packed samples fit neatly (three samples per 4.5 bytes,
  but far more likely two samples per 3 bytes, or one sample per 2 bytes with
  the top 4 bits either zero or a status flag).

None of this can be verified without USB traffic capture. Marking §5 as
**unresolved**.

---

## 6. Python re-implementation skeleton

```python
"""
PM10 USB-HID protocol — first-cut Python implementation on macOS.

Requires: hidapi via `pip install hidapi`  (already in the project venv).
"""
import hid
import struct
import time

VID = 0x4444
PID = 0x5555

REPORT_LEN = 64   # HID output report data length (report id + 64 = 65 bytes on wire)


def _cks(payload: bytes) -> int:
    """CUsbFilter checksum: (sum & 0x7f)."""
    return sum(payload) & 0x7F


def _pkt(*payload: int, ck_from: int | None = None) -> bytes:
    """Build a 65-byte HID output report: [report_id=0x00] + 64-byte payload."""
    buf = bytearray(REPORT_LEN)
    for i, b in enumerate(payload):
        buf[i] = b & 0xFF
    if ck_from is not None:
        # Append ck immediately after covered region (mirrors 0x00430e70 etc.)
        ck = _cks(bytes(buf[ck_from : ck_from + len(payload) - ck_from]))
        buf[len(payload)] = ck
    # Prepend report ID
    return b'\x00' + bytes(buf)


def open_device() -> hid.device:
    d = hid.device()
    d.open(VID, PID)
    d.set_nonblocking(True)
    return d


def send(d: hid.device, pkt: bytes) -> None:
    assert len(pkt) == 65, f"expected 65-byte report, got {len(pkt)}"
    d.write(pkt)


def recv(d: hid.device, timeout_ms: int = 1000) -> bytes:
    """Read one HID input report; DLL requests 650 bytes but device likely returns 64."""
    return bytes(d.read(650, timeout_ms=timeout_ms))


# ---- Concrete commands extracted from the EXE ----------------------------

def cmd_get_device_info() -> bytes:
    """Site 0x004334c7: [0x00, 0x81, 0x01, 0x00, ...] – no checksum."""
    return _pkt(0x81, 0x01)


def cmd_get_datetime() -> bytes:
    """Site 0x00433673: [0x00, 0x82, 0x02, ...] – no checksum."""
    return _pkt(0x82, 0x02)


def cmd_set_datetime(year: int, month: int, day: int,
                     hour: int, minute: int, second: int, flag: int = 0) -> bytes:
    """Site 0x00433944: 0x82 0x02 + 7 datetime bytes + checksum over 9 bytes."""
    payload = [0x82, 0x02, year & 0x7F, month, day, hour, minute, second, flag]
    ck = _cks(bytes(payload))
    return _pkt(*payload, ck)


def cmd_start_record() -> bytes:
    """Site 0x00433aa0 arm-A: 0x84 0x04, no checksum."""
    return _pkt(0x84, 0x04)


def cmd_start_live_stream() -> bytes:
    """Site 0x00433ed2: 0x90 0x03 with 2-byte checksum. TODO: verify third byte."""
    payload = [0x90, 0x03]
    ck = _cks(bytes(payload))
    return _pkt(*payload, ck)


def cmd_list_records(fw_version: str = "1.01") -> bytes:
    """Site 0x00434078: firmware-dependent double-checksum payload.
    TODO: confirm exact bytes — the fw=='1.01' branch only sums 2 bytes, the
    other branch sums 6.  Empirically: try 0x96 0x00 with 2-byte cksum first.
    """
    payload = [0x96, 0x00]  # TODO: verify
    ck = _cks(bytes(payload))
    return _pkt(*payload, ck)


def cmd_request_record_header(idx: int) -> bytes:
    """Site 0x00439e7b: 0x96 <idx_lo> + 2-byte checksum at buf[3]."""
    payload = [0x96, idx & 0x7F]
    ck = _cks(bytes(payload))
    return _pkt(*payload, ck)


def cmd_request_record_block(record_id: int, offset14: int) -> bytes:
    """Site 0x00430ede: 0x98 <off_lo> <off_hi> 0x01 + 6-byte checksum at buf[7].

    offset14 is a 14-bit block offset split into two 7-bit-clean bytes
    exactly as the EXE does at 0x00430e83..0x00430e9e:
        lo = offset14 & 0x7F
        hi = (offset14 >> 7) & 0x4F     # NB the mask is 0x4F, not 0x7F,
                                         # so the effective range is 13 bits
                                         # (max ~0x27FF).
    """
    lo = offset14 & 0x7F
    hi = (offset14 >> 7) & 0x4F
    payload = [0x98, lo, hi, 0x01, 0x00, 0x00]  # 6 bytes covered by checksum
    ck = _cks(bytes(payload))
    return _pkt(*payload, ck)


def cmd_delete_record(idx: int) -> bytes:
    """Site 0x0043aabc/0x0043adea final arm: 0xB0 0x02 <idx> <ck>."""
    payload = [0xB0, 0x02, idx & 0x7F]
    ck = _cks(bytes(payload))
    return _pkt(*payload, ck)


def cmd_disconnect() -> bytes:
    """Sites 0x0043417b / 0x0043bc12 / 0x0043c542: 0xFE, no checksum."""
    return _pkt(0xFE)


# ---- End-to-end skeleton -------------------------------------------------

def handshake_and_download_first_record():
    d = open_device()
    try:
        # 1. Connect handshake (matches the "Connect Device" dialog flow).
        send(d, cmd_get_device_info());              _ = recv(d)
        send(d, cmd_get_datetime());                 _ = recv(d)

        # 2. Ask for the record list.  TODO: response framing not yet
        #    decoded — read *until timeout* and look for repeated 0x96
        #    responses; each 65-byte input report likely contains one
        #    record-list entry.
        send(d, cmd_list_records())
        listing = []
        while True:
            r = recv(d, timeout_ms=500)
            if not r:
                break
            listing.append(r)
        print(f"Got {len(listing)} listing reports")

        # 3. Download record 0.
        send(d, cmd_request_record_header(0))
        header = recv(d)

        # 4. Pull data blocks until the device returns an "end" report.
        #    TODO: end-of-record signal is unknown — treat empty read as end.
        blocks = []
        offset = 0
        while True:
            send(d, cmd_request_record_block(0, offset))
            blk = recv(d, timeout_ms=500)
            if not blk:
                break
            blocks.append(blk)
            offset += 1
            if offset > 200:   # sanity cap
                break

        return header, blocks
    finally:
        send(d, cmd_disconnect())
        d.close()


if __name__ == "__main__":
    hdr, blks = handshake_and_download_first_record()
    print("header:", hdr.hex(" "))
    print("blocks:", len(blks))
```

Notes on the skeleton:

- `hid.device.write` on macOS takes a bytes object whose **first byte is the
  HID report ID** (0x00 here). That matches the Windows layout exactly.
- `set_nonblocking(True)` + explicit `timeout_ms` on `read` mirrors the DLL's
  `WaitForSingleObject(hEvent, ms)` pattern.
- Wherever the EXE calls `0x401dac` with `count=N`, the Python helper `_cks`
  sums the same `N` bytes and stores the result at buffer offset `1+N`
  (identical to `mov byte [buf+1+N], al`).

### 6.1 Still unknown / verify empirically

- **RX packet framing.** The EXE has no static `switch` on the RX opcode
  visible to `rizin` (see §2). First verification step is to `print(r.hex())`
  each 65-byte report and correlate with the TX opcode — the response almost
  certainly echoes the command byte with bit-7 set as the leading byte.
- **List-command variant** (2- vs 6-byte checksum at 0x00434078 /
  0x0043ba59). Try the 2-byte form first (matches "1.01" firmware).
- **Sample encoding.** Once a real record is downloaded, expect the payload
  to be either 12-bit big-endian packed samples or int16 little-endian at
  150 Hz. See §5.
- **Live-stream stop.** The exact opcode is not clearly identified — likely
  one of `0x90 0x00`, `0xFE`, or the reset in 0x0043b81e (`0x90 <mode>` with
  mode from `fcn.00401492`).

---

## 7. Bibliography of pinned offsets

Everything cited in this document is at one of these EXE offsets. Line numbers
are byte offsets in `Portable ECG Monitor.exe`.

| Address | Purpose |
|---|---|
| 0x00401a0f | `jmp` stub → 0x004366a0 (SendToUsb wrapper). |
| 0x00401dac | `jmp` stub → 0x0043ff00 (7-bit sum). |
| 0x0043ff00 | Checksum: sum of N bytes, AND 0x7F, returned in AL. |
| 0x004366a0 | Wrapper: reorders args (buf, len) and calls DLL SendToUsb. |
| 0x005fc500 | IAT slot for `UsbDll.dll::CUsbFilter::SendToUsb`. |
| 0x005fc50c | IAT slot for `UsbDll.dll::CUsbFilter::OpenUsb`. |
| 0x00432ea8 | First `OpenUsb` call site (Connect dialog). |
| 0x00437630 | Second `OpenUsb` call site (reconnect path). |
| 0x004334c7 | First `SendToUsb` after connect – `0x81 0x01`. |
| DLL 0x10001710 | `OpenUsb` implementation. |
| DLL 0x10001a00 | `ReadThread` — 650-byte ReadFile + `SendMessageW(hwnd, 0x464, 0, N)`. |
| DLL 0x10001b00 | `SendToUsb` — WriteFile + GetOverlappedResult. |
| DLL 0x10001b80 | `set_hwnd(HWND)`. |
| DLL 0x10001b90 | `set_hevent(HANDLE)`. |
| DLL 0x10004150 | Global flag polled by ReadThread (thread-run bit). |
