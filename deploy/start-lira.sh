#!/bin/bash
# LIRA 设备端进程启动脚本（板上 ~/start-lira.sh）
# 每次启动前清 __pycache__：防止 scp 更新源码后旧字节码被加载
# （2026-10-04 实测：pyc 未失效导致启动即崩 AttributeError）
pkill -f 'python -m lira.main' 2>/dev/null
sleep 1
find /home/orangepi/lira/device/lira -name '__pycache__' -exec rm -rf {} + 2>/dev/null
cd /home/orangepi/lira/device
LIRA_DEV_CONSOLE=1 setsid nohup .venv/bin/python -m lira.main </dev/null >/tmp/lira.log 2>&1 &
echo "started pid $!"
