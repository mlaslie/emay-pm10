"""EMAY / Contec PM10 non-BLE variant — record downloader.

Protocol (empirically confirmed on PM107934):
- USB-HID VID 0x4444 PID 0x5555, 64-byte reports.
- 0x81 = get info
- 0x82 = get datetime
- 0x90 = state trigger; puts device into USB-connected mode. Response starts 0xE0.
- 0xA0 <id_lo> <id_hi> = download record — replies with many 64-byte packets
  starting with 0xD0. Record 0 doesn't exist; records 1+ do.

Response packet format (empirical from A0[1]):
    byte 0    : 0xD0 (opcode ack)
    bytes 1-4 : chunk header (offset counter?)
    bytes 4-63: 60 bytes = 30 samples of 16-bit ECG data (12-bit offset-binary)

Usage:
  .venv/bin/python cli/download.py <index>      # e.g., 1
  .venv/bin/python cli/download.py all          # try indices 1..10
"""
import argparse
import datetime
import struct
import sys
import time

import hid


VID, PID, REPORT_LEN = 0x4444, 0x5555, 64


def ts() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def pkt(*payload: int) -> bytes:
    body = bytearray(REPORT_LEN)
    for i, b in enumerate(payload):
        body[i] = b & 0xFF
    return b"\x00" + bytes(body)


def send(d, buf: bytes) -> None:
    d.write(buf)


def drain(d, wait_ms: int) -> list[bytes]:
    got = []
    end = time.time() + wait_ms / 1000
    last_pkt = time.time()
    while time.time() < end:
        data = d.read(REPORT_LEN, timeout_ms=100)
        if data:
            got.append(bytes(data))
            last_pkt = time.time()
            # Extend deadline if we're still receiving
            end = max(end, last_pkt + wait_ms / 1000)
    return got


def handshake(d) -> None:
    """Info → state trigger. After this the device shows USB icon on LCD."""
    send(d, pkt(0x81, 0x01)); drain(d, 300)
    send(d, pkt(0x82, 0x02)); drain(d, 300)
    send(d, pkt(0x90));       drain(d, 500)


def download_record(d, idx: int) -> bytes | None:
    """Send 0xA0 for record `idx`, return concatenated raw packets or None."""
    lo, hi = idx & 0x7F, (idx >> 7) & 0x7F
    print(f"[{ts()}] requesting record {idx}")
    send(d, pkt(0xA0, lo, hi))
    packets = drain(d, 3000)
    if not packets:
        print(f"         no response — record {idx} probably absent")
        return None
    print(f"         got {len(packets)} packets ({sum(len(p) for p in packets)} bytes)")
    return b"".join(packets)


def decode_samples(raw: bytes) -> list[int]:
    """Extract 16-bit big-endian samples from packet payloads.

    Each 64-byte packet: [0xD0, hdr1, hdr2, hdr3, s0_hi, s0_lo, s1_hi, s1_lo, ...]
    → 60 payload bytes = 30 samples.
    """
    samples = []
    for i in range(0, len(raw), REPORT_LEN):
        pkt = raw[i:i + REPORT_LEN]
        if len(pkt) < REPORT_LEN or pkt[0] != 0xD0:
            continue
        # Skip 4-byte header (0xD0 + 3 header bytes)
        payload = pkt[4:]
        for j in range(0, len(payload), 2):
            if j + 1 < len(payload):
                s = struct.unpack(">H", payload[j:j+2])[0]
                samples.append(s)
    return samples


def save_raw(raw: bytes, idx: int) -> str:
    path = f"/Users/mlaslie/emay-pm10/pm10_record_{idx:02d}.bin"
    with open(path, "wb") as f:
        f.write(raw)
    return path


def save_csv(samples: list[int], idx: int) -> str:
    """Save samples as CSV: t_sec, raw_u16, mv (offset-binary 0x4000 = 0 mV, ±5 mV = ±2048 LSB)."""
    path = f"/Users/mlaslie/emay-pm10/pm10_record_{idx:02d}.csv"
    fs = 150  # Hz (per Contec datasheet)
    with open(path, "w") as f:
        f.write("t_sec,raw_u16,mv\n")
        for i, s in enumerate(samples):
            t = i / fs
            # Convert offset-binary to mV: (s - 0x4000) * (5 mV / 0x0800)
            mv = (s - 0x4000) * (5.0 / 0x0800)
            f.write(f"{t:.4f},{s},{mv:.4f}\n")
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("index", help="record index (int) or 'all' to try 1..10")
    args = ap.parse_args()

    d = hid.device()
    d.open(VID, PID)
    d.set_nonblocking(True)
    print(f"[{ts()}] opened.")

    try:
        handshake(d)
        print(f"[{ts()}] handshake done — device should show USB icon.\n")

        indices = list(range(1, 11)) if args.index == "all" else [int(args.index)]
        for idx in indices:
            raw = download_record(d, idx)
            if raw is None:
                continue
            path_bin = save_raw(raw, idx)
            samples = decode_samples(raw)
            path_csv = save_csv(samples, idx)
            print(f"  → {path_bin}  ({len(raw)}B)")
            print(f"  → {path_csv}  ({len(samples)} samples ≈ {len(samples)/150:.1f}s at 150 Hz)")
    finally:
        d.close()


if __name__ == "__main__":
    main()
