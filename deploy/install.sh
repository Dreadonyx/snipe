#!/usr/bin/env bash
# Install or update Snipe on a fresh Linux VM (Debian/Ubuntu/Oracle Linux). Idempotent.
# Run from the project directory, with a filled-in .env next to it:
#     bash deploy/install.sh
set -euo pipefail
cd "$(dirname "$0")/.."

[[ -f .env ]] || { echo "Missing .env — copy .env.example and fill it in first." >&2; exit 1; }

# 1 GB VMs can run out of memory while installing packages; a swapfile prevents that
if [[ ! -f /swapfile ]] && (( $(awk '/MemTotal/ {print $2}' /proc/meminfo) < 2000000 )); then
    sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

command -v docker >/dev/null || curl -fsSL https://get.docker.com | sudo sh

sudo docker build -t snipe .

config_mount=()
[[ -f config.yaml ]] && config_mount=(-v "$PWD/config.yaml:/app/config.yaml:ro")

# Seed the database volume from a local snipe.db once, so existing subscribers carry over
if [[ -f snipe.db ]] && ! sudo docker volume inspect snipe-data >/dev/null 2>&1; then
    sudo docker volume create snipe-data >/dev/null
    sudo docker run --rm -v snipe-data:/data -v "$PWD/snipe.db:/seed.db:ro" --entrypoint sh snipe \
        -c 'cp /seed.db /data/snipe.db'
    echo "Seeded database from snipe.db"
fi

sudo docker rm -f snipe >/dev/null 2>&1 || true
sudo docker run -d --name snipe --restart unless-stopped \
    --env-file .env -v snipe-data:/data "${config_mount[@]}" \
    --log-opt max-size=10m --log-opt max-file=3 \
    snipe >/dev/null

sleep 5
sudo docker logs --tail 20 snipe
echo
echo "✅ Snipe is running. Logs: sudo docker logs -f snipe"
