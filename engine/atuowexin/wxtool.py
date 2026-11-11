# -*- coding: utf-8 -*-
"""插件核心六个动作（一个动作 = 一个工具）。

功能只有一件事：读取微信聊天记录。六个动作按顺序串成一条流水线：

1. check    检测微信是否安装（没装就提示安装）
2. locate   找到聊天记录位置（db_storage / 账号目录）
3. close    微信在跑就先退出；没跑就跳过
4. hook     起 Hook 扫描 -> 拉起微信停在登录页 -> 等用户登录 -> 取密钥暂存（超时=未找到，回到 locate）
5. list     扫描会话名称与最新时间，交给上层
6. read     按会话名/时间批量读内容

不含：发送消息、导出 PDF/Excel、总结、自动化点击登录。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import autopilot, capture, paths
from .model import redact_pii

#: 默认扫描窗口（秒）——用户规定 5 分钟。
DEFAULT_SCAN_SECONDS = 300.0

_EXE_HINTS = (
    Path(r"C:\Program Files\Tencent\Weixin\Weixin.exe"),
    Path(r"C:\Program Files\Tencent\WeChat\WeChat.exe"),
)


def _key_files() -> list[Path]:
	home = Path.home()
	return [
		home / ".atuowexin" / "data" / "capture" / "key.txt",
		home / ".atuowexin" / "key.txt",
	]


def stored_key() -> str:
	"""读已暂存的密钥（64 位 hex），没有返回空串。"""
	for path in _key_files():
		try:
			text = path.read_text(encoding="utf-8").strip()
		except OSError:
			continue
		if len(text) == 64 and all(ch in "0123456789abcdefABCDEF" for ch in text):
			return text.lower()
	return ""


def store_key(key: str) -> str:
	"""暂存密钥（写两处：capture 目录与插件 home），返回写入的主路径。"""
	key = (key or "").strip().lower()
	paths_written = []
	for path in _key_files():
		try:
			path.parent.mkdir(parents=True, exist_ok=True)
			path.write_text(key + "\n", encoding="utf-8")
			try:
				os.chmod(path, 0o600)
			except OSError:
				pass
			paths_written.append(str(path))
		except OSError:
			continue
	return paths_written[0] if paths_written else ""


# --------------------------------------------------------------------------- #
# 1. check
# --------------------------------------------------------------------------- #


def check() -> dict[str, Any]:
	"""检测微信是否安装。没装就返回 installed=False + 安装提示。"""
	exe = autopilot.wechat_exe()
	installed = bool(exe)
	version = ""
	if exe:
		version = Path(exe).parent.name if Path(exe).parent.name[:1].isdigit() else ""
	roots = paths.detect_wechat_data_roots()
	payload = {
		"installed": installed,
		"exe": exe,
		"version": version,
		"running": autopilot.wechat_running(),
		"data_roots": [str(item.get("path")) for item in roots],
		"hint": "" if installed else "没有检测到微信：请先安装桌面版微信（Weixin.exe），装好后重新调用本工具。",
		"next": "wx_locate" if installed else "",
	}
	return payload


# --------------------------------------------------------------------------- #
# 2. locate
# --------------------------------------------------------------------------- #


def locate(root: str = "") -> dict[str, Any]:
	"""找到聊天记录位置：账号目录 + db_storage。"""
	if root:
		account = Path(root).expanduser()
		db_storage = account / "db_storage" if (account / "db_storage").is_dir() else account
		candidates = [{"account": str(account), "db_storage": str(db_storage)}]
	else:
		candidates = []
		for item in paths.detect_wechat_data_roots():
			base = Path(str(item.get("path")))
			for account in sorted(base.iterdir()) if base.is_dir() else []:
				if account.is_dir() and (account / "db_storage").is_dir():
					candidates.append({"account": str(account), "db_storage": str(account / "db_storage")})
			if candidates:
				break

	if not candidates:
		return {
			"found": False,
			"hint": "没找到聊天记录目录：确认微信已登录过（会生成 xwechat_files/<wxid>/db_storage），或用 root 参数指定账号目录。",
			"next": "wx_check",
		}

	target = candidates[0]
	dbs = list(Path(target["db_storage"]).rglob("*.db"))
	target.update(
		{
			"found": True,
			"db_count": len(dbs),
			"has_session_db": (Path(target["db_storage"]) / "session" / "session.db").is_file(),
			"key_present": bool(stored_key()),
			"next": "wx_close" if autopilot.wechat_running() else "wx_hook",
		}
	)
	return target


# --------------------------------------------------------------------------- #
# 3. close
# --------------------------------------------------------------------------- #


def close() -> dict[str, Any]:
	"""微信在跑就退出；没跑直接返回（不算错）。"""
	was_running = autopilot.wechat_running()
	if not was_running:
		return {"closed": True, "was_running": False, "hint": "微信没在运行，跳过。", "next": "wx_hook"}
	ok = autopilot.stop_wechat()
	return {
		"closed": ok,
		"was_running": True,
		"hint": "" if ok else "关不掉微信（可能被托盘/保护进程占用），请手动在托盘右键退出。",
		"next": "wx_hook" if ok else "",
	}


# --------------------------------------------------------------------------- #
# 4. hook
# --------------------------------------------------------------------------- #


def hook(
	root: str,
	*,
	scan_seconds: float = DEFAULT_SCAN_SECONDS,
	workdir=None,
) -> dict[str, Any]:
	"""起 Hook → 拉起微信停在登录页 → 等用户登录 → 截密钥并暂存。

	扫描窗口默认 5 分钟；超时返回 found=False（未找到），并提示回到 wx_locate。
	不自动点登录：停在登录页由用户操作。
	"""
	import threading

	account = locate(root)
	if not account.get("found"):
		return {"found": False, "stage": "locate_failed", "hint": account.get("hint", ""), "next": "wx_locate"}

	if autopilot.wechat_running():
		return {"found": False, "stage": "wechat_running", "hint": "微信正在运行：先调用 wx_close 退出，再调用本工具。", "next": "wx_close"}

	box: dict[str, Any] = {}

	def _capture() -> None:
		box["result"] = capture.capture_key(
			Path(account["account"]),
			Path(workdir or paths.default_workdir()) / "keymap.json",
			workdir=workdir,
			timeout=float(scan_seconds) + 30.0,
			login_wait=float(scan_seconds),
		)

	worker = threading.Thread(target=_capture, name="wxtool-hook", daemon=True)
	worker.start()
	time.sleep(2.0)

	exe = autopilot.wechat_exe()
	if not exe or not autopilot.start_wechat(exe):
		return {"found": False, "stage": "start_failed", "hint": "起步了微信启动，请手动打开微信并登录。", "next": "wx_hook"}

	worker.join(timeout=float(scan_seconds) + 60.0)
	result = box.get("result")
	if result is None:
		return {"found": False, "stage": "timeout", "hint": "扫描超时，未返回结果；请确认微信已停在登录页后重试。", "next": "wx_locate"}

	if not result.ok:
		return {
			"found": False,
			"stage": result.stage or "not_found",
			"hint": result.error or "扫描窗口内没拿到密钥。",
			"user_action": result.user_action,
			"next": "wx_locate",
		}

	path = store_key(result.key)
	return {
		"found": True,
		"stage": "captured",
		"key_prefix": result.key[:8] + "…",
		"key_file": path,
		"validation_method": result.validation_method,
		"next": "wx_list",
	}


# --------------------------------------------------------------------------- #
# 5. list / 6. read
# --------------------------------------------------------------------------- #


def _backend():
	from .backends.wcdb_server import WcdbBackend

	key = stored_key()
	if not key:
		raise RuntimeError("没有暂存的密钥：先调用 wx_hook 完成扫描并暂存。")
	account = locate("")
	if not account.get("found"):
		raise RuntimeError(account.get("hint") or "找不到聊天记录目录")
	return WcdbBackend({"key": key, "data_dir": account["account"], "retries": 4})


def _fmt(ts: int) -> str:
	if not ts:
		return ""
	try:
		return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
	except (OSError, ValueError):
		return ""


def list_conversations(*, limit: int = 200, query: str = "") -> dict[str, Any]:
	"""扫描会话：名称 + 最新时间，输出给上层。"""
	backend = _backend()
	try:
		conversations = backend.list_conversations()
		if query:
			needle = query.lower()
			conversations = [c for c in conversations if needle in c.display_name.lower() or needle in c.id.lower()]
		conversations.sort(key=lambda c: c.last_time, reverse=True)
		items = [
			{"name": c.display_name, "id": c.id, "last_time": c.last_time, "last_time_text": _fmt(c.last_time)}
			for c in conversations[: max(1, int(limit))]
		]
		return {"count": len(items), "total": len(conversations), "conversations": items}
	finally:
		backend.close()


def read_conversations(
	names: Sequence[str],
	*,
	since: int = 0,
	until: int = 0,
	limit: int = 200,
	redacted: bool = False,
) -> dict[str, Any]:
	"""按会话名（或 id）批量读内容；每个会话最多 limit 条。

	redacted=True 时对正文做 PII 脱敏（手机/证件/银行卡/邮箱/wxid/车牌）。
	"""
	backend = _backend()
	try:
		all_conversations = backend.list_conversations()
		result: list[dict[str, Any]] = []
		for raw_name in names:
			needle = str(raw_name).strip()
			if not needle:
				continue
			matched = [
				c for c in all_conversations
				if needle == c.display_name or needle == c.id or needle.lower() in c.display_name.lower() or needle.lower() in c.id.lower()
			]
			if not matched:
				result.append({"name": needle, "found": False, "messages": []})
				continue
			target = matched[0]
			collected = list(backend.iter_messages(target.id, since=since, until=until, limit=limit))
			# 群聊里 sender 只有 wxid：用通讯录接口换成昵称/备注
			senders = sorted({m.sender_id for m in collected if m.sender_id})
			names = backend._load_display_names(senders) if senders else {}
			bucket: list[dict[str, Any]] = []
			for message in collected:
				text = message.text or ""
				if message.kind != "text":
					# 非文本正文是 XML：不塞进 text，给一个占位
					text = "[%s]" % message.kind
				bucket.append(
					{
						"ts": message.ts,
						"time": _fmt(message.ts),
						"sender": names.get(message.sender_id) or message.sender_name or message.sender_id,
						"sender_id": message.sender_id,
						"is_self": message.is_self,
						"kind": message.kind,
						"text": (redact_pii(text) if redacted else text),
					}
				)
			result.append(
				{
					"name": target.display_name,
					"id": target.id,
					"found": True,
					"count": len(bucket),
					"messages": bucket,
				}
			)
		return {"requested": len(names), "conversations": result}
	finally:
		backend.close()


# --------------------------------------------------------------------------- #
# 8. unread：只看未读
# --------------------------------------------------------------------------- #


def default_decrypted_dir() -> Path:
	"""解密产物的默认位置：<workdir>/decrypted，其次账号目录旁边的 decrypted_local。"""
	candidates = [Path(paths.default_workdir()) / "decrypted"]
	account = locate("")
	if account.get("found"):
		base = Path(account["account"])
		candidates.extend([base / "decrypted", base / "db_storage" / ".." / "decrypted_local"])
	for item in candidates:
		try:
			if (item / "session" / "session.db").is_file():
				return item.resolve()
		except OSError:
			continue
	return candidates[0]


def _contact_names(db: Path) -> dict:
	"""从明文 contact.db 取 username -> 备注/昵称。"""
	import sqlite3

	names = {}
	try:
		conn = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True)
		tables = [row[0] for row in conn.execute("select name from sqlite_master where type='table'")]
		for table in tables:
			cols = [row[1] for row in conn.execute('PRAGMA table_info("%s")' % table)]
			if "username" in cols and ("remark" in cols or "nick_name" in cols):
				nick = "nick_name" if "nick_name" in cols else "username"
				remark = "remark" if "remark" in cols else nick
				for user, rem, nk in conn.execute('select username, "%s", "%s" from "%s"' % (remark, nick, table)):
					names[user] = rem or nk or user
				break
		conn.close()
	except Exception:
		pass
	return names


def unread(decrypted_dir: str = "", *, limit: int = 50, redacted: bool = False) -> dict:
	"""只看未读：从解密后的 session.db 读 unread_count>0 的会话。"""
	import sqlite3

	root = Path(decrypted_dir) if decrypted_dir else default_decrypted_dir()
	session_db = root / "session" / "session.db"
	if not session_db.is_file():
		return {
			"ok": False,
			"hint": "还没有解密数据：先跑 wx decrypt（工具 7），或用 --decrypted-dir 指定解密目录。",
			"decrypted_dir": str(root),
		}

	conn = sqlite3.connect("file:%s?mode=ro" % session_db.as_posix(), uri=True)
	rows = conn.execute(
		"select username, unread_count, last_timestamp, summary from SessionTable "
		"where unread_count > 0 order by last_timestamp desc limit ?",
		(max(1, int(limit)),),
	).fetchall()
	conn.close()

	names = _contact_names(root / "contact" / "contact.db")
	items = []
	for username, count, ts, summary in rows:
		text = str(summary or "")
		if redacted:
			text = redact_pii(text)
		items.append(
			{
				"conversation": names.get(username) or username,
				"id": username,
				"unread": int(count or 0),
				"last_time": int(ts or 0),
				"last_time_text": _fmt(int(ts or 0)),
				"summary": text[:200],
			}
		)
	total = sum(item["unread"] for item in items)
	return {
		"ok": True,
		"conversations": len(items),
		"unread_total": total,
		"items": items,
		"decrypted_dir": str(root),
	}


__all__ = [
	"DEFAULT_SCAN_SECONDS",
	"default_decrypted_dir",
	"unread",
	"check",
	"close",
	"hook",
	"list_conversations",
	"locate",
	"read_conversations",
	"store_key",
	"stored_key",
]
