"""EMAY PM10 web app — Flask backend that talks to the device over USB-HID,
stores decoded ECG recordings on disk, renders them (both a standard clinical
strip and interactive plots), sends them to Gemini for AI interpretation, and
exports PDF/CSV reports.

Layout at a glance:
  - Device I/O and the reverse-engineered USB-HID protocol: `_pkt`, `_open_device`,
    `_handshake`, `_download_record`, `_restore_packet`, `_decode_samples`.
  - Signal processing: `_samples_to_mv`, `_clean_signal`, `_detect_r_peaks`.
  - On-disk record model: `_record_dir`, `_list_records`, `_save_record`,
    `_decode_and_clean`, `_build_csv`.
  - Rendering: `_render_plot` (matplotlib overview), `_render_clinical_strip`
    + `_render_zoom_tile` (standard 25/50 mm/s ECG paper style).
  - Gemini analysis with code-execution tool: `_run_gemini_analysis`.
  - PDF export: `_generate_pdf`.
  - HTTP surface: Flask routes at the bottom of the file.

Data lives under `webapp/data/`:
  - `data/config.json` — user settings (Gemini key, model, HR limits, filter cutoffs).
  - `data/records/<record_id>/` — one directory per recording, holding raw.bin,
    meta.json, plot.png, strip.png, zoom_*.png, ai_plot_*.png, analysis.json,
    report.pdf. All persisted to disk so nothing lives only in memory.
"""
import datetime
import json
import math
import os
import statistics
import struct
import time
from pathlib import Path

import hid
import matplotlib
matplotlib.use("Agg")                      # server-side — no interactive backend
import matplotlib.pyplot as plt
import numpy as np
from flask import Flask, Response, jsonify, request, send_file, send_from_directory
from matplotlib.ticker import FuncFormatter, MultipleLocator
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Image as RLImage, Paragraph, SimpleDocTemplate, Spacer
from scipy.signal import butter, iirnotch, sosfiltfilt, tf2sos

try:
    import google.generativeai as genai
except ImportError:
    genai = None


# ── Paths ─────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
DATA = ROOT / "data"
RECORDS = DATA / "records"
CONFIG_FILE = DATA / "config.json"
RECORDS.mkdir(parents=True, exist_ok=True)

# ── Device constants ──────────────────────────────────────────────────────
VID, PID, RLEN = 0x4444, 0x5555, 64
SR_HZ = 250                       # sample rate (300 packets × 25 samples = 30 s)
V_PER_LSB = 5.0 / 0x0800          # 12-bit offset-binary → mV
MSB_START = 3                     # header bytes carrying bit 7 of each data byte, 7 per byte, LSB-first
SAMPLE_START = 11                 # per-packet offset where samples begin
SAMPLE_END = 61                   # exclusive
SAMPLES_PER_PACKET = (SAMPLE_END - SAMPLE_START) // 2
DECODER_VERSION = 2               # bump when _decode_samples changes; stale records are re-derived on startup

DEFAULT_CONFIG = {
    "gemini_api_key": "",
    "gemini_model": "gemini-3.5-flash",
    "hr_low_bpm": 60,
    "hr_high_bpm": 100,
    # Signal cleanup applied before rendering clinical views and Gemini analysis.
    # Raw samples are always preserved in raw.bin and available via CSV export.
    "filter_enabled": True,
    "filter_hp_hz": 0.5,     # high-pass — remove baseline wander
    "filter_lp_hz": 40.0,    # low-pass — remove HF muscle / EMI hash
    "notch_enabled": True,
    "notch_hz": 60.0,        # mains frequency (60 in N. America, 50 elsewhere)
}


# ── Config helpers ────────────────────────────────────────────────────────
def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except json.JSONDecodeError:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


# ── PM10 protocol helpers (reused from download.py / restore.py) ──────────
def _pkt(*payload: int) -> bytes:
    body = bytearray(RLEN)
    for i, b in enumerate(payload):
        body[i] = b & 0xFF
    return b"\x00" + bytes(body)


def _drain(d, wait_ms: int) -> list[bytes]:
    got = []
    end = time.time() + wait_ms / 1000
    while time.time() < end:
        data = d.read(RLEN, timeout_ms=100)
        if data:
            got.append(bytes(data))
            end = max(end, time.time() + wait_ms / 1000)
    return got


def _open_device():
    dev = hid.device()
    dev.open(VID, PID)
    dev.set_nonblocking(True)
    return dev


def _handshake(dev) -> None:
    dev.write(_pkt(0x81, 0x01)); _drain(dev, 300)
    dev.write(_pkt(0x82, 0x02)); _drain(dev, 300)
    dev.write(_pkt(0x90));       _drain(dev, 500)


def _download_record(dev, idx: int) -> bytes | None:
    lo, hi = idx & 0x7F, (idx >> 7) & 0x7F
    dev.write(_pkt(0xA0, lo, hi))
    packets = _drain(dev, 3000)
    return b"".join(packets) if packets else None


