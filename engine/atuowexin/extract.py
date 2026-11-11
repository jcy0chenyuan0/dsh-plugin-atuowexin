# -*- coding: utf-8 -*-
"""完整摘录（extract）：把后端里的会话与消息**全量/增量**落进归一化库与会话工作区。

「完整」体现在四处：

1. 默认把**所有会话**都摘（不抽样），``max_messages_per_conversation = 0`` 表示不限条数；
2. 每条消息都保留原始字段（``raw``）与媒体引用（``media``），非文本消息也留有类型与占位说明；
3. 增量同步以「每会话最大时间戳」为游标，重复运行不会重复计数（``uid`` 主键去重）；
4. 同时落两份：数据库（便于查询/总结）与 ``messages.jsonl``（便于离线带走/审计）。
"""

from __future__ import annotations

import csv
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from . import isolate
from .backends import Backend, BackendError
from .model import Conversation, Message, format_ts
from .store import Store

#: 写入数据库时的批大小（同时决定内存占用与事务次数）。
BATCH_SIZE = 200


@dataclass
class ConversationSync:
	"""单个会话的同步结果。"""

	conversation_id: str
	display_name: str
	fetched: int = 0
	added: int = 0
	cursor_from: int = 0
	cursor_to: int = 0
	error: str = ""

	def to_dict(self) -> dict[str, Any]:
		"""转成可 JSON 序列化的字典。"""
		return {
			"conversation_id": self.conversation_id,
			"display_name": self.display_name,
			"fetched": self.fetched,
			"added": self.added,
			"cursor_from": self.cursor_from,
			"cursor_to": self.cursor_to,
			"error": self.error,
		}


@dataclass
class SyncReport:
	"""一次同步的总体结果。"""

	backend: str = ""
	reason: str = ""
	synthetic: bool = False
	full: bool = False
	conversations: int = 0
	fetched: int = 0
	added: int = 0
	errors: list[str] = field(default_factory=list)
	details: list[ConversationSync] = field(default_factory=list)
	elapsed_ms: int = 0

	def to_dict(self) -> dict[str, Any]:
		"""转成可 JSON 序列化的字典。"""
		return {
			"backend": self.backend,
			"reason": self.reason,
			"synthetic": self.synthetic,
			"full": self.full,
			"conversations": self.conversations,
			"fetched": self.fetched,
			"added": self.added,
			"errors": self.errors,
			"details": [item.to_dict() for item in self.details],
			"elapsed_ms": self.elapsed_ms,
		}

	def summary_text(self) -> str:
		"""给人和模型看的一行摘要。"""
		tag = "（合成数据）" if self.synthetic else ""
		return (
			f"后端 {self.backend}{tag}：{self.conversations} 个会话，"
			f"读取 {self.fetched} 条，新增 {self.added} 条，耗时 {self.elapsed_ms} ms"
			+ (f"，{len(self.errors)} 个会话出错" if self.errors else "")
		)


