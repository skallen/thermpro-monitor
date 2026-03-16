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
    conn = sqlite3.connect(path, timeout=2.5)
    conn.row_factory = sqlite3.Row
    return conn


def _pick_default_device(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT address FROM latest_readings ORDER BY recorded_at DESC LIMIT 1"
    ).fetchone()
    return str(row["address"]) if row else None


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

            devices_payload = [
                {
                    "address": row["address"],
                    "name": row["name"],
                    "recorded_at": row["recorded_at"],
                    "temperature_c": row["temperature_c"],
                    "humidity_pct": row["humidity_pct"],
                    "battery_pct": row["battery_pct"],
                    "rssi": row["rssi"],
                }
                for row in rows
            ]
            default_address = devices_payload[0]["address"] if devices_payload else None

        return {"devices": devices_payload, "default_address": default_address}, 200

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

        return (
            {
                "address": address,
                "name": latest["name"],
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
