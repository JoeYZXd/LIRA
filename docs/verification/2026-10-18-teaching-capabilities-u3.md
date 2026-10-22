# U3 教材字表与版本原文执行证据

本地实现与复核通过，未提交、部署或修改原计划。

新增 teaching_content_versions SQLite表，加法且幂等初始化；旧教材JSON及字表筛选不改写。管理服务可手工保存课文/诗词、从指定导入页建立草稿、按版本确认、编辑、删除。编辑追加新草稿，旧确认不能覆盖新版本。模型只读目录、实际字表与当前已确认原文，无发布/确认工具。OCR/正文仅作为资料。

原文默认每页4000字符，上限8000，返回version、review_state、source、total_length、truncated和next_offset；跨版本续读报冲突，不将字表或截断片段宣称为完整课文。工具为 list_teaching_content、read_teaching_content、read_textbook_characters，仅装配实时registry。

失败先行：缺 teaching_content 模块导致新测试收集失败。新增测试覆盖真实SQLite/registry、确认绑定版本、编辑失效、缺全文、重复教材编号、OCR注入资料及分页冲突。既有PDF测试发现环境缺pypdfium2，已按项目声明补齐测试虚拟环境，未修改依赖声明或跳过测试。

Root权威验证：

```text
.venv/bin/python -m pytest tests/test_teaching_content.py tests/test_textbook_import.py tests/test_textbook_practice.py tests/test_conversation_paths.py tests/test_agent_task_state.py tests/test_realtime_capabilities.py -q
97 passed in 12.91s
```

修改文件：device/lira/teaching_content.py、device/lira/conversation_paths.py、device/tests/test_teaching_content.py。

U9通过paths.teaching_content调用管理写服务，路由须鉴权、隐私及来源校验；删除后云会话及派生缓存清理由后续集成负责。U3未进行设备数据升级或真实家庭教材导入，不宣称完整教学验收。
