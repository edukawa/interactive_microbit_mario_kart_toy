#!/usr/bin/env python3
# LEGO Mario → micro:bit analog bridge
# Streams "<throttle>,<steer>:\n" string every 100ms (float each in [-1..1])
# Python 3.9, bleak 0.21.x, pyobjc 9.x

import asyncio
from typing import Optional
from bleak import BleakScanner, BleakClient, BLEDevice, AdvertisementData

# ---- LEGO Mario (IMU notifications on 0x1624) ----
LEGO_SERVICE = "00001623-1212-efde-1623-785feabcd123"
LEGO_CH      = "00001624-1212-efde-1623-785feabcd123"
SUBSCRIBE_IMU = bytearray([0x0A,0x00,0x41,0x00,0x00,0x05,0x00,0x00,0x00,0x01])
SUBSCRIBE_RGB = bytearray([0x0A,0x00,0x41,0x01,0x00,0x05,0x00,0x00,0x00,0x01])  # enabled but not used

# ---- micro:bit Nordic UART Service (NUS) ----
NUS_SERVICE = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
MICROBIT_NAMES = ("bbc micro:bit", "micro:bit")

TICK_SEC = 0.10  # 100ms

class Bridge:
    def __init__(self, x_scale: float, z_scale: float, deadzone: float, expo: float,
                 invert_x: bool, invert_z: bool):
        # BLE
        self.mario: Optional[BleakClient] = None
        self.microbit: Optional[BleakClient] = None
        self._nus_rx_char = None
        self._nus_write_response = False

        # IMU smoothing/bias
        self._ema_x = 0.0
        self._ema_z = 0.0
        self._alpha = 0.20   # EMA coefficient
        self._bias_x = 0.0
        self._bias_z = 0.0

        # Mapping feel
        self.x_scale = x_scale    # larger => less sensitive steering
        self.z_scale = z_scale    # larger => less sensitive throttle
        self.deadzone = deadzone  # 0..0.3 typically
        self.expo = expo          # 1.0 linear, 1.2–1.6 = finer center control
        self.invert_x = invert_x
        self.invert_z = invert_z

        # Last computed analogs
        self._throttle = 0.0
        self._steer = 0.0

        # Sender loop
        self._sender_task: Optional[asyncio.Task] = None

    # ---------- Discovery ----------
    async def find_mario(self, timeout=8.0) -> Optional[BLEDevice]:
        print(f"Scanning for Mario… (≤{timeout:.0f}s)")
        def filt(dev: BLEDevice, adv: AdvertisementData) -> bool:
            name = (adv.local_name or dev.name or "").lower()
            svc  = {u.lower() for u in (adv.service_uuids or [])}
            return (LEGO_SERVICE.lower() in svc) or ("mario" in name)
        return await BleakScanner.find_device_by_filter(filt, timeout=timeout)

    async def find_microbit(self, timeout=8.0) -> Optional[BLEDevice]:
        print(f"Scanning for micro:bit… (≤{timeout:.0f}s)")
        def filt(dev: BLEDevice, adv: AdvertisementData) -> bool:
            name = (adv.local_name or dev.name or "").lower()
            svc  = {u.lower() for u in (adv.service_uuids or [])}
            return (NUS_SERVICE.lower() in svc) or any(tag in name for tag in MICROBIT_NAMES)
        return await BleakScanner.find_device_by_filter(filt, timeout=timeout)

    # ---------- Connect Mario ----------
    async def connect_mario(self):
        dev = await self.find_mario()
        if not dev:
            raise RuntimeError("Mario not found. Press Mario’s Bluetooth button and retry.")
        print(f"Mario: {dev.name} ({dev.address})")
        self.mario = BleakClient(dev)
        await self.mario.connect()
        print("Mario connected.")
        await self.mario.start_notify(LEGO_CH, self.on_mario_notify)
        await asyncio.sleep(0.2)
        await self.mario.write_gatt_char(LEGO_CH, SUBSCRIBE_IMU)
        await asyncio.sleep(0.2)
        await self.mario.write_gatt_char(LEGO_CH, SUBSCRIBE_RGB)
        print("Mario subscribed for IMU.")
        await asyncio.sleep(0.3)

        # quick zero bias (hold still ~0.5s)
        print("Calibrating zero (hold Mario still ~0.5s)…")
        xs, zs = [], []
        for _ in range(6):
            await asyncio.sleep(0.1)
            xs.append(self._ema_x); zs.append(self._ema_z)
        if xs:
            self._bias_x = sum(xs)/len(xs)
            self._bias_z = sum(zs)/len(zs)
        print(f"Bias: x={self._bias_x:.1f}, z={self._bias_z:.1f}")

    # ---------- Connect micro:bit ----------
    async def connect_microbit(self):
        dev = await self.find_microbit()
        if not dev:
            raise RuntimeError("micro:bit not found. Make sure BLE UART is running & not paired in macOS Settings.")
        print(f"micro:bit: {dev.name} ({dev.address})")
        self.microbit = BleakClient(dev)
        await self.microbit.connect()

        svcs = await self.microbit.get_services()
        nus = svcs.get_service(NUS_SERVICE)
        if not nus:
            raise RuntimeError("NUS service not found on micro:bit")

        writable = None
        for c in nus.characteristics:
            props = set(c.properties)
            if "write-without-response" in props or "write" in props:
                writable = c
                break
        if not writable:
            raise RuntimeError("No writable NUS characteristic found on micro:bit")

        self._nus_rx_char = writable
        self._nus_write_response = "write" in set(writable.properties)
        print("Using NUS write char:", self._nus_rx_char.uuid, self._nus_rx_char.properties)
        print("micro:bit connected.")

        # start continuous sender
        self._sender_task = asyncio.create_task(self._sender_loop())

    # ---------- Mario notifications (IMU) ----------
    def on_mario_notify(self, _handle: int, data: bytearray):
        # IMU packet starts with 0x07, tilt bytes at [4]=x, [5]=y, [6]=z (signed)
        if len(data) < 7 or data[0] != 0x07:
            return
        rx = self._signed(data[4])
        rz = self._signed(data[6])

        # EMA smoothing
        a = self._alpha
        self._ema_x = (1 - a) * self._ema_x + a * rx
        self._ema_z = (1 - a) * self._ema_z + a * rz

        # debiased, normalized → [-1..1], deadzone, expo
        steer = self._map_axis(self._ema_x, self._bias_x, self.x_scale, self.deadzone, self.expo)
        thr   = self._map_axis(self._ema_z, self._bias_z, self.z_scale, self.deadzone, self.expo)

        # convention: forward is NEGATIVE z tilt on Mario => throttle positive
        thr = -thr

        if self.invert_x: steer = -steer
        if self.invert_z: thr   = -thr

        self._steer = steer
        self._throttle = thr

    # ---------- Axis mapping helpers ----------
    @staticmethod
    def _signed(b: int) -> int:
        return b - 256 if b > 127 else b

    @staticmethod
    def _clip(v: float, lo=-1.0, hi=1.0) -> float:
        return hi if v > hi else lo if v < lo else v

    def _map_axis(self, v: float, bias: float, scale: float, dz: float, expo: float) -> float:
        # normalize
        x = (v - bias) / max(1e-6, scale)
        x = self._clip(x)

        # deadzone (keep slope continuous)
        ax = abs(x)
        if ax <= dz:
            return 0.0
        # rescale remaining range to 0..1
        x = (ax - dz) / (1.0 - dz) * (1 if x >= 0 else -1)

        # expo power curve: 1.0 = linear; >1 soft center, <1 aggressive
        if expo != 1.0:
            x = (abs(x) ** expo) * (1 if x >= 0 else -1)

        return self._clip(x)

    # ---------- Continuous sender ----------
    async def _sender_loop(self):
        while self.microbit and self.microbit.is_connected:
            line = f"{self._throttle:.2f},{self._steer:.2f}:\n"
            try:
                await self.microbit.write_gatt_char(
                    self._nus_rx_char.uuid, line.encode(), response=self._nus_write_response
                )
                print(">>", line.strip())
            except Exception as e:
                print("Write error:", e)
            await asyncio.sleep(TICK_SEC)

