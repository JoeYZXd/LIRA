"""一次性诊断脚本：从板上 dev 控制台拉取实时语音交互日志。

用法（板上执行）：.venv/bin/python fetch_voice_log.py [条数]
只读 db 取会话签名密钥 → 本地签名 → 127.0.0.1 拉 /dev/api/voice-log。
"""

import json
import sqlite3
import sys
import urllib.request

from itsdangerous import URLSafeTimedSerializer

DB = "/home/orangepi/lira/data/device.db"
URL = "http://127.0.0.1:8080/dev/api/voice-log"

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
row = conn.execute(
    "SELECT value FROM meta WHERE key='dev_console_session_secret'"
).fetchone()
if row is None:
    sys.exit("no session secret in db")
serializer = URLSafeTimedSerializer(row[0], salt="lira-dev-session")
token = serializer.dumps("dev-session")

req = urllib.request.Request(URL)
req.add_header("Cookie", f"lira_dev_session={token}")
payload = json.load(urllib.request.urlopen(req, timeout=10))

entries = payload["entries"]
limit = int(sys.argv[1]) if len(sys.argv) > 1 else len(entries)
import time

for e in entries[-limit:]:
    print(f"{time.strftime('%H:%M:%S', time.localtime(e['ts']))} [{e['kind']:>8}] {e['text']}")
print(f"engine.state = {payload.get('state')}")
