#!/usr/bin/env python3
"""Web dashboard for ThermoPro monitor data."""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, render_template, request

LOGGER = logging.getLogger("thermpro-web")
MIN_WINDOW_SECONDS = 10
MAX_WINDOW_SECONDS = 48 * 60 * 60
DEFAULT_WINDOW_SECONDS = 60 * 60
DEFAULT_DASHBOARD_SETTINGS = {
    "selected_address": None,
    "window_seconds": DEFAULT_WINDOW_SECONDS,
    "show_temp": True,
    "show_humidity": True,
    "temperature_unit": "C",
}


@dataclass(frozen=True)
class WebConfig:
    db_path: Path
    host: str
    port: int
    log_level: str

    @staticmethod
    def from_env() -> "WebConfig":
        db_path = Path(os.getenv("THERMPRO_DB_PATH", "/var/lib/thermpro-monitor/readings.db"))
        host = os.getenv("THERMPRO_WEB_HOST", "0.0.0.0")
        port = int(os.getenv("THERMPRO_WEB_PORT", "8080"))
        log_level = os.getenv("THERMPRO_WEB_LOG_LEVEL", "INFO").upper()
        return WebConfig(db_path=db_path, host=host, port=port, log_level=log_level)


def _configure_logging(level: str) -> None:
    numeric_level = getattr(logging, level, logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _clamp_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except ValueError:
        return default
    return max(minimum, min(maximum, parsed))


def _open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=2.5)
    conn.row_factory = sqlite3.Row
    _ensure_settings_schema(conn)
    return conn


def _pick_default_device(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT address FROM latest_readings ORDER BY recorded_at DESC LIMIT 1"
    ).fetchone()
    return str(row["address"]) if row else None


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return bool(row)


def _ensure_settings_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sensor_settings (
          address TEXT PRIMARY KEY,
          sensor_alias TEXT,
          temp_low_c REAL,
          temp_high_c REAL,
          humidity_low REAL,
          humidity_high REAL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dashboard_settings (
          id INTEGER PRIMARY KEY CHECK(id = 1),
          selected_address TEXT,
          window_seconds INTEGER NOT NULL DEFAULT 3600,
          show_temp INTEGER NOT NULL DEFAULT 1,
          show_humidity INTEGER NOT NULL DEFAULT 1,
          temperature_unit TEXT NOT NULL DEFAULT 'C',
          updated_at TEXT NOT NULL
        );
        """
    )
    conn.commit()


def _coerce_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off"}:
            return False
    return default


def _coerce_float_or_none(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:  # NaN
        return None
    return parsed


def _coerce_threshold_or_none(value: object) -> float | None:
    parsed = _coerce_float_or_none(value)
    if parsed is None:
        return None
    return None if parsed == 0 else parsed


def _dashboard_settings_from_row(row: sqlite3.Row | None) -> dict:
    if row is None:
        return dict(DEFAULT_DASHBOARD_SETTINGS)
    return {
        "selected_address": row["selected_address"] or None,
        "window_seconds": _clamp_int(
            str(row["window_seconds"]),
            default=DEFAULT_WINDOW_SECONDS,
            minimum=MIN_WINDOW_SECONDS,
            maximum=MAX_WINDOW_SECONDS,
        ),
        "show_temp": bool(row["show_temp"]),
        "show_humidity": bool(row["show_humidity"]),
        "temperature_unit": "F" if row["temperature_unit"] == "F" else "C",
    }


def _load_dashboard_settings(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT selected_address, window_seconds, show_temp, show_humidity, temperature_unit
        FROM dashboard_settings
        WHERE id = 1
        LIMIT 1
        """
    ).fetchone()
    return _dashboard_settings_from_row(row)


def _load_sensor_settings(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        """
        SELECT address, sensor_alias, temp_low_c, temp_high_c, humidity_low, humidity_high
        FROM sensor_settings
        ORDER BY address ASC
        """
    ).fetchall()
    return {
        str(row["address"]).lower(): {
            "alias": row["sensor_alias"],
            "temp_low_c": row["temp_low_c"],
            "temp_high_c": row["temp_high_c"],
            "humidity_low": row["humidity_low"],
            "humidity_high": row["humidity_high"],
        }
        for row in rows
    }


def _normalize_address(address: str) -> str:
    return address.strip().lower()


def _sensor_metadata_from_name(name: str | None) -> dict[str, str]:
    normalized = str(name or "").upper()
    if normalized.startswith("GVH5100"):
        model = "Govee H5100"
    elif normalized.startswith("TP3"):
        model = "ThermoPro TP3-series"
    else:
        model = "Unknown"
    return {
        "sensor_type": "Hygrometer/Thermometer",
        "sensor_model": model,
    }


