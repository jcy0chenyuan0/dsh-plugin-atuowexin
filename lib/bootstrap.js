/**
 * 环境自举：插件加载时后台检查/安装缺失环境。
 *
 * 只做三件事：
 * 1. 当 `autoInstall: false` 或 `DSH_BOOTSTRAP=0` 时跳过；
 * 2. 6 小时内不重复检查（避免每次热重载都拉起 winget）；
 * 3. 用 detached 的 PowerShell 跑仓库根目录的 bootstrap.ps1，不阻塞 DSH 启动。
 *
 * 安装是尽力而为：失败只影响环境，不影响插件加载。
 */
import { spawn } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const STATE_PATH = join(PACKAGE_ROOT, ".bootstrap-state.json");
const CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000;

let scheduled = false;

export function scheduleBootstrap(config = {}) {
	if (scheduled) return;
	scheduled = true;

	if (config.autoInstall === false) return;
	if (process.env.DSH_BOOTSTRAP === "0") return;
	if (process.platform !== "win32") return;

	const script = join(PACKAGE_ROOT, "bootstrap.ps1");
	if (!existsSync(script)) return;

	try {
		const state = JSON.parse(readFileSync(STATE_PATH, "utf8"));
		if (Number.isFinite(state.lastAttempt) && Date.now() - state.lastAttempt < CHECK_INTERVAL_MS) return;
	} catch {
		// 没有状态文件 = 首次加载
	}

	let child;
	try {
		child = spawn("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-AutoInstall", "-Quiet"], {
			detached: true,
			stdio: "ignore",
			windowsHide: true
		});
		child.unref();
	} catch {
		return;
	}

	try {
		writeFileSync(STATE_PATH, JSON.stringify({ lastAttempt: Date.now() }), "utf8");
	} catch {
		// 状态写不进去也不影响下一次尝试
	}
}
