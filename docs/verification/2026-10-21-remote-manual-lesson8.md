# 第8课遥控器手动测试推送

单次任务 3f112fc6894d4d83a6de21d80112d461，经后台 KIMI 准备、豆包合成后推送。投递后原生回执 waiting_start、sequence=1、actual_repeats=0。助手未发送 start/confirm/replay/previous 控制或遥控按键，后续由家长操作，结果仍未核对。

投递曾遇到音频上传409。电视缓存最多4个任务，V2终态ACK未像V1一样释放音频；过去4次测试占满缓存。最小修复为仅在任务终态、全部事件已ACK并持久化后调用既有TaskAssetStore.release，保留状态及服务端日志。回归先复现失败，再验证终态ACK前保留音频、ACK后可接收第五个任务；10组原生测试通过。签名构建并安装电视APK后，重新确认上次已完成任务的终态ACK，重试的是同一待投递任务，未另建任务或自动开始。

[状态及测试凭据](artifacts/remote-manual-lesson8-20261021/receipt.json)。私有准备与投递日志位于设备 /home/orangepi/lira/deploy-backups/remote-manual-lesson8-20261021；持续调试日志采集保持运行。
