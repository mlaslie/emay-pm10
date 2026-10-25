"""Attempt to restore EMAY PM10 to English + reasonable defaults.

STRATEGY:
1. First query current device info via 0x81 (safe, read-only) — log response.
2. Query current settings via 0x82 0x02 (get datetime), 0x83 0x01 (get date v2) — read-only.
3. If user confirms, send ONE SET_DEVICE_PARAMETERS packet (opcode 0x83 0x01)
   with the non-BLE / USB packet layout reverse-engineered from EXE at
   VA 0x0043b320. Byte values:
     buf[3]  = 1   (language: English)         # HIGH-confidence guess
     buf[4]  = 1   (filter: ON)                # CONFIRMED via IDC_TXT_0113
     buf[5]  = 1   (unknown; default post-connect = 1 at 0x433bb1)
     buf[6]  = 1   (unknown; default post-connect = 1 at 0x433bb7)
     buf[7]  = 1   (unknown; safest low-value guess vs constructor 0xFF)
     buf[8]  = 1   (per constructor: (0!=1) → 1)
     buf[9]  = 0   (per constructor: (0==1) → 0)
     buf[10] = 0   (unknown; avoid constructor 0xFF)
     buf[11] = 0   (HR-low hi 7 bits: 60 >> 7)
     buf[12] = 60  (HR-low lo 7 bits: 0x3C)
     buf[13] = 0   (HR-high hi 7 bits: 100 >> 7)
     buf[14] = 100 (HR-high lo 7 bits: 0x64)
     buf[15..64] = 0
     NO checksum (USB-branch omits it — confirmed).

This is a considered guess, not certainty. Risk: could further alter settings.
"""
import argparse
import datetime
import sys
import time

import hid


VID, PID, RLEN = 0x4444, 0x5555, 64


def ts() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def pkt(*payload: int) -> bytes:
    body = bytearray(RLEN)
    for i, b in enumerate(payload):
        body[i] = b & 0xFF
    return b"\x00" + bytes(body)


def send(d, buf: bytes) -> None:
    d.write(buf)


def drain(d, wait_ms: int) -> list[bytes]:
    got = []
    end = time.time() + wait_ms / 1000
    while time.time() < end:
        data = d.read(RLEN, timeout_ms=100)
        if data:
            got.append(bytes(data))
    return got


def build_settings_packet(
    language: int = 1,           # buf[3] — GUESS: 0=Chinese, 1=English
    filter_on: int = 1,          # buf[4] — CONFIRMED via IDC_TXT_0113
    f5: int = 1,                 # buf[5] — unknown, use post-connect default
    f6: int = 1,                 # buf[6] — unknown, use post-connect default
    f7: int = 1,                 # buf[7] — unknown, safest value
    f8: int = 1,                 # buf[8] — from constructor logic
    f9: int = 0,                 # buf[9] — from constructor logic
    f10: int = 0,                # buf[10] — unknown, safest value
    hr_low: int = 60,            # buf[11-12] — 14-bit big-endian
    hr_high: int = 100,          # buf[13-14] — 14-bit big-endian
) -> bytes:
    """Build the 65-byte HID output report for USB PM10 SET_DEVICE_PARAMETERS.

    Layout matches disassembly at 0x0043b254..0x0043b2ff (non-BLE branch)."""
    if not (25 <= hr_low <= 300):
        raise ValueError(f"hr_low out of range: {hr_low}")
    if not (25 <= hr_high <= 300):
        raise ValueError(f"hr_high out of range: {hr_high}")
    body = bytearray(RLEN)
    body[0] = 0x83                          # opcode
    body[1] = 0x01                          # sub-op
    body[2] = language & 0x7F               # buf[3]
    body[3] = filter_on & 0x7F              # buf[4]
    body[4] = f5 & 0x7F                     # buf[5]
    body[5] = f6 & 0x7F                     # buf[6]
    body[6] = f7 & 0x7F                     # buf[7]
    body[7] = f8 & 0x7F                     # buf[8]
    body[8] = f9 & 0x7F                     # buf[9]
    body[9] = f10 & 0x7F                    # buf[10]
    body[10] = (hr_low >> 7) & 0x7F         # buf[11]
    body[11] = hr_low & 0x7F                # buf[12]
    body[12] = (hr_high >> 7) & 0x7F        # buf[13]
    body[13] = hr_high & 0x7F               # buf[14]
    # buf[15..64] left zero — no checksum on USB branch
    return b"\x00" + bytes(body)


def cmd_snapshot(args) -> None:
    """Read-only: query current device state via 0x81 and 0x82."""
    d = hid.device(); d.open(VID, PID); d.set_nonblocking(True)
    print(f"[{ts()}] opened.")
    try:
        for label, cmd in [
            ("info 81_01", pkt(0x81, 0x01)),
            ("datetime 82_02", pkt(0x82, 0x02)),
            ("date-v2 83_00", pkt(0x83, 0x00, 0, 0)),
        ]:
            send(d, cmd)
            responses = drain(d, 500)
            print(f"[{ts()}] {label}:  {len(responses)} rsp(s)")
            for r in responses:
                print(f"  {r.hex()}")
    finally:
        d.close()


def cmd_apply(args) -> None:
    """Send the settings-restore packet."""
    packet = build_settings_packet(
        language=args.language,
        filter_on=1,
        hr_low=60,
        hr_high=100,
    )
    print(f"About to send restore packet:")
    print(f"  {packet.hex()}")
    print(f"  bytes 1-14 (payload): {packet[1:15].hex()}")
    print()
    d = hid.device(); d.open(VID, PID); d.set_nonblocking(True)
    print(f"[{ts()}] opened.")
    try:
        # Handshake first (matches connect flow)
        send(d, pkt(0x81, 0x01)); drain(d, 300)
        send(d, pkt(0x82, 0x02)); drain(d, 300)
        # Send the restore
        print(f"[{ts()}] sending settings restore...")
        send(d, packet)
        resp = drain(d, 2000)
        print(f"[{ts()}] got {len(resp)} response packet(s)")
        for r in resp:
            print(f"  RX  {r.hex()}")
        # Then re-query state to verify
        print(f"\n[{ts()}] re-querying state:")
        send(d, pkt(0x81, 0x01))
        for r in drain(d, 400):
            print(f"  81 -> {r.hex()}")
    finally:
        d.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("snapshot", help="Read-only: query current device state").set_defaults(func=cmd_snapshot)
    p = sub.add_parser("apply", help="Send the settings-restore packet")
    p.add_argument("--language", type=int, default=1,
                   help="Language index: 0=Chinese, 1=English (default 1)")
    p.set_defaults(func=cmd_apply)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
