# Raspberry Pi ThermoPro Monitor

This project includes:

- `thermpro_monitor.py`: BLE collector service (writes readings to SQLite)
- `thermpro_web.py`: web app dashboard
- `templates/index.html`: graph UI with:
  - title `ThermPro Monitor`
  - temperature/humidity toggles (either or both)
  - timescale control from 10 seconds to 48 hours
  - battery status in the graph's upper-right corner

## 1. Install system packages

```bash
sudo apt update
sudo apt install -y git python3-venv bluez bluetooth pi-bluetooth sqlite3
```

Add your login user to Bluetooth (example user: `kallen3d`):

```bash
sudo usermod -aG bluetooth kallen3d
```

Log out and back in once after changing groups.

## 2. Install the project

```bash
sudo mkdir -p /opt/thermpro-monitor
sudo chown -R kallen3d:kallen3d /opt/thermpro-monitor
cd /opt/thermpro-monitor
git clone https://github.com/skallen/thermpro-monitor.git .

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Create the data directory:

```bash
sudo mkdir -p /var/lib/thermpro-monitor
sudo chown kallen3d:kallen3d /var/lib/thermpro-monitor
```

## 3. Configure runtime settings

```bash
cp thermpro-monitor.env.example thermpro-monitor.env
```

Optional env settings:

- `THERMPRO_ALLOWED_MACS`: comma-separated MAC list. Leave empty to accept all matching names.
- `THERMPRO_NAME_PREFIXES`: default `TP3` for common ThermoPro hygrometers.
- `THERMPRO_MIN_SAVE_SECONDS`: min seconds between writes per device.
- `THERMPRO_WEB_PORT`: dashboard port (default `8080`).

## 4. Enable services

```bash
sudo cp thermpro-monitor.service /etc/systemd/system/thermpro-monitor.service
sudo cp thermpro-web.service /etc/systemd/system/thermpro-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now thermpro-monitor.service
sudo systemctl enable --now thermpro-web.service
```

Check logs:

```bash
journalctl -u thermpro-monitor.service -f
journalctl -u thermpro-web.service -f
```

## 5. Open the app

From the Pi browser:

```bash
http://localhost:8080
```

From another machine on your LAN:

```bash
http://<pi-ip-address>:8080
```

## 6. Add a desktop app icon on Raspberry Pi OS

```bash
mkdir -p ~/.local/share/applications
cp /opt/thermpro-monitor/desktop/thermpro-monitor.desktop ~/.local/share/applications/
cp /opt/thermpro-monitor/desktop/thermpro-monitor.desktop ~/Desktop/
chmod +x ~/Desktop/thermpro-monitor.desktop
```

You can then click `ThermPro Monitor` from the desktop/main page.
