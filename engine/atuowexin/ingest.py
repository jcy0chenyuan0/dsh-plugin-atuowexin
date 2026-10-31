# -*- coding: utf-8 -*-
"""一键接入真实数据（ingest）：**解密 → 校验 → 接上插件 → 全量摘录**。

这个模块存在的唯一理由：把"拿到密钥之后"压缩成一次调用。真实微信库的接入顺序里
最容易出错的几步都在这里被固定下来：

1. **先用一个库探针验证密钥**。密钥形态搞混（raw key ↔ passphrase）是这个领域最高频的
   坑，而 HMAC 校验会立刻给出确定答案——所以先花 1 秒验一个最小的库（``session.db``），
   再决定要不要解密整个目录，避免解了 20 个库才发现密钥不对。
2. **`-wal` 逐一判定**。SQLCipher 库里常见几十 MB 的 WAL，但它们的**性质完全不同**：
   一种装着尚未 checkpoint 的已提交数据（必须合并，否则最新消息丢失），
   另一种是 SQLite 重置过的残留帧（头部 salt 与帧 salt 不一致，内容早已进主库，
   **照抄会写坏库**）。判定必须在解密前做，且按 sqlite3 自己的规则（salt 匹配）
   逐帧判断，而不是看到非空 WAL 就警告或就合并。
3. **解密产物落地 + 配置改写 + 全量摘录**一次完成，并回报每一步的计数。

本模块只调用 :mod:`atuowexin.sqlcipher`（它自己不做任何密钥提取），
因此"密钥从哪来"始终是使用者的决定。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import config as config_module
from . import paths
from . import sqlcipher
from .backends import build_backend
from .backends.decrypted_sqlite import strip_account_suffix
from .extract import sync
from .store import open_store

#: 探针优先使用的库：越小越好，``session.db`` 通常最小且足以判定密钥。
PROBE_PREFERENCE = ("session.db", "contact.db", "general.db", "message_0.db")


@dataclass
class IngestReport:
	"""一次 ingest 的完整结果（可直接 JSON 序列化）。"""

	ok: bool = False
	stage: str = ""
	source: str = ""
	out: str = ""
	key_type: str = ""
	self_wxid: str = ""
	error: str = ""
	hint: str = ""
	probe: dict[str, Any] = field(default_factory=dict)
	decrypted: list[dict[str, Any]] = field(default_factory=list)
	decrypted_ok: int = 0
	decrypted_failed: int = 0
	keymap_entries: int = 0
	#: 没给密钥时自动取到的结果（ensure_keys 的报告）；没走这条路时为空
	auto_key: dict[str, Any] = field(default_factory=dict)
	#: 实际使用的 keymap 路径（自动取到时为 <atuowexin home>/keymap.json）
	keymap: str = ""
	wal_files: list[str] = field(default_factory=list)
	wal_merged: int = 0
	wal_stale: int = 0
	wal_applied_frames: int = 0
	wal_failed: list[str] = field(default_factory=list)
	config_path: str = ""
	sync: dict[str, Any] = field(default_factory=dict)
	elapsed_ms: int = 0

	def to_dict(self) -> dict[str, Any]:
		"""转成可 JSON 序列化的字典。"""
		return {
			"ok": self.ok,
			"stage": self.stage,
			"source": self.source,
			"out": self.out,
			"key_type": self.key_type,
			"self_wxid": self.self_wxid,
			"error": self.error,
			"hint": self.hint,
			"probe": self.probe,
			"decrypted_ok": self.decrypted_ok,
			"decrypted_failed": self.decrypted_failed,
			"keymap_entries": self.keymap_entries,
			"auto_key": self.auto_key,
			"keymap": self.keymap,
			"decrypted": self.decrypted[:50],
			"wal_files": self.wal_files[:50],
			"wal_merged": self.wal_merged,
			"wal_stale": self.wal_stale,
			"wal_applied_frames": self.wal_applied_frames,
			"wal_failed": self.wal_failed[:20],
			"config_path": self.config_path,
			"sync": self.sync,
			"elapsed_ms": self.elapsed_ms,
		}

	def render(self) -> str:
		"""渲染成给人看的文本。"""
		lines: list[str] = []
		if not self.ok:
			lines.append(f"接入失败（阶段：{self.stage or 'unknown'}）")
			if self.error:
				lines.append(f"原因：{self.error}")
			if self.hint:
				lines.append(f"下一步：{self.hint}")
			if self.probe:
				lines.append(f"探针：{self.probe.get('src', '')} -> {self.probe.get('error', '')}")
			return "\n".join(lines)

		lines.append(f"密钥验证通过（形态：{self.key_type}）")
		if self.keymap_entries:
			lines.append(f"密钥来源：每库一把 key（keymap 共 {self.keymap_entries} 把，按 salt 匹配）")
		if self.self_wxid:
			lines.append(f"识别到本账号 wxid：{self.self_wxid}（用于判定哪条是「我」发的）")
		lines.append(f"解密：成功 {self.decrypted_ok} 个库，失败 {self.decrypted_failed} 个 → {self.out}")
		if self.wal_files:
			if self.wal_merged:
				lines.append(
					f"WAL 合并：{self.wal_merged} 个库有**未 checkpoint 的新数据**，共回放 "
					f"{self.wal_applied_frames} 帧（这些消息/改动如果没有合并就会丢）"
				)
			if self.wal_stale:
				lines.append(
					f"WAL 忽略：{self.wal_stale} 个 -wal 是 SQLite **重置过的残留帧**"
					"（头部 salt 与帧 salt 不一致），内容早已 checkpoint 进主库；"
					"照抄它们只会把库写坏，本次已按 sqlite3 的规则忽略。"
				)
			if not self.wal_merged and not self.wal_stale:
				lines.append(f"WAL：{len(self.wal_files)} 个 -wal 文件里没有已提交的新帧，主库已是最新")
		if self.wal_failed:
			lines.append("WAL 合并失败（主库仍可用，但最新改动可能缺失）：")
			for item in self.wal_failed[:10]:
				lines.append(f"  - {item}")
		if self.config_path:
			lines.append(f"已把 backend.decrypted_sqlite.root 写入配置：{self.config_path}")
		if self.sync:
			lines.append(
				f"摘录：{self.sync.get('conversations', 0)} 个会话，读取 {self.sync.get('fetched', 0)} 条，"
				f"新增 {self.sync.get('added', 0)} 条"
			)
			for item in self.sync.get("details", [])[:12]:
				flag = f" ❌ {item['error']}" if item.get("error") else ""
				lines.append(f"  - {item.get('display_name', '')}（{item.get('conversation_id', '')}）：新增 {item.get('added', 0)}{flag}")
		lines.append(f"耗时 {self.elapsed_ms} ms")
		return "\n".join(lines)


def detect_account_id(root: str | Path) -> str:
	"""从数据目录推导"本账号 wxid"。

	微信 4.x 的账号目录形如 ``wxid_ab12cd34ef56gh78_f4b1``（尾部是目录哈希），
	而 ``Name2Id`` 里存的是不带哈希的真 wxid，所以必须剥掉尾部哈希，
	否则 ``is_self`` 永远判不出来、把用户自己发的消息都算成对方发的。

	:param root: 加密数据目录（账号目录、``xwechat_files`` 或 ``db_storage`` 都可以）。
	:return: 账号 wxid；推不出来返回空串（此时宁可不判 self，也不乱认）。
	"""
	candidate = Path(root).expanduser()
	for path in [candidate, *candidate.parents]:
		name = path.name
		if name.startswith(("wxid_", "gh_")):
			return strip_account_suffix(name)
	return ""


def db_salt(path: str | Path) -> str:
	"""读库文件第 1 页前 16 字节的 salt，返回小写 hex。

	**keymap 的键就是这个 salt** —— 微信 4.x 每库独立加密，salt 各不相同，
	所以"哪把 key 解哪个库"完全由这个 16 字节决定。
	"""
	with open(Path(path), "rb") as handle:
		return handle.read(16).hex()


def load_keymap(value: dict[str, str] | str | Path | None) -> dict[str, str]:
	"""加载"每库一把 key"的映射 ``{salt_hex: raw key}``。

	:param value: 映射本身，或 keymap JSON 文件路径（``None``/空表示没有）。
	:return: 归一化后的映射（键转小写、剥掉 ``x'...'`` 包裹）。
	:raises ValueError: 文件不存在，或内容不是 JSON 对象。
	"""
	if not value:
		return {}
	if isinstance(value, dict):
		raw: Any = value
	else:
		path = Path(value).expanduser()
		if not path.is_file():
			raise ValueError(f"keymap 文件不存在：{path}")
		try:
			raw = json.loads(path.read_text(encoding="utf-8"))
		except (OSError, ValueError) as error:
			raise ValueError(f"keymap 不是合法 JSON：{path}（{error}）") from error
	if not isinstance(raw, dict):
		raise ValueError("keymap 顶层必须是 JSON 对象：{salt: key}")
	table: dict[str, str] = {}
	for name, secret in raw.items():
		if not isinstance(secret, str):
			continue
		salt = str(name).strip().lower()
		key = secret.strip()
		if key.startswith("x'") and key.endswith("'"):
			key = key[2:-1]  # WCDB 在内存里就是 x'<hex>' 这种 SQL 字面量形态
		if salt and key:
			table[salt] = key
	return table


def _find_probe(encrypted_root: Path, keymap: dict[str, str] | None = None) -> Path | None:
	"""挑一个最小的库当探针。

	给了 keymap 时，**只能在 keymap 里有密钥的库里挑**：每库一把 key 的情况下，
	拿一个没有密钥的库当探针只会得到误导性的 "hmac mismatch"。
	"""
	candidates: list[Path] = []
	for name in PROBE_PREFERENCE:
		candidates.extend(sorted(encrypted_root.rglob(name)))
	if not candidates:
		candidates = sorted(encrypted_root.rglob("*.db"))
	if keymap:
		usable = [item for item in candidates if db_salt(item) in keymap]
		if usable:
			candidates = usable
	candidates.sort(key=lambda item: item.stat().st_size)
	return candidates[0] if candidates else None


def _decrypt_tree_with_keymap(
	root: Path,
	out_root: Path,
	keymap: dict[str, str],
	*,
	version: int,
	fallback_key: str = "",
	key_type: str = "auto",
	timeout: int = 1800,
	wal: bool = True,
	progress: Callable[[str], None] | None = None,
) -> list[Any]:
	"""逐库按 salt 取 key 解密（keymap 是"每库一把"，不能像单 key 那样一把解全部）。"""
	from . import sqlcipher as _sqlcipher

	results: list[Any] = []
	for path in sorted(root.rglob("*.db")):
		relative = path.relative_to(root)
		target = out_root / relative
		salt = db_salt(path)
		mapped = keymap.get(salt)
		if mapped:
			result = _sqlcipher.decrypt_file(path, target, key=mapped, key_type="raw", version=version, timeout=timeout, wal=wal)
		elif fallback_key:
			result = _sqlcipher.decrypt_file(path, target, key=fallback_key, key_type=key_type, version=version, timeout=timeout, wal=wal)
		else:
			result = _sqlcipher.DecryptResult(
				src=str(path),
				dst=str(target),
				ok=False,
				version=version,
				key_type="raw",
				error=f"keymap 里没有该库的密钥（salt={salt}）",
			)
		results.append(result)
		if progress is not None:
			progress(f"  {relative}: {'OK' if result.ok else result.error}")
	return results


def ingest(
	config: dict[str, Any],
	*,
	source: str | Path,
	key: str = "",
	key_type: str = "auto",
	keymap: dict[str, str] | str | Path | None = None,
	version: int = 4,
	out: str | Path | None = None,
	probe_only: bool = False,
	run_sync: bool = True,
	full: bool = True,
	persist: bool = True,
	wal: bool = True,
	auto_key: bool = True,
	auto_key_deep: bool = False,
	auto_key_watch_seconds: float = 0.0,
	config_path: str | Path | None = None,
	progress: Callable[[str], None] | None = None,
) -> IngestReport:
	"""把加密的微信数据目录接入插件。

	:param config: 已加载的配置（会被就地更新 ``backend`` 段）。
	:param source: **加密的**数据目录（例如 ``...\\db_storage`` 或账号目录）。
	:param key: 64 位十六进制 raw key 或 passphrase（单把 key 模式）。
	:param key_type: ``auto`` / ``raw`` / ``passphrase``；``auto`` 会两种形态各试一次。
	:param keymap: **每库一把 key** 的映射：``{salt_hex: raw key}`` 或 keymap JSON 路径。
		给了它就以"按 salt 逐库取 key"的方式解密（微信 4.x 每库独立加密时唯一可行的方式）；
		某个库没在映射里、而 ``key`` 也给了的话，会退回用 ``key`` 试。
	:param version: SQLCipher 参数集，微信 4.x 用 4，3.x 用 3。
	:param out: 解密产物目录，默认 ``<workdir>/decrypted``。
	:param probe_only: 只验证密钥，不解密其余库。
	:param run_sync: 解密后是否立刻全量摘录。
	:param full: 摘录是否忽略游标。
	:param persist: 是否把 ``backend.decrypted_sqlite.root`` 写回配置文件。
	:param config_path: 配置文件路径（默认 ``~/.atuowexin/config.json``）。
	:param progress: 进度回调。
	:return: :class:`IngestReport`。
	"""
	started = time.time()
	workdir = Path(str(config.get("workdir") or "."))
	encrypted_root = Path(source).expanduser()
	target_root = Path(out).expanduser() if out is not None else workdir / "decrypted"
	report = IngestReport(source=str(encrypted_root), out=str(target_root), stage="probe")

	def _say(message: str) -> None:
		if progress is not None:
			progress(message)

	if not sqlcipher.is_available():
		report.error = "未找到可用的 PowerShell（pwsh 或系统自带 powershell.exe），内置解密不可用。"
		report.hint = "改用外部工具解密后，直接配置 backend.decrypted_sqlite.root。"
		return _finish(report, started)
	if not encrypted_root.is_dir():
		report.error = f"源目录不存在：{encrypted_root}"
		return _finish(report, started)
	try:
		keymap_table = load_keymap(keymap)
	except ValueError as error:
		report.error = str(error)
		report.hint = "keymap 形如 {\"<库salt的hex>\": \"<64位hex raw key>\", ...}；salt 就是库文件第 1 页前 16 字节。"
		return _finish(report, started)
	report.keymap_entries = len(keymap_table)

	# 没给密钥时**自动去取**（"每次使用之前先看有没有 key，缺了才去读"）：
	# 先看标准位置有没有现成的 keymap，缺的库再尝试从微信进程内存里提取。
	if not keymap_table and not str(key or "").strip() and auto_key:
		from . import keyfinder

		home_keymap = paths.atuowexin_home() / "keymap.json"
		seed_paths = []
		env_seed = os.environ.get("ATUOWEXIN_KEYMAP")
		if env_seed:
			seed_paths.append(env_seed)
		workdir_value = str((config or {}).get("workdir") or "")
		if workdir_value:
			seed_paths.append(Path(workdir_value) / "keymap.json")
		seed_paths.extend([home_keymap])

		# 先看有没有现成的 keymap 当路标：有路标时提取只要十几秒；
		# 没有路标时通用锚点扫描每个库要几十 MB（分钟级），所以**默认不硬扫**，
		# 要扫得显式开 --auto-key-deep（诚实：不让 insest 因为一件可选的优化而卡十分钟）。
		seeds = {}
		for item in seed_paths:
			for salt, value in keyfinder.load_keymap(item).items():
				seeds.setdefault(salt, value)
		if seeds or auto_key_deep:
			_say(f"没有提供密钥 → 自动检查（{'有 ' + str(len(seeds)) + ' 把现成密钥当路标' if seeds else '无路标，深扫模式'}）")
			ensured = keyfinder.ensure_keys(
				encrypted_root,
				keymap_path=home_keymap,
				version=version,
				seed_paths=seed_paths,
				use_anchors=bool(auto_key_deep),
				watch_seconds=float(auto_key_watch_seconds or 0.0),
				progress=_say,
			)
			report.auto_key = ensured.to_dict()
			if ensured.covered:
				keymap_table = {}
				for item in [home_keymap, *seed_paths]:
					for salt, value in keyfinder.load_keymap(item).items():
						keymap_table.setdefault(salt, value)
				report.keymap_entries = len(keymap_table)
				report.keymap = str(home_keymap)
				_say(f"自动密钥：{ensured.detail}")
				if ensured.written:
					_say(f"已写入 keymap：{home_keymap}（⚠️ 该文件无法轮换，请妥善保管）")
			else:
				_say(f"自动密钥没取齐：{ensured.detail}")
				if ensured.hint:
					_say(f"→ {ensured.hint}")
		else:
			report.auto_key = {
				"ok": False,
				"detail": "没有现成 keymap 当路标；未做深扫（避免一次 ingest 卡几分钟）",
				"hint": "要用通用锚点深扫请加 --auto-key-deep，或先跑 `key find`。",
			}
			_say("没有现成 keymap 当路标 → 跳过自动提取（要深扫加 --auto-key-deep）")

	if not keymap_table and not str(key or "").strip():
		report.error = "缺少密钥：请给 --key（单把）或 --keymap（每库一把）"
		report.hint = (
			"也可以不手动给：跑 `python -m atuowexin key ensure --root <db_storage>` 让它"
			"先检查、缺了自动从微信进程内存里读（要求微信在运行）。"
		)
		return _finish(report, started)
	if keymap_table:
		_say(f"已加载 keymap：{len(keymap_table)} 把密钥（按库 salt 匹配）")

	# 1) 探针：先验密钥，再决定要不要解全部
	probe_source = _find_probe(encrypted_root, keymap_table)
	if probe_source is None:
		report.error = f"该目录下没有找到任何 .db：{encrypted_root}"
		report.hint = "请指向 db_storage（或包含 .db 的目录）。"
		return _finish(report, started)
	probe_salt = db_salt(probe_source)
	probe_mapped = keymap_table.get(probe_salt)
	if keymap_table and not probe_mapped and not str(key or "").strip():
		report.error = f"keymap 里没有可用于探针的库（{probe_source.name} 的 salt={probe_salt}）"
		report.hint = "确认这份 keymap 与当前数据目录配套：键必须是这些库文件第 1 页前 16 字节的 hex。"
		return _finish(report, started)
	probe_key = probe_mapped or str(key or "")
	probe_key_type = "raw" if probe_mapped else key_type

	with tempfile.TemporaryDirectory(prefix="atuowexin-probe-") as probe_dir:
		probe_target = Path(probe_dir) / probe_source.name
		_say(f"探针：{probe_source.name}（{probe_source.stat().st_size // 1024} KB，salt {probe_salt[:8]}…）")
		probe_result = sqlcipher.decrypt_file(
			probe_source, probe_target, key=probe_key, key_type=probe_key_type, version=version, timeout=600
		)
		report.probe = {
			"src": str(probe_source),
			"ok": probe_result.ok,
			"key_type": probe_result.key_type,
			"error": probe_result.error,
			"pages": probe_result.pages,
		}
		if not probe_result.ok:
			report.error = probe_result.error
			report.hint = (
				"密钥不对，或形态不对（raw key ↔ passphrase）。"
				"`--key-type auto` 已两种都试过；若仍失败，说明拿到的 key 与这些库不匹配——"
				"常见原因是密钥来自旧版本（每库 salt 不同 → 需要用 passphrase 重新派生）。"
			)
			return _finish(report, started)
		report.key_type = probe_result.key_type
		report.self_wxid = detect_account_id(encrypted_root)
		_say(f"密钥验证通过（形态：{probe_result.key_type}）" + (f"，账号 {report.self_wxid}" if report.self_wxid else ""))

	# 2) WAL 扫描：哪些 -wal 里还有**未 checkpoint 的已提交数据**，哪些只是残留
	report.wal_files = [str(item) for item in sorted(encrypted_root.rglob("*.db-wal")) if item.stat().st_size > 0]
	if report.wal_files:
		pending = 0
		stale = 0
		for path in report.wal_files:
			info = sqlcipher.wal_info(path)
			if info.get("valid_frames") and info.get("last_commit"):
				pending += 1
			elif info.get("stale"):
				stale += 1
		if pending:
			_say(f"发现 {pending} 个 -wal 里有未 checkpoint 的已提交数据，解密时会自动合并")
		if stale:
			_say(f"另有 {stale} 个 -wal 是 SQLite 重置后的残留帧（salt 不匹配），按 sqlite3 的规则忽略")

	if probe_only:
		report.ok = True
		report.stage = "probe"
		return _finish(report, started)

	# 3) 批量解密
	report.stage = "decrypt"
	if keymap_table:
		_say(f"批量解密（每库一把 key）→ {target_root}")
		results = _decrypt_tree_with_keymap(
			encrypted_root,
			target_root,
			keymap_table,
			version=version,
			fallback_key=str(key or ""),
			key_type=report.key_type or key_type,
			timeout=1800,
			wal=wal,
			progress=_say,
		)
	else:
		_say(f"批量解密 → {target_root}")
		results = sqlcipher.decrypt_tree(
			encrypted_root,
			target_root,
			key=key,
			key_type=report.key_type or key_type,
			version=version,
			timeout=1800,
			wal=wal,
		)
	for item in results:
		entry = {
			"src": item.src,
			"dst": item.dst,
			"ok": item.ok,
			"pages": item.pages,
			"error": item.error,
			"wal_frames": getattr(item, "wal_frames", 0),
			"wal_valid_frames": getattr(item, "wal_valid_frames", 0),
			"wal_applied": getattr(item, "wal_applied", 0),
			"wal_stale": bool(getattr(item, "wal_stale", False)),
			"wal_error": getattr(item, "wal_error", ""),
		}
		report.decrypted.append(entry)
		if item.ok:
			report.decrypted_ok += 1
			if entry["wal_applied"]:
				report.wal_merged += 1
				report.wal_applied_frames += int(entry["wal_applied"])
			elif entry["wal_stale"]:
				report.wal_stale += 1
			if entry["wal_error"]:
				report.wal_failed.append(f"{Path(item.src).name}：{entry['wal_error']}")
		else:
			report.decrypted_failed += 1
	if report.decrypted_ok == 0:
		report.error = "没有任何库解密成功"
		report.hint = "检查 version 参数（微信 4.x 用 4）与 key 形态。"
		return _finish(report, started)

	# 4) 校验解密产物确实是 SQLite
	report.stage = "verify"
	verified = _verify_plaintext(target_root)
	if not verified.get("ok"):
		report.error = f"解密产物不是可读的 SQLite：{verified.get('error')}"
		return _finish(report, started)
	_say(f"校验通过：首个库 {verified.get('db')}，{verified.get('tables')} 张表")

	# 5) 写回配置
	if persist:
		report.stage = "config"
		backend_settings = config.setdefault("backend", {}).setdefault("decrypted_sqlite", {})
		config["backend"]["name"] = "decrypted_sqlite"
		backend_settings["root"] = str(target_root)
		if report.self_wxid:
			# 写进去，后续判定「我发的消息」才准
			backend_settings["self_wxid"] = report.self_wxid
		report.config_path = str(config_module.save_config(config, config_path))
		_say(f"配置已更新：{report.config_path}")

	# 6) 全量摘录
	if run_sync:
		report.stage = "sync"
		_say("开始全量摘录")
		backend, reason = build_backend(config, name="decrypted_sqlite")
		config["_backend_reason"] = reason
		config["_backend_name"] = getattr(backend, "name", "")
		with open_store(config.get("workdir") or ".") as store:
			sync_report = sync(store, backend, config, full=full, progress=progress)
			report.sync = sync_report.to_dict()
			report.sync["reason"] = reason

	report.ok = report.decrypted_ok > 0 and not report.sync.get("errors") and not report.wal_failed
	report.stage = "done"
	return _finish(report, started)


def _verify_plaintext(root: Path) -> dict[str, Any]:
	"""确认解密产物能被标准 sqlite3 打开（防止"解出来一堆垃圾还以为成功"）。"""
	candidates = sorted(root.rglob("*.db"))
	if not candidates:
		return {"ok": False, "error": "解密目录里没有 .db"}
	first = candidates[0]
	try:
		connection = sqlite3.connect(f"file:{first.as_posix()}?mode=ro", uri=True)
		try:
			tables = [str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
		finally:
			connection.close()
	except sqlite3.Error as error:
		return {"ok": False, "error": f"{first.name}: {error}", "db": str(first)}
	return {"ok": True, "db": str(first), "tables": len(tables)}


def _finish(report: IngestReport, started: float) -> IngestReport:
	"""收尾：填耗时。"""
	report.elapsed_ms = int((time.time() - started) * 1000)
	return report


def default_out(config: dict[str, Any]) -> Path:
	"""默认的解密产物目录。"""
	return Path(str(config.get("workdir") or ".")) / "decrypted"


__all__ = ["IngestReport", "ingest", "default_out", "PROBE_PREFERENCE"]
