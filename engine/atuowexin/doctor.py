# -*- coding: utf-8 -*-
"""体检（doctor）：把「为什么读不到数据」变成可执行的下一步。

设计原则：**每一项都给出可操作的建议**。用户看到的不是「backend not ok」，
而是「检测到你的微信数据在 ``%USERPROFILE%\\Documents\\xwechat_files``，但那是加密库，
请先用 chatlog 解密（命令如下）或者用 MemoTrace 导出后指向 backend.decrypted_sqlite.root」。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__, config as config_module, paths
from .backends import build_backend, health_all
from .store import Store


@dataclass
class Check:
	"""一项体检结果。"""

	name: str
	ok: bool
	detail: str = ""
	hint: str = ""
	level: str = "info"  # info | warn | error

	def to_dict(self) -> dict[str, Any]:
		"""转成可 JSON 序列化的字典。"""
		return {"name": self.name, "ok": self.ok, "detail": self.detail, "hint": self.hint, "level": self.level}


@dataclass
class DoctorReport:
	"""一次体检的完整结果。"""

	checks: list[Check] = field(default_factory=list)
	config_path: str = ""
	workdir: str = ""
	backend: str = ""
	backend_reason: str = ""
	started_at: int = 0

	@property
	def ok(self) -> bool:
		"""没有任何 ``error`` 级失败即视为通过。"""
		return all(check.ok for check in self.checks if check.level == "error")

	def to_dict(self) -> dict[str, Any]:
		"""转成可 JSON 序列化的字典。"""
		return {
			"ok": self.ok,
			"version": __version__,
			"config_path": self.config_path,
			"workdir": self.workdir,
			"backend": self.backend,
			"backend_reason": self.backend_reason,
			"checks": [check.to_dict() for check in self.checks],
			"started_at": self.started_at,
		}

	def render(self) -> str:
		"""渲染成给人看的体检报告。"""
		lines = [f"atuowexin {__version__} 环境体检", f"配置文件：{self.config_path}", f"工作目录：{self.workdir}", ""]
		icon = {True: "[ OK ]", False: "[FAIL]"}
		for check in self.checks:
			mark = icon[check.ok] if check.level == "error" else ("[WARN]" if not check.ok else "[ OK ]")
			lines.append(f"{mark} {check.name}：{check.detail}")
			if check.hint and not check.ok:
				lines.append(f"       → {check.hint}")
		lines.append("")
		lines.append(f"当前后端：{self.backend}（{self.backend_reason or '未解析'}）")
		return "\n".join(lines)


def run_doctor(config: dict[str, Any], *, config_path: str | Path | None = None) -> DoctorReport:
	"""执行全部体检项。

	:param config: 已合并的配置。
	:param config_path: 配置文件路径（仅用于展示）。
	:return: :class:`DoctorReport`。
	"""
	report = DoctorReport(
		config_path=str(config_path if config_path is not None else paths.config_path()),
		workdir=str((config or {}).get("workdir") or ""),
		started_at=int(time.time()),
	)

	# 1. 运行时
	version = sys.version_info
	report.checks.append(
		Check(
			"Python 运行时",
			version >= (3, 9),
			f"{sys.version.split()[0]}（{sys.executable}）",
			"需要 Python 3.9 及以上",
		)
	)

	# 2. 工作目录可写
	workdir = Path(report.workdir or ".")
	try:
		workdir.mkdir(parents=True, exist_ok=True)
		probe = workdir / ".atuowexin-write-probe"
		probe.write_text("ok", encoding="utf-8")
		probe.unlink()
		report.checks.append(Check("工作目录可写", True, str(workdir)))
	except OSError as error:
		report.checks.append(Check("工作目录可写", False, f"{workdir}：{error}", "换一个可写目录，或设置 ATUOWEXIN_WORKDIR", "error"))

	# 3. 配置自检
	problems = config_module.validate(config)
	report.checks.append(
		Check(
			"配置自检",
			not problems,
			"通过" if not problems else "；".join(problems),
			"运行 `python -m atuowexin config init` 生成模板，或手工补齐上述字段",
			"error" if problems else "info",
		)
	)

	# 4. 读取后端
	backend_results = health_all(config)
	for item in backend_results:
		name = str(item.get("backend"))
		report.checks.append(
			Check(
				f"读取后端 · {name}",
				bool(item.get("ok")),
				str(item.get("detail") or ""),
				_backend_hint(name, item),
				"info" if item.get("ok") else "warn",
			)
		)

	try:
		backend, reason = build_backend(config)
		report.backend = getattr(backend, "name", "")
		report.backend_reason = reason
		report.checks.append(Check("当前生效后端", True, f"{report.backend}（{reason}）"))
	except Exception as error:
		report.checks.append(Check("当前生效后端", False, str(error), "检查 backend 配置段", "error"))

	# 6. 微信数据目录
	roots = paths.detect_wechat_data_roots()
	if roots:
		detail = "；".join(f"{item['path']}（{item['schema']}，账号 {len(item['accounts'])} 个）" for item in roots)
		report.checks.append(
			Check(
				"本机微信数据目录",
				True,
				detail,
				"这些目录里的 .db 是 **SQLCipher 加密**的，本插件不去解密。要读真实聊天记录，请先用外部工具"
				"（chatlog / MemoTrace 留痕）导出为明文库，再把 backend.decrypted_sqlite.root 指过去；"
				"若你已有密钥，也可以用 `python -m atuowexin decrypt --help` 自行解密。"
				"⚠️ 微信 ≥ 4.1.10 起密钥「零驻留」，纯内存扫描拿不到：只能（a）暂时降到 ≤4.1.9 提取一次 passphrase"
				"（该 passphrase 跨版本长期有效，提取后可升回新版）或（b）在登录解锁瞬间用调试器/Frida 抓取。",
			)
		)
	else:
		report.checks.append(
			Check(
				"本机微信数据目录",
				False,
				"未检测到 xwechat_files / WeChat Files",
				"如果你把微信数据放在自定义盘符，请手动配置 backend.decrypted_sqlite.root",
				"warn",
			)
		)

	# 7. chatlog 可用性
	chatlog_bin = shutil.which("chatlog")
	docker_bin = shutil.which("docker")
	report.checks.append(
		Check(
			"chatlog / Docker",
			bool(chatlog_bin or docker_bin),
			f"chatlog={'已安装' if chatlog_bin else '未找到'}；docker={'已安装' if docker_bin else '未找到'}",
			"chatlog 主仓库已于 2025-10 被作者删除，但 Docker 镜像仍在："
			"docker run -d --name chatlog -p 127.0.0.1:5030:5030 -v <微信数据目录>:/app/data ghcr.io/sjzar/chatlog:latest",
			"warn" if not (chatlog_bin or docker_bin) else "info",
		)
	)

	# 8. SQLCipher 解密工具（可选）
	try:
		from . import sqlcipher  # type: ignore[attr-defined]

		shell = sqlcipher.powershell_path()
		report.checks.append(
			Check(
				"SQLCipher 解密工具",
				bool(shell),
				f"PowerShell：{shell or '未找到'}",
				"Windows 上需要 PowerShell（pwsh 或系统自带 powershell.exe）才能用内置解密；否则请用外部工具解密",
				"warn" if not shell else "info",
			)
		)
	except ImportError:
		report.checks.append(Check("SQLCipher 解密工具", False, "模块未安装（sqlcipher.py 缺失）", "可选功能：缺少时不影响读取已解密库与 chatlog", "warn"))

	# 8.5 WAL：加密库里还有多少**未 checkpoint 的已提交数据**（决定解密是否完整）
	report.checks.append(_wal_check(roots))

	# 8.6 解密密钥：有没有、够不够、现在能不能自动取 —— “每次用之前先看有没有 key”的落点
	report.checks.append(_key_check())

	# 8.6 可选依赖（zstd 解压正文 / Pillow 截图判据）
	try:
		from . import deps as deps_module

		states = deps_module.status()
		missing = [name for name, info in states.items() if not info.get("ok")]
		detail = "；".join(
			"{0}={1}".format(name, (info.get("module") or "未安装")) for name, info in states.items()
		)
		report.checks.append(
			Check(
				"可选依赖（zstd 解压正文）",
				not missing,
				detail + "（目录：{0}）".format(deps_module.optional_dir()),
				"缺 zstandard 时 WCDB 压缩过的消息正文只能留占位符：运行 "
				"`python -m atuowexin deps install`（装进交付物自己的 .tools/python，不动系统 Python）",
				"warn" if missing else "info",
			)
		)
	except Exception as error:  # pragma: no cover - 依赖模块本身不该让 doctor 崩
		report.checks.append(Check("可选依赖（zstd 解压正文）", False, str(error), "", "warn"))

	# 9. 已有数据概览
	try:
		if paths.db_path(report.workdir).exists():
			with Store(paths.db_path(report.workdir)) as store:
				overview = store.overview()
			report.checks.append(
				Check("本地归一化库", True, f"{overview['conversations']} 会话 / {overview['messages']} 消息")
			)
		else:
			report.checks.append(Check("本地归一化库", False, f"尚未创建（{paths.db_path(report.workdir)}）", "运行 `python -m atuowexin sync` 开始首次摘录", "warn"))
	except Exception as error:  # pragma: no cover - 库损坏时不该让 doctor 崩
		report.checks.append(Check("本地归一化库", False, str(error), "删除损坏的 atuowexin.db 后重新 sync", "warn"))

	return report


def _wal_check(roots: list[dict[str, Any]]) -> Check:
	"""体检项：本机加密库旁边的 ``-wal`` 里有没有**未 checkpoint 的已提交数据**。

	这一项存在的理由：只看主库解密会得到一个"看起来完整"的结果，而最新消息可能
	还在 WAL 里；反过来，WAL 也可能是 SQLite 重置后的残留帧，照抄会把库写坏。
	所以这里按 sqlite3 的规则（头部/帧 salt + 累计校验和）逐帧判定，给出确定的结论。
	"""
	try:
		from . import sqlcipher
	except ImportError:  # pragma: no cover
		return Check("WAL 待合并数据", False, "sqlcipher 模块缺失", "", "warn")

	candidates: list[Path] = []
	for item in roots:
		root = Path(str(item.get("path")))
		for account in item.get("accounts") or []:
			candidates.append(root / str(account))
	if not candidates:
		return Check("WAL 待合并数据", True, "未检测到微信数据目录，跳过", "", "info")

	pending: list[str] = []
	stale = 0
	corrupt: list[str] = []
	empty = 0
	journals: list[str] = []
	for base in candidates:
		for wal in sorted(base.rglob("*.db-wal")):
			try:
				if wal.stat().st_size <= 32:
					empty += 1
					continue
			except OSError:
				continue
			info = sqlcipher.wal_info(wal)
			if info.get("valid_frames") and info.get("last_commit"):
				pending.append(f"{wal.name}（{info['valid_frames']} 帧 / {info['commits']} 次提交）")
			elif info.get("corrupt"):
				corrupt.append(wal.name)
			elif info.get("stale"):
				stale += 1
		for journal in sorted(base.rglob("*.db-journal")):
			try:
				if journal.stat().st_size > 0:
					journals.append(journal.name)
			except OSError:
				continue

	if pending or corrupt:
		detail = []
		if pending:
			detail.append("有未 checkpoint 的新数据：" + "、".join(pending[:6]))
		if corrupt:
			detail.append("校验链断裂（撕裂写入）：" + "、".join(corrupt[:6]))
		if stale:
			detail.append(f"另有 {stale} 个是重置后的残留帧（会按 sqlite3 的规则忽略）")
		return Check(
			"WAL 待合并数据",
			False,
			"；".join(detail),
			"用 `python -m atuowexin ingest --src <加密目录> --keymap <keymap.json>` 解密时**会自动合并**已提交的 WAL 帧；"
			"残留帧则必须忽略（照抄会把主库写坏）。",
			"warn",
		)
	detail = f"{stale} 个 -wal 均为重置后的残留帧（已 checkpoint，无新数据）"
	if empty:
		detail += f"；{empty} 个空 WAL"
	if journals:
		detail += f"；⚠️ 存在 {len(journals)} 个非空 .db-journal（热日志），主库可能含未提交页"
	return Check("WAL 待合并数据", not journals, detail, "先完全退出微信再复制数据目录，可让 WAL 合并回主库", "warn" if journals else "info")


def _key_check() -> Check:
	"""体检项：解密密钥（有没有 / 够不够 / 现在能不能自动取）。

	这一步就是"每次使用之前先看有没有 key"的落点：keymap 在、且覆盖了本机所有加密库 →
	直接可用；不齐或缺失 → 告诉用户一条命令（``key ensure``）就能自动去读，
	并且如实说明**现在能不能读**（取决于微信在不在运行、什么版本）。
	"""
	try:
		from . import keyfinder
	except ImportError:  # pragma: no cover
		return Check("解密密钥", False, "keyfinder 模块缺失", "", "warn")

	keymap_path = paths.atuowexin_home() / "keymap.json"
	table = keyfinder.load_keymap(keymap_path)
	parts = [f"keymap {'已配置' if table else '未配置'}（{keymap_path}，{len(table)} 把）"]

	encrypted = None
	try:
		for item in paths.detect_wechat_data_roots():
			base = Path(str(item.get("path")))
			for account in item.get("accounts") or []:
				for candidate in (base / str(account) / "db_storage", base / str(account)):
					if candidate.is_dir():
						encrypted = candidate
						break
				if encrypted is not None:
					break
			if encrypted is not None:
				break
	except Exception:  # pragma: no cover - 探测失败不影响体检其它项
		encrypted = None

	ok = bool(table)
	missing_names: list[str] = []
	if encrypted is not None:
		try:
			dbs = [db for db in sorted(encrypted.rglob("*.db")) if db.is_file()]
			covered = 0
			for db in dbs:
				try:
					salt = db.read_bytes()[:16].hex()
				except OSError:
					continue
				if salt in table:
					covered += 1
				else:
					missing_names.append(db.name)
			parts.append(f"本机加密库 {covered}/{len(dbs)} 个有密钥")
			ok = bool(dbs) and not missing_names
		except OSError:
			pass

	try:
		processes = keyfinder.list_wechat_processes()
	except Exception:  # pragma: no cover
		processes = []
	parts.append(f"微信进程 {len(processes)} 个")

	if ok:
		return Check("解密密钥", True, "；".join(parts), "", "info")

	hint_parts: list[str] = []
	hint_parts.append(
	"一条龙：`python -m atuowexin key auto`（关微信 → 起 Hook → 拉起微信 → 点登录 → 取 key → WCDB 校验）；"
	"它会在需要你登录时返回 stage=login_required（退出码 3）。"
)
	if not processes:
		hint_parts.append("现在微信没在运行：先跑 key auto（或手动启动并登录微信后再捕获）。")
	else:
		hint_parts.append("微信在运行：缺的通常是本次没被打开过的库，在微信里点开对应功能后再跑一次。")
	if missing_names:
		hint_parts.append("缺：" + "、".join(missing_names[:6]))
	return Check("解密密钥", False, "；".join(parts), " ".join(hint_parts), "warn")


def _backend_hint(name: str, item: dict[str, Any]) -> str:
	"""按后端类型给出下一步建议。"""
	if item.get("ok"):
		return ""
	if name == "chatlog":
		return "启动 chatlog（TUI 或 `chatlog server -a 127.0.0.1:5030`），确保微信已登录且库已解密"
	if name == "wcdb":
		return ("WCDB 没打开成功：确认 ①微信在运行且已登录（快照要一致）②桌面 wx_export/key.txt 或 backend.wcdb.key 有 64 位 key ③参考工具的 dll/ 与 scripts/node_modules(koffi) 在位；"
			"必要时先退出微信再重试")
	if name == "decrypted_sqlite":
		return "把 backend.decrypted_sqlite.root 指向**已解密**的数据目录（含 db_storage，或 3.x 的 MSG*.db）"
	if name == "mock":
		return "合成数据后端仅用于离线自测"
	return ""


__all__ = ["Check", "DoctorReport", "run_doctor"]
