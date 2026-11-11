# -*- coding: utf-8 -*-
"""一条命令跑完：关微信 -> 起 Hook -> 拉起微信 -> 点登录 -> 取 key -> 校验。

为什么要有它：前面的能力散在 capture / wcdb / keyfinder 里，用户得自己记顺序，
顺序错了就失败。这里把它封成一条流水线。

关键设计：**需要用户登录时立刻返回**，不傻等到超时。
返回 stage=login_required + user_action=login_wechat 就是给上层（AI/UI）的信号：
别再重试，去请用户点一下「进入微信」。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import capture as capture_module
from .capture import CaptureResult, check_prerequisites, needs_user_login

#: 微信主程序常见位置。
_EXE_CANDIDATES = (
	Path(r"C:\Program Files\Tencent\Weixin\Weixin.exe"),
	Path(r"C:\Program Files\Tencent\WeChat\WeChat.exe"),
	Path(r"C:\Program Files (x86)\Tencent\WeChat\WeChat.exe"),
)

#: 登录窗口里「进入微信」按钮的相对位置（实测 379x396 窗口：x 约 0.59W、y 约 0.95H）。
_LOGIN_BUTTON = (0.59, 0.947)


def wechat_exe(explicit: str = "") -> str:
	"""找微信可执行文件。"""
	if explicit and Path(explicit).is_file():
		return str(explicit)
	env = os.environ.get("ATUOWEXIN_WECHAT_EXE", "")
	if env and Path(env).is_file():
		return env
	for candidate in _EXE_CANDIDATES:
		if candidate.is_file():
			return str(candidate)
	return ""


def _run_hidden(args) -> bool:
	try:
		subprocess.run(args, capture_output=True, timeout=30)
		return True
	except (OSError, subprocess.SubprocessError):
		return False


def _wechat_pids():
	"""用 tasklist 拿 pid，避免依赖 psutil。"""
	pids = []
	for name in ("Weixin.exe", "WeChat.exe"):
		try:
			completed = subprocess.run(
				["tasklist", "/FI", "IMAGENAME eq " + name, "/FO", "CSV", "/NH"],
				capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
			)
		except (OSError, subprocess.SubprocessError):
			continue
		for line in (completed.stdout or "").splitlines():
			parts = [item.strip('"') for item in line.split('","')]
			if len(parts) >= 2 and parts[0].lower() == name.lower():
				try:
					pids.append(int(parts[1]))
				except ValueError:
					pass
	return pids


def wechat_running() -> bool:
	"""微信是否在运行。"""
	return bool(_wechat_pids())


def stop_wechat(progress: Callable[[str], None] | None = None) -> bool:
	"""关掉微信；返回是否已经没有微信进程。"""
	if not wechat_running():
		return True
	if progress:
		progress("关闭微信 ...")
	for pid in _wechat_pids():
		_run_hidden(["taskkill", "/F", "/PID", str(pid)])
	for _ in range(30):
		if not wechat_running():
			return True
		time.sleep(0.5)
	return not wechat_running()


def start_wechat(exe: str) -> bool:
	"""拉起微信（不等待登录）。"""
	if not exe or not Path(exe).is_file():
		return False
	try:
		subprocess.Popen([exe], close_fds=True)
		return True
	except OSError:
		try:
			os.startfile(str(exe))  # type: ignore[attr-defined]
			return True
		except OSError:
			return False


# --------------------------------------------------------------------------- #
# 自动点「进入微信」：尽力而为，失败就交回给用户
# --------------------------------------------------------------------------- #


def _enum_windows():
	"""列出可见顶层窗口 (hwnd, title, rect)。"""
	import ctypes.wintypes

	user32 = ctypes.windll.user32
	results = []

	@ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
	def _callback(hwnd, _lparam):
		if not user32.IsWindowVisible(hwnd):
			return True
		length = user32.GetWindowTextLengthW(hwnd)
		if length <= 0:
			return True
		buffer = ctypes.create_unicode_buffer(length + 1)
		user32.GetWindowTextW(hwnd, buffer, length + 1)
		rect = ctypes.wintypes.RECT()
		user32.GetWindowRect(hwnd, ctypes.byref(rect))
		results.append((int(hwnd), buffer.value, (rect.left, rect.top, rect.right, rect.bottom)))
		return True

	user32.EnumWindows(_callback, 0)
	return results


def click_login_button(timeout: float = 40.0) -> bool:
	"""找到微信登录窗口并点一下「进入微信」；成功返回 True。"""
	try:
		import ctypes.wintypes  # noqa: F401
	except Exception:  # noqa: BLE001
		return False

	user32 = ctypes.windll.user32
	deadline = time.time() + max(5.0, timeout)
	while time.time() < deadline:
		for hwnd, title, (left, top, right, bottom) in _enum_windows():
			if title != "微信":
				continue
			width, height = right - left, bottom - top
			# 登录窗口是小窗；主窗口大得多
			if not (250 <= width <= 700 and 250 <= height <= 800):
				continue
			x = int(left + width * _LOGIN_BUTTON[0])
			y = int(top + height * _LOGIN_BUTTON[1])
			user32.SetForegroundWindow(hwnd)
			time.sleep(0.2)
			user32.SetCursorPos(x, y)
			user32.mouse_event(0x0002, 0, 0, 0, 0)  # 左键按下
			user32.mouse_event(0x0004, 0, 0, 0, 0)  # 左键抬起
			return True
		time.sleep(1.0)
	return False


# --------------------------------------------------------------------------- #
# 流水线
# --------------------------------------------------------------------------- #


@dataclass
class AutopilotResult:
	"""整条流水线的结果。"""

	ok: bool = False
	stage: str = ""
	message: str = ""
	user_action: str = ""
	key_prefix: str = ""
	validation_method: str = ""
	capture: CaptureResult | None = None
	log: list[str] = field(default_factory=list)

	def to_dict(self, *, show_key: bool = False) -> dict[str, Any]:
		payload = {
			"ok": self.ok,
			"stage": self.stage,
			"message": self.message,
			"user_action": self.user_action,
			"key_prefix": self.key_prefix,
			"validation_method": self.validation_method,
			"log": self.log[-30:],
		}
		if self.capture is not None:
			payload["capture"] = self.capture.to_dict(show_key=show_key)
		return payload


def bootstrap(
	*,
	root,
	keymap_path,
	workdir=None,
	exe: str = "",
	keep_open: bool = False,
	auto_click: bool = True,
	login_wait: float = 40.0,
	timeout: float = 180.0,
	progress: Callable[[str], None] | None = None,
) -> AutopilotResult:
	"""整条流水线：关微信 -> 起捕获 -> 拉起微信 -> 点登录 -> 取 key -> 校验。"""
	log = []
	result = AutopilotResult()
	emit = progress or (lambda message: None)

	def note(message: str) -> None:
		log.append(message)
		emit(message)

	prereqs = check_prerequisites()
	missing = [item for item in prereqs if not item.ok]
	if missing:
		result.stage = "preflight_failed"
		result.message = "前置条件不足：" + "、".join(item.name for item in missing)
		result.user_action = "install_dependencies"
		result.log = log
		return result

	exe_path = wechat_exe(exe)
	if not exe_path:
		result.stage = "wechat_not_found"
		result.message = "找不到 Weixin.exe；用 --exe 指定路径"
		result.user_action = "install_wechat"
		result.log = log
		return result

	# 1) 关微信（Hook 必须赶在它写密钥之前）
	if keep_open and wechat_running():
		result.stage = "wechat_must_close"
		result.message = "微信正在运行，但 Hook 只能在它启动前安装；请先退出微信，或去掉 --keep-open"
		result.user_action = "close_wechat"
		result.log = log
		return result
	if not stop_wechat(note):
		result.stage = "close_failed"
		result.message = "关不掉微信（可能被保护进程占用）"
		result.user_action = "close_wechat"
		result.log = log
		return result

	# 2) 起捕获（先让它进入等待，再拉起微信）
	note("起捕获（安装 Hook）...")
	box = {}

	def _worker() -> None:
		box["result"] = capture_module.capture_key(
			root,
			keymap_path,
			workdir=workdir,
			login_wait=login_wait,
			timeout=timeout,
			progress=None,
		)

	worker = threading.Thread(target=_worker, name="atuowexin-autopilot", daemon=True)
	worker.start()
	time.sleep(2.5)

	# 3) 拉起微信
	note("拉起微信 ...")
	if not start_wechat(exe_path):
		result.stage = "start_failed"
		result.message = "起不来微信：%s" % exe_path
		result.user_action = "start_wechat"
		result.log = log
		return result

	status_file = (Path(workdir).expanduser() if workdir else Path.home() / ".atuowexin" / "data") / "capture" / "key_status.txt"

	# 4) 尽力自动点「进入微信」（只在需要时点一次）
	clicked = False
	click_deadline = time.time() + max(10.0, login_wait)
	while time.time() < click_deadline and worker.is_alive():
		if auto_click and not clicked:
			status = capture_module._read_text(status_file)
			base_status = status.split(":", 1)[0].strip()
			if needs_user_login(status) and base_status in ("waiting_start", "hook_ok"):
				if click_login_button(timeout=5.0):
					clicked = True
					note("已自动点击「进入微信」；如需手机上确认请处理")
		time.sleep(1.0)

	worker.join(timeout=max(30.0, timeout))
	captured = box.get("result")
	result.capture = captured
	if captured is None:
		result.stage = "timeout"
		result.message = "捕获线程未在超时内返回"
		result.user_action = "retry"
		result.log = log
		return result

	if captured.ok:
		result.ok = True
		result.stage = "done"
		result.key_prefix = (captured.key[:8] + "…") if captured.key else ""
		result.validation_method = captured.validation_method
		result.message = "密钥已就绪（%s）" % (captured.validation_method or "unknown")
		result.log = log
		return result

	result.stage = captured.stage or "capture_failed"
	result.message = captured.error or "捕获未完成"
	result.user_action = captured.user_action
	result.log = log
	return result


__all__ = [
	"AutopilotResult",
	"bootstrap",
	"click_login_button",
	"start_wechat",
	"stop_wechat",
	"wechat_exe",
	"wechat_running",
]
