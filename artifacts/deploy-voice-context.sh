#!/bin/bash
set -Eeuo pipefail
cd /home/orangepi/lira
backup="deploy-backups/voice-context-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup"
tar -czf "$backup/before.tar.gz" device/lira/audio/tts.py device/lira/llm/client.py device/lira/llm/search_agent.py device/lira/dialog/state_machine.py device/pyproject.toml
systemctl --user stop lira
rollback() {
  trap - ERR
  tar -xzf "$backup/before.tar.gz"
  systemctl --user start lira
  exit 1
}
trap rollback ERR
tar -xzf /tmp/lira-voice-context.tar.gz
device/.venv/bin/python - <<'PY'
from pathlib import Path
p=Path('device/lira/dialog/state_machine.py')
s=p.read_text()
old='on_wait_feedback=lambda: self.speak_progress(owner, pb.WAIT_REMOTE)'
new='on_wait_feedback=lambda message=None: self.speak_progress(owner, message or "正在处理您的问题，请稍等。")'
if old not in s and new not in s:
    raise RuntimeError('unexpected feedback wiring')
p.write_text(s.replace(old,new))
PY
device/.venv/bin/python -m compileall -q device/lira/audio device/lira/llm device/lira/dialog/state_machine.py
systemctl --user start lira
echo "BACKUP=$backup"
