# -*- coding: utf-8 -*-
"""后端抽象：把「不同来源的微信数据」统一成同一组读取接口。

三个真实存在的来源，风险从低到高：

- ``decrypted_sqlite``：**你已经解密的本地库**（chatlog / MemoTrace / PyWxDump 的产物）。
  本插件既不取密钥也不解密，合规风险最低，是官方推荐的接入方式。
- ``chatlog``：本机 chatlog HTTP 服务（``127.0.0.1:5030``）。它自己负责解密与媒体转码，
  本插件只读它的 JSON 接口。注意 chatlog 无鉴权，务必只监听回环地址。
- ``mock``：合成数据，用于**离线自测**。它会明确标注自己是合成数据，绝不冒充真实聊天。
"""

from __future__ import annotations

import re
from typing import Any, Iterator, Protocol, runtime_checkable

from ..model import Conversation, Message


class BackendError(RuntimeError):
	"""后端不可用或返回了无法解析的数据。"""


@runtime_checkable
class Backend(Protocol):
	"""读取后端协议。所有实现都必须是**只读**的。"""

	name: str

	def health(self) -> dict[str, Any]:
		"""返回 ``{"ok": bool, "detail": str, ...}``；不抛异常。"""
		...

	def list_conversations(self) -> list[Conversation]:
		"""列出该来源里的全部会话。"""
		...

	def iter_messages(
		self,
		conversation_id: str,
		*,
		since: int = 0,
		until: int = 0,
		limit: int = 0,
	) -> Iterator[Message]:
		"""按会话增量迭代消息（``since`` 为秒级时间戳，``limit`` 0 表示不限）。"""
		...


_APP_TYPE_RE = re.compile(r"<appmsg[^>]*>.*?<type>\s*(\d+)\s*</type>", re.S | re.I)
_MD5_ATTR_RE = re.compile(r'md5\s*=\s*"([0-9a-fA-F]{16,64})"')
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)


def app_subtype_from_xml(xml_text: str) -> int:
	"""从 appmsg XML 里读 ``<type>``，得到 49 系列的 sub_type。

	微信 4.x 的消息表没有独立的 SubType 列，子类型只能从正文 XML 里取。

	:param xml_text: 消息正文（可能带 CDATA）。
	:return: sub_type；取不到返回 0。
	"""
	if not xml_text:
		return 0
	match = _APP_TYPE_RE.search(_CDATA_RE.sub(r"\1", xml_text))
	if match is None:
		return 0
	try:
		return int(match.group(1))
	except ValueError:
		return 0


def media_refs_from_xml(xml_text: str, kind: str) -> list[dict[str, Any]]:
	"""从消息 XML 里抠出媒体 md5，生成媒体引用列表。

	图片/视频/文件的真实字节不在消息表里（图片还在加密的 ``.dat`` 里），这里只登记
	「这条消息指向哪个资源」，下载与解密交给 chatlog 或用户自己的工具。

	:param xml_text: 消息正文。
	:param kind: 归一化类型（image/video/file/...）。
	:return: ``[{"kind": ..., "md5": ...}]``，可能为空。
	"""
	refs: list[dict[str, Any]] = []
	seen: set[str] = set()
	for match in _MD5_ATTR_RE.finditer(xml_text or ""):
		digest = match.group(1).lower()
		if digest in seen:
			continue
		seen.add(digest)
		refs.append({"kind": kind, "md5": digest})
	return refs


class BaseBackend:
	"""后端实现的公共部分：媒体 URL 默认为空、``describe`` 统一输出。"""

	name = "base"

	def health(self) -> dict[str, Any]:  # pragma: no cover - 子类必须覆盖
		"""默认认为健康；真实后端必须覆盖。"""
		return {"ok": True, "detail": ""}

	def list_conversations(self) -> list[Conversation]:  # pragma: no cover - 子类必须覆盖
		"""默认返回空列表；真实后端必须覆盖。"""
		return []

	def iter_messages(self, conversation_id: str, *, since: int = 0, until: int = 0, limit: int = 0) -> Iterator[Message]:  # pragma: no cover
		"""默认不产出消息；真实后端必须覆盖。"""
		return iter(())

	def media_url(self, ref: dict[str, Any]) -> str:
		"""媒体下载地址；只有 chatlog 后端能给出。"""
		return ""

	def describe(self) -> dict[str, Any]:
		"""给 doctor/CLI 看的自述信息。"""
		return {"name": self.name, "synthetic": False}


__all__ = [
	"BackendError",
	"Backend",
	"BaseBackend",
	"app_subtype_from_xml",
	"media_refs_from_xml",
]
