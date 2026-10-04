#!/bin/bash
# LIRA 设备端进程启动脚本（板上 ~/lira/deploy/start-lira.sh）
# 2026-10-04 起主循环由 systemd user unit 常驻（deploy/system/lira.service）：
#   systemctl --user enable --now lira   # 开机自启 + 立即启动
# 本脚本降级为手动重启入口（清 pycache 纪律已并入 unit 的 ExecStartPre）。
systemctl --user restart lira
sleep 2
systemctl --user is-active lira && journalctl --user -u lira -n 5 --no-pager
