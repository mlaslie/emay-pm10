# emay-pm10

A **reverse-engineered, open-source companion for the EMAY / Contec PM10** handheld ECG monitor.

Older PM10 units are no longer supported by EMAY's current apps — this project lets you plug the device into a Mac / Linux / Windows box over USB, pull your ECG recordings off it, view them at proper clinical resolution, get an AI-assisted interpretation from Google Gemini, and export a PDF report or CSV time series.

> **⚠ Disclaimer.** This is a hobby project. It is not a medical device, not FDA/CE cleared, and its output is not a diagnosis. The AI interpretation is a computer observation on a single-lead consumer trace. Always consult a qualified clinician for anything meaningful.

---

## Features

- **USB-HID download** — pulls stored recordings off a PM10 via the reverse-engineered `0xA0` download command. Works on the older non-BLE hardware revision that Contec's current PC software rejects as "not compatible".
- **Clean-signal rendering** — zero-phase Butterworth 0.5–40 Hz bandpass + configurable notch removes the 12-bit ADC quantization steps and baseline drift, revealing P- and T-waves that are buried in the raw output.
- **Two visualization modes** in the browser:
  - **Interactive** — Plotly chart with zoom, pan, range slider, fullscreen, R-peak overlays.
  - **Clinical** — standard 25 mm/s × 10 mm/mV strip chart on red medical grid, plus zoom tiles of the shortest / longest / median R-R interval.
- **AI analysis** — sends the full time-series CSV to Gemini with code-execution enabled, and Gemini's own scipy/matplotlib output (HRV metrics, ensemble beat overlay, verdict) is rendered back in the UI with proper markdown + LaTeX.
- **Export** — one-click PDF report (waveform + AI interpretation) or raw+filtered CSV time series.
- **Device recovery** — the app can send the reverse-engineered `0x83 0x01` "Set Device Parameters" packet to restore language / filter / HR limits if the PM10 got wedged (e.g., by earlier experimentation).

---

## Requirements

- Python 3.11 or newer
- macOS, Linux, or Windows
- An EMAY / Contec PM10 (this project targets the older USB-HID variant that identifies as VID `0x4444` / PID `0x5555`, product string `HID Transfer`, manufacturer `Nuvoton`)
- A USB **data** cable (many cables shipped with the device are charge-only)
- Optional: a Google Gemini API key for AI analysis. Free-tier keys work.

---

## Install

```bash
git clone https://github.com/mlaslie/emay-pm10.git
cd emay-pm10

python3 -m venv .venv
source .venv/bin/activate    # or .venv\Scripts\activate on Windows

pip install -r requirements.txt
```

**macOS note.** `hidapi` may need `brew install hidapi` first if pip's wheels don't work for your Python version.

**Linux note.** You may need to grant your user permission to access the HID device. Simplest: create `/etc/udev/rules.d/99-pm10.rules` with

```
SUBSYSTEM=="usb", ATTR{idVendor}=="4444", ATTR{idProduct}=="5555", MODE="0666"
```

then `sudo udevadm control --reload-rules && sudo udevadm trigger`.

---

## Run the web app

```bash
python webapp/app.py
```

Then open **http://127.0.0.1:5555** in your browser.

Set the Gemini API key from the **⚙ Settings** modal once. Everything else is stored in `webapp/data/`.

Change the port with `PORT=5556 python webapp/app.py`.

---

## Sync your first recording

1. Power on the PM10.
2. Plug it into the computer with a USB **data** cable.
3. In the browser, click **Sync from device**. The header pill should turn green ("PM10 connected"). Records appear in the list.
4. Click any record → view interactive chart, switch to Clinical view, hit **Analyze with AI**, or download **PDF** / **CSV**.

If the header pill stays red ("PM10 not detected"), verify the cable is data-capable — the fastest check is running the CLI enumerator: `python cli/hidscan.py` should print a "PM10 CANDIDATE" line.

---

## Command-line tools

For scripting, quick tests, or if you don't want the web app:

```bash
python cli/hidscan.py                       # verify the PM10 is visible over USB
python cli/scan.py 12                       # BLE scan (informational only)
python cli/download.py all                  # dump every record on the device
python cli/plot.py pm10_record_01.bin 250   # plot a downloaded record
python cli/restore.py snapshot              # read-only device state
python cli/restore.py apply                 # send known-good defaults packet
```

Each tool has a docstring at the top explaining what it does and why.

---

## Layout

