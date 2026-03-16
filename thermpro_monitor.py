#!/usr/bin/env python3
"""Monitor ThermoPro BLE hygrometers and store readings in SQLite."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from struct import Struct

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

LOGGER = logging.getLogger("thermpro-monitor")
UNPACK_TEMP_HUMID = Struct("<hB").unpack
BATTERY_VALUE_TO_LEVEL = {0: 1, 1: 50, 2: 100}


def _csv_env(var_name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    value = os.getenv(var_name, "").strip()
    if not value:
        return default
    return tuple(part.strip() for part in value.split(",") if part.strip())


@dataclass(frozen=True)
class Config:
    db_path: Path
    allowed_macs: frozenset[str]
    name_prefixes: tuple[str, ...]
    min_save_seconds: float
    log_level: str

    @staticmethod
    def from_env() -> "Config":
        db_path = Path(os.getenv("THERMPRO_DB_PATH", "/var/lib/thermpro-monitor/readings.db"))
        allowed_macs = frozenset(mac.lower() for mac in _csv_env("THERMPRO_ALLOWED_MACS"))
        name_prefixes = _csv_env("THERMPRO_NAME_PREFIXES", default=("TP3",))
        min_save_seconds = float(os.getenv("THERMPRO_MIN_SAVE_SECONDS", "10"))
        log_level = os.getenv("THERMPRO_LOG_LEVEL", "INFO").upper()
        return Config(
            db_path=db_path,
            allowed_macs=allowed_macs,
            name_prefixes=name_prefixes,
            min_save_seconds=min_save_seconds,
            log_level=log_level,
        )


@dataclass(frozen=True)
class ParsedReading:
    temperature_c: float
    humidity_pct: int
    battery_pct: int | None
    company_id: int
    payload_hex: str


def parse_tp3_manufacturer_data(manufacturer_data: dict[int, bytes]) -> ParsedReading | None:
    """Parse TP35x/TP39x advertisement data into temperature/humidity/battery."""
    for company_id, payload in manufacturer_data.items():
        data = company_id.to_bytes(2, byteorder="little", signed=False) + payload
        if len(data) < 6:
            continue

        temp_humi = data[1:4]
        if temp_humi == b"\xff\xff\xff":
            continue

        temp_raw, humidity = UNPACK_TEMP_HUMID(temp_humi)
        temperature_c = round(temp_raw / 10, 1)

        # Sanity checks to avoid false positives from non-ThermoPro payloads.
        if not (-55 <= temperature_c <= 125):
            continue
        if not (0 <= humidity <= 100):
            continue

        battery_pct = BATTERY_VALUE_TO_LEVEL.get(data[4] & 0x03)
        return ParsedReading(
            temperature_c=temperature_c,
            humidity_pct=humidity,
            battery_pct=battery_pct,
            company_id=company_id,
            payload_hex=payload.hex(),
        )
    return None


class Storage:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()

    def _create_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS readings (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              recorded_at TEXT NOT NULL,
              address TEXT NOT NULL,
              name TEXT NOT NULL,
              temperature_c REAL NOT NULL,
              humidity_pct INTEGER NOT NULL,
              battery_pct INTEGER,
              rssi INTEGER,
              company_id INTEGER NOT NULL,
              payload_hex TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_readings_device_time
              ON readings(address, recorded_at DESC);

            CREATE TABLE IF NOT EXISTS latest_readings (
              address TEXT PRIMARY KEY,
              recorded_at TEXT NOT NULL,
              name TEXT NOT NULL,
              temperature_c REAL NOT NULL,
              humidity_pct INTEGER NOT NULL,
              battery_pct INTEGER,
              rssi INTEGER
            );
            """
        )
        self._conn.commit()

    def write_reading(
        self,
        recorded_at: str,
        address: str,
        name: str,
        parsed: ParsedReading,
        rssi: int | None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO readings (
              recorded_at, address, name, temperature_c, humidity_pct,
              battery_pct, rssi, company_id, payload_hex
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                recorded_at,
                address,
                name,
                parsed.temperature_c,
                parsed.humidity_pct,
                parsed.battery_pct,
                rssi,
                parsed.company_id,
                parsed.payload_hex,
            ),
        )
        self._conn.execute(
            """
            INSERT INTO latest_readings (
              address, recorded_at, name, temperature_c, humidity_pct, battery_pct, rssi
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(address) DO UPDATE SET
              recorded_at=excluded.recorded_at,
              name=excluded.name,
              temperature_c=excluded.temperature_c,
              humidity_pct=excluded.humidity_pct,
              battery_pct=excluded.battery_pct,
              rssi=excluded.rssi
            """,
            (
                address,
                recorded_at,
                name,
                parsed.temperature_c,
                parsed.humidity_pct,
                parsed.battery_pct,
                rssi,
            ),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


class ThermoProMonitor:
    def __init__(self, config: Config, storage: Storage) -> None:
        self._config = config
        self._storage = storage
        self._last_saved_monotonic: dict[str, float] = {}

    def _is_target_device(self, address: str, name: str) -> bool:
        if self._config.allowed_macs and address.lower() not in self._config.allowed_macs:
            return False
        if not name:
            return False
        return name.startswith(self._config.name_prefixes)

    def on_advertisement(self, device: BLEDevice, adv: AdvertisementData) -> None:
        name = adv.local_name or device.name or ""
        address = device.address.lower()
        if not self._is_target_device(address, name):
            return

        parsed = parse_tp3_manufacturer_data(adv.manufacturer_data)
        if not parsed:
            return

        loop = asyncio.get_running_loop()
        now_monotonic = loop.time()
        last = self._last_saved_monotonic.get(address)
        if last is not None and now_monotonic - last < self._config.min_save_seconds:
            return
        self._last_saved_monotonic[address] = now_monotonic

        recorded_at = datetime.now(timezone.utc).isoformat()
        self._storage.write_reading(recorded_at, address, name, parsed, adv.rssi)
        LOGGER.info(
            "%s",
            json.dumps(
                {
                    "ts": recorded_at,
                    "address": address,
                    "name": name,
                    "temperature_c": parsed.temperature_c,
                    "humidity_pct": parsed.humidity_pct,
                    "battery_pct": parsed.battery_pct,
                    "rssi": adv.rssi,
                },
                separators=(",", ":"),
            ),
        )


def configure_logging(level: str) -> None:
    numeric_level = getattr(logging, level, logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


async def run(config: Config) -> None:
    storage = Storage(config.db_path)
    monitor = ThermoProMonitor(config, storage)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    LOGGER.info(
        "starting monitor: db=%s, allowed_macs=%s, name_prefixes=%s, min_save_seconds=%s",
        config.db_path,
        sorted(config.allowed_macs) if config.allowed_macs else "all",
        config.name_prefixes,
        config.min_save_seconds,
    )

    try:
        while not stop_event.is_set():
            scanner = BleakScanner(detection_callback=monitor.on_advertisement)
            try:
                await scanner.start()
                while not stop_event.is_set():
                    await asyncio.sleep(2)
            except Exception:
                LOGGER.exception("scanner crashed; restarting in 5 seconds")
                await asyncio.sleep(5)
            finally:
                with suppress(Exception):
                    await scanner.stop()
    finally:
        storage.close()


def main() -> None:
    config = Config.from_env()
    configure_logging(config.log_level)
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
