/**
 * dsh-plugin-atuowexin — 微信数据「完整摘录 → 会话隔离 → AI 总结 → 回应草稿」的 DSH 工具集。
 *
 * ## 这个模块做什么
 *
 * 它只是**很薄的一层桥**：把模型给的参数翻译成 `python -m atuowexin --json <命令>` 的 argv，
 * 用 `subprocess` 服务在会话沙箱里跑起来，再解析 stdout 上那个唯一 JSON 对象，渲染成文本块。
 * 真正的活（读库、隔离、调模型、闸门）全在 `engine/atuowexin` 里，这边不重复实现，
 * 因此 CLI 与插件永远不会行为分叉。
 *
 * ## 为什么一个宿主包都不 import
 *
 * 按绝对路径挂载的工作区插件没有可达的 `node_modules`，`import ... from "@deepseek-ai/dsh-tools"`
 * 会在导入阶段直接失败（loader 只报一句 `failed to import`）。而把 `dsh-tools` 声明成真依赖
 * 更糟：它会带进第二份 `@deepseek-ai/cordis`，进程里出现第二个工具注册表。
 * 所以这里只用 Node 内置模块，并按注册表的**原始 JSON Schema 方言**注册工具
 * （对象层的 `required: [...]` 数组，而不是 `defineTool` 的逐属性 `required: true`）。
 *
 * ## 安全立场（与引擎一致）
 *
 * - 读取、解密、导出侧只读：不写微信库、不发送消息、不碰微信协议；数据来源是你自己解密的库或本机 chatlog。
 * - 密钥捕获是单独的显式 opt-in 工具：它会在启动期调用原生 Hook，且只把通过目标库 HMAC 校验的结果写入插件 workdir。
 * - 会话隔离：每条会话一个工作区，任何下游处理都只喂本会话的数据。
 *
 * @module dsh-plugin-atuowexin
 */
