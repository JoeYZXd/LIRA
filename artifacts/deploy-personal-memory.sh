#!/bin/bash
set -Eeuo pipefail
cd /home/orangepi/lira
backup="deploy-backups/personal-memory-$(date +%Y%m%d-%H%M%S)"
override="$HOME/.config/systemd/user/lira.service.d/40-personal-memory.conf"
mkdir -m 700 -p "$backup"
systemctl --user stop lira
tar -czf "$backup/before.tar.gz" device/lira/main.py device/lira/chat/service.py device/lira/dialog/state_machine.py device/lira/dialog/route_dispatch.py device/lira/llm/client.py device/lira/llm/search_agent.py data
chmod 600 "$backup/before.tar.gz"
if [ -f "$override" ]; then cp "$override" "$backup/override.conf"; fi
rollback() {
  trap - ERR
  tar -xzf "$backup/before.tar.gz"
  if [ -f "$backup/override.conf" ]; then cp "$backup/override.conf" "$override"; else rm -f "$override"; fi
  systemctl --user daemon-reload
  systemctl --user start lira
  exit 1
}
trap rollback ERR
tar -xzf /tmp/lira-personal-memory.tar.gz
device/.venv/bin/python -m compileall -q device/lira
mkdir -p "$(dirname "$override")"
cat > "$override" <<'UNIT'
[Service]
Environment=LIRA_PERSONAL_MEMORY=1
UNIT
systemctl --user daemon-reload
systemctl --user start lira
echo "BACKUP=$backup"
