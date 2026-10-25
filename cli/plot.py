"""Plot decoded PM10 ECG record.

Packet layout (empirically verified 100% in-range for offset 11):
  byte 0    : 0xD0 (opcode ack)
  bytes 1-2 : packet header/metadata
  bytes 3-10: bit 7 of each data byte, 7 per header byte, LSB-first
              (data bytes are sent 7-bit clean, bit 7 always 0)
  bytes 11-60: 25 samples × 16-bit big-endian (offset-binary, 0x4000 = 0 mV)
  bytes 61-62: 2 bytes (padding?)
  byte 63   : sequence counter low byte
"""
import struct
import sys

import matplotlib.pyplot as plt
import numpy as np


REPORT_LEN = 64
MSB_START = 3
SAMPLE_START = 11
SAMPLE_END = 61  # exclusive
SAMPLES_PER_PACKET = (SAMPLE_END - SAMPLE_START) // 2
SR = 250  # sample rate; from 7500 samples / 30 s recording
V_PER_LSB = 5.0 / 0x0800  # 12-bit offset-binary centred at 0x4000, ±5 mV


def load(path: str) -> tuple[np.ndarray, list[dict]]:
    raw = open(path, "rb").read()
    samples = []
    meta = []
    for i in range(0, len(raw), REPORT_LEN):
        pkt = bytearray(raw[i:i + REPORT_LEN])
        if len(pkt) < REPORT_LEN:
            continue
        meta.append({
            "op": pkt[0], "hdr": pkt[1:11].hex(), "seq": pkt[63],
        })
        for n in range(SAMPLE_END - SAMPLE_START):
            if (pkt[MSB_START + n // 7] >> (n % 7)) & 1:
                pkt[SAMPLE_START + n] |= 0x80
        for j in range(SAMPLE_START, SAMPLE_END, 2):
            samples.append(struct.unpack(">H", pkt[j:j+2])[0])
    return np.array(samples, dtype=np.int32), meta


def plot(samples: np.ndarray, out_png: str, sr: int = SR) -> None:
    mv = (samples.astype(np.float64) - 0x4000) * V_PER_LSB
    t = np.arange(len(mv)) / sr

    fig, axes = plt.subplots(3, 1, figsize=(14, 9), sharex=False)

    axes[0].plot(t, mv, linewidth=0.6, color="#0a4")
    axes[0].set_title(f"EMAY PM10 ECG — full record  ({len(mv)} samples @ {sr} Hz = {t[-1]:.1f} s)")
    axes[0].set_xlabel("time (s)")
    axes[0].set_ylabel("mV")
    axes[0].grid(True, alpha=0.3)
    axes[0].axhline(0, color="k", linewidth=0.4)

    # Zoomed 5-second window from middle of recording
    z0 = int(len(mv) * 0.4)
    z1 = int(len(mv) * 0.4 + 5 * sr)
    axes[1].plot(t[z0:z1], mv[z0:z1], linewidth=1.0, color="tab:red")
    axes[1].set_title(f"Zoom: {t[z0]:.1f}–{t[z1-1]:.1f} s")
    axes[1].set_xlabel("time (s)")
    axes[1].set_ylabel("mV")
    axes[1].grid(True, alpha=0.3)
    axes[1].axhline(0, color="k", linewidth=0.4)

    # Simple R-peak detection: local maxima > 0.5 mV, min 300 ms apart
    peak_min = int(0.3 * sr)
    peaks = []
    i = 0
    while i < len(mv):
        if mv[i] > 0.5:
            w = mv[i:min(i + peak_min, len(mv))]
            k = int(np.argmax(w))
            peaks.append(i + k)
            i += peak_min
        else:
            i += 1
    axes[2].plot(t, mv, linewidth=0.6, color="tab:blue")
    peak_t = np.array(peaks) / sr
    peak_v = mv[peaks]
    axes[2].scatter(peak_t, peak_v, s=15, color="red", zorder=3)
    axes[2].set_title(f"Detected R-peaks: {len(peaks)}  →  heart rate ≈ {60 * len(peaks) / (t[-1] if t[-1] > 0 else 1):.0f} bpm")
    axes[2].set_xlabel("time (s)")
    axes[2].set_ylabel("mV")
    axes[2].grid(True, alpha=0.3)
    axes[2].axhline(0, color="k", linewidth=0.4)

    fig.tight_layout()
    fig.savefig(out_png, dpi=110)
    print(f"wrote {out_png}")


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "pm10_record_01.bin"
    sr = int(sys.argv[2]) if len(sys.argv) > 2 else SR
    samples, meta = load(path)
    print(f"decoded {len(samples)} samples from {len(meta)} packets")
    print(f"range: {samples.min()}..{samples.max()} (0x{samples.min():04X}..0x{samples.max():04X})")
    print(f"median: {int(np.median(samples))} (0x{int(np.median(samples)):04X})")
    plot(samples, path.rsplit(".", 1)[0] + ".png", sr=sr)


if __name__ == "__main__":
    main()
