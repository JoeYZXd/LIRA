#!/bin/bash
set -Eeuo pipefail
cd /home/orangepi/lira
backup="/home/orangepi/lira/deploy-backups/bilingual-audio-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup"
override="$HOME/.config/systemd/user/lira.service.d/30-bilingual-audio.conf"
tar -czf "$backup/before.tar.gz" device/lira/audio/tts.py device/lira/audio/asr.py device/lira/llm/prompts.py
if [ -f "$override" ]; then cp "$override" "$backup/override.conf"; fi
systemctl --user stop lira
rollback() {
  trap - ERR
  tar -xzf "$backup/before.tar.gz"
  if [ -f "$backup/override.conf" ]; then cp "$backup/override.conf" "$override"; else rm -f "$override"; fi
  systemctl --user daemon-reload
  systemctl --user start lira
  exit 1
}
trap rollback ERR
tar -xzf /tmp/lira-bilingual-deploy.tar.gz
mkdir -p "$(dirname "$override")"
cat > "$override" <<'UNIT'
[Service]
Environment=LIRA_TTS_BACKEND=kokoro
Environment=LIRA_TTS_THREADS=4
Environment=LIRA_TTS_SPEAKER_ZH=3
Environment=LIRA_TTS_SPEAKER_EN=0
Environment=LIRA_ASR_DECODING_METHOD=modified_beam_search
UNIT
systemctl --user daemon-reload
systemctl --user start lira
echo "BACKUP=$backup"