import { existsSync } from "node:fs";
import { readdir, readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { scheduleBootstrap } from "./bootstrap.js";

/** Cordis 插件身份。 */
export const name = "plugin-atuowexin";

/** 激活前必须就绪的服务。 */
export const inject = ["tools", "subprocess"];

const DEFAULT_TIMEOUT_MS = 180_000;
const SYNC_TIMEOUT_MS = 600_000;
const KEY_CAPTURE_TIMEOUT_MS = 300_000;
const DEFAULT_GRACE_MS = 5_000;
const DEFAULT_MAX_OUTPUT_BYTES = 400_000;
const MAX_TIMER_DELAY_MS = 2_147_483_647;

/** 这个插件所在的包目录（`<pkg>/lib/index.js` → `<pkg>`）。 */
const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

/** 强制 UTF-8：Windows 上捆绑解释器默认 cp936，中文会乱码。 */
const BASE_ENV = {
	PYTHONIOENCODING: "utf-8",
	PYTHONUTF8: "1",
	PYTHONUNBUFFERED: "1",
	PYTHONDONTWRITEBYTECODE: "1"
};

/** Harness 家目录（安装运行时 payload 的地方）。 */
function harnessHome() {
	return process.env.DSH_HOME ?? join(homedir(), ".dsh");
}

/** 一个运行时 payload 里的解释器路径。 */
function payloadInterpreter(root) {
	return process.platform === "win32"
		? join(root, "dependencies", "python", "python.exe")
		: join(root, "dependencies", "python", "bin", "python3");
}

/**
 * 找 Harness 自带运行时里的 Python。
 * @returns `{interpreter, version?}`，找不到返回 undefined。
 */
async function bundledPython() {
	const directory = process.env.DSH_RUNTIMES ?? join(harnessHome(), "dsh-runtimes");
	let entries;
	try {
		entries = await readdir(directory, { withFileTypes: true });
	} catch {
		return undefined;
	}
	const candidates = [];
	for (const entry of entries) {
		if (!entry.isDirectory()) continue;
		const root = join(directory, entry.name);
		let manifest;
		try {
			manifest = JSON.parse(await readFile(join(root, "runtime.json"), "utf8"));
		} catch {
			manifest = undefined;
		}
		if (manifest?.platform !== undefined && manifest.platform !== process.platform) continue;
		if (manifest?.arch !== undefined && manifest.arch !== process.arch) continue;
		const interpreter = payloadInterpreter(root);
		if (!existsSync(interpreter)) continue;
		candidates.push({ interpreter, ...(typeof manifest.python === "string" ? { version: manifest.python } : {}) });
	}
	candidates.sort((left, right) => left.interpreter.localeCompare(right.interpreter));
	return candidates[0];
}

/** 读取采集流的 JSON 视图。 */
function readStream(reader) {
	const read = reader.readFrom(0);
	return {
		text: read.text,
		truncated: read.lossy,
		...(read.spillPath !== undefined ? { spillPath: read.spillPath } : {})
	};
}

/** 从 stdout 里抠出 CLI 输出的唯一 JSON 对象。 */
function parseJsonOutput(text) {
	const trimmed = (text ?? "").trim();
	if (trimmed.length === 0) return undefined;
	try {
		return JSON.parse(trimmed);
	} catch {
		const start = trimmed.lastIndexOf("\n{");
		if (start >= 0) {
			try {
				return JSON.parse(trimmed.slice(start + 1));
			} catch {
				return undefined;
			}
		}
		return undefined;
	}
}

const STREAM_SCHEMA = {
	type: "object",
	additionalProperties: false,
	properties: {
		text: { type: "string" },
		truncated: { type: "boolean" },
		spillPath: { type: "string", description: "流太长被截断时，完整内容的落盘路径。" }
	},
	required: ["text", "truncated"]
};

const RESULT_SCHEMA = {
	type: "object",
	additionalProperties: false,
	properties: {
		ok: { type: "boolean", description: "命令是否成功（退出码 0 且能解析出 JSON）。" },
		command: { type: "string", description: "实际执行的 atuowexin 子命令。" },
		exitCode: { oneOf: [{ type: "integer" }, { type: "null" }] },
		timedOut: { type: "boolean" },
		aborted: { type: "boolean" },
		elapsedMs: { type: "number" },
		error: { type: "string", description: "失败时的可读原因（引擎的错误信息原样透传）。" },
		data: { type: "object", description: "引擎输出的结构化结果（不同子命令字段不同）。" },
		stdout: STREAM_SCHEMA,
		stderr: STREAM_SCHEMA
	},
	required: ["ok", "command", "exitCode", "timedOut", "aborted", "elapsedMs", "stdout", "stderr"]
};

/**
 * 把结构化结果渲染成模型可读的文本块。
 * @param value - 工具结果值。
 * @returns 内容块数组。
 */
function renderResult(value) {
	const sections = [];
	if (value.error) sections.push(`[失败] ${value.error}`);
	const data = value.data;
	if (!value.error && data && typeof data === "object") {
		sections.push(renderData(value.command, data));
	}
	const stderr = value.stderr?.text?.trim();
	if (stderr) sections.push(`[stderr]\n${stderr}`);
	if (value.timedOut) sections.push(`[超时] 超过 ${value.timeoutMs ?? "?"} ms 被终止`);
	else if (value.exitCode !== 0 && value.exitCode !== null) sections.push(`[exit code: ${value.exitCode}]`);
	const notes = [];
	if (value.stdout?.truncated) notes.push("stdout 被截断");
	if (value.stderr?.truncated) notes.push("stderr 被截断");
	if (notes.length > 0) sections.push(notes.join(" · "));
	return [{ type: "text", text: sections.filter(Boolean).join("\n\n") }];
}

/**
 * 会话显示名：引擎输出的字段随来源不同（`display_name` / `remark` / `name`），统一取第一个非空的。
 * @param item - 会话对象。
 * @returns 显示名。
 */
function displayName(item) {
	if (!item || typeof item !== "object") return "";
	return item.display_name || item.remark || item.name || item.id || "";
}

/**
 * 按子命令把引擎返回的 JSON 渲染成人话。
 * @param command - 子命令名。
 * @param data - 引擎输出的对象。
 * @returns 文本。
 */
function renderData(command, data) {
	if (command === "doctor") {
		const lines = [data.ok ? "体检通过" : "体检有问题", `后端：${data.backend}（${data.backend_reason || "未解析"}）`, `工作目录：${data.workdir}`];
		for (const check of data.checks ?? []) {
			const mark = check.ok ? "OK  " : (check.level === "error" ? "FAIL" : "WARN");
			lines.push(`${mark} ${check.name}：${check.detail}`);
			if (!check.ok && check.hint) lines.push(`     → ${check.hint}`);
		}
		return lines.join("\n");
	}
	if (command === "sync") {
		const errors = data.errors ?? [];
		const head = `摘录完成｜后端 ${data.backend}${data.synthetic ? "（合成数据）" : ""}｜${data.conversations} 会话｜读取 ${data.fetched} 条｜新增 ${data.added} 条｜${data.elapsed_ms} ms`;
		const rows = (data.details ?? []).map(
			(item) => `- ${displayName(item)}（${item.conversation_id}）：新增 ${item.added}${item.error ? ` ❌ ${item.error}` : ""}`
		);
		const lines = [head, ...rows];
		if (errors.length > 0) {
			// 摘录失败的原因必须显式出现在结果里：真实数据读不到时，这几行就是唯一的行动指引
			lines.push("", "❌ 摘录失败的原因：", ...errors.map((item) => `- ${item}`));
		}
		if (data.reason) lines.push("", `后端选择：${data.reason}`);
		return lines.filter(Boolean).join("\n");
	}
	if (command === "conversations") {
		const items = data.conversations ?? [];
		if (items.length === 0) return "（还没有会话，先运行 wechat_sync）";
		return ["会话列表（名字 / id / 类型 / 消息数 / 最近时间）：", ...items.map((item) => `- ${displayName(item)} | ${item.id} | ${item.kind} | ${item.msg_count} | ${item.last_time}`)].join("\n");
	}
	if (command === "messages") {
		const items = data.messages ?? [];
		return [`会话 ${displayName(data.conversation)}（${data.conversation?.id}），返回 ${items.length} 条：`, ...items.map((item) => `- [${item.ts}] ${item.is_self ? "我" : item.sender_name || item.sender_id}: ${item.text || `[${item.kind}]`}`)].join("\n");
	}
	if (command === "export") {
		return [`已导出 ${data.messages} 条到 ${data.dir}`, ...(data.files ?? []).map((item) => `- ${item}`)].join("\n");
	}
	return JSON.stringify(data, null, 2).slice(0, 4000);
}

/**
 * 编译一个「CLI 包装」工具。
 * @param spec - 工具声明：名称、描述、参数 schema、argv 构造器。
 * @param hooks - 运行时钩子（跑命令）。
 * @returns 原始 JSON Schema 方言的工具定义。
 */
function cliTool(spec, hooks) {
	return {
		name: spec.name,
		description: spec.description,
		parameters: spec.parameters,
		output: {
			schema: RESULT_SCHEMA,
			render: (_args, value) => renderResult(value)
		},
		async execute(args, exec) {
			const { command, argv } = spec.build(args ?? {});
			const result = await hooks.run(command, argv, args ?? {}, exec);
			return result;
		},
		presentCall: (args) => ({
			card: "terminal",
			title: spec.title ?? spec.name,
			description: spec.present ? spec.present(args ?? {}) : spec.name
		})
	};
}

const NULLABLE_STRING = { oneOf: [{ type: "string" }, { type: "null" }] };

/**
 * 注册全部 atuowexin 工具。
 * @param ctx - 提供 `tools`（必需）与 `subprocess`；`sandboxPolicy`/`sandbox` 存在时用于隔离。
 * @param config - 插件配置（见 README「插件配置」）。
 */
export function apply(ctx, config = {}) {
	scheduleBootstrap(config);
	const engineDir = config.engineDir === undefined ? join(PACKAGE_ROOT, "engine") : String(config.engineDir);
	const configuredPython = config.pythonPath === undefined
		? (process.env.ATUOWEXIN_PYTHON || process.env.DSH_PYTHON || undefined)
		: String(config.pythonPath);
	const configuredWorkdir = config.workdir === undefined ? undefined : String(config.workdir);
	const configuredConfig = config.configPath === undefined ? undefined : String(config.configPath);
	const backend = config.backend === undefined ? undefined : String(config.backend);
	const defaultTimeoutMs = Number.isFinite(config.timeoutMs) ? Number(config.timeoutMs) : DEFAULT_TIMEOUT_MS;
	const graceMs = Number.isFinite(config.graceMs) ? Number(config.graceMs) : DEFAULT_GRACE_MS;
	const maxOutputBytes = Number.isFinite(config.maxOutputBytes) ? Number(config.maxOutputBytes) : DEFAULT_MAX_OUTPUT_BYTES;

	if (!existsSync(engineDir)) throw new Error(`plugin-atuowexin: engineDir 不存在：${engineDir}`);

	let discovered;
	/** 解析解释器：显式配置 > 自带运行时 > PATH 上的 python。 */
	const resolvePython = async (signal) => {
		if (configuredPython !== undefined) return { interpreter: await ctx.subprocess.resolveExecutable(configuredPython, BASE_ENV, signal) };
		discovered ??= await bundledPython();
		if (discovered !== undefined) return discovered;
		for (const candidate of (process.platform === "win32" ? ["python", "py", "python3"] : ["python3", "python"])) {
			try {
				return { interpreter: await ctx.subprocess.resolveExecutable(candidate, BASE_ENV, signal) };
			} catch (error) {
				if (signal?.aborted) throw error;
			}
		}
		throw new Error("plugin-atuowexin: 找不到 Python，请安装 Python 3.11+，或在 config.pythonPath / ATUOWEXIN_PYTHON / DSH_PYTHON 指定解释器");
	};

	/**
	 * 跑一个 atuowexin 子命令。
	 * @param command - 子命令名（用于渲染）。
	 * @param args - 追加到 argv 的参数。
	 * @param raw - 模型传入的原始参数（取 timeoutMs / workdir）。
	 * @param exec - 工具执行上下文。
	 * @returns 结构化结果值。
	 */
	const run = async (command, args, raw, exec) => {
		const startedAt = Date.now();
		const timeoutMs = Number.isFinite(raw.timeoutMs) ? Number(raw.timeoutMs) : defaultTimeoutMs;
		if (timeoutMs > MAX_TIMER_DELAY_MS) throw new Error(`plugin-atuowexin: timeoutMs 不能大于 ${MAX_TIMER_DELAY_MS}`);
		const python = await resolvePython(exec.signal);
		const workdir = typeof raw.workdir === "string" && raw.workdir.length > 0
			? (exec.agent?.session?.header?.cwd !== undefined ? resolve(exec.agent.session.header.cwd, raw.workdir) : raw.workdir)
			: configuredWorkdir;

		const argv = [python.interpreter, "-m", "atuowexin", "--json"];
		if (configuredConfig !== undefined) argv.push("--config", configuredConfig);
		if (workdir !== undefined) argv.push("--workdir", workdir);
		if (backend !== undefined) argv.push("--backend", backend);
		argv.push(command, ...args);

		const env = { ...BASE_ENV, PYTHONPATH: engineDir };
		let sandbox;
		const policy = ctx.get?.("sandboxPolicy")?.resolve(exec.agent === undefined ? {} : { session: exec.agent.session });
		let finalArgv = argv;
		if (policy !== undefined && policy.mode !== "danger-full-access") {
			const confiner = ctx.get?.("sandbox");
			if (confiner === undefined) throw new Error(`plugin-atuowexin: 当前会话运行在 ${policy.mode} 模式但没有沙箱提供者，拒绝无隔离执行`);
			const confined = await confiner.confine(argv, { mode: policy.mode, workspaceRoot: policy.workspaceRoot }, exec.signal);
			finalArgv = confined.argv;
			sandbox = { mode: policy.mode, enforcement: confined.enforcement };
		}

		let handle;
		try {
			handle = ctx.subprocess.spawn({
				argv: finalArgv,
				cwd: engineDir,
				stdio: {
					stdin: "ignore",
					stdout: { maxBytes: maxOutputBytes, spill: { maxBytes: maxOutputBytes * 8 } },
					stderr: { maxBytes: maxOutputBytes, spill: { maxBytes: maxOutputBytes * 8 } }
				},
				graceMs,
				signal: exec.signal,
				env
			});
		} catch (error) {
			throw new Error(`plugin-atuowexin: 无法启动 ${python.interpreter}：${String(error)}`);
		}

		let timedOut = false;
		const timer = setTimeout(() => {
			timedOut = true;
			try {
				handle.terminate();
			} catch {
				/* 进程已结束 */
			}
		}, timeoutMs);
		let outcome;
		try {
			outcome = await handle.done;
		} finally {
			clearTimeout(timer);
		}
		const stdout = readStream(handle.collected.stdout);
		const stderr = readStream(handle.collected.stderr);
		const payload = parseJsonOutput(stdout.text);
		const exitCode = outcome.exitCode === undefined ? null : outcome.exitCode;
		const failed = payload !== undefined && payload.ok === false && typeof payload.error === "string";
		return {
			ok: !timedOut && exitCode === 0 && payload !== undefined && payload.ok !== false,
			command,
			exitCode,
			timedOut,
			aborted: exec.signal.aborted,
			elapsedMs: Date.now() - startedAt,
			...(failed ? { error: payload.error } : {}),
			...(payload !== undefined ? { data: payload } : {}),
			...(sandbox !== undefined ? { sandbox } : {}),
			stdout,
			stderr
		};
	};

	const runSync = (command, args, raw, exec) => run(command, args, raw, exec);

	// --------------------------------------------------------------------- //
	// 工具定义
	// --------------------------------------------------------------------- //

	ctx.tools.register(
		cliTool(
			{
				name: "wechat_doctor",
				title: "微信环境体检",
				description:
					"检查 atuowexin 的运行环境：工作目录、配置、三种读取后端是否可用、本机微信数据目录位置、" +
					"chatlog/Docker 是否安装、SQLCipher 解密工具是否可用、-wal 里还有多少未合并的新数据、" +
					"可选依赖（zstd 解压被压缩的正文）、本地已摘录数据量。" +
					"任何一步读不到微信数据时，先跑这个工具，它会给出可执行的下一步。" +
					"读取与导出侧是**只读**的：不含发送消息、不写微信库、不调用云端模型；密钥捕获是单独的显式 opt-in Hook 工具。",
				parameters: { type: "object", additionalProperties: false, properties: {} },
				build: () => ({ command: "doctor", argv: [] })
			},
			{ run: runSync }
		)
	);

	ctx.tools.register(
		cliTool(
			{
				name: "wechat_sync",
				title: "完整摘录微信数据",
				description:
					"把微信会话与消息**完整摘录**到本地归一化库（SQLite）与每条会话的隔离工作区（JSONL）。" +
					"默认增量：以每条会话的最大时间戳为游标，重复调用不会重复计数。full=true 时从最早重新摘一遍（同样幂等）。" +
					"数据来源按配置自动选择：本机 chatlogHTTP 服务（支持微信 4.x）→ 你已解密的 SQLite 目录 → 合成数据（仅离线自测，会明确标注）。",
				parameters: {
					type: "object",
					additionalProperties: false,
					properties: {
						full: { type: "boolean", description: "忽略游标，从头重新摘一遍。" },
						conversation: {
							type: "array",
							items: { type: "string" },
							description: "只摘这些会话（id 或名字，支持子串）。省略则摘全部会话。"
						},
						since: { type: "string", description: "最早时间：时间戳 / 2026-01-31 / 2026-01-31 09:30 / 7d / 12h。" },
						limit: { type: "number", description: "每个会话最多摘多少条（0 或不填 = 不限，保证「完整」）。" },
						timeoutMs: { type: "number", description: `超时毫秒数，默认 ${SYNC_TIMEOUT_MS}。` }
					}
				},
				build: (args) => {
					const argv = [];
					if (args.full === true) argv.push("--full");
					for (const item of args.conversation ?? []) argv.push("--conversation", String(item));
					if (typeof args.since === "string" && args.since.length > 0) argv.push("--since", args.since);
					if (Number.isFinite(args.limit) && Number(args.limit) > 0) argv.push("--limit", String(args.limit));
					return { command: "sync", argv };
				},
				present: (args) => (args.full ? "全量重摘" : "增量摘录")
			},
			{ run: (command, argv, raw, exec) => run(command, argv, { timeoutMs: SYNC_TIMEOUT_MS, ...raw }, exec) }
		)
	);

	ctx.tools.register(
		cliTool(
			{
				name: "wechat_conversations",
				title: "列出微信会话",
				description: "列出本地已摘录的会话（名字、id、类型、消息数、最近时间），可按关键词或类型过滤。会话 id 是后续所有工具的寻址键。",
				parameters: {
					type: "object",
					additionalProperties: false,
					properties: {
						query: { type: "string", description: "按名字/备注/id 模糊过滤。" },
						kind: { type: "string", enum: ["friend", "group", "official", "service"], description: "只看某一类会话。" },
						limit: { type: "number", description: "最多返回多少条（默认 50）。" }
					}
				},
				build: (args) => {
					const argv = [];
					if (typeof args.query === "string" && args.query.length > 0) argv.push("--query", args.query);
					if (typeof args.kind === "string" && args.kind.length > 0) argv.push("--kind", args.kind);
					if (Number.isFinite(args.limit)) argv.push("--limit", String(args.limit));
					return { command: "conversations", argv };
				}
			},
			{ run: runSync }
		)
	);

	ctx.tools.register(
		cliTool(
			{
				name: "wechat_messages",
				title: "读取某会话的消息",
				description:
					"读取**某一条会话**的已摘录消息（强制按会话隔离，不存在「跨会话查询」这种调用）。支持时间范围、关键词与分页；" +
					"非文本消息（图片/语音/文件等）会以 [图片] 之类的类型占位出现。redacted=true 时对手机号/证件号/卡号脱敏。",
				parameters: {
					type: "object",
					additionalProperties: false,
					properties: {
						conversation: { type: "string", description: "会话 id 或名字（必填）。" },
						since: { type: "string", description: "起始时间（时间戳 / 2026-01-31 / 7d）。" },
						until: { type: "string", description: "结束时间。" },
						keyword: { type: "string", description: "只返回正文包含该关键词的消息。" },
						limit: { type: "number", description: "最多返回多少条（默认 50）。" },
						offset: { type: "number", description: "跳过多少条（配合 limit 翻页）。" },
						redacted: { type: "boolean", description: "输出前对手机号/身份证/银行卡脱敏。" }
					},
					required: ["conversation"]
				},
				build: (args) => {
					const argv = ["--conversation", String(args.conversation)];
					if (typeof args.since === "string" && args.since.length > 0) argv.push("--since", args.since);
					if (typeof args.until === "string" && args.until.length > 0) argv.push("--until", args.until);
					if (typeof args.keyword === "string" && args.keyword.length > 0) argv.push("--keyword", args.keyword);
					if (Number.isFinite(args.limit)) argv.push("--limit", String(args.limit));
					if (Number.isFinite(args.offset)) argv.push("--offset", String(args.offset));
					if (args.redacted === true) argv.push("--redacted");
					return { command: "messages", argv };
				}
			},
			{ run: runSync }
		)
	);

	ctx.tools.register(
		cliTool(
			{
				name: "wechat_export",
				title: "导出会话为文件",
				description:
					"把**某一条会话**的完整消息导出为 jsonl、csv、md、txt、html、xlsx、pdf 与 conversation.json。" +
					"formats 可任意组合；html 支持搜索与气泡布局，xlsx 有冻结表头/筛选，图片会复制到 HTML 同级的 图片/ 目录。" +
					"txt/html 无额外依赖，xlsx 需要 openpyxl，pdf 需要 fpdf2；缺依赖时会明确报错，不静默降级。" +
					"便于离线交给别人或用别的工具处理。redacted=true 时先脱敏。",
				parameters: {
					type: "object",
					additionalProperties: false,
					properties: {
						conversation: { type: "string", description: "会话 id 或名字（必填）。" },
						out: { type: "string", description: "输出目录；省略则用工作目录下的 exports/。" },
						formats: {
							type: "string",
							description: "逗号分隔的格式，默认 jsonl,csv,md；可选 jsonl,csv,md,txt,html,xlsx,pdf。xlsx 需要 openpyxl，pdf 需要 fpdf2。"
						},
						redacted: { type: "boolean", description: "导出前脱敏。" }
					},
					required: ["conversation"]
				},
				build: (args) => {
					const argv = ["--conversation", String(args.conversation)];
					if (typeof args.out === "string" && args.out.length > 0) argv.push("--out", args.out);
					if (typeof args.formats === "string" && args.formats.length > 0) argv.push("--formats", args.formats);
					if (args.redacted === true) argv.push("--redacted");
					return { command: "export", argv };
				}
			},
			{ run: runSync }
		)
	);

	ctx.tools.register(
		cliTool(
			{
				name: "wechat_key_capture",
				title: "启动期 Hook 捕获微信密钥",
				description:
					"显式 opt-in 的密钥捕获入口：等待微信关闭 → 等待启动/登录 → 注入 wx_key.dll Hook → 捕获 64 位十六进制 key。" +
					"这一步会写入目标进程内存，只能在你明确需要时调用；捕获结果改用 WCDB 试开校验" +
					"（能打开并列出会话才算可用）—— 4.1.10+ 的库不是标准 SQLCipher 派生，HMAC 会误判。" +
					"前置条件会逐项检查 node、koffi、wx_key.dll 与管理员权限，状态会通过 stderr 实时返回；产物默认放插件 workdir/capture。",
				parameters: {
					type: "object",
					additionalProperties: false,
					properties: {
						root: { type: "string", description: "**加密**库目录（db_storage）；省略则自动探测本机微信数据目录。" },
						out: { type: "string", description: "校验通过后写入的 keymap.json 路径；省略则用插件 workdir/keymap.json。" },
						dll: { type: "string", description: "wx_key.dll 文件或所在目录；省略则查找参考工具 dll/ 目录与插件 engine/tools/dll/。" },
						node: { type: "string", description: "Node.js 可执行文件；省略则从 PATH 查找 node。" },
						timeout: { type: "number", description: "捕获等待时长（秒，默认 120）。" },
						show_key: { type: "boolean", description: "显式要求在结果中回显完整密钥；默认只显示前缀。" },
						timeoutMs: { type: "number", description: `整个命令的超时毫秒数，默认 ${KEY_CAPTURE_TIMEOUT_MS}。` }
					}
				},
				build: (args) => {
					const argv = ["capture"];
					if (typeof args.root === "string" && args.root.length > 0) argv.push("--root", args.root);
					if (typeof args.out === "string" && args.out.length > 0) argv.push("--out", args.out);
					if (typeof args.dll === "string" && args.dll.length > 0) argv.push("--dll", args.dll);
					if (typeof args.node === "string" && args.node.length > 0) argv.push("--node", args.node);
					if (Number.isFinite(args.timeout) && Number(args.timeout) > 0) argv.push("--timeout", String(args.timeout));
					if (args.show_key === true) argv.push("--show-key");
					return { command: "key", argv };
				},
				present: () => "启动期捕获并校验微信密钥"
			},
			{ run: (command, argv, raw, exec) => {
				const requested = Number.isFinite(raw.timeoutMs) ? Number(raw.timeoutMs) : KEY_CAPTURE_TIMEOUT_MS;
				const captureSeconds = Number.isFinite(raw.timeout) && Number(raw.timeout) > 0 ? Number(raw.timeout) : 120;
				return run(command, argv, { ...raw, timeoutMs: Math.max(requested, captureSeconds * 1000 + 60_000) }, exec);
			} }
		)
	);

	ctx.tools.register(
		cliTool(
			{
				name: "wechat_prepare",
				title: "一键准备：关微信 → 起 Hook → 拉起微信 → 登录 → 取 key → 校验",
				description:
					"一条命令跑完整条准备流水线，省掉手动记顺序：①微信在跑就先关掉（Hook 必须赶在它写密钥之前）；" +
					"②起捕获（注入 wx_key.dll）；③自动拉起微信；④尽力自动点「进入微信」；⑤捕获 64 位 key；" +
					"⑥用 WCDB 试开校验（能列会话才算可用）。" +
					"返回值里 stage 是状态机：done=就绪；login_required=需要用户去微信点「进入微信」（首次登录需手机扫码）——" +
					"看到它**不要重试**，直接告诉用户去登录，登录完再调用一次即可。" +
					"产物默认放插件 workdir/capture；默认不回显密钥明文。",
				parameters: {
					type: "object",
					additionalProperties: false,
					properties: {
						root: { type: "string", description: "**加密**库目录（db_storage）；省略则自动探测。" },
						out: { type: "string", description: "keymap.json 路径；省略用插件 workdir/keymap.json。" },
						exe: { type: "string", description: "微信可执行文件路径；省略自动找 Weixin.exe。" },
						keep_open: { type: "boolean", description: "true 时若微信在跑就直接返回 wechat_must_close，不去关闭它。" },
						auto_click: { type: "boolean", description: "是否自动点「进入微信」，默认 true。" },
						login_wait: { type: "number", description: "微信起来后等用户登录的秒数（默认 40）；超时返回 login_required。" },
						timeout: { type: "number", description: "整条流水线的秒级上限（默认 180）。" },
						show_key: { type: "boolean", description: "在结果中回显完整密钥；默认只显示前缀。" },
						timeoutMs: { type: "number", description: "整个命令的超时毫秒数，默认 240000。" }
					}
				},
				build: (args) => {
					const argv = ["auto"];
					if (typeof args.root === "string" && args.root.length > 0) argv.push("--root", args.root);
					if (typeof args.out === "string" && args.out.length > 0) argv.push("--out", args.out);
					if (typeof args.exe === "string" && args.exe.length > 0) argv.push("--exe", args.exe);
					if (args.keep_open === true) argv.push("--keep-open");
					if (args.auto_click === false) argv.push("--no-auto-click");
					if (Number.isFinite(args.login_wait) && Number(args.login_wait) > 0) argv.push("--login-wait", String(args.login_wait));
					if (Number.isFinite(args.timeout) && Number(args.timeout) > 0) argv.push("--timeout", String(args.timeout));
					if (args.show_key === true) argv.push("--show-keys");
					return { command: "key", argv };
				},
				present: () => "准备微信数据（关微信→Hook→拉起→登录→取 key→校验）"
			},
			{ run: (command, argv, raw, exec) => {
				const requested = Number.isFinite(raw.timeoutMs) ? Number(raw.timeoutMs) : 240_000;
				const pipeline = Number.isFinite(raw.timeout) && Number(raw.timeout) > 0 ? Number(raw.timeout) : 180;
				return run(command, argv, { ...raw, timeoutMs: Math.max(requested, pipeline * 1000 + 90_000) }, exec);
			} }
		)
	);

	ctx.tools.register(
		cliTool(
			{
				name: "wechat_find_key",
				title: "确保数据库密钥就绪（缺了才去读）",
				description:
					"**先看有没有 key，没有就去读**：检查加密库里每个 .db 的密钥是否齐备；全都有就立刻返回" +
					"（不碰内存，毫秒级）；缺了才从**正在运行的微信进程内存**里自动找出来并合并写进 keymap.json。" +
					"只读扫描：OpenProcess(PROCESS_VM_READ) + ReadProcessMemory，**不注入、不写目标内存、不碰协议**；" +
					"每把候选密钥都必须通过目标库第 1 页的 HMAC 校验才会被采纳（绝不猜、绝不写空 keymap）。" +
					"结果默认**不回显密钥明文**，只写文件并报路径。" +
					"找法：优先拿**已有密钥当路标**（实测同一批库的密钥在内存里聚簇，相隔约 1.6 KB），" +
					"所以只要 keymap.json 里有一把，就能把同一邻域里其他库的密钥一起找出来；没有路标时才退回" +
					"按库路径锚点扫描（慢得多）。" +
					"⚠️ 已知限制：微信 ≥4.1.10 起密钥不再常驻内存（4.1.13.65 实测 657 MB 内存里一把都没有）；" +
					"≤4.1.9（实测 4.1.7.30）可以正常提取。缺的通常是**本次没被微信打开过**的库——" +
					"在微信里点开对应功能后再调用一次即可。",
				parameters: {
					type: "object",
					additionalProperties: false,
					properties: {
						root: { type: "string", description: "**加密**库目录（db_storage）；省略则自动探测本机微信数据目录。" },
						out: { type: "string", description: "keymap.json 路径（既当已有的 key 来源，也当输出）；省略则用 <atuowexin home>/keymap.json。" },
						ensure: { type: "boolean", description: "true（默认）= 先检查、缺了才提取；false = 无条件扫描一次。" },
						check_only: { type: "boolean", description: "只检查密钥是否齐备，不提取（用于询问「现在能不能读」）。" },
						watch: { type: "boolean", description: "持续轮询（密钥可能只在微信重新登录的瞬间驻留内存）。" },
						seconds: { type: "number", description: "watch 的总时长秒数（默认 60）。" },
						sqlcipher_version: { type: "number", description: "SQLCipher 参数集：微信 4.x 用 4（默认），3.x 用 3。" },
						timeoutMs: { type: "number", description: `超时毫秒数，默认 ${defaultTimeoutMs}。` }
					}
				},
				build: (args) => {
					const argv = [args.ensure === false ? "find" : "ensure"];
					if (typeof args.root === "string" && args.root.length > 0) argv.push("--root", args.root);
					if (typeof args.out === "string" && args.out.length > 0) argv.push("--out", args.out);
					if (args.check_only === true) argv.push("--check-only");
					if (args.watch === true) {
						argv.push("--watch");
						if (Number.isFinite(args.seconds)) argv.push("--seconds", String(args.seconds));
					}
					if (Number.isFinite(args.sqlcipher_version)) argv.push("--version", String(args.sqlcipher_version));
					return { command: "key", argv };
				},
				present: (args) => (args.check_only === true ? "检查密钥是否齐备" : "确保密钥就绪（缺了自动提取）")
			},
			{ run: runSync }
		)
	);
}

export { renderData, parseJsonOutput, NULLABLE_STRING };