def create_app(config: WebConfig) -> Flask:
    app = Flask(__name__)
    app.config["THERMPRO_DB_PATH"] = str(config.db_path)

    @app.get("/")
    def index() -> str:
        return render_template("index.html")

    @app.get("/api/devices")
    def devices() -> tuple[dict, int]:
        db_path = Path(app.config["THERMPRO_DB_PATH"])
        if not db_path.exists():
            return {"devices": [], "default_address": None}, 200

        with _open_db(db_path) as conn:
            if not _table_exists(conn, "latest_readings"):
                return {"devices": [], "default_address": None}, 200
            rows = conn.execute(
                """
                SELECT
                  address,
                  name,
                  recorded_at,
                  temperature_c,
                  humidity_pct,
                  battery_pct,
                  rssi
                FROM latest_readings
                ORDER BY name ASC, address ASC
                """
            ).fetchall()

            devices_payload = []
            for row in rows:
                device = {
                    "address": row["address"],
                    "name": row["name"],
                    "recorded_at": row["recorded_at"],
                    "temperature_c": row["temperature_c"],
                    "humidity_pct": row["humidity_pct"],
                    "battery_pct": row["battery_pct"],
                    "rssi": row["rssi"],
                }
                device.update(_sensor_metadata_from_name(row["name"]))
                devices_payload.append(device)
            default_address = devices_payload[0]["address"] if devices_payload else None

        return {"devices": devices_payload, "default_address": default_address}, 200

    @app.get("/api/settings")
    def settings_get() -> tuple[dict, int]:
        db_path = Path(app.config["THERMPRO_DB_PATH"])
        with _open_db(db_path) as conn:
            dashboard_settings = _load_dashboard_settings(conn)
            sensor_settings = _load_sensor_settings(conn)
        return {
            "dashboard_settings": dashboard_settings,
            "sensor_settings": sensor_settings,
        }, 200

    @app.put("/api/settings/dashboard")
    def settings_dashboard_put() -> tuple[dict, int]:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return {"error": "invalid dashboard settings payload"}, 400

        selected_address = _normalize_address(str(payload.get("selected_address", "")))
        selected_address_value = selected_address or None
        window_seconds = _clamp_int(
            str(payload.get("window_seconds", DEFAULT_WINDOW_SECONDS)),
            default=DEFAULT_WINDOW_SECONDS,
            minimum=MIN_WINDOW_SECONDS,
            maximum=MAX_WINDOW_SECONDS,
        )
        show_temp = _coerce_bool(payload.get("show_temp"), True)
        show_humidity = _coerce_bool(payload.get("show_humidity"), True)
        temperature_unit = str(payload.get("temperature_unit", "C")).strip().upper()
        if temperature_unit not in {"C", "F"}:
            temperature_unit = "C"

        db_path = Path(app.config["THERMPRO_DB_PATH"])
        with _open_db(db_path) as conn:
            conn.execute(
                """
                INSERT INTO dashboard_settings (
                  id, selected_address, window_seconds, show_temp, show_humidity, temperature_unit, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  selected_address=excluded.selected_address,
                  window_seconds=excluded.window_seconds,
                  show_temp=excluded.show_temp,
                  show_humidity=excluded.show_humidity,
                  temperature_unit=excluded.temperature_unit,
                  updated_at=excluded.updated_at
                """,
                (
                    selected_address_value,
                    window_seconds,
                    1 if show_temp else 0,
                    1 if show_humidity else 0,
                    temperature_unit,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
            dashboard_settings = _load_dashboard_settings(conn)

        return {"dashboard_settings": dashboard_settings}, 200

    @app.put("/api/settings/sensor/<path:address>")
    def settings_sensor_put(address: str) -> tuple[dict, int]:
        normalized_address = _normalize_address(address)
        if not normalized_address:
            return {"error": "invalid address"}, 400

        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return {"error": "invalid sensor settings payload"}, 400

        alias = payload.get("alias")
        alias_value = str(alias).strip() if isinstance(alias, str) else ""
        alias_value = alias_value or None

        temp_low_c = _coerce_threshold_or_none(payload.get("temp_low_c"))
        temp_high_c = _coerce_threshold_or_none(payload.get("temp_high_c"))
        humidity_low = _coerce_threshold_or_none(payload.get("humidity_low"))
        humidity_high = _coerce_threshold_or_none(payload.get("humidity_high"))

        if humidity_low is not None:
            humidity_low = max(0.0, min(100.0, humidity_low))
        if humidity_high is not None:
            humidity_high = max(0.0, min(100.0, humidity_high))

        if temp_low_c is not None and temp_high_c is not None and temp_low_c > temp_high_c:
            return {"error": "temp_low_c must be less than or equal to temp_high_c"}, 400
        if humidity_low is not None and humidity_high is not None and humidity_low > humidity_high:
            return {"error": "humidity_low must be less than or equal to humidity_high"}, 400

        db_path = Path(app.config["THERMPRO_DB_PATH"])
        with _open_db(db_path) as conn:
            if (
                alias_value is None
                and temp_low_c is None
                and temp_high_c is None
                and humidity_low is None
                and humidity_high is None
            ):
                conn.execute(
                    "DELETE FROM sensor_settings WHERE lower(address) = ?",
                    (normalized_address,),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO sensor_settings (
                      address, sensor_alias, temp_low_c, temp_high_c, humidity_low, humidity_high, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(address) DO UPDATE SET
                      sensor_alias=excluded.sensor_alias,
                      temp_low_c=excluded.temp_low_c,
                      temp_high_c=excluded.temp_high_c,
                      humidity_low=excluded.humidity_low,
                      humidity_high=excluded.humidity_high,
                      updated_at=excluded.updated_at
                    """,
                    (
                        normalized_address,
                        alias_value,
                        temp_low_c,
                        temp_high_c,
                        humidity_low,
                        humidity_high,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            conn.commit()

            row = conn.execute(
                """
                SELECT address, sensor_alias, temp_low_c, temp_high_c, humidity_low, humidity_high
                FROM sensor_settings
                WHERE lower(address) = ?
                LIMIT 1
                """,
                (normalized_address,),
            ).fetchone()

        if not row:
            return {"address": normalized_address, "deleted": True}, 200

        return {
            "address": row["address"],
            "alias": row["sensor_alias"],
            "temp_low_c": row["temp_low_c"],
            "temp_high_c": row["temp_high_c"],
            "humidity_low": row["humidity_low"],
            "humidity_high": row["humidity_high"],
            "deleted": False,
        }, 200

    @app.get("/api/history")
    def history() -> tuple[dict, int]:
        db_path = Path(app.config["THERMPRO_DB_PATH"])
        if not db_path.exists():
            return {"error": f"database file not found: {db_path}"}, 404

        requested_seconds = request.args.get("seconds")
        window_seconds = _clamp_int(
            requested_seconds,
            default=60 * 60,
            minimum=MIN_WINDOW_SECONDS,
            maximum=MAX_WINDOW_SECONDS,
        )
        requested_address = request.args.get("address", "").strip().lower()

        with _open_db(db_path) as conn:
            if not _table_exists(conn, "latest_readings") or not _table_exists(conn, "readings"):
                return {
                    "error": "sensor data tables not found. start thermpro_monitor.py to collect readings"
                }, 404
            address = requested_address or _pick_default_device(conn)
            if not address:
                return {
                    "address": None,
                    "name": None,
                    "battery_pct": None,
                    "window_seconds": window_seconds,
                    "points": [],
                }, 200

            latest = conn.execute(
                """
                SELECT name, battery_pct, recorded_at, temperature_c, humidity_pct
                FROM latest_readings
                WHERE lower(address) = ?
                LIMIT 1
                """,
                (address,),
            ).fetchone()
            if not latest:
                return {"error": f"no data for address: {address}"}, 404

            since = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).isoformat()
            rows = conn.execute(
                """
                SELECT recorded_at, temperature_c, humidity_pct
                FROM readings
                WHERE lower(address) = ?
                  AND recorded_at >= ?
                ORDER BY recorded_at ASC
                """,
                (address, since),
            ).fetchall()

        points = [
            {
                "recorded_at": row["recorded_at"],
                "temperature_c": row["temperature_c"],
                "humidity_pct": row["humidity_pct"],
            }
            for row in rows
        ]

        metadata = _sensor_metadata_from_name(latest["name"])

        return (
            {
                "address": address,
                "name": latest["name"],
                "sensor_type": metadata["sensor_type"],
                "sensor_model": metadata["sensor_model"],
                "battery_pct": latest["battery_pct"],
                "latest_recorded_at": latest["recorded_at"],
                "latest_temperature_c": latest["temperature_c"],
                "latest_humidity_pct": latest["humidity_pct"],
                "window_seconds": window_seconds,
                "points": points,
            },
            200,
        )

    @app.get("/health")
    def health() -> tuple[dict, int]:
        return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}, 200

    return app


def main() -> None:
    config = WebConfig.from_env()
    _configure_logging(config.log_level)
    LOGGER.info("starting web app: db=%s host=%s port=%d", config.db_path, config.host, config.port)
    app = create_app(config)
    app.run(host=config.host, port=config.port)


if __name__ == "__main__":
    main()
