# -*- coding: utf-8 -*-
"""atuowexin —— 微信本地聊天记录「完整摘录 + 会话隔离 + 导出」引擎（只读）。

设计三条底线（详见 ../../README.md）：

1. **只读不注入**：只消费「你已经解密的本地数据」或本机 chatlog HTTP 接口，
   不做内存搜索、不做 DLL 注入、不碰微信网络协议、**不发送任何消息**。
2. **会话强隔离**：每条会话一份独立工作区（``conversations/<slug>/``），
   任何查询都必须带 conversation_id，绝不跨会话拼接。
3. **不调用云端模型**：0.3.0 起模型层与发送层已从代码中删除（归档在
   ``<repo>/archive/atuowexin-v0.2.0-full-src.zip``），引擎不需要任何 API key。

模块按需导入：``import atuowexin`` 只暴露版本信息，重活留给具体子模块，
这样 ``store``/``backends`` 里任何一处依赖缺失都不会让整个包导入失败。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__version__ = "0.5.0"


def _bootstrap_optional_dependencies() -> list[str]:
	"""把交付物里的**可选依赖目录**接进 ``sys.path``。

	核心引擎是纯标准库，但真机上需要两个"可选依赖"：

	* ``zstandard`` —— 解 WCDB 压缩过的消息正文（微信 4.x 常见）；
	* ``wxauto4`` —— 可选的 UI 自动化发送后端。

	它们装在 ``<repo>/.tools/python``（``pip install --target`` 装进去，不污染 Harness
	自带运行时）。pywin32 用这种方式安装时**不会自动注册**，所以除了主目录还要把
	``win32`` / ``win32/lib`` 加进 ``sys.path``，并把 ``pywin32_system32`` 加进 DLL
	搜索目录——否则 ``import wxauto4`` 会报 ``No module named 'win32gui'``。

	也可用环境变量 ``ATUOWEXIN_PYTHONPATH`` 指向别处的可选依赖目录。

	:return: 本次真正加进 ``sys.path`` 的路径列表（便于诊断）。
	"""
	candidates: list[Path] = []
	configured = os.environ.get("ATUOWEXIN_PYTHONPATH")
	if configured:
		candidates.append(Path(configured))
	# engine/atuowexin/__init__.py -> atuowexin -> engine -> <repo>
	candidates.append(Path(__file__).resolve().parents[2] / ".tools" / "python")

	added: list[str] = []
	for vendor in candidates:
		try:
			if not vendor.is_dir():
				continue
		except OSError:
			continue
		for extra in (
			vendor,
			vendor / "win32",
			vendor / "win32" / "lib",
			vendor / "pythonwin",
			vendor / "win32comext",
			vendor / "pywin32_system32",
		):
			try:
				if extra.is_dir() and str(extra) not in sys.path:
					sys.path.append(str(extra))
					added.append(str(extra))
			except OSError:
				continue
		dll_dir = vendor / "pywin32_system32"
		try:
			if dll_dir.is_dir() and hasattr(os, "add_dll_directory"):
				os.add_dll_directory(str(dll_dir))
		except OSError:
			pass
	return added


#: 可选依赖目录（启动时解析一次；空列表表示本机没装可选依赖）。
VENDOR_PATHS = _bootstrap_optional_dependencies()

__all__ = ["__version__", "VERSION_INFO", "VENDOR_PATHS"]

VERSION_INFO = {
	"name": "atuowexin",
	"version": __version__,
	"engine": "stdlib-only",
	"wechat": "4.0.x / 4.1.x (Windows) 优先，兼容 3.9.x 已解密库",
	"vendor_paths": len(VENDOR_PATHS),
}
