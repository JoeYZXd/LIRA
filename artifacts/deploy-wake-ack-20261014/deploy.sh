#!/bin/bash
set -Eeuo pipefail
cd /home/orangepi/lira
backup="/home/orangepi/lira/deploy-backups/wake-ack-20261014-$(date +%H%M%S)"
mkdir -p "$backup"
tar -czf "$backup/before.tar.gz" device/lira/main.py device/lira/dialog/phrasebook.py
rollback() {
  status=$?
  trap - ERR
  tar -xzf "$backup/before.tar.gz" -C /home/orangepi/lira
  systemctl --user restart lira
  echo "DEPLOY_FAILED=$status; restored=$backup"
  exit "$status"
}
trap rollback ERR
systemctl --user stop lira
tar -xzf /tmp/lira-wake-ack-20261014.tar.gz
device/.venv/bin/python -m py_compile device/lira/main.py device/lira/dialog/phrasebook.py
systemctl --user start lira
sleep 10
systemctl --user is-active --quiet lira
echo "BACKUP=$backup"
systemctl --user show lira -p ActiveState -p SubState -p MainPID -p NRestarts
sha256sum device/lira/main.py device/lira/dialog/phrasebook.py
