# -*- coding: utf-8 -*-
"""chatlog HTTP 后端：读本机 chatlog 服务的 JSON 接口。

chatlog（Go 写的微信数据服务）在 2025-10 已由作者删库，但 Docker 镜像与旧版二进制仍在
社区流传，且它是目前**唯一能处理微信 4.x**的成熟读取方案，所以这里把它作为首选后端之一。

它对外提供的接口（v0.0.31 源码级清单）::

    GET /health                                   -> {"status":"ok"}
    GET /api/v1/session?limit=&offset=            -> {items:[{userName,nickName,content,nTime}]}
    GET /api/v1/contact?keyword=&limit=&offset=   -> {items:[{userName,alias,remark,nickName}]}
    GET /api/v1/chatroom?limit=&offset=           -> {items:[...]}
    GET /api/v1/chatlog?time=&talker=&limit=&offset=&format=json
    GET /image|video|file|voice/<key>             -> 媒体（voice 会实时转 mp3）

两个必须记住的坑：

1. **零鉴权**，``chatlog server`` 默认还绑 ``0.0.0.0:5030``。本后端默认只连回环地址，
   并在配置里支持可选的 ``auth_key``（放在 ``Authorization`` 与 ``X-Auth-Key`` 上）。
2. 数据库未就绪时 ``/api/v1/*`` 返回 **503**，body 是 ``{"error": "database is not ready"}``，
   之类。这里会把 503 翻译成可执行的中文提示，而不是抛一堆栈。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

from ..model import Conversation, Message, classify, conversation_kind, normalize_ts
from .base import BaseBackend, BackendError, media_refs_from_xml

#: 单次 HTTP 请求默认超时（秒）。
DEFAULT_TIMEOUT = 30.0
#: chatlog 分页默认页大小。
DEFAULT_PAGE_SIZE = 500
#: 翻页硬上限，防止服务端 offset 语义异常时死循环。
MAX_PAGES = 2000


class ChatlogBackend(BaseBackend):
	"""只读访问本机 chatlog HTTP 服务。"""

	name = "chatlog"

	def __init__(self, config: dict[str, Any] | None = None):
		settings = dict(config or {})
		self.base_url = str(settings.get("base_url") or "http://127.0.0.1:5030").rstrip("/")
		self.timeout = float(settings.get("timeout") or DEFAULT_TIMEOUT)
		self.page_size = max(1, int(settings.get("page_size") or DEFAULT_PAGE_SIZE))
		self.auth_key = str(settings.get("auth_key") or "")
		self._contact_cache: dict[str, dict[str, Any]] | None = None
		self._room_cache: dict[str, dict[str, Any]] | None = None

	# -- HTTP 基础 --------------------------------------------------------- #

	def _headers(self) -> dict[str, str]:
		"""构造请求头（无鉴权时只有 JSON 声明）。"""
		headers = {"Accept": "application/json", "User-Agent": "atuowexin/0.1"}
		if self.auth_key:
			headers["Authorization"] = f"Bearer {self.auth_key}"
			headers["X-Auth-Key"] = self.auth_key
		return headers

	def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
		"""GET 一个 JSON 接口，带 503 就绪状态翻译。"""
		query = urllib.parse.urlencode({key: value for key, value in (params or {}).items() if value not in (None, "")})
		url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
		request = urllib.request.Request(url, headers=self._headers(), method="GET")
		try:
			with urllib.request.urlopen(request, timeout=self.timeout) as response:
				payload = response.read()
		except urllib.error.HTTPError as error:
			body = ""
			try:
				body = error.read().decode("utf-8", "replace")[:400]
			except Exception:  # pragma: no cover - 读 body 失败不影响主流程
				body = ""
			if error.code == 503:
				raise BackendError(
					f"chatlog 数据库未就绪（503）：{body or 'no body'}。"
					"请先在微信登录状态下启动 chatlog 完成密钥提取与解密。"
				) from error
			raise BackendError(f"chatlog 返回 HTTP {error.code}：{url} {body}") from error
		except urllib.error.URLError as error:
			raise BackendError(
				f"连不上 chatlog（{self.base_url}）：{error.reason}。"
				"请确认 chatlog 已启动，且 base_url 指向本机回环地址。"
			) from error
		if not payload:
			return None
		try:
			return json.loads(payload.decode("utf-8", "replace"))
		except ValueError as error:
			raise BackendError(f"chatlog 返回的不是 JSON：{url}") from error

	# -- Backend ----------------------------------------------------------- #

	def health(self) -> dict[str, Any]:
		"""探活：先 ``/health``，再用 ``/api/v1/session`` 确认库已解密可查。"""
		try:
			payload = self._get("/health")
		except BackendError as error:
			return {"ok": False, "detail": str(error), "base_url": self.base_url}
		status = ""
		if isinstance(payload, dict):
			status = str(payload.get("status") or "")
		if status and status != "ok":
			return {"ok": False, "detail": f"chatlog /health 返回 {status!r}", "base_url": self.base_url}
		try:
			self._get("/api/v1/session", {"limit": 1, "format": "json"})
		except BackendError as error:
			return {"ok": False, "detail": str(error), "base_url": self.base_url}
		return {"ok": True, "detail": "chatlog 服务可用且数据库已就绪", "base_url": self.base_url}

	def list_conversations(self) -> list[Conversation]:
		"""会话列表 = session（最近会话）+ contact（联系人）+ chatroom（群）三表合并。"""
		sessions = self._items(self._get("/api/v1/session", {"limit": 5000, "format": "json"}))
		contacts = self._contact_map()
		rooms = self._room_map()

		conversations: dict[str, Conversation] = {}
		for item in sessions:
			username = str(_pick(item, "userName", "username") or "")
			if not username:
				continue
			kind = conversation_kind(username)
			conversations[username] = Conversation(
				id=username,
				name=str(_pick(item, "nickName", "nickname") or ""),
				remark="",
				kind=kind,
				last_time=normalize_ts(_pick(item, "nTime", "last_timestamp", "sort_timestamp")),
				extra={"summary": str(_pick(item, "content", "summary") or "")[:200]},
			)
		for username, contact in contacts.items():
			kind = conversation_kind(username)
			target = conversations.get(username)
			if target is None:
				if kind == "friend" and not contact.get("has_messages"):
					continue
				target = Conversation(id=username, kind=kind)
				conversations[username] = target
			target.name = target.name or str(contact.get("nick_name") or "")
			target.remark = str(contact.get("remark") or contact.get("alias") or "")
		for username, room in rooms.items():
			target = conversations.get(username)
			if target is None:
				target = Conversation(id=username, kind="group")
				conversations[username] = target
			target.kind = "group"
			target.member_count = int(room.get("member_count") or target.member_count or 0)
			target.name = target.name or str(room.get("nick_name") or room.get("remark") or "")
		return sorted(conversations.values(), key=lambda item: item.last_time, reverse=True)

	def iter_messages(self, conversation_id: str, *, since: int = 0, until: int = 0, limit: int = 0) -> Iterator[Message]:
		"""按时间升序翻页拉取某会话的消息。"""
		offset = 0
		produced = 0
		window = self._time_window(since, until)
		for _ in range(MAX_PAGES):
			payload = self._get(
				"/api/v1/chatlog",
				{
					"time": window,
					"talker": conversation_id,
					"limit": self.page_size,
					"offset": offset,
					"format": "json",
				},
			)
			items = self._items(payload)
			if not items:
				return
			for item in items:
				message = self._to_message(item, conversation_id)
				if message is None:
					continue
				if since and message.ts < since:
					continue
				if until and message.ts > until:
					continue
				yield message
				produced += 1
				if limit and produced >= limit:
					return
			if len(items) < self.page_size:
				return
			offset += len(items)

	def media_url(self, ref: dict[str, Any]) -> str:
		"""媒体下载地址：按类型拼 chatlog 的媒体路由。"""
		kind = str(ref.get("kind") or "")
		key = str(ref.get("md5") or ref.get("svr_id") or ref.get("path") or "")
		if not key:
			return ""
		route = {"image": "image", "video": "video", "file": "file", "voice": "voice", "sticker": "image", "sticker_gif": "image"}.get(kind, "file")
		return f"{self.base_url}/{route}/{urllib.parse.quote(key)}"

	def describe(self) -> dict[str, Any]:
		"""自述信息。"""
		return {"name": self.name, "synthetic": False, "base_url": self.base_url}

	# -- 内部工具 ----------------------------------------------------------- #

	@staticmethod
	def _items(payload: Any) -> list[dict[str, Any]]:
		"""兼容 ``{"items": [...]}`` 与裸数组两种返回形状。"""
		if payload is None:
			return []
		if isinstance(payload, dict):
			items = payload.get("items")
			if isinstance(items, list):
				return [item for item in items if isinstance(item, dict)]
			# 有的版本把结果放在 data / result 里
			for key in ("data", "result", "messages", "contacts", "sessions"):
				value = payload.get(key)
				if isinstance(value, list):
					return [item for item in value if isinstance(item, dict)]
			return []
		if isinstance(payload, list):
			return [item for item in payload if isinstance(item, dict)]
		return []

	def _contact_map(self) -> dict[str, dict[str, Any]]:
		"""联系人表（带缓存；只读一次，避免每会话一次请求）。"""
		if self._contact_cache is not None:
			return self._contact_cache
		try:
			items = self._items(self._get("/api/v1/contact", {"limit": 20000, "format": "json"}))
		except BackendError:
			items = []
		table: dict[str, dict[str, Any]] = {}
		for item in items:
			username = str(_pick(item, "userName", "username") or "")
			if not username:
				continue
			table[username] = {
				"nick_name": _pick(item, "nickName", "nickname") or "",
				"remark": _pick(item, "remark") or "",
				"alias": _pick(item, "alias") or "",
			}
		self._contact_cache = table
		return table

	def _room_map(self) -> dict[str, dict[str, Any]]:
		"""群表（带缓存）。不同 chatlog 版本字段名不一致，这里做宽容解析。"""
		if self._room_cache is not None:
			return self._room_cache
		try:
			items = self._items(self._get("/api/v1/chatroom", {"limit": 5000, "format": "json"}))
		except BackendError:
			items = []
		table: dict[str, dict[str, Any]] = {}
		for item in items:
			username = str(_pick(item, "userName", "username", "chatRoomName") or "")
			if not username:
				continue
			users = _pick(item, "users", "members") or []
			table[username] = {
				"nick_name": _pick(item, "nickName", "nickname") or "",
				"remark": _pick(item, "remark") or "",
				"member_count": len(users) if isinstance(users, list) else 0,
			}
		self._room_cache = table
		return table

	def _to_message(self, item: dict[str, Any], conversation_id: str) -> Message | None:
		"""把 chatlog 的一条 JSON 消息映射成归一化 :class:`Message`。"""
		talker = str(_pick(item, "talker", "talkerName", "username") or conversation_id)
		msg_type = _pick(item, "type", "msgType") or 0
		sub_type = _pick(item, "subType", "sub_type") or 0
		content = str(_pick(item, "content", "text", "msg") or "")
		contents = _pick(item, "contents") or {}
		if not isinstance(contents, dict):
			contents = {}
		kind = classify(msg_type, sub_type)
		# 图片/视频等类型的正文是 XML，转成人能读的文本，并抽出媒体 md5
		media: list[dict[str, Any]] = []
		if kind in {"image", "video", "file", "sticker", "sticker_gif", "voice"}:
			if kind == "voice":
				server = _pick(contents, "voice", "svr_id", "svrId") or _pick(item, "serverId", "server_id") or ""
				if server:
					media.append({"kind": "voice", "svr_id": str(server)})
			digest = str(_pick(contents, "md5", "rawmd5") or "")
			if digest:
				media.append({"kind": kind, "md5": digest, "path": str(_pick(contents, "path") or "")})
			if not media:
				media = media_refs_from_xml(content, kind)
			content = _summarize_media_content(kind, contents, content)
		else:
			media = media_refs_from_xml(content, kind)
		message = Message(
			conversation_id=talker or conversation_id,
			seq=int(_pick(item, "seq", "localId", "local_id") or 0),
			server_id=int(_pick(item, "serverId", "server_id", "MsgSvrID") or 0),
			sender_id=str(_pick(item, "sender", "senderName") or ""),
			sender_name=str(_pick(item, "senderName", "sender") or ""),
			is_self=bool(_pick(item, "isSelf", "is_self")),
			ts=normalize_ts(_pick(item, "time", "createTime", "create_time")),
			type=int(msg_type or 0),
			sub_type=int(sub_type or 0),
			kind=kind,
			text=content,
			media=media,
			raw={"chatlog": {key: value for key, value in item.items() if key in {"seq", "type", "subType", "isChatRoom"}}},
		)
		return message

	@staticmethod
	def _time_window(since: int, until: int) -> str:
		"""构造 chatlog 的 ``time`` 参数：``YYYY-MM-DD/HH:MM~YYYY-MM-DD/HH:MM``。"""
		start = int(since) if since else 0
		end = int(until) if until else int(time.time()) + 86400
		if start <= 0:
			start = int(time.mktime(time.strptime("2000-01-01", "%Y-%m-%d")))
		return f"{_chatlog_time(start)}~{_chatlog_time(end)}"


def _chatlog_time(ts: int) -> str:
	"""秒级时间戳 → chatlog 的 ``YYYY-MM-DD/HH:MM``。"""
	return time.strftime("%Y-%m-%d/%H:%M", time.localtime(int(ts)))


def _summarize_media_content(kind: str, contents: dict[str, Any], content: str) -> str:
	"""把「一段 XML」变成一句可读描述，例如 ``[图片]``、``[文件] 需求说明书.docx``。"""
	name = str(_pick(contents, "fileName", "filename", "title") or "")
	if kind == "file":
		return f"[文件] {name}" if name else "[文件]"
	if kind == "voice":
		length = _pick(contents, "length", "voiceLength") or ""
		return f"[语音 {length} 秒]" if length else "[语音]"
	if kind in {"image", "sticker", "sticker_gif"}:
		return "[图片]" if kind == "image" else "[表情]"
	if kind == "video":
		return "[视频]"
	from ..model import strip_markup

	stripped = strip_markup(content)
	return stripped[:200]


def _pick(container: Any, *keys: str) -> Any:
	"""从 dict 里按候选键名取第一个存在且非空的值。"""
	if not isinstance(container, dict):
		return None
	for key in keys:
		if key in container:
			value = container[key]
			if value not in (None, ""):
				return value
	return None


__all__ = ["ChatlogBackend"]
