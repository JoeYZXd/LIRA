#!/bin/bash
set -Eeuo pipefail
cd /home/orangepi/lira
backup="/home/orangepi/lira/deploy-backups/search-agent-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup"
systemctl --user stop lira
tar -czf "$backup/before.tar.gz" device/lira/config.py device/lira/llm/client.py device/config.yaml
rollback() {
  trap - ERR
  tar -xzf "$backup/before.tar.gz"
  systemctl --user start lira
  exit 1
}
trap rollback ERR
tar -xzf /tmp/search-agent-deploy.tar.gz
cd device
.venv/bin/python - <<'PY'
from pathlib import Path
import yaml
p = Path('config.yaml')
c = yaml.safe_load(p.read_text())
c['llm']['web_search_enabled'] = True
p.write_text(yaml.safe_dump(c, allow_unicode=True, sort_keys=False))
p.chmod(0o600)
PY
.venv/bin/python -m lira.main --dry-run > "$backup/dry-run.log" 2>&1
systemctl --user start lira
echo "BACKUP=$backup"
