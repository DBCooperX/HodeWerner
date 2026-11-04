#!/usr/bin/env bash
# Installs HodeWerner inside a Debian 12/13 container or VM.
# Run as root from the repo folder: bash deploy/install.sh
set -euo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

APP_DIR=/opt/hodewerner
APP_USER=hodewerner
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv ffmpeg curl unzip ca-certificates

echo "==> Installing Deno (yt-dlp needs it for YouTube)"
case "$(uname -m)" in
  x86_64)  DENO_ARCH=x86_64-unknown-linux-gnu ;;
  aarch64) DENO_ARCH=aarch64-unknown-linux-gnu ;;
  *) echo "Unsupported CPU: $(uname -m)"; exit 1 ;;
esac
curl -fsSL -o /tmp/deno.zip "https://github.com/denoland/deno/releases/latest/download/deno-${DENO_ARCH}.zip"
unzip -o /tmp/deno.zip -d /usr/local/bin >/dev/null
chmod 755 /usr/local/bin/deno
rm /tmp/deno.zip
deno --version | head -1

echo "==> Creating user and copying files"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR"
for f in bot.py requirements.txt .env.example README.md; do
  install -m 644 "$SRC_DIR/$f" "$APP_DIR/$f"
done
if [ ! -f "$APP_DIR/.env" ]; then
  install -m 600 "$SRC_DIR/.env.example" "$APP_DIR/.env"
  NEED_ENV=1
fi

echo "==> Setting up Python environment"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> Installing services"
install -m 644 "$SRC_DIR/deploy/hodewerner.service" /etc/systemd/system/
install -m 644 "$SRC_DIR/deploy/hodewerner-update.service" /etc/systemd/system/
install -m 644 "$SRC_DIR/deploy/hodewerner-update.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable hodewerner.service hodewerner-update.timer
systemctl start hodewerner-update.timer

if [ "${NEED_ENV:-0}" = 1 ]; then
  echo
  echo "Almost done. Put your bot token and server ID in $APP_DIR/.env:"
  echo "    nano $APP_DIR/.env"
  echo "then start the bot with:"
  echo "    systemctl start hodewerner"
else
  systemctl restart hodewerner
  echo "HodeWerner restarted."
fi
echo "Logs: journalctl -u hodewerner -f"
