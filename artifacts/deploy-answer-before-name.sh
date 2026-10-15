#!/bin/bash
set -Eeuo pipefail
cd /home/orangepi/lira
backup="deploy-backups/answer-before-name-$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 -p "$backup"
tar -czf "$backup/before.tar.gz" device/lira/personal_memory.py device/lira/main.py device/lira/llm/client.py device/lira/llm/VOICE_AGENT.md device/lira/llm/PERSONA.md device/lira/llm/prompts.py
systemctl --user stop lira
rollback() {
  trap - ERR
  tar -xzf "$backup/before.tar.gz"
  systemctl --user start lira
  exit 1
}
trap rollback ERR
tar -xzf /tmp/lira-answer-before-name.tar.gz
device/.venv/bin/python -m compileall -q device/lira
systemctl --user start lira
echo "BACKUP=$backup"