def _decode_samples(raw: bytes) -> np.ndarray:
    """Data bytes arrive 7-bit clean (bit 7 always 0); their real bit 7 is packed
    into header bytes MSB_START.. — bit k of byte MSB_START + n//7 belongs to
    data byte n, with k = n % 7."""
    samples = []
    for i in range(0, len(raw), RLEN):
        pkt = bytearray(raw[i:i + RLEN])
        if len(pkt) < RLEN:
            continue
        for n in range(SAMPLE_END - SAMPLE_START):
            if (pkt[MSB_START + n // 7] >> (n % 7)) & 1:
                pkt[SAMPLE_START + n] |= 0x80
        for j in range(SAMPLE_START, SAMPLE_END, 2):
            samples.append(struct.unpack(">H", pkt[j:j + 2])[0])
    return np.array(samples, dtype=np.int32)


def _samples_to_mv(samples: np.ndarray) -> np.ndarray:
    return (samples.astype(np.float64) - 0x4000) * V_PER_LSB


def _clean_signal(mv: np.ndarray, sr: int, cfg: dict) -> np.ndarray:
    """Apply zero-phase Butterworth bandpass + optional 60 Hz notch to mV samples."""
    if not cfg.get("filter_enabled", True) or mv.size < 20:
        return mv
    hp = float(cfg.get("filter_hp_hz", 0.5))
    lp = float(cfg.get("filter_lp_hz", 40.0))
    nyq = sr / 2.0
    # Clamp cutoffs into a sane range.
    lp = min(lp, nyq * 0.95)
    hp = max(hp, 0.01)
    if hp >= lp:
        return mv
    sos_bp = butter(3, [hp / nyq, lp / nyq], btype="band", output="sos")
    out = sosfiltfilt(sos_bp, mv)
    if cfg.get("notch_enabled", True):
        notch_hz = float(cfg.get("notch_hz", 60.0))
        if notch_hz < nyq:
            b, a = iirnotch(notch_hz, Q=30.0, fs=sr)
            sos_notch = tf2sos(b, a)
            out = sosfiltfilt(sos_notch, out)
    return out


def _detect_r_peaks(mv: np.ndarray, sr: int, threshold: float = 0.4) -> list[int]:
    peak_min = int(0.3 * sr)
    peaks = []
    i = 0
    while i < len(mv):
        if mv[i] > threshold:
            j = i + int(np.argmax(mv[i:min(i + peak_min, len(mv))]))
            peaks.append(j)
            i = j + peak_min
        else:
            i += 1
    return peaks


def _restore_packet(language: int = 1, filter_on: int = 1,
                     hr_low: int = 60, hr_high: int = 100) -> bytes:
    body = bytearray(RLEN)
    body[0], body[1] = 0x83, 0x01
    body[2] = language & 0x7F
    body[3] = filter_on & 0x7F
    body[4] = 1              # f5
    body[5] = 1              # f6
    body[6] = 1              # f7
    body[7] = 1              # f8 (computed default)
    body[8] = 0              # f9
    body[9] = 0              # f10
    body[10] = (hr_low >> 7) & 0x7F
    body[11] = hr_low & 0x7F
    body[12] = (hr_high >> 7) & 0x7F
    body[13] = hr_high & 0x7F
    return b"\x00" + bytes(body)


# ── Storage helpers ───────────────────────────────────────────────────────
def _record_dir(record_id: str) -> Path:
    return RECORDS / record_id


def _list_records() -> list[dict]:
    out = []
    for path in sorted(RECORDS.iterdir()):
        meta_file = path / "meta.json"
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text())
                meta["id"] = path.name
                meta["has_analysis"] = (path / "analysis.json").exists()
                meta["has_pdf"] = (path / "report.pdf").exists()
                out.append(meta)
            except json.JSONDecodeError:
                pass
    out.sort(key=lambda r: r.get("captured_at", ""), reverse=True)
    return out


def _derive_record(d: Path, captured_at: str, device_index: int) -> dict:
    """Decode raw.bin in record dir `d`, write meta.json + plot.png, return meta."""
    cfg = load_config()
    samples = _decode_samples((d / "raw.bin").read_bytes())
    mv_raw = _samples_to_mv(samples)
    mv_clean = _clean_signal(mv_raw, SR_HZ, cfg)
    peaks = _detect_r_peaks(mv_clean, SR_HZ)
    bpm = 60 * len(peaks) / (len(samples) / SR_HZ) if samples.size else 0
    meta = {
        "captured_at": captured_at,
        "device_index": device_index,
        "sample_count": int(samples.size),
        "sample_rate_hz": SR_HZ,
        "duration_s": round(samples.size / SR_HZ, 2),
        "r_peaks": len(peaks),
        "heart_rate_bpm": round(bpm, 1),
        "filter": _filter_meta(cfg),
        "decoder_version": DECODER_VERSION,
    }
    (d / "meta.json").write_text(json.dumps(meta, indent=2))
    _render_plot(mv_clean, peaks, meta, d / "plot.png")
    return meta


def _save_record(raw: bytes, device_index: int) -> dict:
    now = datetime.datetime.now()
    record_id = now.strftime("%Y%m%d_%H%M%S") + f"_r{device_index}"
    d = _record_dir(record_id)
    d.mkdir(exist_ok=True)
    (d / "raw.bin").write_bytes(raw)
    meta = _derive_record(d, now.isoformat(timespec="seconds"), device_index)
    return {**meta, "id": record_id, "has_analysis": False, "has_pdf": False}


def _refresh_stale_records() -> None:
    """Re-derive meta.json / plot.png for records decoded by an older decoder.
    Cached clinical tiles are dropped so they re-render; AI analyses and PDFs
    are left alone but flagged, since they were computed from the old decode."""
    for d in RECORDS.iterdir():
        meta_file = d / "meta.json"
        if not (d / "raw.bin").exists() or not meta_file.exists():
            continue
        try:
            old = json.loads(meta_file.read_text())
        except json.JSONDecodeError:
            continue
        if old.get("decoder_version", 1) >= DECODER_VERSION:
            continue
        for f in [*d.glob("zoom_*.png"), d / "strip.png"]:
            f.unlink(missing_ok=True)
        meta = _derive_record(d, old["captured_at"], old["device_index"])
        if (d / "analysis.json").exists() or (d / "report.pdf").exists():
            meta["analysis_decoder_version"] = old.get("decoder_version", 1)
            meta_file.write_text(json.dumps(meta, indent=2))
        print(f"  re-decoded {d.name}: {old.get('sample_count')} → {meta['sample_count']} samples")


def _filter_meta(cfg: dict) -> dict:
    if not cfg.get("filter_enabled", True):
        return {"applied": False}
    m = {
        "applied": True,
        "bandpass_hz": [cfg.get("filter_hp_hz", 0.5), cfg.get("filter_lp_hz", 40.0)],
    }
    if cfg.get("notch_enabled", True):
        m["notch_hz"] = cfg.get("notch_hz", 60.0)
    return m


CLINICAL_GRID_MAJOR = "#D45B5B"      # 5-mm cell borders
CLINICAL_GRID_MINOR = "#F5BABA"      # 1-mm cell borders


def _clinical_axes(ax, x_start_s: float, x_end_s: float,
                    mv_range: tuple[float, float],
                    mm_per_s: float, mm_per_mv: float,
                    x_label_every_s: float = 1.0,
                    y_label_every_mv: float = 1.0) -> None:
    """Style an axes as ECG paper: red 1 mm / 5 mm grid, labelled sparingly."""
    lg_x = 5.0 / mm_per_s   # major cell width in seconds
    sm_x = 1.0 / mm_per_s   # minor cell width in seconds
    lg_y = 5.0 / mm_per_mv  # major cell height in mV
    sm_y = 1.0 / mm_per_mv  # minor cell height in mV

    ax.xaxis.set_major_locator(MultipleLocator(lg_x))
    ax.xaxis.set_minor_locator(MultipleLocator(sm_x))
    ax.yaxis.set_major_locator(MultipleLocator(lg_y))
    ax.yaxis.set_minor_locator(MultipleLocator(sm_y))

    # Only label majors that hit the requested cadence, with enough decimals to disambiguate.
    x_decimals = 0 if x_label_every_s >= 1 else (1 if x_label_every_s >= 0.1 else 2)
    y_decimals = 0 if y_label_every_mv >= 1 else (1 if y_label_every_mv >= 0.1 else 2)

    def _x_fmt(x, _pos):
        n = x / x_label_every_s
        return f"{x:.{x_decimals}f}s" if abs(n - round(n)) < 0.02 else ""

    def _y_fmt(y, _pos):
        n = y / y_label_every_mv
        return f"{y:.{y_decimals}f}" if abs(n - round(n)) < 0.02 else ""

    ax.xaxis.set_major_formatter(FuncFormatter(_x_fmt))
    ax.yaxis.set_major_formatter(FuncFormatter(_y_fmt))

    ax.grid(True, which="major", color=CLINICAL_GRID_MAJOR, linewidth=0.5, alpha=0.9)
    ax.grid(True, which="minor", color=CLINICAL_GRID_MINOR, linewidth=0.3, alpha=0.55)
    ax.tick_params(axis="both", which="both", length=0, labelsize=6, colors="#555")
    ax.set_xlim(x_start_s, x_end_s)
    ax.set_ylim(*mv_range)
    for spine in ax.spines.values():
        spine.set_edgecolor("#c9c9c9")
        spine.set_linewidth(0.6)


def _mv_range_for(mv: np.ndarray, min_half: float = 1.5, pad_mv: float = 0.3,
                   snap_step: float = 0.5) -> tuple[float, float]:
    """Symmetric mV range that fits the whole trace with headroom, snapped to grid."""
    peak = float(np.max(np.abs(mv))) if mv.size else min_half
    half = max(min_half, math.ceil((peak + pad_mv) / snap_step) * snap_step)
    return (-half, half)


def _render_clinical_strip(mv: np.ndarray, sr: int, out: Path,
                            seconds_per_row: float = 10.0) -> None:
    """Render a full recording as a standard clinical strip chart.

    25 mm/s × 10 mm/mV, red medical grid on white paper, black trace, one row
    per `seconds_per_row` window. Rendered at 300 DPI. Figure is sized in inches
    so 1 inch on the page = 25.4 mm on the "paper" (no set_aspect games).
    """
    MM_PER_S = 25.0
    MM_PER_MV = 10.0
    DPI = 300
    IN_PER_MM = 1.0 / 25.4
    total_s = len(mv) / sr
    rows = max(1, math.ceil(total_s / seconds_per_row))
    mv_range = _mv_range_for(mv)                       # dynamic ±N mV
    row_w_mm = seconds_per_row * MM_PER_S              # e.g. 250 mm for 10 s
    row_h_mm = (mv_range[1] - mv_range[0]) * MM_PER_MV # e.g. 40 mm for ±2 mV
    row_w_in = row_w_mm * IN_PER_MM
    row_h_in = row_h_mm * IN_PER_MM

    # Manual figure layout so cells stay perfectly square.
    left_in, right_in, top_in, bot_in = 0.45, 0.20, 0.40, 0.20
    gap_in = 0.20
    fig_w_in = left_in + row_w_in + right_in
    fig_h_in = top_in + rows * row_h_in + (rows - 1) * gap_in + bot_in

    fig = plt.figure(figsize=(fig_w_in, fig_h_in), dpi=DPI, facecolor="white")
    for r in range(rows):
        row_start_s = r * seconds_per_row
        row_end_s = row_start_s + seconds_per_row
        # Compute axes position in figure coordinates (fractions of fig size).
        y_top = 1.0 - (top_in + r * (row_h_in + gap_in)) / fig_h_in
        y_bot = y_top - row_h_in / fig_h_in
        x_lo = left_in / fig_w_in
        x_hi = 1.0 - right_in / fig_w_in
        ax = fig.add_axes([x_lo, y_bot, x_hi - x_lo, y_top - y_bot])
        _clinical_axes(ax, row_start_s, row_end_s, mv_range, MM_PER_S, MM_PER_MV)

        i0 = int(row_start_s * sr)
        i1 = min(int(row_end_s * sr), len(mv))
        if i1 > i0:
            t = np.arange(i0, i1) / sr
            ax.plot(t, mv[i0:i1], color="black", linewidth=0.9, zorder=3)

        ax.text(row_start_s + 0.07, mv_range[1] - (mv_range[1] * 0.10),
                f"{int(row_start_s)}–{min(int(row_end_s), math.ceil(total_s))} s",
                fontsize=7, color="#444",
                bbox=dict(facecolor="white", alpha=0.85, edgecolor="none", pad=1.5),
                zorder=4, va="top")

    fig.text(0.5, 1.0 - 0.14 / fig_h_in,
             f"EMAY PM10 · {total_s:.1f} s · 25 mm/s, 10 mm/mV clinical strip",
             ha="center", fontsize=9, color="#222")
    fig.savefig(out, dpi=DPI, facecolor="white")
    plt.close(fig)


def _pick_regions_of_interest(peaks: list[int], sr: int, total_s: float,
                               window_s: float = 2.5) -> list[dict]:
    """Return a list of {start_s, end_s, label} zoom-tile regions.

    Picks the shortest R-R interval, the longest R-R interval, and one median-R-R
    interval — each centred in a `window_s`-second window. Deduplicates overlaps.
    """
    if len(peaks) < 2:
        return []
    rr = [(peaks[i + 1] - peaks[i]) / sr for i in range(len(peaks) - 1)]
    if not rr:
        return []
    short_i = min(range(len(rr)), key=lambda i: rr[i])
    long_i = max(range(len(rr)), key=lambda i: rr[i])
    med = statistics.median(rr)
    med_i = min(range(len(rr)), key=lambda i: abs(rr[i] - med))

    def _region(i: int, label: str) -> dict:
        centre = (peaks[i] + peaks[i + 1]) / 2 / sr
        start = max(0.0, centre - window_s / 2)
        end = min(total_s, start + window_s)
        return {"start_s": start, "end_s": end,
                "label": label, "rr_ms": int(round(rr[i] * 1000))}

    candidates = [
        _region(short_i, f"shortest R–R ({int(round(rr[short_i]*1000))} ms)"),
        _region(long_i,  f"longest R–R ({int(round(rr[long_i]*1000))} ms)"),
        _region(med_i,   f"median R–R ({int(round(rr[med_i]*1000))} ms)"),
    ]
    # Deduplicate overlapping windows (same beat picked twice).
    kept: list[dict] = []
    for c in candidates:
        if not any(abs(k["start_s"] - c["start_s"]) < window_s * 0.5 for k in kept):
            kept.append(c)
    return kept


def _render_zoom_tile(mv: np.ndarray, sr: int, region: dict, out: Path) -> None:
    """Render one high-magnification zoom tile in clinical format (50 mm/s × 20 mm/mV)."""
    MM_PER_S = 50.0
    MM_PER_MV = 20.0
    DPI = 300
    IN_PER_MM = 1.0 / 25.4
    start_s, end_s = region["start_s"], region["end_s"]
    i0 = int(start_s * sr)
    i1 = min(int(end_s * sr), len(mv))
    segment = mv[i0:i1]
    if segment.size == 0:
        return
    mv_range = _mv_range_for(segment, min_half=1.0, pad_mv=0.2, snap_step=0.25)

    plot_w_mm = (end_s - start_s) * MM_PER_S
    plot_h_mm = (mv_range[1] - mv_range[0]) * MM_PER_MV
    plot_w_in = plot_w_mm * IN_PER_MM
    plot_h_in = plot_h_mm * IN_PER_MM

    left_in, right_in, top_in, bot_in = 0.45, 0.20, 0.55, 0.30
    fig_w_in = left_in + plot_w_in + right_in
    fig_h_in = top_in + plot_h_in + bot_in

    fig = plt.figure(figsize=(fig_w_in, fig_h_in), dpi=DPI, facecolor="white")
    ax = fig.add_axes([
        left_in / fig_w_in,
        bot_in / fig_h_in,
        plot_w_in / fig_w_in,
        plot_h_in / fig_h_in,
    ])
    _clinical_axes(ax, start_s, end_s, mv_range, MM_PER_S, MM_PER_MV,
                    x_label_every_s=0.2, y_label_every_mv=0.5)
    t = np.arange(i0, i1) / sr
    ax.plot(t, segment, color="black", linewidth=1.1, zorder=3)
    fig.text(0.5, 1.0 - 0.20 / fig_h_in,
             f"Zoom: {region['label']} @ {start_s:.2f}–{end_s:.2f} s  ·  50 mm/s · 20 mm/mV",
             ha="center", fontsize=8, color="#222")
    fig.savefig(out, dpi=DPI, facecolor="white")
    plt.close(fig)


def _regenerate_clinical_images(record_id: str) -> tuple[Path, list[tuple[Path, dict]]]:
    """(Re)build strip.png + zoom_N.png for a record. Returns (strip_path, [(zoom_path, region), ...])."""
    d = _record_dir(record_id)
    raw = (d / "raw.bin").read_bytes()
    cfg = load_config()
    samples = _decode_samples(raw)
    mv_raw = _samples_to_mv(samples)
    mv = _clean_signal(mv_raw, SR_HZ, cfg)
    peaks = _detect_r_peaks(mv, SR_HZ)
    strip_path = d / "strip.png"
    _render_clinical_strip(mv, SR_HZ, strip_path)
    # Clear any old zoom tiles before writing the new set.
    for old in d.glob("zoom_*.png"):
        old.unlink()
    regions = _pick_regions_of_interest(peaks, SR_HZ, len(mv) / SR_HZ)
    zoom_paths: list[tuple[Path, dict]] = []
    for i, region in enumerate(regions):
        zp = d / f"zoom_{i}.png"
        _render_zoom_tile(mv, SR_HZ, region, zp)
        zoom_paths.append((zp, region))
    return strip_path, zoom_paths


def _decode_and_clean(record_id: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[int], dict]:
    """Load raw.bin → (raw_u16, mv_raw, mv_clean, r_peaks_on_clean, filter_meta)."""
    d = _record_dir(record_id)
    raw = (d / "raw.bin").read_bytes()
    cfg = load_config()
    samples = _decode_samples(raw)
    mv_raw = _samples_to_mv(samples)
    mv_clean = _clean_signal(mv_raw, SR_HZ, cfg)
    peaks = _detect_r_peaks(mv_clean, SR_HZ)
    return samples, mv_raw, mv_clean, peaks, _filter_meta(cfg)


def _render_plot(mv: np.ndarray, peaks: list[int], meta: dict, out: Path) -> None:
    sr = meta["sample_rate_hz"]
    t = np.arange(len(mv)) / sr
    fig, axes = plt.subplots(2, 1, figsize=(12, 7))
    axes[0].plot(t, mv, linewidth=0.7, color="#0a4")
    axes[0].scatter(np.array(peaks) / sr, mv[peaks] if len(peaks) else [], s=15, color="red", zorder=3)
    axes[0].set_title(f"ECG — {meta['duration_s']} s @ {sr} Hz — {meta['heart_rate_bpm']} bpm ({meta['r_peaks']} R-peaks)")
    axes[0].set_xlabel("time (s)"); axes[0].set_ylabel("mV")
    axes[0].grid(True, alpha=0.3); axes[0].axhline(0, color="k", linewidth=0.4)
    # Zoom on middle 5 s
    if len(mv) > 5 * sr:
        z0 = max(0, len(mv) // 2 - int(2.5 * sr))
        z1 = min(len(mv), z0 + 5 * sr)
        axes[1].plot(t[z0:z1], mv[z0:z1], linewidth=1.0, color="tab:red")
        axes[1].set_title(f"Zoom: {t[z0]:.1f}–{t[z1-1]:.1f} s")
        axes[1].set_xlabel("time (s)"); axes[1].set_ylabel("mV")
        axes[1].grid(True, alpha=0.3); axes[1].axhline(0, color="k", linewidth=0.4)
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    plt.close(fig)


# ── Gemini analysis ───────────────────────────────────────────────────────
ANALYSIS_PROMPT = """You are an expert biomedical data analyst and MLOps engineer specializing in digital signal processing (DSP) for health vitals. I have attached a CSV file containing timeseries data from a 2-lead consumer ECG session (EMAY PM10 recording).

Please parse the file (handling the commented '#' header lines automatically to extract sampling rate and metadata) and perform a rigorous automated analysis to produce a final report focusing on abnormalities and borderline features.

Run a Python script to process the data and include the following explicit sections in your final response:

### 1. Recording Metadata & Signal Integrity
- Report the sample rate (Hz), duration, and total samples.
- Check the 'raw_u16' column minimum and maximum values to verify if there is any sensor saturation, signal clipping, or severe baseline wander.

### 2. Quantitative Rhythm Analysis
- Implement a peak-detection algorithm (e.g., using scipy.signal.find_peaks with an appropriate height and distance threshold based on the sample rate) to isolate the R-peaks.
- Calculate and display a summary table with: Total Beats, Mean Heart Rate (BPM), Min/Max Heart Rate, Median RR-interval, and HRV metrics (SDNN and RMSSD in milliseconds).

### 3. Comprehensive Anomaly & Borderline Detection
Run an algorithmic check for the following specific conditions and explicitly state the findings for each:
- Dropped Beats / Pauses: Check if any individual RR-interval exceeds 1.3x the median RR-interval.
- Premature Contractions (PVCs/PACs): Check if any individual RR-interval is shorter than 0.7x the median RR-interval.
- Erratic Rhythm (Borderline Arrhythmia): Report the maximum consecutive beat-by-beat change (Max delta RR). Highlight if it flags sudden unexplained spikes.
- Morphological Consistency (Ensemble Averaging): Extract a window around every detected R-peak (e.g., -0.2s to +0.4s), overlay all waveforms, and evaluate their structural variance. Report if any individual beats deviate significantly from the average morphology.
- ST-Segment & T-Wave Assessment: Check the post-QRS segment for any borderline ST-elevation or ST-depression, and ensure T-waves are appropriately upright.

### 4. Required Visualizations
Generate and display the following two inline plots:
1. A close-up plot of the first 5 seconds showing the filtered voltage ('mv_filtered') to visually inspect the QRS, P, and T wave structures.
2. An ensemble overlay plot of all extracted heartbeats aligned at the R-peak (using transparency) with the calculated Ensemble Average boldly highlighted, allowing for instant visual outlier detection.

Conclude with a clear, definitive "Verdict" stating whether the session represents a normal sinus rhythm or if there are any technical artifacts, borderline trends, or physiological irregularities that warrant attention.
"""


def _run_gemini_analysis(record_id: str) -> dict:
    """Send the recording to Gemini for interpretation.

    We pass ONLY the CSV — the exact same file the user can download from the UI.
    Gemini parses the `#` comment header for sample rate + metadata as instructed
    by the prompt, and runs its own Python analysis in the code-execution sandbox
    to compute HRV metrics and generate the two required plots.

    The clinical strip / zoom PNGs are NOT sent — they're only rendered for the
    UI's Clinical view (see the /strip.png and /zoom_N.png endpoints).
    """
    cfg = load_config()
    if not cfg.get("gemini_api_key"):
        raise RuntimeError("Gemini API key not configured — set it in Settings.")
    if genai is None:
        raise RuntimeError("google-generativeai package not installed on server.")

    d = _record_dir(record_id)
    meta = json.loads((d / "meta.json").read_text())

    # `tools="code_execution"` gives Gemini a Python sandbox — required for it to
    # actually run scipy.signal.find_peaks, compute HRV, and emit the plots.
    genai.configure(api_key=cfg["gemini_api_key"])
    model = genai.GenerativeModel(cfg["gemini_model"], tools="code_execution")

    csv_text, _ = _build_csv(record_id)
    csv_bytes = csv_text.encode("utf-8")

    contents = [
        ANALYSIS_PROMPT,
        {"mime_type": "text/csv", "data": csv_bytes},
    ]

    response = model.generate_content(contents)

    # Wipe any previous AI plots before saving the new set — the model may
    # produce a different number of plots each run.
    for old in d.glob("ai_plot_*.png"):
        old.unlink()

    text_parts: list[str] = []
    code_blocks: list[dict] = []
    plot_files: list[str] = []
    plot_index = 0
    for part in response.candidates[0].content.parts:
        t = getattr(part, "text", None)
        if t:
            text_parts.append(t)
            continue
        ec = getattr(part, "executable_code", None)
        if ec is not None and getattr(ec, "code", None):
            code_blocks.append({"stage": "code", "language": str(getattr(ec, "language", "")),
                                "code": ec.code, "output": None})
            continue
        cer = getattr(part, "code_execution_result", None)
        if cer is not None and getattr(cer, "output", None):
            if code_blocks and code_blocks[-1]["output"] is None:
                code_blocks[-1]["output"] = cer.output
                code_blocks[-1]["outcome"] = str(getattr(cer, "outcome", ""))
            else:
                code_blocks.append({"stage": "output", "code": None,
                                    "output": cer.output,
                                    "outcome": str(getattr(cer, "outcome", ""))})
            continue
        inline = getattr(part, "inline_data", None)
        if inline is not None and getattr(inline, "data", None):
            mime = getattr(inline, "mime_type", "") or ""
            if mime.startswith("image/"):
                ext = "png" if "png" in mime else ("jpg" if "jpe" in mime else "bin")
                fname = f"ai_plot_{plot_index}.{ext}"
                (d / fname).write_bytes(inline.data)
                plot_files.append(fname)
                plot_index += 1
            continue

    result = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "model": cfg["gemini_model"],
        "csv_bytes_sent": len(csv_bytes),
        "csv_rows_sent": meta["sample_count"],
        "code_execution_enabled": True,
        "attachment_manifest": [
            f"CSV — {meta['sample_count']} rows at {meta['sample_rate_hz']} Hz "
            f"over {meta['duration_s']:.2f} s. "
            f"Columns: t_s, sample_index, raw_u16, mv_raw, mv_filtered."
        ],
        "text": "\n\n".join(t.strip() for t in text_parts if t.strip()),
        "code_blocks": code_blocks,
        "plots": plot_files,
    }
    (d / "analysis.json").write_text(json.dumps(result, indent=2))
    # A fresh analysis is on the current decode — drop the stale-analysis flag.
    if meta.pop("analysis_decoder_version", None) is not None:
        (d / "meta.json").write_text(json.dumps(meta, indent=2))
    return result


# ── PDF export ────────────────────────────────────────────────────────────
def _generate_pdf(record_id: str) -> Path:
    d = _record_dir(record_id)
    meta = json.loads((d / "meta.json").read_text())
    analysis_file = d / "analysis.json"
    analysis = json.loads(analysis_file.read_text()) if analysis_file.exists() else None

    out = d / "report.pdf"
    doc = SimpleDocTemplate(str(out), pagesize=letter,
                            leftMargin=0.6*inch, rightMargin=0.6*inch,
                            topMargin=0.6*inch, bottomMargin=0.6*inch)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("title", parent=styles["Title"], fontSize=18, spaceAfter=6)
    heading_style = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=13, spaceAfter=4)
    body_style = ParagraphStyle("body", parent=styles["BodyText"], fontSize=10, leading=13)

    story = []
    story.append(Paragraph("EMAY PM10 — ECG Report", title_style))
    story.append(Paragraph(
        f"Captured {meta['captured_at']} · {meta['duration_s']} s @ {meta['sample_rate_hz']} Hz · "
        f"{meta['heart_rate_bpm']} bpm ({meta['r_peaks']} R-peaks)",
        body_style,
    ))
    story.append(Spacer(1, 8))
    story.append(RLImage(str(d / "plot.png"), width=6.9*inch, height=4.0*inch))
    story.append(Spacer(1, 12))

    if analysis:
        story.append(Paragraph(f"Automated interpretation ({analysis['model']}, {analysis['generated_at']})", heading_style))
        for line in (analysis.get("text") or "").split("\n"):
            line = line.strip()
            if not line:
                story.append(Spacer(1, 4))
            else:
                safe = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                story.append(Paragraph(safe, body_style))
        for i, fname in enumerate(analysis.get("plots") or []):
            plot_path = d / fname
            if not plot_path.exists():
                continue
            story.append(Spacer(1, 8))
            story.append(Paragraph(f"AI-generated plot {i + 1}", body_style))
            story.append(RLImage(str(plot_path), width=6.9*inch, height=4.0*inch, kind="proportional"))
    else:
        story.append(Paragraph("No automated analysis available for this record.", body_style))

    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "This report was generated by an unregulated, at-home tool from a single-lead consumer-grade ECG device. "
        "It is not a medical diagnosis and must not be relied upon for clinical decisions.",
        ParagraphStyle("dis", parent=body_style, fontSize=8, textColor="#666"),
    ))

    doc.build(story)
    return out


