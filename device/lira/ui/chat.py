"""聊天路由与页面（home-console 计划 U3，R5-R9）。

路由面（**全部 async def**——ChatStore 的 sqlite 连接按"单事件循环线程"
约定使用，sync 端点会落进线程池破坏该纪律，见 chat/store.py 模块注释）::

    GET  /chat                       选人页（成员卡片 + 新建成员表单）
    GET  /chat/{member_id}           会话页（最近 50 条 + 发送 + 加载更多 + 管理）
    POST /chat/members               创建成员 → 303 进会话；重名 → 显式错误重渲染
    POST /chat/{member_id}/rename    改名 → 303 回会话；重名 → 显式错误重渲染
    POST /chat/{member_id}/delete    删除成员（级联消息）→ 303 回选人页
    POST /chat/{member_id}/messages  发消息（JSON 进出，调 ChatService）
    GET  /chat/{member_id}/messages  历史分页（全聊天面唯一 GET 查询参数：数值 before）

安全纪律（测试化要求，非建议）：
  - 同源守卫：全部状态变更 POST 校验 Origin——存在且 host 与请求 host
    不符 → 显式拒绝；缺失放行（非浏览器 LAN 请求为既有局域网明文边界的
    接受主体，R9 无口令边界的自然延伸）。
  - 转义边界：服务端渲染一律走共享 Jinja 自动转义（聊天模板禁用 safe
    过滤器）；脚本内嵌数值仅经 dataset 属性；JS 侧动态文本只进元素内容
    位置（createElement + textContent），数值 ID 走 dataset/元素属性，
    动态值绝不拼进属性字符串。
  - 文本参数一律 POST body；数据端点错误回 JSON 不 302。
  - chat=None 装配：入口显式"暂不可用"，端点显式报错（不 500、不静默）。
  - 文案单一出处：页面提示与 JSON 错误串一律取自 phrasebook CHAT_* 族
    （与发送降级文案同库审校）。

日志纪律（R10）：成员生命周期事件只记数字 ID 与来源（source=web-chat），
绝不记名字或消息内容；消息内容不落任何日志（ChatService 已保证）。
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from lira.chat.service import MAX_MESSAGE_CHARS
from lira.chat.store import DuplicateMemberError
from lira.dialog.phrasebook import (
    CHAT_BAD_JSON_BODY,
    CHAT_GREETING,
    CHAT_MEMBER_MISSING,
    CHAT_NAME_DUPLICATE,
    CHAT_NAME_INVALID,
    CHAT_NEED_JSON,
    CHAT_OFFLINE_NOTE,
    CHAT_ORIGIN_REJECT,
)

__all__ = ["mount_chat", "PAGE_SIZE", "EMOJI_PRESETS"]

logger = logging.getLogger(__name__)

#: 会话页首屏条数与"加载更多"分页大小（计划 Key Decisions 边界数值）
PAGE_SIZE = 50
#: 新建成员的 emoji 预设（页内单选；非预设值一律回落库默认头像）
EMOJI_PRESETS = ("🙂", "😊", "👵", "👴", "👩", "👨", "👧", "👦")
#: 成员称呼上限（字）；模板 maxlength 经上下文同源注入（CHAT_NAME_INVALID 同数）
_MAX_NAME_CHARS = 20


def _page(rows: list[dict]) -> tuple[list[dict], bool]:
    """分页收敛（fetch limit=PAGE_SIZE+1 的约定形状）：多取的最旧一条丢弃，
    换取 has_more 标记（"加载更多"是否还有下一页）。rows 旧→新。"""
    if len(rows) > PAGE_SIZE:
        return rows[1:], True
    return rows, False


def _origin_ok(request: Request) -> bool:
    """同源守卫：Origin 存在且 host 与请求 host 不符 → 拒绝；缺失放行。"""
    origin = request.headers.get("origin")
    if not origin:
        return True
    return urlsplit(origin).netloc == request.headers.get("host", "")


def _clean_member_name(raw: str) -> str:
    """空串/超长返回空串（调用方显式报错），其余去首尾空白。"""
    name = raw.strip()
    if not name or len(name) > _MAX_NAME_CHARS:
        return ""
    return name


def _clean_emoji(raw: str) -> str | None:
    """预设之外（含空串）一律回落库默认头像（None = 用默认）。"""
    return raw if raw in EMOJI_PRESETS else None


def mount_chat(app: FastAPI, services, templates: Jinja2Templates) -> None:
    """向设备 UI 应用挂载聊天路由（由 create_app 调用，chat=None 亦挂载）。"""

    def _chat():
        return services.chat

    def _picker_context(error: str | None = None) -> dict:
        chat = _chat()
        return {
            "mode": "picker",
            "chat_available": chat is not None,
            "unavailable_note": CHAT_OFFLINE_NOTE,
            "members": [] if chat is None else chat.store.list_members(),
            "emoji_presets": EMOJI_PRESETS,
            "max_name_chars": _MAX_NAME_CHARS,
            "error": error,
        }

    def _conversation_context(member: dict, *, rename_error: str | None = None) -> dict:
        store = _chat().store
        rows, has_more = _page(store.recent_messages(member["id"], limit=PAGE_SIZE + 1))
        return {
            "mode": "conversation",
            "chat_available": True,
            "member": member,
            "messages": rows,
            "greeting": CHAT_GREETING,
            "has_more": has_more,
            "oldest_id": rows[0]["id"] if rows else 0,
            "max_message_chars": MAX_MESSAGE_CHARS,
            "max_name_chars": _MAX_NAME_CHARS,
            "rename_error": rename_error,
        }

    def _member_or_404(member_id: int) -> dict:
        member = _chat().store.get_member(member_id)
        if member is None:
            raise HTTPException(status_code=404, detail=CHAT_MEMBER_MISSING)
        return member

    def _origin_reject_page(request: Request) -> templates.TemplateResponse:
        """表单类 POST 的跨源拒绝：显式错误重渲染选人页（不执行任何变更）。"""
        return templates.TemplateResponse(
            request, "chat.html", _picker_context(error=CHAT_ORIGIN_REJECT), status_code=403
        )

    def _form_guard(request: Request) -> templates.TemplateResponse | None:
        """表单 POST 公共前置（规范序：先 chat 未装配、后同源）——两态均不
        执行任何变更；返回 None 表示放行。

        - chat 未装配 → 503 显式"暂不可用"重渲染选人页；
        - 跨源 → 403 显式拒绝重渲染选人页。
        JSON 端点不经过本 helper（错误须回 JSON，前置检查就地内联复用
        _chat/_origin_ok 原子检查）。
        """
        if _chat() is None:
            return templates.TemplateResponse(
                request, "chat.html", _picker_context(error=CHAT_OFFLINE_NOTE), status_code=503
            )
        if not _origin_ok(request):
            return _origin_reject_page(request)
        return None

    # ---------- 选人页 ----------

    @app.get("/chat")
    async def chat_picker(request: Request):
        return templates.TemplateResponse(request, "chat.html", _picker_context())

    # ---------- 会话页 ----------

    @app.get("/chat/{member_id}")
    async def chat_conversation(member_id: int, request: Request):
        if _chat() is None:
            return templates.TemplateResponse(request, "chat.html", _picker_context())
        member = _member_or_404(member_id)
        return templates.TemplateResponse(request, "chat.html", _conversation_context(member))

    # ---------- 成员生命周期（表单 POST，全部先过同源守卫） ----------

    @app.post("/chat/members")
    async def create_member(request: Request):
        resp = _form_guard(request)
        if resp is not None:
            return resp
        form = await request.form()
        name = _clean_member_name(str(form.get("name", "")))
        if not name:
            return templates.TemplateResponse(
                request, "chat.html", _picker_context(error=CHAT_NAME_INVALID), status_code=400
            )
        emoji = _clean_emoji(str(form.get("emoji", "")))
        try:
            member = _chat().store.create_member(name, emoji)
        except DuplicateMemberError:
            logger.info("chat event=member_create_rejected reason=duplicate")
            return templates.TemplateResponse(
                request,
                "chat.html",
                _picker_context(error=CHAT_NAME_DUPLICATE.format(name=name)),
                status_code=400,
            )
        logger.info("chat event=member_created source=web-chat member_id=%d", member["id"])
        return RedirectResponse(f"/chat/{member['id']}", status_code=303)

    @app.post("/chat/{member_id}/rename")
    async def rename_member(member_id: int, request: Request):
        resp = _form_guard(request)
        if resp is not None:
            return resp
        member = _member_or_404(member_id)
        form = await request.form()
        name = _clean_member_name(str(form.get("name", "")))
        if not name:
            return templates.TemplateResponse(
                request,
                "chat.html",
                _conversation_context(member, rename_error=CHAT_NAME_INVALID),
                status_code=400,
            )
        try:
            updated = _chat().store.rename_member(member_id, name)
        except DuplicateMemberError:
            logger.info("chat event=member_rename_rejected reason=duplicate member_id=%d", member_id)
            return templates.TemplateResponse(
                request,
                "chat.html",
                _conversation_context(
                    member, rename_error=CHAT_NAME_DUPLICATE.format(name=name)
                ),
                status_code=400,
            )
        logger.info("chat event=member_renamed source=web-chat member_id=%d", member_id)
        return RedirectResponse(f"/chat/{updated['id']}", status_code=303)

    @app.post("/chat/{member_id}/delete")
    async def delete_member(member_id: int, request: Request):
        resp = _form_guard(request)
        if resp is not None:
            return resp
        member = _member_or_404(member_id)
        # 级联删除其全部消息（store FK ON DELETE CASCADE）
        _chat().store.delete_member(member["id"])
        logger.info("chat event=member_deleted source=web-chat member_id=%d", member["id"])
        return RedirectResponse("/chat", status_code=303)

    # ---------- 消息收发（JSON 进出；数据端点错误回 JSON 不 302） ----------

    @app.post("/chat/{member_id}/messages")
    async def send_message(member_id: int, request: Request):
        # JSON 端点前置就地内联（错误须回 JSON 不回 HTML；_form_guard 回的是页面）
        chat = _chat()
        if chat is None:
            return JSONResponse(
                {"state": "unavailable", "message": CHAT_OFFLINE_NOTE, "reply": None, "saved": False},
                status_code=503,
            )
        if not _origin_ok(request):
            return JSONResponse({"error": CHAT_ORIGIN_REJECT}, status_code=403)
        if not str(request.headers.get("content-type", "")).startswith("application/json"):
            # 纵深防御：消息端点只收 JSON（浏览器 fetch 恒为 application/json）
            return JSONResponse({"error": CHAT_NEED_JSON}, status_code=415)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - 任何解析失败都按显式 400，不 500
            return JSONResponse({"error": CHAT_BAD_JSON_BODY}, status_code=400)
        # 文本参数只走 POST body（query 参数一律忽略）
        text = str(body.get("text", "")) if isinstance(body, dict) else ""
        result = await chat.send(member_id, text)
        return JSONResponse(
            {
                "state": result.state,
                "message": result.message,
                "reply": result.reply,
                "saved": result.saved,
            }
        )

    @app.get("/chat/{member_id}/messages")
    async def list_messages(member_id: int, request: Request, before: int | None = None):
        chat = _chat()
        if chat is None:
            return JSONResponse({"error": CHAT_OFFLINE_NOTE}, status_code=503)
        store = chat.store
        if store.get_member(member_id) is None:
            return JSONResponse({"error": CHAT_MEMBER_MISSING}, status_code=404)
        if before is None:
            rows, has_more = _page(store.recent_messages(member_id, limit=PAGE_SIZE + 1))
        else:
            # 多取一条判断"更早还有没有"；_page 丢弃最旧一条（贴近 before 的一页）
            rows, has_more = _page(store.messages_before(member_id, before, limit=PAGE_SIZE + 1))
        return JSONResponse(
            {
                "messages": [
                    {"id": m["id"], "role": m["role"], "content": m["content"]} for m in rows
                ],
                "has_more": has_more,
            }
        )
