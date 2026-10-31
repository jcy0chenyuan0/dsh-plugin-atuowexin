# -*- coding: utf-8 -*-
"""路径解析：引擎工作目录、会话隔离目录、微信数据目录探测。

微信 4.x 换了目录名与结构（``xwechat_files`` + ``db_storage``），3.x 是
``WeChat Files`` + ``Msg/Multi/MSG*.db``。这里只负责**找得到**，解释结构是
``backends.decrypted_sqlite`` 的事。
"""

from __future__ import annotations

import os
from pathlib import Path


def atuowexin_home() -> Path:
	"""引擎主目录：``ATUOWEXIN_HOME`` 或 ``~/.atuowexin``。"""
	configured = os.environ.get("ATUOWEXIN_HOME")
	if configured:
		return Path(configured).expanduser()
	return Path.home() / ".atuowexin"


def config_path() -> Path:
	"""配置文件路径：``ATUOWEXIN_CONFIG`` 或 ``<home>/config.json``。"""
	configured = os.environ.get("ATUOWEXIN_CONFIG")
	if configured:
		return Path(configured).expanduser()
	return atuowexin_home() / "config.json"


def default_workdir() -> Path:
	"""默认工作目录：``ATUOWEXIN_WORKDIR`` 或 ``<home>/data``。"""
	configured = os.environ.get("ATUOWEXIN_WORKDIR")
	if configured:
		return Path(configured).expanduser()
	return atuowexin_home() / "data"


def ensure_dir(path: str | os.PathLike[str]) -> Path:
	"""确保目录存在并返回它（幂等）。"""
	target = Path(path)
	target.mkdir(parents=True, exist_ok=True)
	return target


# --------------------------------------------------------------------------- #
# 工作目录内布局
# --------------------------------------------------------------------------- #


def db_path(workdir: str | os.PathLike[str]) -> Path:
	"""归一化数据库：``<workdir>/atuowexin.db``。"""
	return Path(workdir) / "atuowexin.db"


def conversations_root(workdir: str | os.PathLike[str]) -> Path:
	"""会话隔离工作区根目录：``<workdir>/conversations``。"""
	return Path(workdir) / "conversations"


def conversation_dir(workdir: str | os.PathLike[str], slug: str) -> Path:
	"""某会话的隔离目录。``slug`` 由 :func:`atuowexin.model.conversation_slug` 生成。"""
	return conversations_root(workdir) / slug


def conversation_workspace(workdir: str | os.PathLike[str], conversation) -> Path:
	"""按会话对象取隔离目录（自动用 :attr:`Conversation.slug`）。"""
	slug = getattr(conversation, "slug", None) or str(conversation)
	return conversation_dir(workdir, slug)


def exports_root(workdir: str | os.PathLike[str]) -> Path:
	"""完整摘录导出根目录：``<workdir>/exports``。"""
	return Path(workdir) / "exports"


def logs_dir(workdir: str | os.PathLike[str]) -> Path:
	"""日志目录：``<workdir>/logs``。"""
	return Path(workdir) / "logs"


WORKSPACE_FILES = {
	"messages": "messages.jsonl",
	"summary_md": "summary.md",
	"summary_json": "summary.json",
	"profile": "profile.json",
	"notes": "notes.md",
	"drafts": "drafts",
	"exports": "exports",
}


# --------------------------------------------------------------------------- #
# 微信数据目录探测（只探测，不解密）
# --------------------------------------------------------------------------- #

#: 常见 4.x 根目录名。
V4_DIRNAME = "xwechat_files"
#: 常见 3.x 根目录名。
V3_DIRNAME = "WeChat Files"


def _candidate_bases() -> list[Path]:
	"""可能放着微信数据目录的「上级目录」清单。"""
	bases: list[Path] = []
	home = Path.home()
	for name in ("Documents", "文档", "OneDrive/Documents", "OneDrive/文档"):
		bases.append(home / name)
		# 微信 4.x 允许自定义父目录，实测常见形态是 Documents\Tencent Files\xwechat_files
		bases.append(home / name / "Tencent Files")
	# 盘符根下的常见自定义安装位置
	for drive in ("C:", "D:", "E:"):
		bases.append(Path(f"{drive}/"))
		bases.append(Path(f"{drive}/WeChat"))
		bases.append(Path(f"{drive}/Tencent Files"))
	registered = _registered_save_path()
	if registered is not None:
		bases.insert(0, registered)
	return bases