# ── Flask app ─────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/records", methods=["GET"])
def api_records():
    return jsonify(_list_records())


@app.route("/api/records/<record_id>", methods=["GET"])
def api_record(record_id: str):
    d = _record_dir(record_id)
    if not d.exists():
        return jsonify({"error": "not found"}), 404
    meta = json.loads((d / "meta.json").read_text())
    analysis_file = d / "analysis.json"
    analysis = json.loads(analysis_file.read_text()) if analysis_file.exists() else None
    return jsonify({"meta": meta, "analysis": analysis, "id": record_id})


@app.route("/api/records/<record_id>", methods=["DELETE"])
def api_record_delete(record_id: str):
    d = _record_dir(record_id)
    if d.exists():
        for f in d.iterdir():
            f.unlink()
        d.rmdir()
    return jsonify({"ok": True})


@app.route("/api/records/<record_id>/plot.png")
def api_record_plot(record_id: str):
    d = _record_dir(record_id)
    return send_file(d / "plot.png", mimetype="image/png")


@app.route("/api/records/<record_id>/strip.png")
def api_record_strip(record_id: str):
    """Standard clinical strip chart. Always re-rendered from the current filter
    settings — cheap enough (~1 s) and guarantees the Clinical view reflects
    whatever the user just changed in Settings."""
    d = _record_dir(record_id)
    _regenerate_clinical_images(record_id)
    return send_file(d / "strip.png", mimetype="image/png")


