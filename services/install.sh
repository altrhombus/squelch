#!/usr/bin/env bash
# Squelch install script — run on a Raspberry Pi (Raspberry Pi OS / Debian/Ubuntu)
# Usage: bash services/install.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
SERVICE_USER="${SUDO_USER:-$(whoami)}"

echo "==> Installing system dependencies"
sudo apt-get update -qq
sudo apt-get install -y \
  rtl-sdr \
  gnuradio \
  gr-osmosdr \
  gr-rds \
  ffmpeg \
  python3 \
  python3-pip \
  python3-venv

# nrsc5 is optional (HD Radio); install if available
if apt-cache show nrsc5 &>/dev/null; then
  sudo apt-get install -y nrsc5
  echo "    nrsc5 (HD Radio) installed"
else
  echo "    nrsc5 not in apt — skipping HD Radio (can be built from source later)"
fi

# Blacklist the kernel DVB driver that conflicts with RTL-SDR
if ! grep -q "blacklist dvb_usb_rtl28xxu" /etc/modprobe.d/blacklist-rtl.conf 2>/dev/null; then
  echo "==> Blacklisting conflicting kernel DVB driver"
  echo "blacklist dvb_usb_rtl28xxu" | sudo tee /etc/modprobe.d/blacklist-rtl.conf
  sudo modprobe -r dvb_usb_rtl28xxu 2>/dev/null || true
fi

# USB device permissions
if ! groups "$SERVICE_USER" | grep -q plugdev; then
  echo "==> Adding $SERVICE_USER to plugdev group (for RTL-SDR USB access)"
  sudo usermod -aG plugdev "$SERVICE_USER"
fi

echo "==> Creating Python virtual environment"
cd "$REPO_DIR"
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

echo "==> Setting up config"
if [ ! -f config/settings.yaml ]; then
  cp config/settings.yaml.example config/settings.yaml
  echo "    Created config/settings.yaml — edit it to add your local stations"
fi

echo "==> Creating recordings directory"
mkdir -p ~/recordings

echo "==> Installing systemd service"
SERVICE_FILE="$REPO_DIR/services/squelch.service"
# Patch the service file with actual paths and user
sed \
  -e "s|User=pi|User=$SERVICE_USER|g" \
  -e "s|WorkingDirectory=/home/pi/squelch|WorkingDirectory=$REPO_DIR|g" \
  -e "s|ExecStart=/home/pi/squelch/.venv|ExecStart=$REPO_DIR/.venv|g" \
  "$SERVICE_FILE" | sudo tee /etc/systemd/system/squelch.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable squelch.service

echo ""
echo "==================================================="
echo " Squelch installed successfully!"
echo ""
echo " Next steps:"
echo "   1. Edit config/settings.yaml with your stations"
echo "   2. Start the service:  sudo systemctl start squelch"
echo "   3. Open in browser:    http://$(hostname -I | awk '{print $1}'):8000"
echo ""
echo " Useful commands:"
echo "   sudo systemctl status squelch   — check service status"
echo "   sudo journalctl -u squelch -f   — follow logs"
echo "   sudo systemctl restart squelch  — restart after config change"
echo "==================================================="
