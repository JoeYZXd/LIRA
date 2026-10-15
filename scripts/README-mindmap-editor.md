# LIRA 脑图节点反馈

启动：`python scripts/start_mindmap_editor.py`，然后打开 `http://127.0.0.1:8768/`。
服务仅监听本机回环地址，不连接模型。也支持从原来的 HTML 文件页面提交。
如果页面显示连接失败，重新运行启动命令即可。重启电脑后需要重新启动服务。

每个展开节点都有钢笔按钮。选择操作并输入内容，提交成功后，请求保存在脑图旁的
`2026-10-15-0923-feat-developer-console-mindmap-plan.requests.json`。
浏览器 localStorage 只保存未提交草稿；只有服务成功落盘才会显示“已保存”。
提交后复制通知并粘贴到当前聊天，或从“待处理请求”复制一批请求的通知。

聊天处理规则：

- `modify` / `pending_modify`：读取用户原始要求，直接修改对应脑图节点，不主动讨论。
  完成后将该请求标记为 `applied`，记录简短 `result` 和 `resolved_at`。
- `discuss` / `pending_discussion`：先在当前聊天反馈意见，和用户讨论。
  在形成结论之前不修改目标节点内容，维持待讨论状态；结论确认并更新脑图后标记为 `applied`。
- `idea` / `pending_brainstorm`：使用 `compound-engineering:ce-brainstorm` 生成想法，
  先在当前聊天呈现，保留已有确定内容；完成脑暴后标记为 `ideas_presented`，
  记录结果摘要。只有用户选定的结论才替换确定内容；未选方案保持建议标记。

保留请求 ID、类型、原始输入和创建时间，不删除已处理记录。
每次修改脑图后，更新修订摘要，并把新增或修改的节点标记为本轮高亮；
请求待处理标记与内容修改高亮是两回事。
聊天处理请求文件前重新读取最新内容，写入时保留期间新增的其他请求。
不从页面猜测模型是否已经处理；刷新待处理列表重新读取真实文件状态。

本地记录仅保存用户的节点反馈，不写设备日志、不调用语音/模型接口。
如果页面复制失败，可手动选择通知文字粘贴；页面不会声称消息已发送。

服务检查：`python -m unittest discover -s scripts/tests -p test_mindmap_editor.py`。