async def safe_disconnect(client: Optional[BleakClient]):
    if client:
        try:
            if client.is_connected:
                await client.disconnect()
        except Exception:
            pass

async def main():
    import argparse
    ap = argparse.ArgumentParser(description="LEGO Mario → micro:bit analog bridge")
    ap.add_argument("--x-scale", type=float, default=30.0, help="steering sensitivity scale (bigger = less sensitive)")
    ap.add_argument("--z-scale", type=float, default=30.0, help="throttle sensitivity scale (bigger = less sensitive)")
    ap.add_argument("--deadzone", type=float, default=0.10, help="deadzone around center [0..0.3]")
    ap.add_argument("--expo", type=float, default=1.4, help="expo curve (>1 soft center, 1=linear)")
    ap.add_argument("--invert-x", action="store_true", help="invert steering")
    ap.add_argument("--invert-z", action="store_true", help="invert throttle")
    args = ap.parse_args()

    bridge = Bridge(args.x_scale, args.z_scale, args.deadzone, args.expo, args.invert_x, args.invert_z)
    await bridge.connect_mario()
    await bridge.connect_microbit()
    print("Streaming analog values at 100ms. Ctrl+C to quit.")
    try:
        while True:
            await asyncio.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        await safe_disconnect(bridge.mario)
        await safe_disconnect(bridge.microbit)

if __name__ == "__main__":
    asyncio.run(main())
