#!/bin/bash
set -Eeuo pipefail
cd /home/orangepi/lira
backup="deploy-backups/stream-melo-$(date +%Y%m%d-%H%M%S)"
override="$HOME/.config/systemd/user/lira.service.d/30-bilingual-audio.conf"
mkdir -p "$backup"
tar -czf "$backup/before.tar.gz" device/lira/audio/tts.py device/lira/llm/client.py device/lira/llm/search_agent.py device/lira/dialog/router.py device/lira/dialog/state_machine.py
cp "$override" "$backup/override.conf"
systemctl --user stop lira
rollback() {
  trap - ERR
  tar -xzf "$backup/before.tar.gz"
  cp "$backup/override.conf" "$override"
  systemctl --user daemon-reload
  systemctl --user start lira
  exit 1
}
trap rollback ERR
tar -xzf /tmp/lira-stream-melo.tar.gz
sed -i 's/LIRA_TTS_BACKEND=kokoro/LIRA_TTS_BACKEND=melo/' "$override"
device/.venv/bin/python -m compileall -q device/lira/audio device/lira/llm device/lira/dialog
systemctl --user daemon-reload
systemctl --user start lira
echo "BACKUP=$backup"