```
emay-pm10/
├── webapp/                      # the Flask web app
│   ├── app.py                   # backend: device I/O, DSP, Gemini, HTTP surface
│   ├── static/
│   │   ├── index.html           # single-page UI
│   │   ├── style.css            # light+dark aware theme
│   │   └── app.js               # frontend logic (Plotly, markdown, KaTeX)
│   └── data/                    # runtime storage (gitignored)
│       ├── config.json          # API key, model, filter cutoffs, HR limits
│       └── records/<id>/        # per-recording: raw.bin, meta.json, plot.png,
│                                #                strip.png, zoom_*.png,
│                                #                ai_plot_*.png, analysis.json,
│                                #                report.pdf
├── cli/                         # standalone scripts
│   ├── download.py              # protocol-level record downloader
│   ├── plot.py                  # decode + plot a .bin file
│   ├── restore.py               # snapshot / restore device settings
│   ├── hidscan.py               # list HID devices, flag PM10
│   └── scan.py                  # BLE scan (older PM10s advertise but don't stream)
├── docs/                        # the reverse-engineering story
│   ├── PROTOCOL.md              # full 65-byte packet catalog from the Windows EXE
│   ├── SETTINGS_CMD.md          # byte-level breakdown of the SET_DEVICE_PARAMETERS packet
│   └── NOTES.md                 # session diary from the RE work
├── requirements.txt
├── .gitignore
├── LICENSE
└── README.md
```

---

## How the AI analysis works

Only the **CSV file** is sent to Gemini — 7,500 rows (30 s at 250 Hz) × 5 columns (`t_s`, `sample_index`, `raw_u16`, `mv_raw`, `mv_filtered`) with commented header lines carrying the sample rate and filter description.

The prompt (see `ANALYSIS_PROMPT` in `webapp/app.py`) asks Gemini to act as a biomedical data analyst, run its own `scipy.signal.find_peaks` peak detection, produce HRV metrics (SDNN, RMSSD), flag anomalies (dropped beats, PVCs / PACs, morphological outliers, ST-segment concerns), and generate two required plots inline: a first-5-seconds zoom and an ensemble-beat overlay.

The `tools="code_execution"` flag on the Gemini model gives it a Python sandbox to actually run this analysis rather than just describe it. The multi-part response is unpacked into:

- `text` — the human-readable interpretation (markdown + LaTeX, rendered by marked + KaTeX)
- `code_blocks` — every Python snippet Gemini executed, with its stdout (audit trail, shown collapsed in the UI)
- `plots` — the two required inline PNG plots, saved as `ai_plot_0.png` / `ai_plot_1.png` and rendered under the text

---

## Signal filtering

By default the app applies to every rendered chart, every CSV export's `mv_filtered` column, and the CSV that goes to Gemini:

- **Bandpass** — 3rd-order Butterworth 0.5–40 Hz, zero-phase (`scipy.signal.sosfiltfilt`)
- **Notch** — configurable Q=30 IIR notch (60 Hz for North America; change to 50 Hz elsewhere in Settings)

The `mv_raw` column is always the unfiltered device output. Turning the bandpass off in Settings makes `mv_filtered` equal to `mv_raw`.

---

## The device recovery button

Deep in Settings there's a **Send restore packet to device** button. It sends a single reverse-engineered 65-byte `0x83 0x01` HID output report that writes:

- Language = English
- Filter = ON
- Heart-rate alarm low / high thresholds from your Settings

Use it if the PM10's on-screen language changes, the filter turns off, or recording stops working. This was worked out live in `docs/SETTINGS_CMD.md`.

---

## Reverse-engineering docs

The protocol wasn't published — everything here was reverse-engineered from disassembly of the Windows PC software (`Portable ECG Monitor V1.4.7.exe`, an Inno-Setup Delphi application shipped by Contec) and confirmed against a physical device. Two documents in `docs/` cover the story:

- **`PROTOCOL.md`** — 29 send-site catalog. What each opcode does, byte layouts, checksum rules, the BLE-vs-USB code path split.
- **`SETTINGS_CMD.md`** — the byte-level analysis of the `0x83 0x01` Set-Device-Parameters packet that unlocked device recovery.
- **`NOTES.md`** — chronological session diary. Useful if you're extending the protocol map.

---

## Contributing / extending

The main app is one file (`webapp/app.py`, ~800 lines) organised by section (device I/O, DSP, storage, rendering, Gemini, PDF, HTTP). Adding a new opcode, filter, or export format is usually a single-function change.

If you're extending device support (PM10 BLE variant, PM20, other Contec devices), the read/write helpers `_pkt`, `_open_device`, `_handshake`, and `_download_record` are the natural extension points. See `docs/PROTOCOL.md` for the opcode catalog.

---

## License

MIT — see [LICENSE](../LICENSE).

Not affiliated with EMAY, Contec, or Google.
