# 书架点击“未找到草稿或页面”修复

原因：前一次默写实现将 TextbookPractice.catalog 默认扩展为包含已导入的 review 草稿，管理页面复用了它。实际设备使用默认导入目录时，未整本发布的课本进入书架 books 数组；前端因此调用 /books/{id}，而该接口仍只读取已发布 JSON，返回404。草稿本身没有丢失。

修复：catalog 增加 include_drafts 开关，默认保持语音可读已核对课次；管理书架显式 include_drafts=False，未发布课本继续位于 drafts 数组，通过原 /drafts/{id} 进入可编辑核对页。目录响应增加 Cache-Control: no-store。没有修改或发布家庭教材。

回归：先用与设备相同的默认导入目录复现 books 数组错误含 review_draft，断言失败；修复后教材路由、教材读取与默写相关39项测试全部通过。新用例同时断言草稿可重新打开、语音第7课可用、第8课未核对仍拒绝。

已部署两个文件：lira/textbook_practice.py、lira/ui/textbooks.py。备份 /home/orangepi/lira/deploy-backups/shelf-draft-routing-20261020-070800；服务PID 69903→72724，active，NRestarts=0。其他源码哈希、家庭教材文件哈希、会话模式均未变；LIRA_REALTIME_CAPABILITIES 仍为 false。

真实设备浏览器只读验证通过：从 /dev 的教学管理卡片进入语文→课本列表与导入，点击现有课本能打开编辑页；课文标题和逐课核对按钮可编辑，原始书页缩略图可查看，始终留在同一卡片，没有教材接口失败或JavaScript错误。测试未保存、核对、上传或导出家庭内容。

证据：2026-10-20-shelf-draft-routing-device-smoke.json、2026-10-20-shelf-draft-routing-deployment.json。未暂存或提交；原Git index哈希保持不变。