def _registered_save_path() -> Path | None:
	"""读注册表里 3.x 记录的 ``FileSavePath``（Windows 专属，失败即返回 None）。"""
	if os.name != "nt":
		return None
	try:
		import winreg  # type: ignore[import-not-found]
	except ImportError:
		return None
	try:
		with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeChat") as key:
			value, _ = winreg.QueryValueEx(key, "FileSavePath")
	except OSError:
		return None
	if not value or value in {"MyDocument:", ""}:
		return create_documents_dir()
	candidate = Path(value)
	if candidate.exists():
		return candidate
	return None


def create_documents_dir() -> Path | None:
	"""``FileSavePath`` 常写成 ``MyDocument:``，此时回落到「文档」目录。"""
	home = Path.home()
	for name in ("Documents", "文档"):
		candidate = home / name
		if candidate.is_dir():
			return candidate
	return None


def detect_wechat_data_roots() -> list[dict[str, object]]:
	"""探测本机的微信数据根目录。

	:return: ``[{"path": Path, "schema": "v4"|"v3", "accounts": [目录名...]}]``；
		找不到就返回空列表——插件会据此给出「请先用外部工具解密并指定目录」的指引。
	"""
	found: list[dict[str, object]] = []
	seen: set[str] = set()

	def _consider(candidate: Path, schema: str) -> None:
		"""收录一个候选目录（去重、只收真实存在的目录）。"""
		try:
			if not candidate.is_dir():
				return
		except OSError:
			return
		key = str(candidate).lower()
		if key in seen:
			return
		seen.add(key)
		found.append({"path": candidate, "schema": schema, "accounts": list_accounts(candidate, schema)})

	for base in _candidate_bases():
		for dirname, schema in ((V4_DIRNAME, "v4"), (V3_DIRNAME, "v3")):
			_consider(base / dirname, schema)
		# 「文档」下可能还有一层自定义父目录（例如 Tencent Files），再往下探一层
		if base.name in {"Documents", "文档"}:
			try:
				children = [child for child in base.iterdir() if child.is_dir() and not child.name.startswith(".")]
			except OSError:
				children = []
			for child in children:
				_consider(child / V4_DIRNAME, "v4")
				_consider(child / V3_DIRNAME, "v3")
	return found


def list_accounts(root: Path, schema: str) -> list[str]:
	"""列出数据根目录下的账号目录（wxid 或其哈希）。"""
	accounts: list[str] = []
	try:
		for child in sorted(root.iterdir()):
			if not child.is_dir():
				continue
			name = child.name
			if name.startswith(("all_users", "applet", "temp", "crash")):
				continue
			if schema == "v4" and (child / "db_storage").is_dir():
				accounts.append(name)
			elif schema == "v3" and ((child / "Msg").is_dir() or (child / "FileStorage").is_dir()):
				accounts.append(name)
	except OSError:
		return []
	return accounts


def looks_like_decrypted_root(path: str | os.PathLike[str]) -> str:
	"""判断一个目录更像哪种结构，返回 ``"v4"`` / ``"v3"`` / ``"unknown"``。

	用于 ``backend.decrypted_sqlite.schema=auto`` 的探测。
	"""
	target = Path(path)
	if not target.exists():
		return "unknown"
	probes = [target]
	try:
		probes.extend([child for child in target.iterdir() if child.is_dir()])
		probes.extend([child for child in target.glob("*/*") if child.is_dir()])
	except OSError:
		pass
	for probe in probes:
		try:
			if (probe / "db_storage").is_dir():
				return "v4"
		except OSError:
			continue
	for probe in probes:
		try:
			if (probe / "Msg").is_dir() or (probe / "Multi").is_dir() or (probe / "MSG0.db").is_file():
				return "v3"
		except OSError:
			continue
	# 再看有没有 4.x 的库文件名
	for probe in probes:
		try:
			names = {child.name.lower() for child in probe.iterdir()}
		except OSError:
			continue
		if {"session.db", "contact.db"} & names:
			return "v4"
		if any(name.startswith("msg") and name.endswith(".db") for name in names):
			return "v3"
	return "unknown"


__all__ = [
	"atuowexin_home",
	"config_path",
	"default_workdir",
	"ensure_dir",
	"db_path",
	"conversations_root",
	"conversation_dir",
	"conversation_workspace",
	"exports_root",
	"logs_dir",
	"WORKSPACE_FILES",
	"detect_wechat_data_roots",
	"list_accounts",
	"looks_like_decrypted_root",
]
