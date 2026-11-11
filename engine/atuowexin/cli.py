# -*- coding: utf-8 -*-
"""命令行入口：``python -m atuowexin <命令>``。

CLI 是**唯一**的对外接口：DSH 插件的 JS 半边、cron、你手敲，全都走这里，
因此它有两条硬约束：

1. ``--json`` 时 **stdout 只允许出现一个 JSON 对象**（进度与日志一律走 stderr），
   这样上层可以无脑 ``json.loads(stdout)``。
2. **只读**：本版本只解密/摘录/导出聊天记录，不含发送消息、注入微信进程、
   调用云端模型或任何写回微信的代码。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from . import __version__, config as config_module, isolate, paths
from .backends import BackendError, build_backend
from .doctor import run_doctor
from .extract import export_conversation, export_markdown_bundle, sync
from .render import RenderError
from .model import Conversation, format_ts
from .store import Store, open_store

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
#: 需要用户动手（目前只有「去微信完成登录」）。上层看到它就该提示用户，而不是重试。
EXIT_NEEDS_USER = 3

PROG = "atuowexin"


class CliError(RuntimeError):
	"""可直接展示给用户的命令级错误。"""


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
	"""构造命令行解析器。

	注意 ``default=argparse.SUPPRESS``：公共选项既挂在主解析器、又通过 ``parents`` 挂到
	每个子解析器上，如果子解析器带默认值，它会把用户在**子命令之前**写的值覆盖成默认值
	（``--workdir`` 被悄悄忽略的经典坑）。用 SUPPRESS 让「没写就不设属性」，两条位置都安全。
	"""
	common = argparse.ArgumentParser(add_help=False)
	common.add_argument("--config", default=argparse.SUPPRESS, help="配置文件路径（默认 ~/.atuowexin/config.json）")
	common.add_argument("--workdir", default=argparse.SUPPRESS, help="工作目录（覆盖配置）")
	common.add_argument(
		"--backend",
		default=argparse.SUPPRESS,
		choices=["auto", "chatlog", "wcdb", "decrypted_sqlite", "mock"],
		help="强制指定读取后端（wcdb = 让 WCDB.dll 自己开库，需要参考工具与密钥）",
	)
	common.add_argument("--db-root", default=argparse.SUPPRESS, help="已解密数据目录（覆盖 backend.decrypted_sqlite.root）")
	common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="以 JSON 输出结果（stdout 只输出一个 JSON 对象）")
	common.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS, help="不打印进度")

	parser = argparse.ArgumentParser(prog=PROG, description="微信数据完整摘录 + 会话隔离 + AI 总结 + 回应草稿", parents=[common])
	parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
	sub = parser.add_subparsers(dest="command", required=True)

	sub.add_parser("doctor", help="环境体检", parents=[common])
	sub.add_parser("workspaces", help="列出会话隔离工作区", parents=[common])

	config_parser = sub.add_parser("config", help="配置管理", parents=[common])
	config_parser.add_argument("action", choices=["init", "show", "path", "validate"])

	sync_parser = sub.add_parser("sync", help="完整摘录（默认增量）", parents=[common])
	sync_parser.add_argument("--full", action="store_true", help="忽略游标，从最早重新摘一遍")
	sync_parser.add_argument("--conversation", action="append", default=[], help="只摘这些会话（可重复）")
	sync_parser.add_argument("--since", help="最早时间（YYYY-MM-DD / 时间戳 / 如 7d）")
	sync_parser.add_argument("--limit", type=int, default=0, help="每个会话最多摘多少条（0=不限）")
	sync_parser.add_argument("--export", action="store_true", help="摘完顺便导出到 <workdir>/exports")

	conv_parser = sub.add_parser("conversations", help="列出会话", parents=[common])
	conv_parser.add_argument("--query", default="", help="按名字/备注/id 过滤")
	conv_parser.add_argument("--kind", default="", choices=["", "friend", "group", "official", "service"])
	conv_parser.add_argument("--limit", type=int, default=50)

	msg_parser = sub.add_parser("messages", help="读取某会话的消息", parents=[common])
	msg_parser.add_argument("--conversation", required=True, help="会话 id 或名字")
	msg_parser.add_argument("--since")
	msg_parser.add_argument("--until")
	msg_parser.add_argument("--keyword", default="")
	msg_parser.add_argument("--limit", type=int, default=50)
	msg_parser.add_argument("--offset", type=int, default=0)
	msg_parser.add_argument("--redacted", action="store_true", help="输出前脱敏")

	ingest_parser = sub.add_parser("ingest", help="一键接入真实微信数据：解密 → 校验 → 写配置 → 全量摘录", parents=[common])
	ingest_parser.add_argument("--src", dest="source", required=True, help="**加密的**数据目录（db_storage / xwechat_files / 账号目录）")
	ingest_parser.add_argument("--key", default="", help="单把密钥：64 位十六进制 raw key 或口令")
	ingest_parser.add_argument(
		"--keymap",
		default="",
		help="每库一把 key 的映射文件（JSON：{\"<库salt的hex>\": \"<64位hex key>\"}）。微信 4.x 每库独立加密时必须用它",
	)
	ingest_parser.add_argument("--key-type", choices=["auto", "raw", "passphrase"], default="auto")
	ingest_parser.add_argument("--sqlcipher-version", type=int, choices=[3, 4], default=4)
	ingest_parser.add_argument("--out", help="解密产物目录（默认 <workdir>/decrypted）")
	ingest_parser.add_argument("--probe-only", action="store_true", help="只验密钥，不解密其余库")
	ingest_parser.add_argument("--incremental", action="store_true", help="摘录走增量（默认全量）")
	ingest_parser.add_argument("--no-sync", action="store_true", help="解密后不摘录")
	ingest_parser.add_argument("--no-persist", action="store_true", help="不把 backend.decrypted_sqlite.root 写回配置")
	ingest_parser.add_argument("--no-auto-key", action="store_true", help="不给密钥时不要自动去取（默认会自动检查并从微信进程内存提取）")
	ingest_parser.add_argument("--auto-key-deep", action="store_true", help="没有现成 keymap 当路标时也做通用锚点深扫（每个库几十 MB，分钟级）")
	ingest_parser.add_argument("--auto-key-watch", type=float, default=0.0, help="自动取密钥时改用轮询模式的秒数（密钥可能只在登录瞬间驻留）")
	ingest_parser.add_argument("--no-wal", action="store_true", help="不合并 -wal 里未 checkpoint 的已提交数据")

	export_parser = sub.add_parser("export", help="把会话完整导出为文件", parents=[common])
	export_parser.add_argument("--conversation", default="", help="单个会话（--all 时可不填）")
	export_parser.add_argument("--all", action="store_true", help="导出**全部**会话为一套 Markdown 聊天记录（总览 + 每会话一个文件 + 合订本）")
	export_parser.add_argument("--order", choices=["last_time", "msg_count", "name"], default="last_time", help="--all 时的排序（决定编号）")
	export_parser.add_argument("--limit", type=int, default=0, help="--all 时最多导出多少个会话（0=全部）")
	export_parser.add_argument("--no-combined", action="store_true", help="--all 时不生成合订本，只出总览 + 每会话文件")
	export_parser.add_argument("--out", help="输出目录（默认 <workdir>/exports）")
	export_parser.add_argument(
		"--formats",
		default="jsonl,csv,md",
		help="逗号分隔的输出格式：jsonl,csv,md,txt,html,xlsx,pdf（txt/html 零依赖；xlsx 需 openpyxl；pdf 需 fpdf2）",
	)
	export_parser.add_argument("--redacted", action="store_true")

	watch_parser = sub.add_parser("watch", help="定时自动读取（只做增量摘录，不发送、不调用模型）", parents=[common])
	watch_parser.add_argument("--interval", type=int, default=60)
	watch_parser.add_argument("--iterations", type=int, default=0, help="跑几轮后退出（0=一直跑）")
	watch_parser.add_argument("--once", action="store_true", help="只跑一轮")

	decrypt_parser = sub.add_parser("decrypt", help="SQLCipher 解密（需自备密钥）", parents=[common])
	decrypt_parser.add_argument("--in", dest="source", required=True, help="加密的 .db 文件或目录")
	decrypt_parser.add_argument("--out", dest="target", required=True, help="输出文件或目录")
	decrypt_parser.add_argument("--key", required=True, help="64 位十六进制原始 key，或口令")
	decrypt_parser.add_argument("--key-type", choices=["auto", "raw", "passphrase"], default="auto")
	decrypt_parser.add_argument("--sqlcipher-version", type=int, choices=[3, 4], default=4)
	decrypt_parser.add_argument("--tree", action="store_true", help="按目录批量解密")
	decrypt_parser.add_argument("--no-wal", action="store_true", help="不合并兄弟 -wal 里未 checkpoint 的已提交数据")

	deps_parser = sub.add_parser("deps", help="可选依赖：查看/安装（zstd 解压被压缩的消息正文）", parents=[common])
	deps_parser.add_argument("action", nargs="?", choices=["status", "install", "dir"], default="status")
	deps_parser.add_argument("package", nargs="*", help="要安装的组件（zstandard / pillow），默认全部")

	key_parser = sub.add_parser("key", help="密钥工具：校验、扫描或显式启动期捕获解密密钥", parents=[common])
	key_parser.add_argument(
		"action",
		choices=["find", "ensure", "capture", "auto"],
		help="ensure：先看有没有 key；find：无条件扫一次；capture：启动期 Hook 捕获；auto：关微信→起 Hook→拉起微信→点登录→取 key 一条龙",
	)
	key_parser.add_argument("--root", default="", help="**加密**库目录（db_storage）；省略则自动探测本机微信数据目录")
	key_parser.add_argument("--out", default="", help="keymap.json 输出路径（默认 <atuowexin home>/keymap.json）")
	key_parser.add_argument("--version", dest="sqlcipher_version", type=int, choices=[3, 4], default=4)
	key_parser.add_argument("--watch", action="store_true", help="持续轮询：密钥可能只在微信启动/登录瞬间驻留内存")
	key_parser.add_argument("--seconds", type=float, default=60.0, help="--watch 的总时长（默认 60 秒）")
	key_parser.add_argument("--interval", type=float, default=3.0, help="--watch 的轮询间隔（默认 3 秒）")
	key_parser.add_argument("--pid", type=int, action="append", default=[], help="只扫这个 pid（可重复）")
	key_parser.add_argument("--no-merge", action="store_true", help="不合并已有 keymap，直接覆盖")
	key_parser.add_argument("--show-keys", action="store_true", help="在输出里显示完整密钥（默认只显示前 8 位 + 文件路径）")
	key_parser.add_argument("--seed-keymap", default="", help="用这份已有 keymap 当路标（默认就用 --out 那份，不存在则不用）")
	key_parser.add_argument("--no-seeds", action="store_true", help="不用已有密钥当路标，直接走按库路径锚点的通用扫描（慢很多）")
	key_parser.add_argument("--no-anchors", action="store_true", help="只走路标快路径，不做通用锚点回退")
	key_parser.add_argument("--window-mb", type=float, default=0.0, help="每个路标/锚点周围扫多少 MB（默认 2）")
	key_parser.add_argument("--check-only", action="store_true", help="ensure 时只检查不提取（用于 doctor/CI 里判断密钥是否齐备）")
	key_parser.add_argument("--no-write", action="store_true", help="ensure 时提取到了也不写 keymap.json")
	key_parser.add_argument("--timeout", type=float, default=120.0, help="capture 等待捕获的秒数（默认 120）")
	key_parser.add_argument(
		"--login-wait",
		type=float,
		default=40.0,
		help="微信起来后等用户登录的秒数（默认 40）；超时立刻返回 stage=login_required，退出码 3",
	)
	key_parser.add_argument("--exe", default="", help="auto 用：微信可执行文件路径（默认自动找）")
	key_parser.add_argument("--keep-open", action="store_true", help="auto 用：微信正在跑时不要关它（会直接返回 wechat_must_close）")
	key_parser.add_argument("--no-auto-click", action="store_true", help="auto 用：不要自动点「进入微信」")
	key_parser.add_argument("--node", default="", help="capture 使用的 node 可执行文件")
	key_parser.add_argument("--dll", default="", help="capture 使用的 wx_key.dll 路径或目录")

	return parser


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _emit(args: argparse.Namespace, payload: dict[str, Any], human: str) -> None:
	"""统一的输出：``--json`` 只打 JSON，否则打人类可读文本。"""
	if getattr(args, "json", False):
		sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
	else:
		sys.stdout.write(human.rstrip() + "\n")


def _progress(args: argparse.Namespace):
	"""构造进度回调（永远写 stderr，不污染 JSON）。"""
	if getattr(args, "quiet", False):
		return None

	def _write(line: str) -> None:
		sys.stderr.write(line + "\n")
		sys.stderr.flush()

	return _write


def _opt(args: argparse.Namespace, name: str, default: Any = None) -> Any:
	"""读一个公共选项：因为用了 ``SUPPRESS``，没写过的选项**没有属性**。"""
	return getattr(args, name, default)


def _load_config(args: argparse.Namespace) -> dict[str, Any]:
	"""按 CLI 参数加载配置。"""
	overrides: dict[str, Any] = {}
	workdir = _opt(args, "workdir")
	backend = _opt(args, "backend")
	db_root = _opt(args, "db_root")
	if workdir:
		overrides["workdir"] = workdir
	if backend:
		overrides.setdefault("backend", {})["name"] = backend
	if db_root:
		overrides.setdefault("backend", {}).setdefault("decrypted_sqlite", {})["root"] = db_root
	try:
		return config_module.load_config(_opt(args, "config"), overrides=overrides)
	except config_module.ConfigError as error:
		raise CliError(str(error)) from error


def parse_time(value: str | None) -> int:
	"""解析 CLI 时间参数：时间戳 / ``YYYY-MM-DD`` / ``YYYY-MM-DD HH:MM`` / ``7d`` / ``12h``。"""
	if not value:
		return 0
	text = str(value).strip()
	if not text:
		return 0
	if text.isdigit():
		return int(text)
	if len(text) > 1 and text[:-1].isdigit() and text[-1] in "dh":
		amount = int(text[:-1])
		return int(time.time()) - amount * (86400 if text[-1] == "d" else 3600)
	from .model import parse_time_text

	for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
		import datetime as _dt

		try:
			parsed = _dt.datetime.strptime(text, pattern)
		except ValueError:
			continue
		return int(parsed.timestamp())
	parsed = parse_time_text(text)
	if parsed:
		return parsed
	raise CliError(f"无法解析时间：{value}（支持 时间戳 / 2026-01-31 / 2026-01-31 09:30 / 7d / 12h）")


def _resolve_conversation(store: Store, target: str, *, backend: Any = None) -> Conversation:
	"""把用户输入的 id 或名字解析成会话对象。"""
	needle = (target or "").strip()
	if not needle:
		raise CliError("必须指定会话（--conversation）")
	exact = store.get_conversation(needle)
	if exact is not None:
		return exact
	candidates = store.list_conversations()
	matches = [item for item in candidates if needle.lower() in item.display_name.lower() or needle.lower() in item.id.lower()]
	if not matches and backend is not None:
		try:
			matches = [item for item in backend.list_conversations() if needle.lower() in item.display_name.lower() or needle.lower() in item.id.lower()]
		except BackendError:
			matches = []
	if not matches:
		hint = "；".join(f"{item.display_name}({item.id})" for item in candidates[:8]) or "（本地库还没有会话，先运行 sync）"
		raise CliError(f"找不到会话「{target}」。可用会话：{hint}")
	if len(matches) > 1:
		options = "；".join(f"{item.display_name}({item.id})" for item in matches[:8])
		raise CliError(f"「{target}」匹配到 {len(matches)} 个会话，请用完整 id 指定：{options}")
	return matches[0]


def _open_store(config: dict[str, Any]) -> Store:
	"""打开工作目录下的归一化库。"""
	return open_store(str(config.get("workdir") or "."))


# --------------------------------------------------------------------------- #
# 命令实现
# --------------------------------------------------------------------------- #


def cmd_doctor(args: argparse.Namespace) -> int:
	"""环境体检。"""
	config = _load_config(args)
	report = run_doctor(config, config_path=_opt(args, "config") or paths.config_path())
	_emit(args, report.to_dict(), report.render())
	return EXIT_OK if report.ok else EXIT_ERROR


def cmd_config(args: argparse.Namespace) -> int:
	"""配置管理。"""
	if args.action == "init":
		config = _load_config(args)
		target = config_module.save_config(config, _opt(args, "config"))
		_emit(args, {"ok": True, "path": str(target)}, f"已写入配置模板：{target}")
		return EXIT_OK
	if args.action == "path":
		target = Path(_opt(args, "config")) if _opt(args, "config") else paths.config_path()
		_emit(args, {"path": str(target)}, str(target))
		return EXIT_OK
	config = _load_config(args)
	problems = config_module.validate(config)
	if args.action == "validate":
		_emit(args, {"ok": not problems, "problems": problems}, "配置通过校验" if not problems else "配置有问题：\n- " + "\n- ".join(problems))
		return EXIT_OK if not problems else EXIT_ERROR
	# 只读版配置里没有密钥（llm/send 段已随模型层一起删除），因此原样输出即可
	_emit(args, config, json.dumps(config, ensure_ascii=False, indent=2))
	return EXIT_OK


def cmd_sync(args: argparse.Namespace) -> int:
	"""完整摘录。"""
	config = _load_config(args)
	backend, reason = build_backend(config)
	config["_backend_reason"] = reason
	config["_backend_name"] = getattr(backend, "name", "")
	with _open_store(config) as store:
		report = sync(
			store,
			backend,
			config,
			full=args.full,
			since=parse_time(args.since),
			conversations=args.conversation or None,
			limit=args.limit,
			progress=_progress(args),
		)
		exported: list[dict[str, Any]] = []
		if args.export and not args.json:
			for item in report.details:
				conversation = store.get_conversation(item.conversation_id)
				if conversation is None or item.added == 0:
					continue
				exported.append(export_conversation(store, conversation, paths.exports_root(config["workdir"])))
	payload = report.to_dict()
	payload["reason"] = reason
	payload["exported"] = exported
	_emit(args, payload, report.summary_text())
	return EXIT_OK if not report.errors else EXIT_ERROR


def cmd_conversations(args: argparse.Namespace) -> int:
	"""列出会话。"""
	config = _load_config(args)
	with _open_store(config) as store:
		items = store.list_conversations(query=args.query, kind=args.kind, limit=args.limit)
		if not items:
			backend, reason = build_backend(config)
			items = [item for item in backend.list_conversations() if (not args.query or args.query.lower() in item.display_name.lower())][: args.limit]
			lines = [f"（本地库还没有数据，以下来自后端 {getattr(backend, 'name', '')}：{reason}）"]
		else:
			lines = []
		for item in items:
			lines.append(
				f"{item.display_name}\t{item.id}\t{item.kind}\t消息 {item.msg_count}\t最近 {format_ts(item.last_time)}"
				+ (f"\t成员 {item.member_count}" if item.member_count else "")
			)
	payload = {"conversations": [item.to_dict() for item in items]}
	_emit(args, payload, "\n".join(lines) if lines else "（没有会话）")
	return EXIT_OK


def cmd_messages(args: argparse.Namespace) -> int:
	"""读取某会话消息。"""
	config = _load_config(args)
	with _open_store(config) as store:
		conversation = _resolve_conversation(store, args.conversation)
		messages = store.messages(
			conversation.id,
			since=parse_time(args.since),
			until=parse_time(args.until),
			keyword=args.keyword,
			limit=args.limit,
			offset=args.offset,
		)
	payload = {
		"conversation": conversation.to_dict(),
		"count": len(messages),
		"messages": [item.to_dict() for item in messages],
	}
	human_lines = [f"{conversation.display_name}（{conversation.id}）共 {len(messages)} 条"]
	human_lines.extend(item.display_line(redacted=args.redacted) for item in messages)
	_emit(args, payload, "\n".join(human_lines))
	return EXIT_OK


def cmd_export(args: argparse.Namespace) -> int:
	"""完整导出：单个会话（jsonl/csv/md）或全部会话的 Markdown 整包。"""
	config = _load_config(args)
	with _open_store(config) as store:
		if getattr(args, "all", False):
			out_dir = Path(args.out) if args.out else paths.exports_root(config["workdir"]) / "markdown"
			progress = None if getattr(args, "json", False) else (lambda message: sys.stderr.write(f"{message}\n"))
			result = export_markdown_bundle(
				store,
				out_dir,
				order=args.order,
				limit=args.limit,
				redacted=args.redacted,
				include_combined=not args.no_combined,
				progress=progress,
			)
			human = (
				f"已导出 {result['conversations']} 个会话 / {result['messages']} 条消息 → {result['out_dir']}\n"
				f"- 总览：{result['overview']}\n"
				+ (f"- 合订本：{result['combined']}\n" if result["combined"] else "")
				+ f"- 单会话目录：{Path(result['out_dir']) / '按会话'}"
			)
			_emit(args, result, human)
			return EXIT_OK
		if not args.conversation:
			raise CliError("请给 --conversation 指定会话，或用 --all 导出全部会话")
		formats = tuple(item.strip() for item in str(args.formats or "jsonl,csv,md").split(",") if item.strip())
		conversation = _resolve_conversation(store, args.conversation)
		out_dir = Path(args.out) if args.out else paths.exports_root(config["workdir"])
		try:
			result = export_conversation(store, conversation, out_dir, formats=formats, redacted=args.redacted)
		except RenderError as error:
			# 可选依赖缺失（xlsx/pdf）时给一条能照着做的命令，而不是抛栈
			raise CliError(str(error)) from error
	payload = {"conversation": conversation.to_dict(), **result}
	_emit(args, payload, f"已导出 {result['messages']} 条到 {result['dir']}\n" + "\n".join(f"- {item}" for item in result["files"]))
	return EXIT_OK


def cmd_workspaces(args: argparse.Namespace) -> int:
	"""列出隔离工作区。"""
	config = _load_config(args)
	items = isolate.list_workspaces(config["workdir"])
	lines = [
		f"{item['display_name']}\t{item['conversation_id']}\t消息 {item['messages']}\t{item['dir']}"
		for item in items
	]
	_emit(args, {"workdir": config["workdir"], "workspaces": items}, "\n".join(lines) if lines else "（还没有工作区，运行 sync 后生成）")
	return EXIT_OK


def cmd_watch(args: argparse.Namespace) -> int:
	"""定时自动读取：按间隔增量摘录（只读，不发送、不调用模型）。"""
	config = _load_config(args)
	interval = max(5, int(args.interval or 60))
	max_iterations = 1 if args.once else int(args.iterations or 0)
	progress = _progress(args)
	rounds: list[dict[str, Any]] = []
	iteration = 0
	with _open_store(config) as store:
		while True:
			iteration += 1
			backend, reason = build_backend(config)
			config["_backend_reason"] = reason
			config["_backend_name"] = getattr(backend, "name", "")
			report = sync(store, backend, config, progress=progress)
			entry: dict[str, Any] = {"iteration": iteration, "sync": report.to_dict()}
			rounds.append(entry)
			if progress:
				progress(f"[轮次 {iteration}] {report.summary_text()}（只读，不发送）")
			if max_iterations and iteration >= max_iterations:
				break
			time.sleep(interval)
	payload = {"iterations": iteration, "rounds": rounds, "note": "只读：只做增量摘录，不发送任何消息"}
	_emit(args, payload, f"完成 {iteration} 轮（只读）")
	return EXIT_OK


def cmd_decrypt(args: argparse.Namespace) -> int:
	"""SQLCipher 解密（可选功能，需自备密钥）。"""
	try:
		from . import sqlcipher
	except ImportError as error:  # pragma: no cover
		raise CliError(f"解密模块不可用：{error}") from error
	if not sqlcipher.is_available():
		raise CliError("未找到可用的 PowerShell（pwsh 或系统自带 powershell.exe），内置解密不可用；请用外部工具解密。")
	key_type = args.key_type
	use_wal = not bool(getattr(args, "no_wal", False))
	results = []
	if args.tree:
		for item in sqlcipher.decrypt_tree(
			args.source, args.target, key=args.key, key_type=key_type, version=args.sqlcipher_version, wal=use_wal
		):
			results.append(item.to_dict())
	else:
		item = sqlcipher.decrypt_file(
			args.source, args.target, key=args.key, key_type=key_type, version=args.sqlcipher_version, wal=use_wal
		)
		results.append(item.to_dict())
	ok = all(bool(item.get("ok")) for item in results)
	lines = []
	for item in results:
		lines.append(f"{item.get('src')} -> {item.get('dst')}: {'OK' if item.get('ok') else item.get('error')}")
		if item.get("wal_applied"):
			lines.append(f"    WAL 合并：回放 {item['wal_applied']} 帧（提交 {item['wal_committed']} 次）")
		elif item.get("wal_stale"):
			lines.append("    WAL：检测到重置后的残留帧，已按 sqlite3 的规则忽略")
		if item.get("wal_error"):
			lines.append(f"    WAL 警告：{item['wal_error']}")
	_emit(args, {"ok": ok, "results": results}, "\n".join(lines))
	return EXIT_OK if ok else EXIT_ERROR


def cmd_key(args: argparse.Namespace) -> int:
	"""密钥工具：从运行中的微信进程内存里**自动提取**解密密钥（只读扫描）。

	安全取向（与 ``config show`` 隐藏 key 一致）：**默认不把密钥明文打进 stdout**——
	只显示每个库 salt→密钥前 8 位，并把完整 keymap 写到文件；要看明文得显式 ``--show-keys``。
	这样密钥不会顺着终端日志 / 会话记录到处跑。
	"""
	from . import keyfinder

	if args.action == "auto":
		# 一条龙：关微信 -> 起 Hook -> 拉起微信 -> 点登录 -> 取 key -> WCDB 校验
		from .autopilot import bootstrap

		root = Path(args.root).expanduser() if args.root else _detect_encrypted_root()
		if root is None or not str(root):
			raise CliError("没有指定 --root，也没能自动探测到加密库目录。请用 --root 指向 db_storage。")
		out = Path(args.out).expanduser() if args.out else paths.atuowexin_home() / "keymap.json"
		pilot = bootstrap(
			root=root,
			keymap_path=out,
			workdir=_load_config(args).get("workdir"),
			exe=args.exe,
			keep_open=args.keep_open,
			auto_click=not args.no_auto_click,
			login_wait=args.login_wait,
			timeout=args.timeout,
			progress=_progress(args),
		)
		payload = pilot.to_dict(show_key=args.show_keys)
		_emit(args, payload, pilot.message or ("就绪" if pilot.ok else "未完成"))
		if pilot.ok:
			return EXIT_OK
		if pilot.stage == "login_required" or pilot.user_action == "login_wechat":
			return EXIT_NEEDS_USER
		return EXIT_ERROR

	if args.action == "capture":
		from .capture import capture_key
		root = Path(args.root).expanduser() if args.root else _detect_encrypted_root()
		if root is None or not str(root):
			raise CliError("没有指定 --root，也没能自动探测到加密库目录。请用 --root 指向 db_storage。")
		out = Path(args.out).expanduser() if args.out else paths.atuowexin_home() / "keymap.json"
		result = capture_key(
			root,
			out,
			workdir=_load_config(args).get("workdir"),
			dll=args.dll or None,
			node=args.node or None,
			version=args.sqlcipher_version,
			timeout=args.timeout,
			login_wait=args.login_wait,
			write=not args.no_write,
			progress=_progress(args),
		)
		payload = result.to_dict(show_key=args.show_keys)
		_emit(args, payload, result.error or ("已捕获并写入 keymap" if result.ok else "捕获未完成"))
		if result.ok:
			return EXIT_OK
		if result.stage == "login_required":
			# 明确告诉上层「卡在用户登录」，不是失败、不该盲目重试
			return EXIT_NEEDS_USER
		return EXIT_ERROR

	root = Path(args.root).expanduser() if args.root else _detect_encrypted_root()
	if root is None or not str(root):
		raise CliError(
			"没有指定 --root，也没能自动探测到加密库目录。"
			"请用 --root 指向 db_storage（例如 %USERPROFILE%\\Documents\\Tencent Files\\xwechat_files\\wxid_xxx\\db_storage）。"
		)
	out = Path(args.out).expanduser() if args.out else paths.atuowexin_home() / "keymap.json"
	progress = _progress(args)
	window = int(args.window_mb * 1024 * 1024) if args.window_mb else keyfinder.DEFAULT_WINDOW

	# 已有密钥当路标：实测同一批库的密钥在内存里聚簇（相隔约 1.6 KB），
	# 有路标时只扫那几个区域，比按库路径锚点扫几十 MB 快一两个数量级。
	# 路标来源按优先级依次找：显式 --seed-keymap → ATUOWEXIN_KEYMAP → --out → 工作目录/keymap.json
	seed_paths: list[Path] = []
	if args.seed_keymap:
		seed_paths.append(Path(args.seed_keymap).expanduser())
	else:
		env_seed = os.environ.get("ATUOWEXIN_KEYMAP")
		if env_seed:
			seed_paths.append(Path(env_seed).expanduser())
		seed_paths.append(out)
		try:
			workdir = _load_config(args).get("workdir")
			if workdir:
				seed_paths.append(Path(str(workdir)) / "keymap.json")
		except Exception:  # pragma: no cover - 配置不可用不影响提取
			pass
	seen: set[str] = set()
	seed_paths = [item for item in seed_paths if not (str(item) in seen or seen.add(str(item)))]
	seed_keys: dict[str, str] = {}
	if not args.no_seeds:
		for item in seed_paths:
			for salt, value in keyfinder.load_keymap(item).items():
				seed_keys.setdefault(salt, value)
	if seed_keys and progress is not None:
		progress(f"用 {len(seed_keys)} 把已有密钥当路标")

	# ensure = 先看有没有 key，没有才去读（全都有时**不碰内存**，正常使用几乎零开销）
	if args.action == "ensure":
		ensured = keyfinder.ensure_keys(
			root,
			keymap_path=out,
			version=args.sqlcipher_version,
			extract=not args.check_only,
			write=not args.no_write,
			seed_paths=[item for item in seed_paths if item != out],
			pids=args.pid or None,
			window=window,
			watch_seconds=args.seconds if args.watch else 0.0,
			use_anchors=not args.no_anchors,
			progress=progress,
		)
		payload: dict[str, Any] = {"root": str(root), **ensured.to_dict()}
		lines = [f"密钥状态：{ensured.detail}"]
		if ensured.missing_dbs:
			lines.append("  仍缺：" + "、".join(ensured.missing_dbs[:10]))
		if ensured.written:
			lines.append(f"  已写入：{out}")
		if ensured.hint:
			lines.append(f"  下一步：{ensured.hint}")
		if ensured.written and not ensured.ok:
			lines.append("  ⚠️ keymap.json 是**无法轮换**的数据库万能钥匙：别放云盘/仓库。")
		_emit(args, payload, "\n".join(lines))
		return EXIT_OK if ensured.ok else EXIT_ERROR

	if args.watch:
		result = keyfinder.watch_keys(
			root, version=args.sqlcipher_version, seconds=args.seconds, interval=args.interval, progress=progress
		)
	else:
		result = keyfinder.find_keys(
			root,
			version=args.sqlcipher_version,
			pids=args.pid or None,
			window=window,
			seed_keys=seed_keys or None,
			use_anchors=not args.no_anchors,
			progress=progress,
		)

	payload: dict[str, Any] = {
		"ok": result.ok,
		"root": str(root),
		"out": str(out),
		"databases": result.databases,
		"scanned_pids": result.scanned_pids,
		"scanned_bytes": result.scanned_bytes,
		"elapsed_ms": result.elapsed_ms,
		"error": result.error,
		"hint": result.hint,
		# 默认只给"哪个库找到/没找到"，不给密钥本体
		"found": [
			{
				"db": Path(item.db).name,
				"salt": item.salt,
				"key_prefix": item.key[:8] + "…",
				"pid": item.pid,
			}
			for item in result.found
		],
		"missing": result.missing,
	}
	lines: list[str] = []
	if result.ok:
		keyfinder.save_keymap(out, result.keymap, merge=not args.no_merge)
		payload["written"] = str(out)
		payload["written_count"] = len(result.keymap)
		lines.append(f"从 {len(result.scanned_pids)} 个微信进程的内存里提取到 {len(result.keymap)} 把密钥")
		for item in result.found:
			lines.append(f"  {Path(item.db).name}  salt {item.salt[:8]}…  key {item.key[:8]}…（pid {item.pid}）")
		lines.append(f"已写入 keymap.json：{out}")
		lines.append("⚠️ 这个文件等于你数据库的万能钥匙，且**无法轮换**：别放云盘/仓库，用完移进加密卷。")
		if result.missing:
			lines.append(f"（还有 {len(result.missing)} 个库没找到密钥：本次没被微信打开过的库提取不到）")
		if args.show_keys:
			payload["keys"] = dict(result.keymap)
			lines.append("已按 --show-keys 输出明文密钥。")
	else:
		lines.append(f"未提取到密钥：{result.error}")
		if result.hint:
			lines.append(f"下一步：{result.hint}")
		if result.databases:
			lines.append(f"（本次待解库 {result.databases} 个，扫描 {result.scanned_bytes / 1048576:.0f} MB，用时 {result.elapsed_ms} ms）")
	_emit(args, payload, "\n".join(lines))
	return EXIT_OK if result.ok else EXIT_ERROR


def _detect_encrypted_root() -> Path | None:
	"""自动探测加密库目录（``<xwechat_files>/<账号>/db_storage``）。"""
	try:
		for item in paths.detect_wechat_data_roots():
			base = Path(str(item.get("path")))
			for account in item.get("accounts") or []:
				candidate = base / str(account) / "db_storage"
				if candidate.is_dir():
					return candidate
				if (base / str(account)).is_dir():
					return base / str(account)
	except Exception:  # pragma: no cover - 探测失败就交给调用方报错
		return None
	return None


def cmd_deps(args: argparse.Namespace) -> int:
	"""查看/安装可选依赖（zstd 解压正文）。

	装到**交付物自己的** ``<repo>/.tools/python``，不动系统 Python、不建虚拟环境。
	"""
	from . import deps

	if args.action == "install":
		components = list(args.package or []) or None
		result = deps.install(components)
		_emit(args, result.to_dict(), result.render())
		return EXIT_OK if result.ok else EXIT_ERROR
	if args.action == "dir":
		_emit(args, {"ok": True, "path": str(deps.optional_dir())}, str(deps.optional_dir()))
		return EXIT_OK
	states = deps.status()
	payload = {"ok": True, "path": str(deps.optional_dir()), "components": states}
	lines = [f"可选依赖目录：{deps.optional_dir()}"]
	for name, info in states.items():
		lines.append(
			"  [{0}] {1}：{2}{3}".format(
				"OK" if info.get("ok") else "缺",
				name,
				info.get("module") or "未安装",
				"",
			)
		)
		lines.append(f"       用途：{deps.OPTIONAL_PACKAGES.get(name, '')}")
	if not all(info.get("ok") for info in states.values()):
		lines.append("安装：python -m atuowexin deps install")
	_emit(args, payload, "\n".join(lines))
	return EXIT_OK


def cmd_ingest(args: argparse.Namespace) -> int:
	"""一键接入真实微信数据：解密 → 校验 → 写配置 → 全量摘录。

	密钥两种给法：``--key``（单把，适合口令派生出同一把 enc key 的情况）或
	``--keymap``（**每库一把**，微信 4.x 每库独立加密时的唯一可行方式）。
	"""
	from .ingest import ingest  # 延迟导入：解密/入库是可选的

	config = _load_config(args)
	progress = None if getattr(args, "json", False) else (lambda message: sys.stderr.write(f"{message}\n"))
	report = ingest(
		config,
		source=args.source,
		key=args.key or "",
		key_type=args.key_type,
		keymap=args.keymap or None,
		version=args.sqlcipher_version,
		out=args.out,
		probe_only=bool(args.probe_only),
		run_sync=not bool(args.no_sync),
		full=not bool(args.incremental),
		persist=not bool(args.no_persist),
		wal=not bool(getattr(args, "no_wal", False)),
		auto_key=not bool(getattr(args, "no_auto_key", False)),
		auto_key_deep=bool(getattr(args, "auto_key_deep", False)),
		auto_key_watch_seconds=float(getattr(args, "auto_key_watch", 0.0) or 0.0),
		config_path=getattr(args, "config", None),
		progress=progress,
	)
	_emit(args, report.to_dict(), report.render())
	return EXIT_OK if report.ok else EXIT_ERROR


COMMANDS = {
	"doctor": cmd_doctor,
	"config": cmd_config,
	"sync": cmd_sync,
	"conversations": cmd_conversations,
	"messages": cmd_messages,
	"export": cmd_export,
	"workspaces": cmd_workspaces,
	"watch": cmd_watch,
	"decrypt": cmd_decrypt,
	"ingest": cmd_ingest,
	"deps": cmd_deps,
	"key": cmd_key,
}


def main(argv: Sequence[str] | None = None) -> int:
	"""CLI 主入口。"""
	parser = build_parser()
	args = parser.parse_args(list(argv) if argv is not None else None)
	handler = COMMANDS.get(args.command)
	if handler is None:  # pragma: no cover - argparse 已经限制了取值
		parser.error(f"未知命令：{args.command}")
		return EXIT_USAGE
	try:
		return handler(args)
	except CliError as error:
		_fail(args, str(error))
		return EXIT_ERROR
	except BackendError as error:
		_fail(args, f"后端错误：{error}")
		return EXIT_ERROR
	except KeyboardInterrupt:  # pragma: no cover
		_fail(args, "已中断")
		return EXIT_ERROR


def _fail(args: argparse.Namespace, message: str) -> None:
	"""统一的错误输出：``--json`` 时也保持 stdout 是合法 JSON。"""
	if getattr(args, "json", False):
		sys.stdout.write(json.dumps({"ok": False, "error": message}, ensure_ascii=False) + "\n")
	else:
		sys.stderr.write(f"[错误] {message}\n")


__all__ = ["main", "build_parser", "parse_time", "CliError"]
