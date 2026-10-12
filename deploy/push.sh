#!/usr/bin/env bash
# Copy Snipe to a server over SSH and (re)install it there. Works for any Linux VM.
#     deploy/push.sh ubuntu@203.0.113.7            # first deploy: also seeds the DB from ./snipe.db
#     SEED_DB=0 deploy/push.sh ubuntu@203.0.113.7  # don't copy the local database
set -euo pipefail
cd "$(dirname "$0")/.."
target=${1:?usage: deploy/push.sh user@host}
key=${SSH_KEY:-$HOME/.ssh/snipe_deploy}
ssh_opts=(-i "$key" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)

[[ -f .env ]] || { echo "Missing .env" >&2; exit 1; }

echo "⏳ Waiting for SSH on $target..."
for _ in $(seq 1 30); do ssh "${ssh_opts[@]}" "$target" true 2>/dev/null && break; sleep 10; done

echo "📦 Copying code (committed HEAD) and config..."
git archive --format=tar HEAD | ssh "${ssh_opts[@]}" "$target" 'mkdir -p ~/snipe && tar -xf - -C ~/snipe'
extra=(.env)
[[ -f config.yaml ]] && extra+=(config.yaml)
[[ "${SEED_DB:-1}" == 1 && -f snipe.db ]] && extra+=(snipe.db)
scp -q "${ssh_opts[@]}" "${extra[@]}" "$target:snipe/"
ssh "${ssh_opts[@]}" "$target" 'chmod 600 ~/snipe/.env'

echo "🚀 Installing..."
ssh "${ssh_opts[@]}" "$target" 'cd ~/snipe && bash deploy/install.sh'
