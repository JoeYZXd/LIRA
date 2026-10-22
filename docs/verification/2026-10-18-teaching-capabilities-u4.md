# U4 视觉与 OCR 模型入口执行证据

本地实现及集成复核通过，未提交、部署或修改原计划；真实设备采集及隐私实测仍为后续门槛。

实时registry新增后台capture_text，复用相机与OCR，返回本次capture_id、camera_ocr来源、draft文字和明确局限，不提供不存在的置信度，也不将识别当标准答案。无硬件/隐私unavailable，互斥占用/经典阅读busy，空白/异常/过大输入明确失败。

DevHandle和新服务共用runtime.dev_mutex。实际OCR线程运行期间持锁；取消等待不释放原生资源，晚结果丢弃。已有有效任务的本地权限stamp在完成前复核，暂停/停止/修订后不发布结果。新服务仍draining时暂拒切入经典路径，stop与隐私立即撤销不受阻。

在新能力开关启用时，为现有OCR后端装配透明SharedOcr资源包装：read/detect/recognize由原生RLock序列化，运行时与既有管线共用实例；取消经典协程后原生线程仍可观测为busy。未修改经典阅读协程、算法、路由或工具；开关关闭时保留原OCR对象。

硬件HAL原capture会保留原始临时帧。新增独立capture_transient入口调用原capture并finally清理HAL自己的临时文件，实时工具优先使用；经典入口保持原样。成功、异常、取消后的清理由临时目录中的真实HAL实例验证，新服务不上传原图、不新增OCR正文持久化。

Red：缺realtime_vision模块；新增清理测试4项预期失败（缺方法/残留raw）。复用现有控制台、主循环、相机ISP回归，并用真实Registry/Executor/DevHandle/SQLite历史及threading.Event覆盖取消晚归、权限失效、跨链路原生线程、装配开关、正文不进派生档案。

Root权威验证：

```text
.venv/bin/python -m pytest tests/test_realtime_vision.py tests/test_camera_isp_quality.py tests/test_conversation_paths.py tests/test_dev_console.py tests/test_main_loop.py -q
192 passed in 31.15s
```

变更：device/lira/realtime_vision.py、conversation_paths.py、main.py、ui/dev.py、hal/board/camera_isp.py，以及tests/test_realtime_vision.py。主程序/控制台文件原有用户改动均保留，不能将其整个git diff归为本单元。
