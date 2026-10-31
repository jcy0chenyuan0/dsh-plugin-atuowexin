/**
 * dsh-plugin-atuowexin 的离线测试脚手架。
 *
 * 与 `.tooling/test-python-plugin.mjs` 同一套路：把 Cordis 的 ctx 打桩到「刚好够用」，
 * 用**真实子进程**跑 `python -m atuowexin`，从而验证 JS 半边与 Python 引擎之间的
 * argv 拼装、JSON 解析、错误透传、渲染是否真的接得上，以及会话隔离在磁盘上是否成立。
 *
 * 运行（workdir 任意）：
 *   <node> atuowexin/tests/test-plugin.mjs
 */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const PLUGIN = pathToFileURL(join(PACKAGE_ROOT, "lib", "index.js")).href;
const WORKDIR = join(PACKAGE_ROOT, ".tmp", "dsh-plugin-work");
/** 搜索 Python：显式环境变量 → DSH 自带运行时 → PATH。 */
function discoverPython() {
	const explicit = process.env.ATUOWEXIN_PYTHON ?? process.env.DSH_PYTHON;
	if (explicit) return explicit;
	const dshHome = process.env.DSH_HOME ?? join(process.env.USERPROFILE ?? process.env.HOME ?? "", ".dsh");
	try {
		for (const name of readdirSync(join(dshHome, "dsh-runtimes"))) {
			const exe = process.platform === "win32"
				? join(dshHome, "dsh-runtimes", name, "dependencies", "python", "python.exe")
				: join(dshHome, "dsh-runtimes", name, "dependencies", "python", "bin", "python3");
			if (existsSync(exe)) return exe;
		}
	} catch {
		// 没有运行时目录时回退 PATH
	}
	return process.platform === "win32" ? "python" : "python3";
}
const PYTHON = discoverPython();

rmSync(WORKDIR, { recursive: true, force: true });
mkdirSync(WORKDIR, { recursive: true });

const mod = await import(PLUGIN);
const tools = new Map();
const ctx = {
	tools: {
		register(definition) {
			tools.set(definition.name, definition);
			return () => undefined;
		}
	},
	subprocess: {
		async resolveExecutable(command) {
			if (!existsSync(command)) throw new Error(`not found: ${command}`);
			return command;
		},
		spawn(spec) {
			const child = spawn(spec.argv[0], spec.argv.slice(1), {
				cwd: spec.cwd,
				env: { ...process.env, ...(spec.env ?? {}) }
			});
			let out = Buffer.alloc(0);
			let err = Buffer.alloc(0);
			child.stdout.on("data", (chunk) => {
				out = Buffer.concat([out, chunk]);
			});
			child.stderr.on("data", (chunk) => {
				err = Buffer.concat([err, chunk]);
			});
			child.stdin.end("");
			return {
				collected: {
					stdout: { readFrom: () => ({ text: out.toString("utf8"), nextOffset: out.length, lossy: false }) },
					stderr: { readFrom: () => ({ text: err.toString("utf8"), nextOffset: err.length, lossy: false }) }
				},
				done: new Promise((done) => child.on("close", (code, signal) => done({ exitCode: code, signal }))),
				terminate: () => child.kill()
			};
		}
	},
	get: () => undefined
};

mod.apply(ctx, { pythonPath: PYTHON, workdir: WORKDIR, backend: "mock" });

const exec = { signal: new AbortController().signal, agent: undefined };
const results = [];
const check = (name, ok, detail = "") => results.push({ name, ok, detail });
const call = (name, args = {}) => {
	const tool = tools.get(name);
	if (!tool) throw new Error(`工具未注册：${name}`);
	return tool.execute(args, exec);
};
const render = (name, value) => tools.get(name).output.render({}, value).map((block) => block.text).join("\n");

