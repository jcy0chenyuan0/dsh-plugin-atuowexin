# -*- coding: utf-8 -*-
"""后端注册与自动探测。

``backend.name = "auto"`` 时的优先级：**chatlog → decrypted_sqlite → mock**。
理由：chatlog 能处理微信 4.x 的密钥与媒体，是自动化程度最高的；已解密库是合规最优解；
mock 只用于「本机没有任何真实数据」时把链路跑通——它会明确标注自己是合成数据，
doctor 与 CLI 都会把这一点显示给用户，**绝不把合成数据伪装成真实聊天**。
"""

from __future__ import annotations

from typing import Any

from .base import Backend, BackendError, BaseBackend
from .chatlog_http import ChatlogBackend
from .decrypted_sqlite import DecryptedSqliteBackend
from .mock import MockBackend
from .wcdb_server import WcdbBackend

BACKEND_NAMES = ("auto", "chatlog", "wcdb", "decrypted_sqlite", "mock")


def build_backend(config: dict[str, Any], *, name: str | None = None) -> tuple[Backend, str]:
	"""按配置构造后端。

	:param config: 完整配置（取 ``backend`` 段）。
	:param name: 强制指定后端名；``None`` 时用配置里的 ``backend.name``。
	:return: ``(backend, reason)``，``reason`` 解释为什么用它。
	:raises BackendError: 显式指定了后端但它不可用。
	"""
	settings = dict((config or {}).get("backend") or {})
	requested = (name or settings.get("name") or "auto").strip() or "auto"
	if requested == "mock":
		return MockBackend(settings.get("mock") or {}), "显式指定 mock（合成数据，仅用于离线自测）"
	if requested == "chatlog":
		backend = ChatlogBackend(settings.get("chatlog") or {})
		health = backend.health()
		if not health.get("ok"):
			raise BackendError(f"chatlog 后端不可用：{health.get('detail')}")
		return backend, "显式指定 chatlog"
	if requested == "wcdb":
		backend = WcdbBackend(settings.get("wcdb") or {})
		health = backend.health()
		if not health.get("ok"):
			raise BackendError(f"WCDB 后端不可用：{health.get('detail')}")
		return backend, "显式指定 wcdb（由 WCDB.dll 自己开库）"
	if requested == "decrypted_sqlite":
		return DecryptedSqliteBackend(settings.get("decrypted_sqlite") or {}), "显式指定 decrypted_sqlite"
	if requested != "auto":
		raise BackendError(f"未知后端：{requested}（可选 {'/'.join(BACKEND_NAMES)}）")
	return _auto_backend(settings)


def _auto_backend(settings: dict[str, Any]) -> tuple[Backend, str]:
	"""自动探测：依次尝试 chatlog、已解密库，最后退回合成数据。"""
	reasons: list[str] = []

	chatlog = ChatlogBackend(settings.get("chatlog") or {})
	health = chatlog.health()
	if health.get("ok"):
		return chatlog, f"自动探测命中 chatlog（{chatlog.base_url}）"
	reasons.append(f"chatlog 不可用：{health.get('detail')}")

	# 自动探测只试一次，避免 doctor 卡在重试上（显式 --backend wcdb 才用完整重试）
	auto_wcdb = dict(settings.get("wcdb") or {})
	auto_wcdb.setdefault("retries", 1)
	wcdb = WcdbBackend(auto_wcdb)
	wcdb_health = wcdb.health()
	if wcdb_health.get("ok"):
		return wcdb, f"自动探测命中 WCDB（{wcdb_health.get('detail')}）"
	wcdb.close()
	reasons.append(f"WCDB 不可用：{wcdb_health.get('detail')}")

	root = str((settings.get("decrypted_sqlite") or {}).get("root") or "").strip()
	if root:
		try:
			backend = DecryptedSqliteBackend(settings.get("decrypted_sqlite") or {})
			health = backend.health()
			if health.get("ok"):
				return backend, f"自动探测命中已解密库（{backend.root}，结构 {backend.describe().get('schema')}）"
			reasons.append(f"已解密库不可用：{health.get('detail')}")
		except BackendError as error:
			reasons.append(f"已解密库不可用：{error}")
	else:
		detected = _describe_detected_roots()
		reasons.append(
			"未配置 backend.decrypted_sqlite.root"
			+ (f"（检测到本机微信数据目录：{detected}，但它们是**加密**的，需要先解密或启用 chatlog）" if detected else "")
		)

	mock = MockBackend(settings.get("mock") or {})
	return mock, "退回合成数据后端（离线自测用，不是真实聊天）。原因：" + "；".join(reasons)


def _describe_detected_roots() -> str:
	"""把探测到的微信数据目录渲染成一行提示。"""
	from ..paths import detect_wechat_data_roots

	roots = detect_wechat_data_roots()
	if not roots:
		return ""
	return "，".join(f"{item['path']}（{item['schema']}）" for item in roots[:3])


def health_all(config: dict[str, Any]) -> list[dict[str, Any]]:
	"""逐个后端体检（doctor 用），任何一个失败都不影响其他项。"""
	settings = dict((config or {}).get("backend") or {})
	results: list[dict[str, Any]] = []

	try:
		results.append({"backend": "chatlog", **ChatlogBackend(settings.get("chatlog") or {}).health()})
	except Exception as error:  # pragma: no cover - 配置极端错误时的兜底
		results.append({"backend": "chatlog", "ok": False, "detail": str(error)})

	root = str((settings.get("decrypted_sqlite") or {}).get("root") or "").strip()
	if root:
		try:
			results.append({"backend": "decrypted_sqlite", **DecryptedSqliteBackend(settings.get("decrypted_sqlite") or {}).health()})
		except Exception as error:
			results.append({"backend": "decrypted_sqlite", "ok": False, "detail": str(error)})
	else:
		results.append(
			{
				"backend": "decrypted_sqlite",
				"ok": False,
				"detail": "未配置 backend.decrypted_sqlite.root" + (f"；本机检测到：{_describe_detected_roots()}" if _describe_detected_roots() else ""),
			}
		)

	results.append({"backend": "mock", **MockBackend(settings.get("mock") or {}).health()})
	return results


__all__ = [
	"Backend",
	"BackendError",
	"BaseBackend",
	"ChatlogBackend",
	"DecryptedSqliteBackend",
	"MockBackend",
	"BACKEND_NAMES",
	"build_backend",
	"health_all",
]
