# Raspberry Pi ThermoPro Monitor

This project provides a small Python service for Raspberry Pi 3B+ that:

- listens for BLE advertisements from ThermoPro hygrometers (`TP35x` / `TP39x` family)
- decodes temperature, humidity, and battery level
- stores readings in SQLite
- runs continuously at boot with `systemd`

The packet decode logic matches the upstream ThermoPro parser used by Home Assistant's Bluetooth stack.

## 1. Install system packages

```bash
sudo apt update
sudo apt install -y python3-venv bluez bluetooth pi-bluetooth sqlite3
```

Make sure your `pi` user can access Bluetooth:

```bash
sudo usermod -aG bluetooth pi
```

Log out and back in once after changing groups.

## 2. Install this app

```bash
sudo mkdir -p /opt/thermpro-monitor
sudo chown -R pi:pi /opt/thermpro-monitor
cd /opt/thermpro-monitor
git clone <your-repo-url> .

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Create a writable data directory:

```bash
sudo mkdir -p /var/lib/thermpro-monitor
sudo chown pi:pi /var/lib/thermpro-monitor
```

## 3. Configure sensors

```bash
cp thermpro-monitor.env.example thermpro-monitor.env
```

Edit `thermpro-monitor.env`:

- `THERMPRO_ALLOWED_MACS`: optional comma-separated MAC list. Leave empty to accept all matching ThermoPro names.
- `THERMPRO_NAME_PREFIXES`: default `TP3` (covers TP35x/TP39x hygrometers).
- `THERMPRO_MIN_SAVE_SECONDS`: minimum seconds between DB writes per sensor.

To discover MAC addresses:

```bash
bluetoothctl scan on
```

## 4. Enable auto-start on boot

```bash
sudo cp thermpro-monitor.service /etc/systemd/system/thermpro-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now thermpro-monitor.service
```

Check status and live logs:

```bash
systemctl status thermpro-monitor.service
journalctl -u thermpro-monitor.service -f
```

## 5. Query readings

Latest reading per sensor:

```bash
sqlite3 /var/lib/thermpro-monitor/readings.db \
  "SELECT recorded_at,address,name,temperature_c,humidity_pct,battery_pct,rssi FROM latest_readings;"
```

Recent history:

```bash
sqlite3 /var/lib/thermpro-monitor/readings.db \
  "SELECT recorded_at,address,temperature_c,humidity_pct,battery_pct FROM readings ORDER BY id DESC LIMIT 20;"
```