def sync(
	store: Store,
	backend: Backend,
	config: dict[str, Any],
	*,
	full: bool = False,
	since: int = 0,
	conversations: Sequence[str] | None = None,
	limit: int = 0,
	replace: bool = False,
	progress: Callable[[str], None] | None = None,
) -> SyncReport:
	"""把一个后端的会话与消息同步进 ``store`` 与各会话工作区。

	:param store: 归一化数据库。
	:param backend: 读取后端。
	:param config: 完整配置（使用 ``extract`` 段）。
	:param full: ``True`` 时忽略游标，从最早重新拉一遍（幂等，不会重复计数）。
	:param since: 统一的最早时间下界（秒级时间戳）。
	:param conversations: 只同步这些会话（id 或显示名，支持子串匹配）；``None`` 时用配置里的过滤。
	:param limit: 每个会话最多摘多少条（0 = 用配置，配置为 0 表示不限）。
	:param replace: ``True`` 时用后端当前内容**覆盖**同 uid 的旧行。
		典型场景：先摘了一遍（某些正文因缺少 zstd 依赖只是占位符），补齐依赖后重摘。
	:param progress: 进度回调（收到一行人类可读文本）。
	:return: :class:`SyncReport`。
	"""
	settings = dict((config or {}).get("extract") or {})
	workdir = str((config or {}).get("workdir") or ".")
	cap = int(limit or settings.get("max_messages_per_conversation") or 0)
	replace = bool(replace or settings.get("replace_existing", False))
	write_jsonl = bool(settings.get("write_workspace_jsonl", True))
	filters = list(conversations) if conversations else list(settings.get("conversations") or [])

	started = time.time()
	report = SyncReport(
		backend=getattr(backend, "name", "unknown"),
		reason=str((config or {}).get("_backend_reason") or ""),
		synthetic=bool(getattr(backend, "describe", lambda: {})().get("synthetic")),
		full=full,
	)

	try:
		known = backend.list_conversations()
	except BackendError as error:
		report.errors.append(f"会话列表读取失败：{error}")
		report.elapsed_ms = int((time.time() - started) * 1000)
		return report

	selected = _select(known, filters)
	report.conversations = len(selected)
	if progress:
		progress(f"后端 {report.backend}：共 {len(known)} 个会话，本次处理 {len(selected)} 个")

	for conversation in selected:
		detail = _sync_one(store, backend, conversation, workdir=workdir, full=full, since=since, cap=cap, write_jsonl=write_jsonl, replace=replace)
		report.details.append(detail)
		report.fetched += detail.fetched
		report.added += detail.added
		if detail.error:
			report.errors.append(f"{detail.display_name}：{detail.error}")
		if progress:
			suffix = f"（错误：{detail.error}）" if detail.error else ""
			progress(f"  {detail.display_name}: 读到 {detail.fetched}，新增 {detail.added}{suffix}")

	store.set_state(
		"last_sync",
		{
			"ts": int(time.time()),
			"backend": report.backend,
			"full": full,
			"conversations": report.conversations,
			"added": report.added,
			"fetched": report.fetched,
		},
	)
	report.elapsed_ms = int((time.time() - started) * 1000)
	return report


def _select(conversations: Iterable[Conversation], filters: Sequence[str]) -> list[Conversation]:
	"""按 id/显示名筛选会话；``filters`` 为空表示全选。"""
	items = list(conversations)
	if not filters:
		return items
	needles = [str(item).strip().lower() for item in filters if str(item).strip()]
	selected = []
	for conversation in items:
		haystacks = {conversation.id.lower(), conversation.display_name.lower(), conversation.name.lower(), conversation.remark.lower()}
		if any(needle == hay or needle in hay for needle in needles for hay in haystacks):
			selected.append(conversation)
	return selected


def _sync_one(
	store: Store,
	backend: Backend,
	conversation: Conversation,
	*,
	workdir: str,
	full: bool,
	since: int,
	cap: int,
	write_jsonl: bool,
	replace: bool = False,
) -> ConversationSync:
	"""同步单个会话：游标 → 拉取 → 入库 → 落工作区。"""
	detail = ConversationSync(conversation_id=conversation.id, display_name=conversation.display_name)
	store.upsert_conversation(conversation, backend=getattr(backend, "name", ""))
	detail.cursor_from = 0 if full else max(store.max_ts(conversation.id), int(since or 0))
	workspace = None
	if write_jsonl:
		workspace = isolate.workspace_for(workdir, conversation, backend_name=getattr(backend, "name", "")).ensure()

	batch: list[Message] = []
	try:
		for message in backend.iter_messages(conversation.id, since=detail.cursor_from, limit=cap):
			batch.append(message)
			detail.fetched += 1
			if len(batch) >= BATCH_SIZE:
				detail.added += _flush(store, workspace, batch, replace=replace)
				batch = []
		if batch:
			detail.added += _flush(store, workspace, batch, replace=replace)
		stats = store.refresh_conversation_stats(conversation.id)
		detail.cursor_to = int(stats.get("last_ts") or detail.cursor_from)
		store.set_state(f"cursor:{getattr(backend, 'name', '')}:{conversation.id}", detail.cursor_to)
	except BackendError as error:
		detail.error = str(error)
	except Exception as error:  # pragma: no cover - 单个会话的意外错误不该终止整轮同步
		detail.error = f"{type(error).__name__}: {error}"
	return detail


