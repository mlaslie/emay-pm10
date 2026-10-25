"""Scan for BLE devices and highlight anything that looks like the EMAY/Contec PM10.

Usage: .venv/bin/python cli/scan.py [seconds]
"""
import asyncio
import sys

from bleak import BleakScanner

INTERESTING = ("pm10", "emay", "contec", "ecg", "emg")


async def main(duration: float) -> None:
    print(f"Scanning for {duration:.0f}s ... (make sure the PM10 is powered on)")
    devices = await BleakScanner.discover(timeout=duration, return_adv=True)
    if not devices:
        print("No BLE advertisements seen at all — check Bluetooth permission for the terminal.")
        return
    hits = []
    for address, (device, adv) in sorted(devices.items()):
        name = device.name or adv.local_name or ""
        line = f"{address}  rssi={adv.rssi:>4}  name={name!r}  services={adv.service_uuids}"
        if adv.manufacturer_data:
            line += f"  mfr={ {k: v.hex() for k, v in adv.manufacturer_data.items()} }"
        if any(s in name.lower() for s in INTERESTING):
            hits.append(line)
            print("*** MATCH:", line)
        else:
            print("   ", line)
    print(f"\n{len(devices)} devices seen, {len(hits)} PM10-like matches.")


if __name__ == "__main__":
    asyncio.run(main(float(sys.argv[1]) if len(sys.argv) > 1 else 10.0))