@app.route("/api/records/<record_id>/zoom_<int:i>.png")
def api_record_zoom(record_id: str, i: int):
    """One clinical zoom tile (shortest / longest / median R-R by index)."""
    d = _record_dir(record_id)
    if not (d / f"zoom_{i}.png").exists():
        _regenerate_clinical_images(record_id)
    zoom = d / f"zoom_{i}.png"
    if not zoom.exists():
        return jsonify({"error": "not found"}), 404
    return send_file(zoom, mimetype="image/png")


@app.route("/api/records/<record_id>/ai_plot_<int:i>.<ext>")
def api_record_ai_plot(record_id: str, i: int, ext: str):
    d = _record_dir(record_id)
    p = d / f"ai_plot_{i}.{ext}"
    if not p.exists():
        return jsonify({"error": "not found"}), 404
    mime = "image/jpeg" if ext == "jpg" else f"image/{ext}"
    return send_file(p, mimetype=mime)


@app.route("/api/records/<record_id>/samples")
def api_record_samples(record_id: str):
    """Return the decoded ECG trace as JSON for interactive client-side plotting."""
    d = _record_dir(record_id)
    if not d.exists():
        return jsonify({"error": "not found"}), 404
    _, mv_raw, mv_clean, peaks, filt = _decode_and_clean(record_id)
    meta = json.loads((d / "meta.json").read_text())
    return jsonify({
        "sample_rate_hz": meta["sample_rate_hz"],
        "duration_s": meta["duration_s"],
        "mv": [round(float(x), 4) for x in mv_clean],
        "mv_raw": [round(float(x), 4) for x in mv_raw],
        "peaks": peaks,
        "filter": filt,
    })