def _flush(store: Store, workspace: isolate.ConversationWorkspace | None, batch: list[Message], *, replace: bool = False) -> int:
	"""把一批消息写进数据库与工作区，返回数据库新增条数。"""
	added = store.upsert_messages(batch, replace=replace)
	# 工作区 JSONL 是按 uid 去重追加的；覆盖模式下旧行已经过时（例如正文从占位符变成了明文），
	# 直接把这一批的内存缓存作废，让下一次读取重建，避免两份内容不一致。
	if workspace is not None:
		workspace.append_messages(batch)
	return added


# --------------------------------------------------------------------------- #
# 导出（把归一化库里的一个会话完整导出成人可读/可带走的形式）
# --------------------------------------------------------------------------- #


def export_conversation(
	store: Store,
	conversation: Conversation,
	out_dir: str | Path,
	*,
	formats: Sequence[str] = ("jsonl", "csv", "md"),
	redacted: bool = False,
) -> dict[str, Any]:
	"""把一个会话的消息导出到 ``out_dir``。

	:param store: 归一化数据库。
	:param conversation: 目标会话。
	:param out_dir: 输出目录（会创建 ``<slug>/`` 子目录）。
	:param formats: 需要的格式，支持 ``jsonl`` / ``csv`` / ``md`` / ``txt`` / ``html`` /
		``xlsx`` / ``pdf``（后四种由 :mod:`atuowexin.render` 渲染）。
	:param redacted: 是否对正文脱敏（给别人看/喂云端时用）。
	:return: ``{"dir": ..., "files": [...], "messages": N}``。
	"""
	allowed = {"jsonl", "csv", "md", "txt", "html", "xlsx", "pdf"}
	ordered: list[str] = []
	for item in formats:
		fmt = str(item).strip().lower()
		if not fmt or fmt in ordered:
			continue
		if fmt not in allowed:
			raise ValueError(f"不支持的导出格式：{fmt}（可选：{', '.join(sorted(allowed))}）")
		ordered.append(fmt)
	formats = tuple(ordered)
	target = Path(out_dir) / conversation.slug
	target.mkdir(parents=True, exist_ok=True)
	messages = store.messages(conversation.id)
	written: list[str] = []

	if "jsonl" in formats:
		path = target / "messages.jsonl"
		with path.open("w", encoding="utf-8") as handle:
			for message in messages:
				payload = message.to_dict()
				if redacted:
					from .model import redact as _redact

					payload["text"] = _redact(payload.get("text") or "")
				handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
		written.append(str(path))

	if "csv" in formats:
		path = target / "messages.csv"
		with path.open("w", encoding="utf-8-sig", newline="") as handle:
			writer = csv.writer(handle)
			writer.writerow(["时间", "会话", "发送者", "是否本人", "类型", "内容", "server_id"])
			for message in messages:
				text = message.text
				if redacted:
					from .model import redact as _redact

					text = _redact(text)
				writer.writerow(
					[
						format_ts(message.ts, "%Y-%m-%d %H:%M:%S"),
						conversation.display_name,
						message.sender_name or message.sender_id,
						"是" if message.is_self else "否",
						message.label,
						text,
						message.server_id,
					]
				)
		written.append(str(path))

	if "md" in formats:
		path = target / "messages.md"
		lines = [f"# {conversation.display_name}", "", f"- 会话 id：`{conversation.id}`", f"- 类型：{conversation.kind}", f"- 消息数：{len(messages)}", ""]
		for message in messages:
			line = message.display_line(redacted=redacted)
			lines.append(f"- {line}")
		path.write_text("\n".join(lines) + "\n", encoding="utf-8")
		written.append(str(path))

	# 多格式渲染（txt / html / xlsx / pdf）：版式参考 WeChat-Export-Tool 的 exporters/，
	# 依赖策略见 render.py —— 缺可选依赖时抛 RenderError，不静默降级。
	from . import render as _render

	for fmt in formats:
		if fmt in _render.SUPPORTED:
			written.append(_render.export_format(fmt, messages, conversation, target, redacted=redacted))

	profile = {
		"conversation": conversation.to_dict(),
		"exported_at": int(time.time()),
		"messages": len(messages),
		"formats": list(formats),
		"redacted": redacted,
	}
	(target / "conversation.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
	written.append(str(target / "conversation.json"))
	return {"dir": str(target), "files": written, "messages": len(messages)}


#: 会话类型的中文名（导出成 md 时用）。
_KIND_LABELS = {
	"friend": "好友",
	"group": "群聊",
	"official": "公众号",
	"service": "服务号",
	"system": "系统",
	"unknown": "未知",
}


def _message_bullet(message: Message, *, redacted: bool, self_name: str) -> str:
	"""把一条消息渲染成 Markdown 列表项（多行正文缩进续行，不破坏列表结构）。"""
	who = self_name if message.is_self else (message.sender_name or message.sender_id or "未知")
	text = message.text or ""
	if redacted:
		from .model import redact as _redact

		text = _redact(text)
	stamp = format_ts(message.ts, "%Y-%m-%d %H:%M")
	if not text:
		text = f"[{message.kind or '空消息'}]"
	first, *rest = text.split("\n")
	lines = [f"- **{stamp}** {who}：{first}"]
	lines.extend(f"  {item}" for item in rest)
	return "\n".join(lines)


def _conversation_markdown(
	conversation: Conversation,
	messages: Sequence[Message],
	*,
	redacted: bool,
	self_name: str,
	heading_level: int = 1,
) -> str:
	"""渲染单个会话的 Markdown。"""
	label = _KIND_LABELS.get(conversation.kind, conversation.kind)
	if conversation.member_count:
		label += f"（{conversation.member_count} 人）"
	lines = [f"{'#' * heading_level} {conversation.display_name}", ""]
	lines.append(f"- 会话 id：`{conversation.id}`")
	lines.append(f"- 类型：{label}")
	lines.append(f"- 消息数：{len(messages)}")
	if messages:
		first = format_ts(messages[0].ts, "%Y-%m-%d %H:%M")
		last = format_ts(messages[-1].ts, "%Y-%m-%d %H:%M")
		lines.append(f"- 时间范围：{first} → {last}")
	lines.extend(["", "---", ""])
	lines.extend(_message_bullet(item, redacted=redacted, self_name=self_name) for item in messages)
	lines.append("")
	return "\n".join(lines)


def export_markdown_bundle(
	store: Store,
	out_dir: str | Path,
	*,
	conversations: Sequence[Conversation] | None = None,
	order: str = "last_time",
	limit: int = 0,
	redacted: bool = False,
	self_name: str = "我",
	include_combined: bool = True,
	progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
	"""把所有（或指定的）会话导出成一套 Markdown 聊天记录。

	产物：

	* ``<out_dir>/00-总览.md`` —— 会话清单（类型/条数/时间范围/文件链接）
	* ``<out_dir>/全部聊天记录.md`` —— 全部会话拼成一篇（含目录）
	* ``<out_dir>/按会话/NN-<slug>.md`` —— 每个会话一个文件

	:param store: 归一化数据库。
	:param out_dir: 输出目录。
	:param conversations: 指定会话；``None`` 表示全部。
	:param order: ``last_time`` / ``msg_count`` / ``name``（决定编号与总览顺序）。
	:param limit: 最多导出多少个会话（0=不限）。
	:param redacted: 是否对手机号/证件号/卡号脱敏（给别人看时打开）。
	:param self_name: 代表"我"的名字。
	:param include_combined: 是否额外生成合订本。
	:return: ``{"out_dir", "overview", "combined", "files", "conversations", "messages", "items"}``。
	"""
	target = Path(out_dir)
	target.mkdir(parents=True, exist_ok=True)
	per_dir = target / "按会话"
	per_dir.mkdir(parents=True, exist_ok=True)

	items = list(conversations) if conversations is not None else store.list_conversations(order=order)
	if limit and limit > 0:
		items = items[:limit]

	sections: list[tuple[Conversation, list[Message], Path]] = []
	total = 0
	for index, conversation in enumerate(items, start=1):
		messages = store.messages(conversation.id)
		total += len(messages)
		filename = f"{index:02d}-{conversation.slug}.md"
		path = per_dir / filename
		path.write_text(
			_conversation_markdown(conversation, messages, redacted=redacted, self_name=self_name),
			encoding="utf-8",
		)
		sections.append((conversation, messages, path))
		if progress is not None:
			progress(f"  {index:02d}/{len(items)} {conversation.display_name}：{len(messages)} 条 → 按会话/{filename}")

	stamp = format_ts(int(time.time()), "%Y-%m-%d %H:%M")
	overview_lines = [
		"# 微信聊天记录总览",
		"",
		f"- 导出时间：{stamp}",
		f"- 会话数：{len(sections)}",
		f"- 消息数：{total}",
		f"- 脱敏：{'是' if redacted else '否'}",
		"",
		"| # | 会话 | 类型 | 消息数 | 时间范围 | 文件 |",
		"| --- | --- | --- | --- | --- | --- |",
	]
	for index, (conversation, messages, path) in enumerate(sections, start=1):
		label = _KIND_LABELS.get(conversation.kind, conversation.kind)
		if conversation.member_count:
			label += f"（{conversation.member_count} 人）"
		span = "—"
		if messages:
			span = f"{format_ts(messages[0].ts, '%m-%d %H:%M')} → {format_ts(messages[-1].ts, '%m-%d %H:%M')}"
		overview_lines.append(
			f"| {index:02d} | {conversation.display_name} | {label} | {len(messages)} | {span} | [打开](按会话/{path.name}) |"
		)
	overview_lines.append("")
	overview = target / "00-总览.md"
	overview.write_text("\n".join(overview_lines), encoding="utf-8")

	written = [str(overview), *(str(path) for _, _, path in sections)]
	combined_path = ""
	if include_combined:
		combined_lines = [
			"# 全部聊天记录",
			"",
			f"> 导出时间 {stamp}　共 {len(sections)} 个会话 / {total} 条消息"
			+ ("　（正文已脱敏）" if redacted else ""),
			"",
			"## 目录",
			"",
		]
		combined_lines.extend(
			f"{index:02d}. {conversation.display_name}（{len(messages)} 条）"
			for index, (conversation, messages, _) in enumerate(sections, start=1)
		)
		combined_lines.extend(["", "---", ""])
		for conversation, messages, _ in sections:
			combined_lines.append(_conversation_markdown(conversation, messages, redacted=redacted, self_name=self_name, heading_level=2))
			combined_lines.append("---")
			combined_lines.append("")
		combined = target / "全部聊天记录.md"
		combined.write_text("\n".join(combined_lines), encoding="utf-8")
		combined_path = str(combined)
		written.append(combined_path)

	return {
		"out_dir": str(target),
		"overview": str(overview),
		"combined": combined_path,
		"files": written,
		"conversations": len(sections),
		"messages": total,
		"items": [
			{
				"index": index,
				"conversation_id": conversation.id,
				"display_name": conversation.display_name,
				"kind": conversation.kind,
				"messages": len(messages),
				"file": str(path),
			}
			for index, (conversation, messages, path) in enumerate(sections, start=1)
		],
	}


__all__ = ["SyncReport", "ConversationSync", "sync", "export_conversation", "export_markdown_bundle", "BATCH_SIZE"]
