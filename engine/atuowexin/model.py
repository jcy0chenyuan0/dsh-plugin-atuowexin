# -*- coding: utf-8 -*-
"""归一化数据模型：把「微信各式各样的消息」压成一套与后端无关的结构。

为什么需要它：微信 3.x 与 4.x 的库结构完全不同（3.x 是 ``MSG`` 单表 + ``StrTalker``；
4.x 是每会话一张 ``Msg_<md5(username)>`` 分表 + ``real_sender_id`` 间接寻址），
chatlog 的 HTTP 又是第三种形状。摘录、总结、回应三层只认这里的 ``Conversation`` /
``Message``，所以换后端不需要改上层。

消息类型枚举与渲染规则来自公开资料与各开源实现的交叉比对（PyWxDump / WeChatMsg /
chatlog 的消息类型表），字段含义：

- ``type`` / ``sub_type``：微信原始类型号（4.x 里是 ``local_type & 0xFFFF``）。
- ``seq``：库里本地自增 id，只在同一会话内有序，用于去重兜底。
- ``server_id``：服务器消息 id，跨库稳定，是去重首选键。
- ``ts``：**归一化到秒**。3.x 的 ``CreateTime`` 是秒，部分 4.x 字段是毫秒，故统一走
  :func:`normalize_ts`。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

# --------------------------------------------------------------------------- #
# 消息类型
# --------------------------------------------------------------------------- #

#: 顶层 type → 语义。49/10000 需要再看 sub_type。
_TYPE_KIND: dict[int, str] = {
	1: "text",
	3: "image",
	34: "voice",
	37: "friend_request",
	42: "card",
	43: "video",
	47: "sticker",
	48: "location",
	49: "app",  # 引用 / 文件 / 链接 / 小程序 / 转账 / 红包 …… 由 sub_type 细分
	50: "voip",
	51: "voip",
	53: "voip",
	62: "short_video",
	66: "red_packet",  # 旧版微信红包
	10000: "system",
	10002: "revoke",
}

#: 49 系列的 sub_type → 语义。
_APP_SUBTYPE_KIND: dict[int, str] = {
	1: "text_card",  # 类文本分享
	5: "link",
	6: "file",
	7: "media_note",
	8: "sticker_gif",
	17: "forward",
	19: "merged_forward",
	33: "mini_program",
	36: "mini_program",
	51: "video_account",
	57: "quote",
	63: "live",
	87: "group_announcement",
	88: "live",
	2000: "transfer",
	2001: "red_packet",
	2003: "red_packet_cover",
}

#: 10000 系列的 sub_type → 语义。
_SYSTEM_SUBTYPE_KIND: dict[int, str] = {
	0: "system",
	4: "pat",  # 拍一拍，被拍/拍的账号在 DisplayContent 里
	8000: "system",
}

#: 归一化 kind → 中文标签（用于摘要与转录渲染）。
KIND_LABELS: dict[str, str] = {
	"text": "文本",
	"image": "图片",
	"voice": "语音",
	"video": "视频",
	"sticker": "表情",
	"sticker_gif": "动图表情",
	"file": "文件",
	"link": "链接",
	"text_card": "分享文本",
	"quote": "引用",
	"merged_forward": "合并转发",
	"forward": "转发",
	"mini_program": "小程序",
	"transfer": "转账",
	"red_packet": "红包",
	"red_packet_cover": "红包封面",
	"location": "位置",
	"card": "名片",
	"voip": "通话",
	"short_video": "小视频",
	"live": "直播",
	"video_account": "视频号",
	"media_note": "图文",
	"friend_request": "好友请求",
	"group_announcement": "群公告",
	"system": "系统消息",
	"pat": "拍一拍",
	"revoke": "撤回",
	"unknown": "未知",
}

#: 送去给 LLM 时「只有占位符、没有可用文本」的类型，需要提示模型不要据此推断内容。
NON_TEXT_KINDS = frozenset(
	{
		"image",
		"voice",
		"video",
		"sticker",
		"sticker_gif",
		"file",
		"voip",
		"live",
		"short_video",
	}
)


def classify(msg_type: Any, sub_type: Any = 0) -> str:
	"""把微信的 (type, sub_type) 映射成归一化 kind。

	:param msg_type: 微信消息类型号（4.x 已是 ``local_type & 0xFFFF``）。
	:param sub_type: 49/10000 系列的细分类型。
	:return: :data:`KIND_LABELS` 里的键，未知则 ``"unknown"``。
	"""
	try:
		top = int(msg_type)
	except (TypeError, ValueError):
		return "unknown"
	try:
		sub = int(sub_type or 0)
	except (TypeError, ValueError):
		sub = 0
	if top == 49:
		return _APP_SUBTYPE_KIND.get(sub, "app")
	if top == 10000:
		return _SYSTEM_SUBTYPE_KIND.get(sub, "system")
	return _TYPE_KIND.get(top, "unknown")


def kind_label(kind: str) -> str:
	"""归一化 kind → 中文标签。"""
	return KIND_LABELS.get(kind, kind or "未知")


# --------------------------------------------------------------------------- #
# 时间
# --------------------------------------------------------------------------- #

_MS_THRESHOLD = 10**11  # 1973 年以「秒」计的上下界都远小于此；毫秒时间戳必然大于它


def normalize_ts(value: Any) -> int:
	"""把各种时间戳形态统一成 **秒**。

	微信 3.x 的 ``CreateTime`` 是秒；4.x 部分列是毫秒；chatlog 的 ``time`` 是
	RFC3339 字符串。这里只处理数值，字符串交给 :func:`parse_time_text`。

	:param value: 秒/毫秒时间戳，或可被 ``int()`` 化的字符串。
	:return: 秒级时间戳；无法解析时返回 ``0``。
	"""
	if value is None or isinstance(value, bool):
		return 0
	if isinstance(value, str):
		stripped = value.strip()
		if not stripped:
			return 0
		try:
			value = float(stripped)
		except ValueError:
			return parse_time_text(stripped)
	try:
		number = float(value)
	except (TypeError, ValueError):
		return 0
	if number <= 0:
		return 0
	if number > _MS_THRESHOLD:
		number /= 1000.0
	return int(number)


def parse_time_text(text: str) -> int:
	"""解析 chatlog 风格的 RFC3339 / ``YYYY-MM-DD HH:MM:SS`` 时间文本。

	:param text: 时间字符串。
	:return: 秒级时间戳；解析失败返回 ``0``。
	"""
	from datetime import datetime

	candidate = text.strip().replace("Z", "+00:00")
	for pattern in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
		try:
			parsed = datetime.strptime(candidate, pattern)
		except ValueError:
			continue
		if parsed.tzinfo is not None:
			return int(parsed.timestamp())
		return int(parsed.replace(tzinfo=None).timestamp()) - _LOCAL_OFFSET_SECONDS()
	return 0


def _LOCAL_OFFSET_SECONDS() -> int:
	"""本机时区相对 UTC 的偏移秒数（``strptime`` 得到的是本地朴素时间）。"""
	from datetime import datetime

	return int(datetime.now().astimezone().utcoffset().total_seconds())


def format_ts(ts: int, fmt: str = "%Y-%m-%d %H:%M") -> str:
	"""秒级时间戳 → 本地时间文本。"""
	if not ts:
		return "-"
	from datetime import datetime

	return datetime.fromtimestamp(int(ts)).strftime(fmt)


# --------------------------------------------------------------------------- #
# 正文清洗
# --------------------------------------------------------------------------- #

_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
_DES_RE = re.compile(r"<(?:des|desc)>(.*?)</(?:des|desc)>", re.S | re.I)
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
_FILENAME_RE = re.compile(r"<(?:filename|fileext)>(.*?)</(?:filename|fileext)>", re.S | re.I)

_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_ID_CARD_RE = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")
_BANK_RE = re.compile(r"(?<!\d)(\d{16,19})(?!\d)")


def strip_markup(text: str) -> str:
	"""去掉 XML/HTML 标签与 CDATA，压掉多余空白。"""
	if not text:
		return ""
	value = _CDATA_RE.sub(r"\1", text)
	value = _TAG_RE.sub(" ", value)
	value = value.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"')
	return re.sub(r"\s+", " ", value).strip()


def xml_fields(xml_text: str) -> dict[str, str]:
	"""从 49 系列 XML 里抠出可读字段（标题/描述/文件名）。

	不引入 XML 解析器：微信的 appmsg XML 经常带非法实体，``ElementTree`` 会直接抛错，
	而这里只需要三五个字段，正则更稳。

	:param xml_text: 原始 XML 文本。
	:return: ``{"title": ..., "des": ..., "filename": ...}``，缺失的键为空串。
	"""
	source = xml_text or ""
	cdata = _CDATA_RE.sub(r"\1", source)
	return {
		"title": _first(_TITLE_RE, cdata),
		"des": _first(_DES_RE, cdata),
		"filename": _first(_FILENAME_RE, cdata),
	}


def _first(pattern: re.Pattern[str], text: str) -> str:
	match = pattern.search(text)
	if match is None:
		return ""
	return strip_markup(match.group(1))


def redact(text: str, *, phone: bool = True, id_card: bool = True, bank_card: bool = True) -> str:
	"""脱敏：把手机号/身份证/银行卡号替换成占位符。

	送给云端 LLM 之前默认开启（``privacy`` 配置段可关）。这是**降险不是合规保证**：
	真正合规的做法仍然是本地模型或取得对话参与者同意。

	:param text: 原文。
	:param phone: 是否遮蔽手机号。
	:param id_card: 是否遮蔽 18 位身份证号。
	:param bank_card: 是否遮蔽 16–19 位连续数字（银行卡）。
	:return: 脱敏后的文本。
	"""
	if not text:
		return ""
	value = text
	if phone:
		value = _PHONE_RE.sub(lambda m: m.group(1)[:3] + "****" + m.group(1)[-4:], value)
	if id_card:
		value = _ID_CARD_RE.sub(lambda m: m.group(1)[:6] + "********" + m.group(1)[-4:], value)
	if bank_card:
		value = _BANK_RE.sub(lambda m: m.group(1)[:4] + " **** **** " + m.group(1)[-4:], value)
	return value


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #

GROUP_SUFFIX = "@chatroom"
OFFICIAL_PREFIXES = ("gh_", "mp_")
SERVICE_IDS = frozenset({"weixin", "newsapp", "fmessage", "medianote", "floatbottle", "filehelper", "notifymessage", "officialaccounts"})


def conversation_kind(username: str) -> str:
	"""按 username 猜会话类型：``group`` / ``official`` / ``service`` / ``friend``。"""
	value = (username or "").strip()
	if value.endswith(GROUP_SUFFIX):
		return "group"
	if value.lower() in SERVICE_IDS:
		return "service"
	if value.startswith(OFFICIAL_PREFIXES):
		return "official"
	return "friend"


@dataclass
class Conversation:
	"""一条会话（私聊/群聊/公众号）在归一化层的表示。"""

	id: str
	name: str = ""
	remark: str = ""
	kind: str = "friend"
	last_time: int = 0
	msg_count: int = 0
	member_count: int = 0
	extra: dict[str, Any] = field(default_factory=dict)

	@property
	def is_group(self) -> bool:
		"""是否群聊。"""
		return self.kind == "group" or self.id.endswith(GROUP_SUFFIX)

	@property
	def display_name(self) -> str:
		"""展示名优先备注，其次昵称，最后 username。"""
		return self.remark or self.name or self.id

	@property
	def slug(self) -> str:
		"""稳定的文件系统安全标识：``<名字片段>-<id 哈希 8 位>``。"""
		return conversation_slug(self.display_name, self.id)

	def to_dict(self) -> dict[str, Any]:
		"""转成可 JSON 序列化的字典。"""
		return {
			"id": self.id,
			"name": self.name,
			"remark": self.remark,
			"kind": self.kind,
			"last_time": self.last_time,
			"msg_count": self.msg_count,
			"member_count": self.member_count,
			"extra": self.extra,
		}

	@classmethod
	def from_dict(cls, data: dict[str, Any]) -> "Conversation":
		"""从 :meth:`to_dict` 的结果还原。"""
		return cls(
			id=str(data.get("id", "")),
			name=str(data.get("name") or ""),
			remark=str(data.get("remark") or ""),
			kind=str(data.get("kind") or "friend"),
			last_time=int(data.get("last_time") or 0),
			msg_count=int(data.get("msg_count") or 0),
			member_count=int(data.get("member_count") or 0),
			extra=dict(data.get("extra") or {}),
		)


_UNSAFE_SLUG_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._-]+")


def conversation_slug(name: str, conversation_id: str) -> str:
	"""生成会话工作区目录名：可读 + 稳定 + 文件系统安全。

	会话名会重复、会改、会带 ``/`` 与 emoji，所以尾部一定拼上 username 的短哈希，
	保证「同一会话永远同一目录」且「不同会话不会撞目录」。

	:param name: 展示名。
	:param conversation_id: 会话 username。
	:return: 目录名，形如 ``产品群-a1b2c3d4``。
	"""
	digest = hashlib.md5((conversation_id or "").encode("utf-8")).hexdigest()[:8]
	cleaned = _UNSAFE_SLUG_RE.sub("_", (name or "").strip())[:40].strip("._-")
	return f"{cleaned or 'conv'}-{digest}"


# --------------------------------------------------------------------------- #
# 消息
# --------------------------------------------------------------------------- #


@dataclass
class Message:
	"""一条归一化消息。"""

	conversation_id: str
	seq: int = 0
	server_id: int = 0
	sender_id: str = ""
	sender_name: str = ""
	is_self: bool = False
	ts: int = 0
	type: int = 0
	sub_type: int = 0
	kind: str = "unknown"
	text: str = ""
	media: list[dict[str, Any]] = field(default_factory=list)
	raw: dict[str, Any] = field(default_factory=dict)

	@property
	def uid(self) -> str:
		"""去重键：优先服务器 id，退化到本地 seq，最后用内容哈希。"""
		if self.server_id:
			return f"{self.conversation_id}|s{self.server_id}"
		if self.seq:
			return f"{self.conversation_id}|l{self.seq}"
		payload = f"{self.conversation_id}|{self.ts}|{self.sender_id}|{self.text}".encode("utf-8")
		return f"{self.conversation_id}|h{hashlib.md5(payload).hexdigest()[:16]}"

	@property
	def label(self) -> str:
		"""该消息在转录里的类型标签（非文本才有意义）。"""
		return kind_label(self.kind)

	def display_line(self, *, redacted: bool = False, fmt: str = "%m-%d %H:%M") -> str:
		"""渲染成 ``[时间] 说话人: 内容`` 的一行，非文本消息用 ``[图片]`` 之类占位。"""
		who = "我" if self.is_self else (self.sender_name or self.sender_id or "对方")
		body = self.text.strip()
		if not body:
			body = f"[{self.label}]"
		if redacted:
			body = redact(body)
		return f"[{format_ts(self.ts, fmt)}] {who}: {body}"

	def to_dict(self) -> dict[str, Any]:
		"""转成可 JSON 序列化的字典（不含 uid，读侧用 :attr:`uid` 现算）。"""
		return {
			"conversation_id": self.conversation_id,
			"seq": self.seq,
			"server_id": self.server_id,
			"sender_id": self.sender_id,
			"sender_name": self.sender_name,
			"is_self": self.is_self,
			"ts": self.ts,
			"type": self.type,
			"sub_type": self.sub_type,
			"kind": self.kind,
			"text": self.text,
			"media": self.media,
			"raw": self.raw,
		}

	@classmethod
	def from_dict(cls, data: dict[str, Any]) -> "Message":
		"""从 :meth:`to_dict` 的结果还原。"""
		return cls(
			conversation_id=str(data.get("conversation_id", "")),
			seq=int(data.get("seq") or 0),
			server_id=int(data.get("server_id") or 0),
			sender_id=str(data.get("sender_id") or ""),
			sender_name=str(data.get("sender_name") or ""),
			is_self=bool(data.get("is_self")),
			ts=int(data.get("ts") or 0),
			type=int(data.get("type") or 0),
			sub_type=int(data.get("sub_type") or 0),
			kind=str(data.get("kind") or "unknown"),
			text=str(data.get("text") or ""),
			media=list(data.get("media") or []),
			raw=dict(data.get("raw") or {}),
		)

	def to_row(self) -> tuple:
		"""转成 ``store.messages`` 表的插入元组。"""
		import json

		return (
			self.uid,
			self.conversation_id,
			self.seq,
			self.server_id,
			self.sender_id,
			self.sender_name,
			1 if self.is_self else 0,
			self.ts,
			self.type,
			self.sub_type,
			self.kind,
			self.text,
			json.dumps(self.media, ensure_ascii=False),
			json.dumps(self.raw, ensure_ascii=False),
		)

	@classmethod
	def from_row(cls, row: Any) -> "Message":
		"""从 ``store`` 的查询行还原（行顺序见 :meth:`to_row`）。"""
		import json

		def _loads(value: Any, fallback: Any) -> Any:
			if not value:
				return fallback
			try:
				return json.loads(value)
			except (TypeError, ValueError):
				return fallback

		return cls(
			conversation_id=row[1],
			seq=row[2] or 0,
			server_id=row[3] or 0,
			sender_id=row[4] or "",
			sender_name=row[5] or "",
			is_self=bool(row[6]),
			ts=row[7] or 0,
			type=row[8] or 0,
			sub_type=row[9] or 0,
			kind=row[10] or "unknown",
			text=row[11] or "",
			media=_loads(row[12], []),
			raw=_loads(row[13], {}),
		)


#: ``store.messages`` 的列顺序，供 SQL 与 :meth:`Message.from_row` 共用。
MESSAGE_COLUMNS = (
	"uid",
	"conversation_id",
	"seq",
	"server_id",
	"sender_id",
	"sender_name",
	"is_self",
	"ts",
	"type",
	"sub_type",
	"kind",
	"text",
	"media",
	"raw",
)


# --------------------------------------------------------------------------- #
# 转录与切窗
# --------------------------------------------------------------------------- #


def format_transcript(
	messages: Iterable[Message],
	*,
	redacted: bool = False,
	include_ids: bool = True,
	self_name: str = "我",
	max_chars: int = 0,
) -> str:
	"""把消息渲染成给 LLM 看的转录文本。

	:param messages: 已按时间排序的消息。
	:param redacted: 是否对正文脱敏。
	:param include_ids: 是否在说话人后附上稳定 id（有助于模型区分同名成员）。
	:param self_name: 「自己」的显示名。
	:param max_chars: 超过该长度则从**最旧**的消息开始丢弃；0 表示不截断。
	:return: 多行转录文本。
	"""
	lines: list[str] = []
	for message in messages:
		who = self_name if message.is_self else (message.sender_name or message.sender_id or "对方")
		if include_ids and not message.is_self and message.sender_id:
			who = f"{who}({message.sender_id})"
		body = message.text.strip() or f"[{message.label}]"
		if redacted:
			body = redact(body)
		body = body.replace("\n", " ⏎ ")
		lines.append(f"{format_ts(message.ts, '%Y-%m-%d %H:%M:%S')} {who}: {body}")
	if max_chars > 0:
		total = sum(len(line) + 1 for line in lines)
		while lines and total > max_chars:
			total -= len(lines.pop(0)) + 1
	return "\n".join(lines)


def chunk_messages(messages: list[Message], size: int) -> Iterator[list[Message]]:
	"""把消息按固定条数切窗（总结时的 map 阶段用）。

	:param messages: 消息列表。
	:param size: 每窗条数，<=0 时视为不切窗。
	:return: 逐窗迭代。
	"""
	if size <= 0:
		yield list(messages)
		return
	for start in range(0, len(messages), size):
		yield messages[start : start + size]


__all__ = [
	"Conversation",
	"Message",
	"MESSAGE_COLUMNS",
	"KIND_LABELS",
	"NON_TEXT_KINDS",
	"classify",
	"kind_label",
	"normalize_ts",
	"parse_time_text",
	"format_ts",
	"strip_markup",
	"xml_fields",
	"redact",
	"conversation_kind",
	"conversation_slug",
	"format_transcript",
	"chunk_messages",
]