def _build_csv(record_id: str) -> tuple[str, dict]:
    """Return (csv_text, filter_meta) for a record. Used by both download and Gemini."""
    samples, mv_raw, mv_clean, _peaks, filt = _decode_and_clean(record_id)
    sr = SR_HZ
    filt_desc = "off" if not filt.get("applied") else (
        f"bp {filt['bandpass_hz'][0]}-{filt['bandpass_hz'][1]}Hz"
        + (f"+notch {filt.get('notch_hz')}Hz" if "notch_hz" in filt else "")
    )
    header = (
        f"# EMAY PM10 recording {record_id}\n"
        f"# sample_rate_hz={sr}, samples={len(samples)}, duration_s={len(samples)/sr:.3f}\n"
        f"# raw_u16: 12-bit offset-binary centered at 0x4000; mv = (raw - 16384) * 5/2048\n"
        f"# filter applied to mv_filtered: {filt_desc}\n"
        f"t_s,sample_index,raw_u16,mv_raw,mv_filtered\n"
    )
    lines = [header]
    for i in range(len(samples)):
        lines.append(f"{i/sr:.5f},{i},{int(samples[i])},{mv_raw[i]:.4f},{mv_clean[i]:.4f}\n")
    return "".join(lines), filt


@app.route("/api/records/<record_id>/timeseries.csv")
def api_record_timeseries(record_id: str):
    """CSV export: t_s, sample_index, raw_u16, mv_raw, mv_filtered."""
    d = _record_dir(record_id)
    if not d.exists():
        return jsonify({"error": "not found"}), 404
    body, _ = _build_csv(record_id)
    return Response(
        body,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="pm10_{record_id}.csv"'},
    )


