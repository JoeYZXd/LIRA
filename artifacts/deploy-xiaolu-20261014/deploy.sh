#!/bin/bash
set -Eeuo pipefail
cd /home/orangepi/lira
backup="/home/orangepi/lira/deploy-backups/xiaolu-20261014-$(date +%H%M%S)"
mkdir -p "$backup"
tar -czf "$backup/before.tar.gz" assets/keywords/wakeword_raw.txt device/lira/main.py device/lira/dialog/phrasebook.py device/lira/ui/dev.py
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
tar -xzf /tmp/lira-xiaolu-20261014.tar.gz
device/.venv/bin/python -m py_compile device/lira/main.py device/lira/dialog/phrasebook.py device/lira/ui/dev.py
cd device
.venv/bin/python -c 'from lira.main import _wakeword_text; from lira.audio.kws import WakeWordKws; assert _wakeword_text() == "小鹿"; k = WakeWordKws(); k.create_stream(); print("KWS_LOAD_OK; wakeword=" + _wakeword_text())'
systemctl --user start lira
sleep 10
systemctl --user is-active --quiet lira
echo "BACKUP=$backup"
systemctl --user show lira -p ActiveState -p SubState -p MainPID -p NRestarts
cd /home/orangepi/lira
sha256sum assets/keywords/wakeword_raw.txt device/lira/main.py device/lira/dialog/phrasebook.py device/lira/ui/dev.py
