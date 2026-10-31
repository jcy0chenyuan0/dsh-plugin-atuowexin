# -*- coding: utf-8 -*-
"""可选依赖的发现与安装（**引擎核心仍然只用标准库**）。

为什么需要这个模块
------------------
引擎的核心（解密、摘录、隔离、总结编排）只用标准库，但在真实微信数据上有两个
"锦上添花但影响数据完整性"的可选依赖：

* ``zstandard`` —— 微信 4.x 的 WCDB 会把 ``message_content`` 用 zstd 压过
  （``WCDB_CT_message_content == 4``，魔数 ``28 B5 2F FD``）。没有它，这些消息只能
  留一个占位符，而**标准库没有 zstd**，纯 Python 实现一个 zstd 解码器不值得。
* ``pillow`` —— UIA 发送后端要靠截图做像素判据（正常情况下 Harness 自带运行时已有）。

把它们装到**交付物自己的目录** ``<repo>/.tools/python``，不动系统 Python、不建虚拟环境、
不写注册表；引擎再把这个目录加进 ``sys.path``。这样"解压正文"这件事既不污染环境，
也不会因为换了台机器就静默退化。

设计约束：本模块只依赖标准库，且**所有失败都返回结果而不是抛异常**——
"装不上"只应导致"正文留占位符"，绝不该让摘录失败。
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "OPTIONAL_PACKAGES",
    "optional_dir",
    "ensure_optional_path",
    "module_status",
    "status",
    "install",
    "InstallResult",
]

#: 可选依赖 → 作用说明（doctor 与 CLI 都用它，避免两处措辞漂移）。
OPTIONAL_PACKAGES: dict[str, str] = {
    "zstandard": "解 WCDB 压缩过的消息正文（微信 4.x，缺了只剩占位符）",
}

#: 同一个能力可以接受多个发行包名。
_PROVIDERS: dict[str, tuple[str, ...]] = {
    "zstandard": ("zstandard", "pyzstd", "zstd"),
}

_PIP_NAMES: dict[str, str] = {
    "zstandard": "zstandard",
}


def optional_dir() -> Path:
    """可选依赖目录：``ATUOWEXIN_PYTHONPATH`` 或 ``<repo>/.tools/python``。

    ``<repo>`` 由本文件的层级推导（``atuowexin/deps.py`` → 上溯三层），
    因此无论当前工作目录在哪都指向同一处。
    """
    configured = os.environ.get("ATUOWEXIN_PYTHONPATH")
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[2] / ".tools" / "python"


def ensure_optional_path() -> str:
    """把可选依赖目录追加到 ``sys.path``（幂等）。返回实际加入的路径或空串。"""
    target = optional_dir()
    try:
        if not target.is_dir():
            return ""
        resolved = str(target)
        if resolved not in sys.path:
            sys.path.append(resolved)
        return resolved
    except OSError:
        return ""


def _try_import(name: str):
    """按名字 import；失败返回 ``None``（并清掉半成品模块缓存）。"""
    try:
        return importlib.import_module(name)
    except Exception:
        return None


def module_status(component: str) -> dict:
    """探测某个可选能力是否可用。

    :return: ``{"component", "ok", "module", "path", "providers"}``。
    """
    ensure_optional_path()
    for name in _PROVIDERS.get(component, (component,)):
        module = _try_import(name)
        if module is not None:
            return {
                "component": component,
                "ok": True,
                "module": name,
                "path": getattr(module, "__file__", "") or "",
                "providers": list(_PROVIDERS.get(component, (component,))),
            }
    return {
        "component": component,
        "ok": False,
        "module": "",
        "path": "",
        "providers": list(_PROVIDERS.get(component, (component,))),
    }


def status() -> dict:
    """探测全部可选依赖，返回 ``{component: {...}}``。"""
    return {component: module_status(component) for component in OPTIONAL_PACKAGES}


@dataclass
class InstallResult:
    """一次 ``pip install`` 的结果。"""

    ok: bool
    target: str = ""
    requested: list[str] = field(default_factory=list)
    installed: dict = field(default_factory=dict)
    returncode: int = -1
    output: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        """转成可 JSON 序列化的字典。"""
        return {
            "ok": self.ok,
            "target": self.target,
            "requested": list(self.requested),
            "installed": self.installed,
            "returncode": self.returncode,
            "output": self.output[-4000:],
            "error": self.error,
        }

    def render(self) -> str:
        """渲染成给人看的文本。"""
        if not self.ok:
            return f"安装失败：{self.error or '未知原因'}\n{self.output[-1500:]}"
        lines = [f"已安装到 {self.target}："]
        for component, info in self.installed.items():
            mark = "OK " if info.get("ok") else "缺 "
            lines.append(f"  [{mark}] {component}（{info.get('module') or '未导入'}）")
        lines.append("提示：引擎会自动从该目录 import，无需设置 PYTHONPATH。")
        return "\n".join(lines)


def install(components: "list[str] | tuple[str, ...] | None" = None, *, timeout: int = 600) -> InstallResult:
    """把可选依赖装进 :func:`optional_dir`（不碰系统 Python）。

    :param components: 要装的组件名（``zstandard`` / ``pillow``），默认全部。
    :param timeout: ``pip`` 超时秒数。
    :return: :class:`InstallResult`；``pip`` 报错也只记录在结果里。
    """
    wanted = list(components) if components else list(OPTIONAL_PACKAGES)
    unknown = [name for name in wanted if name not in OPTIONAL_PACKAGES]
    target = optional_dir()
    result = InstallResult(ok=False, target=str(target), requested=wanted)
    if unknown:
        result.error = f"未知的可选依赖：{', '.join(unknown)}（可用：{', '.join(OPTIONAL_PACKAGES)}）"
        return result
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        result.error = f"无法创建目录 {target}：{exc}"
        return result

    packages = [_PIP_NAMES.get(name, name) for name in wanted]
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--target",
        str(target),
    ] + packages
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        result.error = f"pip 超时（{timeout}s）"
        return result
    except OSError as exc:
        result.error = f"无法启动 pip：{exc}"
        return result

    result.returncode = int(completed.returncode)
    result.output = (completed.stdout or b"").decode("utf-8", "replace")
    if completed.returncode != 0:
        result.error = f"pip 返回 {completed.returncode}"
        return result

    # importlib 会缓存"找不到"的结果，装完必须让这些名字重新可查。
    importlib.invalidate_caches()
    for name in list(sys.modules):
        if name.split(".")[0] in {item for component in wanted for item in _PROVIDERS.get(component, ())}:
            sys.modules.pop(name, None)
    result.installed = {name: module_status(name) for name in wanted}
    result.ok = all(info.get("ok") for info in result.installed.values())
    if not result.ok:
        result.error = "pip 成功但仍有组件导入失败（可能是平台轮子缺失）"
    return result