@app.route("/api/records/<record_id>/analyze", methods=["POST"])
def api_record_analyze(record_id: str):
    try:
        result = _run_gemini_analysis(record_id)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/records/<record_id>/pdf")
def api_record_pdf(record_id: str):
    try:
        pdf_path = _generate_pdf(record_id)
        return send_file(pdf_path, mimetype="application/pdf",
                         as_attachment=True, download_name=f"pm10_{record_id}.pdf")
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/sync", methods=["POST"])
def api_sync():
    """Attempt to download records 1..10 from the connected PM10."""
    try:
        dev = _open_device()
    except Exception as exc:
        return jsonify({"error": f"Cannot open device — is the PM10 plugged in and powered on? ({exc})"}), 500
    saved = []
    errors = []
    try:
        _handshake(dev)
        for idx in range(1, 11):
            try:
                raw = _download_record(dev, idx)
                if raw is None:
                    continue
                saved.append(_save_record(raw, idx))
            except Exception as exc:
                errors.append({"index": idx, "error": str(exc)})
    finally:
        dev.close()
    return jsonify({"saved": saved, "errors": errors})


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    cfg = load_config()
    # Never leak the API key back to the browser once set — just say if present.
    return jsonify({
        "gemini_api_key_set": bool(cfg.get("gemini_api_key")),
        "gemini_model": cfg.get("gemini_model", DEFAULT_CONFIG["gemini_model"]),
        "hr_low_bpm": cfg.get("hr_low_bpm", 60),
        "hr_high_bpm": cfg.get("hr_high_bpm", 100),
        "filter_enabled": cfg.get("filter_enabled", True),
        "filter_hp_hz": cfg.get("filter_hp_hz", 0.5),
        "filter_lp_hz": cfg.get("filter_lp_hz", 40.0),
        "notch_enabled": cfg.get("notch_enabled", True),
        "notch_hz": cfg.get("notch_hz", 60.0),
    })


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    incoming = request.get_json(force=True) or {}
    cfg = load_config()
    if "gemini_api_key" in incoming and incoming["gemini_api_key"] != "":
        cfg["gemini_api_key"] = incoming["gemini_api_key"]
    if "gemini_model" in incoming and incoming["gemini_model"]:
        cfg["gemini_model"] = incoming["gemini_model"]
    if "hr_low_bpm" in incoming:
        cfg["hr_low_bpm"] = int(incoming["hr_low_bpm"])
    if "hr_high_bpm" in incoming:
        cfg["hr_high_bpm"] = int(incoming["hr_high_bpm"])
    for k in ("filter_enabled", "notch_enabled"):
        if k in incoming:
            cfg[k] = bool(incoming[k])
    for k in ("filter_hp_hz", "filter_lp_hz", "notch_hz"):
        if k in incoming:
            cfg[k] = float(incoming[k])
    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/device/restore", methods=["POST"])
def api_restore():
    """Send the 0x83 0x01 restore packet — language=English, filter on, HR limits from config."""
    cfg = load_config()
    try:
        dev = _open_device()
    except Exception as exc:
        return jsonify({"error": f"Cannot open device: {exc}"}), 500
    try:
        _handshake(dev)
        packet = _restore_packet(
            language=1, filter_on=1,
            hr_low=cfg.get("hr_low_bpm", 60),
            hr_high=cfg.get("hr_high_bpm", 100),
        )
        dev.write(packet)
        response = _drain(dev, 1500)
        return jsonify({
            "ok": True,
            "sent_hex": packet.hex(),
            "response_hex": [r.hex() for r in response],
        })
    finally:
        dev.close()


@app.route("/api/device/status")
def api_device_status():
    """Quick check: is the device visible on USB?"""
    for d in hid.enumerate():
        if d.get("vendor_id") == VID and d.get("product_id") == PID:
            return jsonify({"connected": True, "product": d.get("product_string")})
    return jsonify({"connected": False})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5555))
    _refresh_stale_records()
    print(f"\n  EMAY PM10 web app running at:  http://127.0.0.1:{port}\n")
    app.run(host="127.0.0.1", port=port, debug=False)
