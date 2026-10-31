# -*- coding: utf-8 -*-
"""配置：默认值 → 配置文件 → 环境变量 三层合并，只做「够用的校验」。

配置文件默认在 ``%USERPROFILE%\\.atuowexin\\config.json``（可用 ``ATUOWEXIN_CONFIG``
指定）。环境变量优先级最高，方便在 CI / 临时会话里覆盖而不动文件。

设计取向：**不引入 pydantic / yaml 之类的依赖**，因为这个引擎要在 DSH 自带运行时里
直接跑（只有标准库保证一定存在）。因此配置就是一个普通 dict，配 ``Config.get`` 的点路径读取。
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from . import paths as _paths

# --------------------------------------------------------------------------- #
# 默认配置
# --------------------------------------------------------------------------- #

DEFAULTS: dict[str, Any] = {
	# 引擎工作目录：所有摘录、隔离工作区、草稿、审计都落在这里
	"workdir": "",
	# 读取后端：auto 会按 chatlog → decrypted_sqlite → mock 的顺序探测
	"backend": {
		"name": "auto",
		"chatlog": {
			"base_url": "http://127.0.0.1:5030",
			"timeout": 30.0,
			"page_size": 500,
			"auth_key": "",
		},
		"decrypted_sqlite": {
			"root": "",
			"schema": "auto",  # auto | v4 | v3
		},
		"mock": {
			"conversations": 3,
			"messages_per": 25,
			"days": 30,
			"seed": 7,
		},
	},
	"extract": {
		"page_size": 500,
		"max_messages_per_conversation": 0,  # 0 = 不限，完整摘录
		"write_workspace_jsonl": True,
		"include_media": True,
		"media_download": False,
		"conversations": [],  # 空 = 全部会话
	},
	# 导出/预览时对手机号、证件号、银行卡号打码（只影响输出，不改动原始数据）
	"privacy": {
		"redact_phone": True,
		"redact_id_card": True,
		"redact_bank_card": True,
	},
}

#: 环境变量 → 配置点路径。
_ENV_MAP: dict[str, tuple[str, type]] = {
	"ATUOWEXIN_WORKDIR": ("workdir", str),
	"ATUOWEXIN_BACKEND": ("backend.name", str),
	"ATUOWEXIN_CHATLOG_URL": ("backend.chatlog.base_url", str),
	"ATUOWEXIN_CHATLOG_KEY": ("backend.chatlog.auth_key", str),
	"ATUOWEXIN_DB_ROOT": ("backend.decrypted_sqlite.root", str),
}


def default_config() -> dict[str, Any]:
	"""返回一份全新的默认配置（深拷贝，调用方可随意改）。"""
	return copy.deepcopy(DEFAULTS)


def merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
	"""递归合并两个配置字典：``override`` 里的值覆盖 ``base``。"""
	result = copy.deepcopy(base)
	for key, value in (override or {}).items():
		if isinstance(value, dict) and isinstance(result.get(key), dict):
			result[key] = merge(result[key], value)
		else:
			result[key] = copy.deepcopy(value)
	return result


def _coerce_bool(value: Any) -> bool:
	"""把环境变量里的 ``1/true/yes/on`` 认成真。"""
	if isinstance(value, bool):
		return value
	return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _set_path(config: dict[str, Any], dotted: str, value: Any) -> None:
	"""按点路径写值，中间层不存在就建。"""
	parts = dotted.split(".")
	cursor = config
	for part in parts[:-1]:
		next_value = cursor.get(part)
		if not isinstance(next_value, dict):
			next_value = {}
			cursor[part] = next_value
		cursor = next_value
	cursor[parts[-1]] = value


def get_path(config: dict[str, Any], dotted: str, fallback: Any = None) -> Any:
	"""按点路径读值，如 ``get_path(cfg, "llm.model")``。"""
	cursor: Any = config
	for part in dotted.split("."):
		if not isinstance(cursor, dict) or part not in cursor:
			return fallback
		cursor = cursor[part]
	return cursor


def load_config(path: str | os.PathLike[str] | None = None, *, overrides: dict[str, Any] | None = None, use_env: bool = True) -> dict[str, Any]:
	"""加载配置：默认值 → 文件 → 环境变量 → 显式 ``overrides``。

	:param path: 配置文件路径；``None`` 时用 :func:`atuowexin.paths.config_path`。
	:param overrides: 调用方（CLI/插件）传的最高优先级覆盖。
	:param use_env: 是否读环境变量。
	:return: 合并后的配置 dict。
	"""
	config = default_config()
	target = Path(path) if path is not None else _paths.config_path()
	if target.is_file():
		try:
			raw = json.loads(target.read_text(encoding="utf-8"))
		except (OSError, ValueError) as error:
			raise ConfigError(f"配置文件无法解析：{target}（{error}）") from error
		if not isinstance(raw, dict):
			raise ConfigError(f"配置文件顶层必须是 JSON 对象：{target}")
		config = merge(config, raw)

	if use_env:
		for env_name, (dotted, kind) in _ENV_MAP.items():
			raw_value = os.environ.get(env_name)
			if raw_value is None or raw_value == "":
				continue
			_set_path(config, dotted, _coerce_bool(raw_value) if kind is bool else raw_value)

	if overrides:
		config = merge(config, overrides)

	config["workdir"] = str(config.get("workdir") or _paths.default_workdir())
	return config


def save_config(config: dict[str, Any], path: str | os.PathLike[str] | None = None) -> Path:
	"""把配置写到磁盘（UTF-8、缩进 2 空格、保留中文）。"""
	target = Path(path) if path is not None else _paths.config_path()
	target.parent.mkdir(parents=True, exist_ok=True)
	target.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
	return target


def validate(config: dict[str, Any]) -> list[str]:
	"""返回配置问题列表（空列表表示通过）。只报「会真的出错」的问题。"""
	problems: list[str] = []
	backend = get_path(config, "backend.name", "auto")
	if backend not in {"auto", "chatlog", "decrypted_sqlite", "mock"}:
		problems.append(f"backend.name 取值非法：{backend}（可选 auto/chatlog/decrypted_sqlite/mock）")
	if backend in {"auto", "chatlog"}:
		url = get_path(config, "backend.chatlog.base_url", "")
		if not str(url).startswith(("http://", "https://")):
			problems.append(f"backend.chatlog.base_url 必须是 http(s) 地址：{url!r}")
	if backend == "decrypted_sqlite" and not get_path(config, "backend.decrypted_sqlite.root"):
		problems.append("backend=decrypted_sqlite 时必须配置 backend.decrypted_sqlite.root")
	return problems


class ConfigError(RuntimeError):
	"""配置层错误。"""


__all__ = [
	"DEFAULTS",
	"ConfigError",
	"default_config",
	"merge",
	"load_config",
	"save_config",
	"validate",
	"get_path",
]
