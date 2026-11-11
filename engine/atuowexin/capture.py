# -*- coding: utf-8 -*-
"""启动期 Hook 密钥捕获的安全编排层。

``engine/tools/capture_key.js`` 只负责和微信进程交互；本模块负责：

* 在启动 Node 之前逐项检查 node、koffi、DLL 与管理员权限；
* 把状态文件映射成稳定的中文进度，并把捕获产物限制在插件 workdir；
* 读取 Node 产出的 64 位十六进制 key，交给 :mod:`keyfinder` 逐库做
  SQLCipher 第 1 页 HMAC 校验；
* 只有通过校验的 ``{salt: key}`` 条目才允许写入 ``keymap.json``。

这个文件不直接加载 DLL，也不把 key 注入任何进程。真正的 Hook 仍由
``capture_key.js`` 调用参考工具提供的 ``wx_key.dll`` 完成，且必须显式
调用 ``capture_key`` 才会启动。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from . import keyfinder, paths

__all__ = [
	"CapturePrerequisite",
	"CaptureResult",
	"STATUS_LABELS",
	"check_prerequisites",
	"capture_key",
	"default_dll_path",
]


# capture_key.js 的状态文件值 → 对用户展示的中文状态。
STATUS_LABELS: dict[str, str] = {
	"started": "准备中",
	"dll_found": "依赖就绪",
	"dll_loaded": "依赖就绪",
	"waiting_close": "等待关闭",
	"waiting_start": "等待登录",
	"injecting": "注入中",
	"hook_ok": "捕获中",
	"polling": "捕获中",
	"captured": "已捕获",
	"dll_not_found": "缺少 wx_key.dll",
	"koffi_missing": "缺少 koffi",
	"timeout_close": "等待关闭超时",
	"timeout_start": "等待登录超时",
	"timeout_poll": "捕获超时",
	"hook_failed": "注入失败",
	"error": "捕获错误",
}


@dataclass
class CapturePrerequisite:
	"""一项捕获前置条件的结果。"""

	name: str
	ok: bool
	detail: str
	hint: str = ""

	def to_dict(self) -> dict[str, Any]:
		return {"name": self.name, "ok": self.ok, "detail": self.detail, "hint": self.hint}


@dataclass
class CaptureResult:
	"""捕获、校验与落盘的完整结果。"""

	ok: bool = False
	stage: str = "preflight"
	status: str = ""
	error: str = ""
	hint: str = ""
	output_dir: str = ""
	key_file: str = ""
	status_file: str = ""
	key: str = ""
	exit_code: int | None = None
	timed_out: bool = False
	lines: list[str] = field(default_factory=list)
	prerequisites: list[CapturePrerequisite] = field(default_factory=list)
	validation: keyfinder.KeyValidationResult | None = None
	validation_method: str = ""
	#: 需要用户做的事（目前只有一种：去微信完成登录）。上层据此提示用户。
	user_action: str = ""

	def to_dict(self, *, show_key: bool = False) -> dict[str, Any]:
		payload: dict[str, Any] = {
			"ok": self.ok,
			"stage": self.stage,
			"status": self.status,
			"error": self.error,
			"hint": self.hint,
			"output_dir": self.output_dir,
			"key_file": self.key_file,
			"status_file": self.status_file,
			"exit_code": self.exit_code,
			"timed_out": self.timed_out,
			"prerequisites": [item.to_dict() for item in self.prerequisites],
			"lines": self.lines[-100:],
			"key_prefix": (self.key[:8] + "…") if self.key else "",
			"validation_method": self.validation_method,
			"user_action": self.user_action,
		}
		if self.validation is not None:
			payload["validation"] = self.validation.to_dict(show_key=show_key)
		if show_key and self.key:
			payload["key"] = self.key
		return payload


def _plugin_root() -> Path:
	"""返回插件包根目录（``engine/atuowexin/capture.py`` 的两级上级）。"""
	return Path(__file__).resolve().parents[2]


def _capture_script() -> Path:
	base = Path(__file__).resolve().parents[1] / "tools"
	# 插件 package.json 声明了 type=module，.js 会被当成 ESM（require 不可用），
	# 所以捕获脚本用 .cjs 扩展名强制走 CommonJS。
	for name in ("capture_key.cjs", "capture_key.js"):
		candidate = base / name
		if candidate.is_file():
			return candidate
	return base / "capture_key.cjs"


def _reference_root() -> Path:
	"""原生依赖目录：默认就是插件自带的 engine/tools（dll + node_modules）。

	外部参考工具可用环境变量覆盖；插件现在自包含，删掉外部工具也能跑。
	"""
	configured = os.environ.get("ATUOWEXIN_WECHAT_EXPORT_TOOL")
	if configured:
		return Path(configured).expanduser()
	return Path(__file__).resolve().parents[1] / "tools"


def default_dll_path() -> Path:
	"""按优先级寻找 ``wx_key.dll``，默认不复制 DLL 进插件。

	优先使用显式环境变量，其次支持未来放入插件 ``engine/tools/dll`` 的布局，
	最后引用用户现有的参考工具目录。返回的路径可能不存在，由前置检查给出
	明确提示。
	"""
	configured = os.environ.get("ATUOWEXIN_WX_KEY_DLL")
	if configured:
		return Path(configured).expanduser()
	candidates = (
		_plugin_root() / "engine" / "tools" / "dll" / "wx_key.dll",
		_reference_root() / "dll" / "wx_key.dll",
	)
	for candidate in candidates:
		if candidate.is_file():
			return candidate
	return candidates[0]


def _dll_file(value: str | Path | None) -> Path:
	"""把 ``--dll`` 接受的文件或目录归一化成 DLL 文件路径。"""
	path = Path(value).expanduser() if value else default_dll_path()
	if path.is_dir():
		return path / "wx_key.dll"
	return path


def _is_admin() -> bool:
	"""检查当前进程是否具备管理员权限。"""
	if os.name != "nt":
		return False
	try:
		return bool(ctypes.windll.shell32.IsUserAnAdmin())
	except (AttributeError, OSError):
		return False


def _node_path(node: str | Path | None) -> str | None:
	"""解析 node 可执行文件，支持 PATH 名称与显式路径。"""
	if node:
		candidate = str(Path(node).expanduser())
		if Path(candidate).is_file():
			return candidate
		return shutil.which(candidate)
	return shutil.which("node")


def _koffi_cwd_candidates(node_cwd: str | Path | None = None) -> list[Path | None]:
	"""返回尝试加载 koffi 的 Node 工作目录。"""
	items: list[Path | None] = []
	if node_cwd:
		items.append(Path(node_cwd).expanduser())
	items.extend((_plugin_root(), _plugin_root() / "engine" / "tools", _capture_script().parent, _reference_root(), _reference_root() / "scripts"))
	items.append(None)
	seen: set[str] = set()
	result: list[Path | None] = []
	for item in items:
		key = "" if item is None else str(item).lower()
		if key in seen:
			continue
		seen.add(key)
		result.append(item)
	return result


def _check_koffi(node: str, node_cwd: str | Path | None = None) -> tuple[bool, str, str]:
	"""在 Node 中真实加载 koffi，返回 (ok, detail, hint)。"""
	for cwd in _koffi_cwd_candidates(node_cwd):
		if cwd is not None and not cwd.is_dir():
			continue
		try:
			completed = subprocess.run(
				[node, "-e", "require('koffi')"],
				cwd=str(cwd) if cwd is not None else None,
				capture_output=True,
				text=True,
				encoding="utf-8",
				errors="replace",
				timeout=10,
				check=False,
			)
		except (OSError, subprocess.SubprocessError) as error:
			continue
		if completed.returncode == 0:
			return True, f"koffi 可加载（cwd={cwd or '默认'}）", ""
	message = (completed.stderr or completed.stdout or "").strip().splitlines()
	detail = message[-1] if message else "Node 无法加载 koffi"
	return False, detail, "在 Node 运行时安装 koffi：npm install koffi"


def check_prerequisites(
	*,
	node: str | Path | None = None,
	dll: str | Path | None = None,
	node_cwd: str | Path | None = None,
	require_admin: bool = True,
) -> list[CapturePrerequisite]:
	"""逐项检查启动期捕获的前置条件。

	函数只读系统状态，不创建捕获目录、不启动微信、不加载 DLL，适合在
	``doctor`` 或 UI 预检中调用。每一项都保留可操作的安装/配置提示。
	"""
	result: list[CapturePrerequisite] = []
	node_exec = _node_path(node)
	if node_exec:
		result.append(CapturePrerequisite("node", True, node_exec))
	else:
		result.append(CapturePrerequisite("node", False, "未找到 node 可执行文件", "安装 Node.js，并确保 node 在 PATH 中"))

	dll_path = _dll_file(dll)
	if dll_path.is_file():
		result.append(CapturePrerequisite("wx_key.dll", True, str(dll_path)))
	else:
		result.append(
			CapturePrerequisite(
				"wx_key.dll",
				False,
				f"找不到 wx_key.dll：{dll_path}",
				"设置 ATUOWEXIN_WX_KEY_DLL，或把 DLL 放到参考工具的 dll\\ 目录",
			)
		)

	if node_exec:
		ok, detail, hint = _check_koffi(node_exec, node_cwd)
		result.append(CapturePrerequisite("koffi", ok, detail, hint))
	else:
		result.append(CapturePrerequisite("koffi", False, "因 node 缺失无法检查 koffi", "先安装 Node.js，再运行 npm install koffi"))

	if require_admin:
		if os.name != "nt":
			result.append(CapturePrerequisite("管理员权限", False, "key capture 只支持 Windows 原生 Hook", "请在 Windows 上运行 key capture"))
		elif _is_admin():
			result.append(CapturePrerequisite("管理员权限", True, "当前进程具备管理员权限"))
		else:
			result.append(CapturePrerequisite("管理员权限", False, "当前进程不是管理员", "以管理员身份启动 DSH/终端，并与微信权限保持一致"))
	return result


#: 这些状态说明「微信起来了但还没登录 / 一直没起来」，需要用户去点一下。
_LOGIN_PENDING = {"waiting_start", "timeout_start", "hook_ok", "polling"}


def needs_user_login(status: str) -> bool:
	"""当前状态是否意味着必须由用户完成微信登录。"""
	return (status or "").split(":", 1)[0].strip() in _LOGIN_PENDING


def _status_text(status: str) -> str:
	"""把带错误详情的状态映射成稳定的中文标签。"""
	value = (status or "").strip()
	if not value:
		return ""
	base = value.split(":", 1)[0]
	return STATUS_LABELS.get(base, value)


def _read_text(path: Path) -> str:
	try:
		return path.read_text(encoding="utf-8", errors="replace").strip()
	except OSError:
		return ""


def _emit_status(progress: Callable[[str], None] | None, status: str, *, last: str) -> str:
	"""状态变化时回调一次，避免轮询刷屏。"""
	if status and status != last and progress is not None:
		label = _status_text(status)
		if ":" in status:
			label += f"（{status.split(':', 1)[1]}）"
		progress(label)
	return status or last


def _terminate(process: subprocess.Popen[str]) -> None:
	try:
		process.terminate()
	except OSError:
		pass
	try:
		process.wait(timeout=3)
	except subprocess.TimeoutExpired:
		try:
			process.kill()
		except OSError:
			pass


def _wcdb_trial(key: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
	"""用 WCDB 实际开一次库——这是 4.1.10+ 上唯一可信的可用性判定。"""
	try:
		from .backends.wcdb_server import trial_open
	except Exception as error:  # noqa: BLE001 - 后端不可导入时退化为“未知”
		return {"ok": False, "detail": "WCDB 后端不可用：%s" % error, "sessions": 0}
	return trial_open(settings, key=key)


def capture_key(
	root: str | Path,
	keymap_path: str | Path,
	*,
	workdir: str | Path | None = None,
	capture_dir: str | Path | None = None,
	dll: str | Path | None = None,
	node: str | Path | None = None,
	node_cwd: str | Path | None = None,
	version: int = 4,
	timeout: float = 180.0,
	login_wait: float = 40.0,
	write: bool = True,
	show_key: bool = False,
	require_admin: bool = True,
	progress: Callable[[str], None] | None = None,
) -> CaptureResult:
	"""启动 Node Hook、读取 raw key、逐库验证并按需写 keymap。

	``timeout`` 是整个 Node 流程的 watchdog（等待微信关闭/启动/登录均包含在内）。
	Node 自己的轮询超时使用同一数值；Python 到时会终止子进程，避免无限等待。
	"""
	result = CaptureResult()
	root_path = Path(root).expanduser()
	map_path = Path(keymap_path).expanduser()
	base = Path(workdir).expanduser() if workdir else paths.default_workdir()
	output = Path(capture_dir).expanduser() if capture_dir else base / "capture"
	result.output_dir = str(output)
	result.key_file = str(output / "key.txt")
	result.status_file = str(output / "key_status.txt")

	result.prerequisites = check_prerequisites(node=node, dll=dll, node_cwd=node_cwd, require_admin=require_admin)
	missing = [item for item in result.prerequisites if not item.ok]
	if missing:
		result.stage = "preflight"
		result.status = "preflight_failed"
		result.error = "捕获前置条件不足：" + "、".join(item.name for item in missing)
		result.hint = "；".join(f"{item.name}：{item.hint or item.detail}" for item in missing)
		return result

	node_exec = _node_path(node)
	assert node_exec is not None  # check_prerequisites 已确认
	dll_path = _dll_file(dll)
	script = _capture_script()
	if not script.is_file():
		result.stage = "preflight"
		result.error = f"找不到捕获脚本：{script}"
		return result
	try:
		output.mkdir(parents=True, exist_ok=True)
		# 旧状态可能来自上一次异常退出，先清掉以免把 stale captured 当成新结果。
		for stale in (output / "key.txt", output / "key_status.txt"):
			try:
				stale.unlink()
			except FileNotFoundError:
				pass
	except OSError as error:
		result.error = f"无法创建捕获工作目录：{output}（{error}）"
		return result

	# capture_key.js 接受 DLL 目录而非文件路径。
	command = [
		node_exec,
		str(script),
		"--out",
		str(output),
		"--dll",
		str(dll_path.parent),
		"--timeout",
		str(max(1000, int(float(timeout) * 1000))),
	]
	result.stage = "running"
	last_status = ""
	lines: list[str] = []
	queue: list[str] = []
	lock = threading.Lock()
	# koffi 装在参考工具的 scripts/node_modules 里；脚本本身在插件目录，
	# Node 按脚本位置解析模块，所以必须显式给 NODE_PATH。
	env = dict(os.environ)
	koffi_modules = _reference_root() / "scripts" / "node_modules"
	if (koffi_modules / "koffi").is_dir():
		existing = env.get("NODE_PATH", "")
		env["NODE_PATH"] = str(koffi_modules) + ((os.pathsep + existing) if existing else "")
	try:
		process = subprocess.Popen(
			command,
			cwd=str(node_cwd or _reference_root()),
			env=env,
			stdout=subprocess.PIPE,
			stderr=subprocess.STDOUT,
			text=True,
			encoding="utf-8",
			errors="replace",
			bufsize=1,
		)
	except OSError as error:
		result.stage = "spawn_failed"
		result.error = f"无法启动 node：{error}"
		return result

	def _reader() -> None:
		stream = process.stdout
		if stream is None:
			return
		try:
			for line in stream:
				with lock:
					queue.append(line.rstrip())
		except OSError:
			pass

	thread = threading.Thread(target=_reader, name="atuowexin-key-capture", daemon=True)
	thread.start()
	deadline = time.monotonic() + max(1.0, float(timeout))
	login_deadline = 0.0
	while process.poll() is None:
		current = _read_text(output / "key_status.txt")
		last_status = _emit_status(progress, current, last=last_status)
		if needs_user_login(current):
			# 微信起来了但没登录：给用户 login_wait 秒去点「进入微信」，超时立刻返回，
			# 让上层（AI/人）明确知道「下一步需要用户登录」，而不是傻等到 timeout。
			if not login_deadline:
				login_deadline = time.monotonic() + max(5.0, float(login_wait))
			elif time.monotonic() >= login_deadline:
				result.stage = "login_required"
				result.user_action = "login_wechat"
				result.status = current
				result.error = "微信还没登录：请在微信窗口点『进入微信』完成登录（首次登录需手机扫码）"
				result.hint = "登录完成后重新执行一次即可继续；已捕获过的密钥会被复用，不用重新配置"
				_terminate(process)
				break
		else:
			login_deadline = 0.0
		if time.monotonic() >= deadline:
			result.timed_out = True
			result.error = f"key capture 超时（{timeout:g} 秒）"
			_terminate(process)
			break
		time.sleep(0.2)
	thread.join(timeout=3)
	with lock:
		lines.extend(queue)
	result.lines = [line for line in lines if line]
	result.exit_code = process.returncode
	current = _read_text(output / "key_status.txt")
	last_status = _emit_status(progress, current, last=last_status)
	result.status = current or last_status

	if result.stage == "login_required":
		return result

	if result.timed_out:
		result.stage = "timeout"
		return result
	if result.exit_code not in (0, None):
		result.stage = "capture_failed"
		base_status = (result.status or "").split(":", 1)[0]
		result.error = result.error or f"Node 捕获失败：{_status_text(result.status) or base_status or result.exit_code}"
		if base_status in {"dll_not_found", "koffi_missing"}:
			result.hint = "请按前置检查安装缺失依赖后重试"
		return result

	raw_key = _read_text(output / "key.txt")
	normalised = keyfinder._normalise_raw_key(raw_key) if raw_key else None
	if normalised is None:
		result.stage = "validation_failed"
		result.error = "Node 已结束，但没有捕获到有效的 64 位十六进制 key"
		return result
	result.key = normalised[0]
	# 只保留 WCDB 一条判定：WCDB 能开库 + 能列会话，这把 key 就是可用的。
	# 不再拿 SQLCipher 页 1 的 HMAC 去否决它 —— 4.1.10+ 的库是 WCDB 自有封装，
	# HMAC 公式不成立，会给出假阴性。
	if progress is not None:
		progress("已捕获，用 WCDB 试开校验")
	wcdb_check = _wcdb_trial(result.key)
	# 捕获产物含有完整密钥，只允许在插件 workdir 下生成，并尽量收紧权限。
	try:
		os.chmod(output / "key.txt", 0o600)
	except OSError:
		pass
	result.validation_method = "wcdb"
	if not wcdb_check.get("ok"):
		result.stage = "validation_failed"
		result.error = "WCDB 试开失败：%s" % wcdb_check.get("detail", "")
		result.hint = "确认微信数据目录与密钥匹配；微信正在运行时快照可能不一致，退出微信后重试最稳"
		return result
	result.stage = "done"
	result.ok = True
	result.status = "captured"
	result.error = ""
	result.hint = "WCDB 试开成功（%s）" % wcdb_check.get("detail", "")
	return result


def capture_and_validate(
	root: str | Path,
	key: str | bytes,
	keymap_path: str | Path,
	*,
	version: int = 4,
	merge: bool = True,
) -> keyfinder.KeyValidationResult:
	"""--key 输入路径的校验入口：只做 WCDB 试开，不跑 HMAC、不写 keymap。

	签名保留 root/keymap_path/merge 是为了兼容既有 CLI 调用；WCDB 方案下
	不再需要 salt→key 表，密钥由 backend.wcdb 直接读取（配置 / 桌面 key.txt / 捕获产物）。
	"""
	result = keyfinder.KeyValidationResult()
	result.version = version
	raw = key if isinstance(key, str) else key.hex()
	normalised = keyfinder._normalise_raw_key(raw)
	if normalised is None:
		result.detail = "密钥不是 64 位十六进制，或形态无法识别"
		return result
	result.key = normalised[0]
	trial = _wcdb_trial(normalised[0])
	result.ok = bool(trial.get("ok"))
	result.all_matched = result.ok
	result.detail = trial.get("detail", "")
	result.databases = 1 if result.ok else 0
	return result

