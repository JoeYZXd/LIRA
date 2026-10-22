#!/bin/bash
set -Eeuo pipefail
cd /home/orangepi/lira
backup="deploy-backups/persona-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup"
tar -czf "$backup/before.tar.gz" device/lira/llm/prompts.py device/lira/llm/VOICE_AGENT.md device/lira/llm/search_agent.py device/lira/llm/client.py device/lira/dialog/state_machine.py
systemctl --user stop lira
rollback() {
  trap - ERR
  tar -xzf "$backup/before.tar.gz"
  systemctl --user start lira
  exit 1
}
trap rollback ERR
tar -xzf /tmp/lira-persona.tar.gz
device/.venv/bin/python - <<'PY'
from pathlib import Path
p=Path('device/lira/dialog/state_machine.py')
s=p.read_text()
p.write_text(s.replace('message or "正在处理您的问题，请稍等。"','message or "我想一下啊。"'))
PY
device/.venv/bin/python -m compileall -q device/lira/llm device/lira/dialog/state_machine.py
systemctl --user start lira
echo "BACKUP=$backup"