check(
	"插件身份与依赖声明",
	mod.name === "plugin-atuowexin" && mod.inject.includes("tools") && mod.inject.includes("subprocess"),
	`${mod.name} inject=${mod.inject}`
);
check(
	"注册了 6 个只读工具",
	tools.size === 6 &&
		["wechat_doctor", "wechat_sync", "wechat_conversations", "wechat_messages", "wechat_export", "wechat_find_key"].every(
			(item) => tools.has(item)
		),
	[...tools.keys()].join(",")
);
check(
	"发送/总结/草稿工具已彻底移除",
	!tools.has("wechat_send") && !tools.has("wechat_summarize") && !tools.has("wechat_reply"),
	[...tools.keys()].join(",")
);
check(
	"密钥工具是只读语义（不暴露明文回显开关，且写明不注入）",
	tools.get("wechat_find_key").parameters.properties.show_keys === undefined &&
		String(tools.get("wechat_find_key").description).includes("不注入") &&
		String(tools.get("wechat_find_key").description).includes("只读"),
	JSON.stringify(Object.keys(tools.get("wechat_find_key").parameters.properties))
);
check(
	"参数 schema 采用对象层 required 数组（原始注册方言）",
	Array.isArray(tools.get("wechat_messages").parameters.required) &&
		tools.get("wechat_messages").parameters.required[0] === "conversation" &&
		tools.get("wechat_messages").parameters.properties.conversation.type === "string",
	JSON.stringify(tools.get("wechat_messages").parameters.required)
);

// 1. doctor
{
	const value = await call("wechat_doctor");
	const text = render("wechat_doctor", value);
	check("doctor 返回可读体检报告", value.ok === true && Array.isArray(value.data?.checks) && text.includes("工作目录"), (text.split("\n")[0] ?? "").slice(0, 60));
}

// 2. sync + conversations
let conversationId = "";
{
	const value = await call("wechat_sync");
	check("sync 摘录成功", value.ok === true && value.data?.added > 0 && value.data?.backend === "mock", `added=${value.data?.added}`);
	const listed = await call("wechat_conversations");
	const items = listed.data?.conversations ?? [];
	conversationId = items[0]?.id ?? "";
	check("conversations 列出会话", listed.ok === true && items.length >= 3 && Boolean(conversationId), items.map((item) => item.display_name).join(","));
}

// 3. messages：按会话隔离
{
	const value = await call("wechat_messages", { conversation: conversationId, limit: 5 });
	const items = value.data?.messages ?? [];
	check(
		"messages 按会话返回且不越界",
		value.ok === true && items.length === 5 && items.every((item) => item.conversation_id === conversationId),
		`count=${items.length}`
	);
}

// 4. export
{
	const value = await call("wechat_export", { conversation: conversationId });
	const files = value.data?.files ?? [];
	check("export 落盘所有格式", value.ok === true && files.length === 4 && files.every((file) => existsSync(file)), files.map((file) => file.split(/[\\/]/).pop()).join(","));
	const explicit = await call("wechat_export", { conversation: conversationId, out: join(WORKDIR, "explicit-export"), redacted: true });
	check("export --out/--redacted 生效", explicit.ok === true && String(explicit.data?.dir).includes("explicit-export"), explicit.data?.dir ?? "");
}

// 5. 错误透传
{
	const value = await call("wechat_messages", { conversation: "根本不存在的会话" });
	check("引擎错误被原样透传", value.ok === false && String(value.error).includes("找不到会话"), String(value.error).slice(0, 50));
}

// 6. 磁盘上的会话隔离：每个工作区只含自己的消息
{
	const root = join(WORKDIR, "conversations");
	const slugs = existsSync(root) ? readdirSync(root) : [];
	let ok = slugs.length >= 3;
	const detail = [];
	for (const slug of slugs) {
		const profile = JSON.parse(readFileSync(join(root, slug, "profile.json"), "utf8"));
		const lines = readFileSync(join(root, slug, "messages.jsonl"), "utf8").split("\n").filter((line) => line.trim().length > 0);
		const own = lines.every((line) => JSON.parse(line).conversation_id === profile.conversation_id);
		ok = ok && own && lines.length > 0;
		detail.push(`${profile.display_name}:${lines.length}条`);
	}
	check("工作区磁盘级隔离（每条只含本会话消息）", ok, detail.join(" "));
}

// 7. 归一化库确实写在 --workdir 下
{
	const dbPath = join(WORKDIR, "atuowexin.db");
	check("归一化库写在 --workdir 下", existsSync(dbPath), dbPath);
}

// 8. 只读体检：不再有"自动发送"检查项
{
	const value = await call("wechat_doctor");
	const names = (value.data?.checks ?? []).map((item) => item.name);
	check(
		"体检里没有任何发送相关检查项",
		value.ok === true && !names.some((name) => String(name).includes("发送")),
		names.join(",")
	);
}

for (const item of results) {
	console.log(`${item.ok ? "PASS" : "FAIL"}  ${item.name}  —  ${item.detail}`);
}
const passed = results.filter((item) => item.ok).length;
console.log(`\n${passed}/${results.length} passed`);
process.exit(passed === results.length ? 0 : 1);
