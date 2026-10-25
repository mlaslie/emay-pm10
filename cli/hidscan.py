"""List every HID device the OS can see, highlighting anything that could be
the PM10 (VID 0x4444, PID 0x5555 — extracted from the Windows PC software).

Usage: .venv/bin/python cli/hidscan.py
"""
import hid

TARGET_VID = 0x4444
TARGET_PID = 0x5555


def main() -> None:
    devs = hid.enumerate()
    print(f"{len(devs)} HID device(s) visible to hidapi:\n")
    hits = 0
    for d in devs:
        vid = d.get("vendor_id", 0)
        pid = d.get("product_id", 0)
        mfr = d.get("manufacturer_string") or ""
        prod = d.get("product_string") or ""
        path = d.get("path", b"").decode(errors="replace")
        marker = " ***PM10 CANDIDATE***" if (vid == TARGET_VID or pid == TARGET_PID or
                                              "PM10" in prod.upper() or "PM10" in mfr.upper() or
                                              "ECG" in prod.upper() or "CONTEC" in mfr.upper() or
                                              "EMAY" in mfr.upper()) else ""
        print(f"  VID=0x{vid:04X} PID=0x{pid:04X}  mfr={mfr!r} prod={prod!r}{marker}")
        print(f"    path={path}")
        if marker:
            hits += 1
    print(f"\n{hits} candidate(s).")


if __name__ == "__main__":
    main()
