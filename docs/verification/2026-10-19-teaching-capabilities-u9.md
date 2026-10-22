# U9 教学管理执行证据

本地路由与浏览器验证通过，未提交或部署，原计划及脑图保持原样。

App 中心新增「教学管理」，「科目管理」明确禁用占位；「课本导入与核对」保留原地址、鉴权和导入核对行为，并可返回教学管理。页面复用现有控制台样式，不作为孩子语音辅导的配置向导。

支持课文/诗词手工录入、选择已有课本识别草稿和页码导入、版本编辑及家长确认。草稿不升级为标准；编辑撤销旧标准。修改或删除资料时先锁内结束旧实时上下文、清派生记录，再变更原文；不关闭经典引擎。

家长可查看实际约定、能力配置与当前模型调用状态、操作结果，保存或修正纸面核对，添加背诵证据附注及删除记录。原始背诵证据不被附注覆盖。管理写入复用开发者认证、来源与隐私校验，严格拒绝 bool 充当编号/版本，冲突返回409。家长管理接口未注册为模型工具。

本单元发现并修复两项竞态：网络保存期间所有表单控件暂时禁用并恢复原状态，避免后到响应覆盖输入或切换成员；等管理锁及 await 关闭旧云上下文后重新检查隐私，隐私中途开启时两个并发写入均403，原文版本及草稿数不变。家长保存网络失败重试保持同一 request_id，避免重复结果。

证明先行：新增路由测试先观察缺路由导致2项失败。Root 权威验证：

```text
pytest tests/test_teaching_routes.py tests/test_teaching_memory.py tests/test_teaching_evidence.py tests/test_textbook_routes.py tests/test_conversation_paths.py tests/test_dev_console.py -q
136 passed in 17.49s

# 后续隐私竞态修复后
pytest tests/test_teaching_routes.py -q
7 passed in 6.64s

# 独立浏览器进程
pytest tests/browser/test_teaching_ui.py tests/browser/test_textbooks_ui.py tests/browser/test_workspace_restore_ui.py -q
8 passed in 34.92s

# 后续输入竞态修复后
pytest tests/browser/test_teaching_ui.py -q
2 passed in 11.08s
```

另发现既有 App 中心布局测试仍写死12个入口，实际已为13；先观察 `assert 13 == 12`，只更新该数量断言后，`TestAppCenter` 的5项独立浏览器检查通过（20.19秒），覆盖390/768/1440/1920布局及窗口关闭恢复。未修改原布局实现。

Root 已查看 `tmp/u9-ui/teaching-380.png` 与 `teaching-1440.png`：控件可读、无横向溢出，原文中 HTML 样例保持文字，不执行脚本。浏览器页面操作使用明确标记的合成 API fixture；另有真实 FastAPI/服务仓库路由验证，不把浏览器 fixture 当作真实设备证据。

变更：`device/lira/ui/teaching.py`、`templates/teaching.html`、`static/teaching.js`、`static/teaching.css`、App/课本入口相关 dev.py/dev.html/textbooks.html/dev-console.js，以及管理专用 `ConversationPaths.manage_teaching_content` 和 `TeachingMemory.parent_delete`。相关 tests/test_teaching_routes.py、browser/test_teaching_ui.py、test_dev_console.py、browser/test_workspace_restore_ui.py。
