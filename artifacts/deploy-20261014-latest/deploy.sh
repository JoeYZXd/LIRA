#!/bin/bash
set -Eeuo pipefail
cd /home/orangepi/lira
backup="/home/orangepi/lira/deploy-backups/latest-20261014-$(date +%H%M%S)"
mkdir -p "$backup"
systemctl --user stop lira
rollback() {
  status=$?
  trap - ERR
  echo "DEPLOY_FAILED=$status; restoring $backup"
  tar -xzf "$backup/before.tar.gz" -C /home/orangepi/lira
  systemctl --user start lira
  exit "$status"
}
paths=()
for p in device/lira device/scripts device/pyproject.toml device/config.yaml device/data data deploy README.md; do
  if [ -e "$p" ]; then paths+=("$p"); fi
done
if ! tar -czf "$backup/before.tar.gz" "${paths[@]}"; then
  systemctl --user start lira
  exit 1
fi
trap rollback ERR
cp /tmp/sha256.txt "$backup/deployed-sha256.txt"
tar -xzf /tmp/deploy.tar.gz
sha256sum -c /tmp/sha256.txt > "$backup/checksums.log"
cd device
.venv/bin/python -m compileall -q lira
.venv/bin/python -m lira.main --dry-run > "$backup/dry-run.log" 2>&1
systemctl --user start lira
echo "BACKUP=$backup"
