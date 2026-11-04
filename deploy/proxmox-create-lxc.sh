#!/usr/bin/env bash
# Creates a Debian LXC on a Proxmox VE host and installs HodeWerner in it,
# or updates the bot in an existing container.
#
# Run as root on the Proxmox host, straight from GitHub:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/DBCooperX/HodeWerner/main/deploy/proxmox-create-lxc.sh)"
#
# Update the bot in an existing container: set UPDATE_CTID=<id> in front of the same command.
# Other settings can be changed the same way, for example CTID=150 STORAGE=local-zfs.
set -euo pipefail

REPO="${REPO:-DBCooperX/HodeWerner}"
BRANCH="${BRANCH:-main}"
GITHUB_TOKEN="${GITHUB_TOKEN:-}"
UPDATE_CTID="${UPDATE_CTID:-}"
HOSTNAME_CT="${HOSTNAME_CT:-hodewerner}"
STORAGE="${STORAGE:-local-lvm}"          # where the container disk goes
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
BRIDGE="${BRIDGE:-vmbr0}"
CORES="${CORES:-1}"
MEMORY="${MEMORY:-1024}"
DISK_GB="${DISK_GB:-4}"

command -v pct >/dev/null || { echo "Run this on the Proxmox host."; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Use the local copy when run from a checkout, otherwise download from GitHub.
SCRIPT_DIR="$(dirname "${BASH_SOURCE[0]:-}")"
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "$SCRIPT_DIR/../bot.py" ]; then
  SRC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
  echo "==> Using local code in $SRC_DIR"
else
  echo "==> Downloading $REPO ($BRANCH) from GitHub"
  AUTH=()
  [ -n "$GITHUB_TOKEN" ] && AUTH=(-H "Authorization: Bearer $GITHUB_TOKEN")
  curl -fsSL "${AUTH[@]}" -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$REPO/tarball/$BRANCH" -o "$WORK/src.tgz" \
    || { echo "Download failed. Check the network, or set GITHUB_TOKEN if the repo is private."; exit 1; }
  SRC_DIR="$WORK/src"
  mkdir -p "$SRC_DIR"
  tar -xzf "$WORK/src.tgz" -C "$SRC_DIR" --strip-components=1
fi

if [ -n "$UPDATE_CTID" ]; then
  CTID="$UPDATE_CTID"
  pct status "$CTID" >/dev/null || { echo "Container $CTID not found."; exit 1; }
  [ "$(pct status "$CTID" | awk '{print $2}')" = running ] || pct start "$CTID"
  echo "==> Updating HodeWerner in container $CTID"
else
  CTID="${CTID:-$(pvesh get /cluster/nextid)}"

  echo "==> Finding the newest Debian template"
  pveam update >/dev/null
  TEMPLATE="$(pveam available --section system | awk '{print $2}' | grep -E '^debian-1[2-9]-standard' | sort -V | tail -1)"
  [ -n "$TEMPLATE" ] || { echo "No Debian template found."; exit 1; }
  if ! pveam list "$TEMPLATE_STORAGE" | grep -q "$TEMPLATE"; then
    pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"
  fi

  echo "==> Creating container $CTID ($HOSTNAME_CT) from $TEMPLATE"
  pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$TEMPLATE" \
    --hostname "$HOSTNAME_CT" \
    --cores "$CORES" --memory "$MEMORY" --swap 512 \
    --rootfs "$STORAGE:$DISK_GB" \
    --net0 "name=eth0,bridge=$BRIDGE,ip=dhcp" \
    --unprivileged 1 --features nesting=1 \
    --onboot 1 --start 1
fi

echo "==> Waiting for network"
for _ in $(seq 1 30); do
  pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
  sleep 2
done

echo "==> Copying HodeWerner into the container"
tar -C "$SRC_DIR" --exclude=.git --exclude=.venv --exclude=.env -czf "$WORK/push.tgz" .
pct push "$CTID" "$WORK/push.tgz" /root/hodewerner-src.tgz
pct exec "$CTID" -- bash -c "rm -rf /root/hodewerner-src && mkdir -p /root/hodewerner-src && tar -xzf /root/hodewerner-src.tgz -C /root/hodewerner-src && rm /root/hodewerner-src.tgz"

echo "==> Installing inside the container"
pct exec "$CTID" -- bash /root/hodewerner-src/deploy/install.sh

echo
if [ -n "$UPDATE_CTID" ]; then
  echo "Container $CTID is updated."
else
  echo "Container $CTID is ready. Next:"
  echo "  pct enter $CTID"
  echo "  nano /opt/hodewerner/.env      # paste token and server ID"
  echo "  systemctl start hodewerner"
fi
