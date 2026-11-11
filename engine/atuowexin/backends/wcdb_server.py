# -*- coding: utf-8 -*-
"""WCDB 后端：把库交给腾讯自己的 WCDB 引擎去开（参考工具的做法）。

## 为什么需要它

微信 4.1.10 之后，密钥零驻留 + 库结构是 WCDB 自家的封装，**标准 SQLCipher 的
HMAC 公式验不出来**（实测 4.1.15.56：同一把 key，WCDB 能开、按 SQLCipher-4
派生算 HMAC 却全库不通过）。既然密钥只有 WCDB 自己懂怎么用，最可靠的判定与读取
方式就是「让 WCDB 自己开」：

1. 起一个 node 子进程跑参考工具的 wcdb_server.js（koffi 加载 WCDB.dll）；
2. 它把库复制成扁平快照后打开，然后暴露只读 HTTP 接口：
   /ping、/sessions、/messages/<id>/<limit>/<offset>；
3. 我们把这几个接口映射成 Backend 协议。

## 边界

* 只读：只调 get_sessions / get_messages，不写库、不发送消息；
* 密钥只在子进程命令行里出现一次（与参考工具一致），不落任何日志；
* 快照失败（微信正在写 -wal）时按 retries 重试，仍失败就如实报错，
  并提示「退出微信后重试」。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterator

from ..model import Conversation, Message, classify, normalize_ts
from .base import BackendError, media_refs_from_xml

#: 参考工具的默认位置（可用配置覆盖）。
_DEFAULT_REFERENCE = Path(r"C:\project\python\WeChat-Export-Tool")
_DEFAULT_NODE = Path(r"C:\Program Files\nodejs\node.exe")
_DEFAULT_KEY_FILES = (
	Path.home() / "Desktop" / "wx_export" / "key.txt",
)


def _default_data_dir() -> Path | None:
	"""自动找本机微信账号目录（含 db_storage 的那一层）。"""
	candidates = [
		Path.home() / "Documents" / "Tencent Files" / "xwechat_files",
		Path.home() / "Documents" / "xwechat_files",
		Path.home() / "Documents" / "WeChat Files",
	]
	for root in candidates:
		if not root.is_dir():
			continue
		accounts = [item for item in root.iterdir() if item.is_dir() and (item / "db_storage").is_dir()]
		# 账号目录一律是 wxid_*；all_users 之类的共享库不是账号根
		accounts.sort(key=lambda item: (not item.name.startswith("wxid_"), item.name))
		if accounts:
			return accounts[0]
	return None


def _pick(payload: dict[str, Any], *names: str, default: Any = "") -> Any:
	"""按顺序取第一个非空字段（不同 WCDB 版本的字段名有差异）。"""
	for name in names:
		value = payload.get(name)
		if value not in (None, "", 0):
			return value
	return default


class WcdbBackend:
	"""WCDB HTTP 子进程后端。"""

	name = "wcdb"

	def __init__(self, settings: dict[str, Any] | None = None) -> None:
		settings = dict(settings or {})
		self.reference_root = Path(settings.get("reference_root") or _DEFAULT_REFERENCE)
		self.script = Path(settings.get("script") or self.reference_root / "scripts" / "wcdb_server.js")
		self.data_dir = Path(str(settings.get("data_dir"))).expanduser() if settings.get("data_dir") else _default_data_dir()
		self.node = str(settings.get("node") or _DEFAULT_NODE)
		self.key = str(settings.get("key") or "").strip()
		self.key_file = Path(settings["key_file"]).expanduser() if settings.get("key_file") else None
		self.self_wxid = str(settings.get("self_wxid") or "").strip()
		self.retries = int(settings.get("retries") or 8)
		self.start_timeout = float(settings.get("start_timeout") or 120.0)
		self._process = None
		self._port = 0
		self._sessions_cache = None
		self._display_names = {}
		self._last_error = ""

	# ------------------------------------------------------------------ #
	# 生命周期
	# ------------------------------------------------------------------ #

	def _resolved_key(self) -> str:
		"""密钥解析顺序：配置 → 环境变量 → 桌面 key.txt → keymap.json 里唯一的那把。"""
		if self.key:
			return self.key
		env = os.environ.get("ATUOWEXIN_WCDB_KEY", "").strip()
		if env:
			return env
		candidates = list(_DEFAULT_KEY_FILES)
		if self.key_file:
			candidates.insert(0, self.key_file)
		for path in candidates:
			try:
				text = path.read_text(encoding="utf-8").strip()
			except OSError:
				continue
			if len(text) == 64 and all(ch in "0123456789abcdefABCDEF" for ch in text):
				return text
		try:
			from ..keyfinder import load_keymap

			mapping = load_keymap(Path.home() / ".atuowexin" / "keymap.json")
			unique = {value.lower() for value in mapping.values()}
			if len(unique) == 1:
				return unique.pop()
		except Exception:  # noqa: BLE001 - keymap 只是兜底
			pass
		return ""

	def _free_port(self) -> int:
		sock = socket.socket()
		sock.bind(("127.0.0.1", 0))
		port = sock.getsockname()[1]
		sock.close()
		return port

	def _call(self, path: str, timeout: float = 120.0) -> str:
		url = "http://127.0.0.1:%d/%s" % (self._port, path.lstrip("/"))
		with urllib.request.urlopen(url, timeout=timeout) as response:
			return response.read().decode("utf-8", "replace")

	def _spawn_once(self, key: str) -> bool:
		"""起一次子进程；能 READY + ping 通就返回 True。"""
		port = self._free_port()
		args = [self.node, str(self.script), key, str(port)]
		if self.data_dir:
			args.append(str(self.data_dir))
		try:
			process = subprocess.Popen(
				args,
				cwd=str(self.script.parent),
				stdout=subprocess.PIPE,
				stderr=subprocess.STDOUT,
			)
		except OSError as error:
			self._last_error = "启动 node 失败：%s" % error
			return False

		deadline = time.time() + self.start_timeout
		while time.time() < deadline:
			line = process.stdout.readline() if process.stdout else b""
			if line:
				text = line.decode("utf-8", "replace").strip()
				if text == "READY":
					self._process, self._port = process, port
					try:
						if self._call("ping", 10) == "pong":
							return True
					except Exception:  # noqa: BLE001
						pass
				if text.startswith(("OPEN_FAIL", "STAGING_FAIL", "DLL_LOAD_FAIL", "INIT_FAIL")):
					self._last_error = text
					break
			if process.poll() is not None and not line:
				break
		try:
			process.terminate()
		except OSError:
			pass
		return False

	def _ensure_started(self) -> None:
		if self._process is not None and self._process.poll() is None:
			return
		key = self._resolved_key()
		if not key:
			raise BackendError("没有可用密钥：先跑 key capture，或配置 backend.wcdb.key / 桌面 wx_export/key.txt")
		if not self.script.is_file():
			raise BackendError("找不到 wcdb_server.js：%s（用 backend.wcdb.script 指向参考工具的 scripts 目录）" % self.script)
		last = ""
		for attempt in range(1, max(1, self.retries) + 1):
			if self._spawn_once(key):
				return
			last = self._last_error or "未知原因"
			if "OPEN_FAIL" in last:
				# 微信正在写 -wal，快照可能不一致：稍等再试（实测抓到一致快照就能秒开）
				time.sleep(2)
		hint = "；微信运行时快照可能不一致，退出微信后重试最稳" if "OPEN_FAIL" in last else ""
		raise BackendError("WCDB 打不开库（%s）%s" % (last, hint))

	def close(self) -> None:
		"""结束子进程（幂等）。"""
		if self._process is not None:
			try:
				self._process.terminate()
			except OSError:
				pass
			self._process = None

	# ------------------------------------------------------------------ #
	# Backend 协议
	# ------------------------------------------------------------------ #

	def health(self) -> dict[str, Any]:
		"""探测：能起来 + 能列会话就算 ok。"""
		try:
			sessions = self._sessions()
			return {
				"ok": True,
				"detail": "WCDB 已连接（%d 个会话）" % len(sessions),
				"sessions": len(sessions),
			}
		except BackendError as error:
			return {"ok": False, "detail": str(error)}

	def _sessions(self):
		if self._sessions_cache is not None:
			return self._sessions_cache
		self._ensure_started()
		raw = self._call("sessions", 180)
		try:
			payload = json.loads(raw)
		except ValueError:
			raise BackendError("WCDB 返回的会话列表不是 JSON：%s" % raw[:120])
		if isinstance(payload, dict):
			payload = payload.get("sessions") or payload.get("list") or []
		self._sessions_cache = [item for item in payload if isinstance(item, dict)]
		return self._sessions_cache

	def _load_display_names(self, usernames):
		if not usernames:
			return {}
		body = json.dumps(usernames, ensure_ascii=False).encode("utf-8")
		url = "http://127.0.0.1:%d/displaynames" % self._port
		request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
		try:
			with urllib.request.urlopen(request, timeout=60) as response:
				payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
		except Exception:  # noqa: BLE001 - 昵称取不到不该让整条链路失败
			return {}
		if isinstance(payload, dict):
			for key, value in payload.items():
				if value:
					self._display_names[str(key)] = str(value)
		return self._display_names

	def list_conversations(self):
		raw_sessions = self._sessions()
		usernames = [str(_pick(item, "username", "user_name", "wxid", default="")) for item in raw_sessions]
		display = self._load_display_names([name for name in usernames if name])
		conversations = []
		for index, item in enumerate(raw_sessions):
			conv_id = usernames[index]
			if not conv_id or conv_id.startswith("brand"):
				continue
			label = display.get(conv_id) or conv_id
			conversations.append(
				Conversation(
					id=conv_id,
					name=label,
					remark="",
					kind="group" if conv_id.endswith("@chatroom") else "friend",
					last_time=normalize_ts(_pick(item, "last_timestamp", "sort_timestamp", "last_time", default=0)),
					msg_count=int(_pick(item, "message_count", "msg_count", default=0) or 0),
					extra={"summary": str(_pick(item, "summary", default=""))},
				)
			)
		return conversations

	def iter_messages(self, conversation_id, *, since: int = 0, until: int = 0, limit: int = 0):
		page = 500
		offset = 0
		emitted = 0
		quoted = urllib.parse.quote(conversation_id, safe="")
		while True:
			raw = self._call("messages/%s/%d/%d" % (quoted, page, offset), 300)
			try:
				batch = json.loads(raw)
			except ValueError:
				raise BackendError("WCDB 返回的消息不是 JSON：%s" % raw[:120])
			if not isinstance(batch, list) or not batch:
				return
			for item in batch:
				message = self._to_message(item, conversation_id)
				if since and message.ts < since:
					continue
				if until and message.ts > until:
					continue
				yield message
				emitted += 1
				if limit and emitted >= limit:
					return
			if len(batch) < page:
				return
			offset += len(batch)

	def _to_message(self, item, conversation_id):
		raw_type = _pick(item, "local_type", "type", default=0)
		try:
			msg_type = int(raw_type) & 0xFFFF
		except (TypeError, ValueError):
			msg_type = 0
		content = str(_pick(item, "message_content", "content", "text", default="") or "")
		kind = classify(msg_type, 0)
		text = content
		media = []
		if kind != "text":
			# 图片/文件等的正文是 XML：留给 media 引用，文字位留空
			media = media_refs_from_xml(content, kind)
			if content.lstrip().startswith("<"):
				text = ""
		sender = str(_pick(item, "sender_username", "sender", "username", default="") or "")
		return Message(
			conversation_id=conversation_id,
			seq=int(_pick(item, "sort_seq", "seq", "local_id", default=0) or 0),
			server_id=int(_pick(item, "server_id", "msg_id", default=0) or 0),
			sender_id=sender,
			sender_name=sender,
			is_self=bool(self.self_wxid and sender == self.self_wxid),
			ts=normalize_ts(_pick(item, "create_time", "createTime", "ts", default=0)),
			type=msg_type,
			sub_type=0,
			kind=kind,
			text=text,
			media=media,
			raw=item,
		)


def trial_open(settings=None, *, key: str = ""):
	"""用 WCDB 实际开一次库，判断这把 key 到底能不能用。

	这是替代「SQLCipher 页 1 HMAC 校验」的可用性判定：能列出会话才算真能用。
	"""
	settings = dict(settings or {})
	if key:
		settings["key"] = key
	backend = WcdbBackend(settings)
	try:
		sessions = backend._sessions()
		return {
			"ok": bool(sessions),
			"detail": ("WCDB 打开成功，%d 个会话" % len(sessions)) if sessions else "WCDB 打开成功但没有会话",
			"sessions": len(sessions),
		}
	except BackendError as error:
		return {"ok": False, "detail": str(error), "sessions": 0}
	finally:
		backend.close()


__all__ = ["WcdbBackend", "trial_open"]
