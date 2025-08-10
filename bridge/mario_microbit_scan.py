#!/usr/bin/env python3
# mario_microbit_scan.py
import asyncio
from bleak import BleakScanner

LEGO_SERVICE = "00001623-1212-efde-1623-785feabcd123"
NUS_SERVICE = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"

async def main():
    print("Scanning for Bluetooth LE devices (5s)…")
    devices = await BleakScanner.discover(timeout=5.0)

    found_mario = False
    found_microbit = False

    for dev in devices:
        uuids = [u.lower() for u in dev.metadata.get("uuids", [])]
        name = dev.name or "Unknown"
        print(f"- {name} ({dev.address}) UUIDs: {uuids}")
        if LEGO_SERVICE.lower() in uuids or "mario" in (name.lower() if name else ""):
            found_mario = True
        if NUS_SERVICE.lower() in uuids or "micro:bit" in (name.lower() if name else ""):
            found_microbit = True

    print("\nSummary:")
    print("LEGO Mario found" if found_mario else "LEGO Mario not found")
    print("micro:bit found" if found_microbit else "micro:bit not found")

if __name__ == "__main__":
    asyncio.run(main())
