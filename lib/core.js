/**
 * 核心八工具（用户定义的功能边界：只读微信聊天记录）。
 *
 * 每个工具 = 一个 CLI 子命令（python -m atuowexin wx <action>），
 * 这里只做参数翻译与描述，不做业务逻辑。
 */
export function registerCoreTools(ctx, { run, cliTool }) {
	const def = (spec) => {
		const runHook = (command, argv, raw, exec) => {
			const timeoutMs = Number.isFinite(raw.timeoutMs) ? raw.timeoutMs : spec.timeoutMs;
			return run(command, argv, timeoutMs ? { ...raw, timeoutMs } : raw, exec);
		};
		ctx.tools.register(cliTool(spec, { run: runHook }));
	};

	def({
		name: "wechat_check",
		title: "1 检测微信",
		description: "检测本机是否安装微信。没装会返回 installed=false 并给出安装提示。",
		parameters: { type: "object", additionalProperties: false, properties: {} },
		build: () => ({ command: "wx", argv: ["check"] }),
		present: () => "检测微信是否安装"
	});

	def({
		name: "wechat_locate",
		title: "2 找聊天记录位置",
		description: "定位本地聊天记录目录（账号目录 + db_storage），返回库数量、是否存在 session.db、是否已有暂存密钥。",
		parameters: {
			type: "object",
			additionalProperties: false,
			properties: { root: { type: "string", description: "可选：直接指定账号目录或 db_storage" } }
		},
		build: (a) => ({ command: "wx", argv: a.root ? ["locate", "--root", a.root] : ["locate"] }),
		present: () => "定位微信聊天记录目录"
	});

	def({
		name: "wechat_close",
		title: "3 退出微信",
		description: "微信在运行就先退出；没运行直接返回（不算失败）。",
		parameters: { type: "object", additionalProperties: false, properties: {} },
		build: () => ({ command: "wx", argv: ["close"] }),
		present: () => "退出微信进程"
	});

	def({
		name: "wechat_hook",
		title: "4 钩子取密钥（5 分钟扫描）",
		description: "先起 Hook，再拉起微信停在登录页，呼唤用户登录；扫描窗口默认 300 秒，拿到密钥就暂存。超时返回 found=false，此时应回到第 2 个工具重新定位后重试。不自动点登录。",
		parameters: {
			type: "object",
			additionalProperties: false,
			properties: {
				root: { type: "string", description: "账号目录或 db_storage（可选）" },
				scan_seconds: { type: "number", description: "扫描窗口秒数，默认 300" }
			}
		},
		build: (a) => {
			const argv = ["hook"];
			if (a.root) argv.push("--root", a.root);
			if (Number.isFinite(a.scan_seconds)) argv.push("--scan", String(a.scan_seconds));
			return { command: "wx", argv };
		},
		present: () => "挂 Hook 并等待登录取密钥",
		timeoutMs: 420_000
	});

	def({
		name: "wechat_list",
		title: "5 会话列表（名称 + 最新时间）",
		description: "扫描会话名称与最新时间，按时间倒序返回给上层，便于挑选要读的会话。",
		parameters: {
			type: "object",
			additionalProperties: false,
			properties: {
				limit: { type: "number", description: "最多返回多少个会话，默认 200" },
				query: { type: "string", description: "按名称/ID 过滤" }
			}
		},
		build: (a) => {
			const argv = ["list"];
			if (Number.isFinite(a.limit)) argv.push("--limit", String(a.limit));
			if (a.query) argv.push("--query", a.query);
			return { command: "wx", argv };
		},
		present: () => "列出会话名称与最新时间"
	});

	def({
		name: "wechat_read",
		title: "6 读会话内容（支持批量）",
		description: "按会话名称或 ID 读消息，可一次给多个会话（批量）。redacted=true 时对正文做 PII 脱敏（手机/证件/银行卡/邮箱/wxid/车牌）。注意：WCDB 接口每个会话最多返回最近 1200 条；要全量请用第 7 个工具解密后读。",
		parameters: {
			type: "object",
			additionalProperties: false,
			properties: {
				conversations: { type: "array", items: { type: "string" }, description: "会话名或 ID 列表（批量）" },
				limit: { type: "number", description: "每个会话最多多少条，默认 200" },
				redacted: { type: "boolean", description: "是否脱敏" }
			},
			required: ["conversations"]
		},
		build: (a) => {
			const argv = ["read"];
			for (const name of a.conversations ?? []) argv.push("--conversation", String(name));
			if (Number.isFinite(a.limit)) argv.push("--limit", String(a.limit));
			if (a.redacted === true) argv.push("--redacted");
			return { command: "wx", argv };
		},
		present: () => "读取会话消息"
	});

	def({
		name: "wechat_decrypt",
		title: "7 解密本地数据（Frida 取 passphrase）",
		description: "Frida spawn 微信并在 codec 配置函数挂钩取候选，用首页 HMAC 判定 passphrase，再派生每库 enc_key 并 AES 整库解密。产物是明文库，可用 sqlite3 直查（不受 1200 条限制）。需要 frida；会重启微信（spawn），请先退出微信。",
		parameters: {
			type: "object",
			additionalProperties: false,
			properties: {
				seconds: { type: "number", description: "等待登录并收集候选的秒数，默认 180" },
				out_dir: { type: "string", description: "解密产物目录（可选）" }
			}
		},
		build: (a) => {
			const argv = ["decrypt"];
			if (Number.isFinite(a.seconds)) argv.push("--seconds", String(a.seconds));
			if (a.out_dir) argv.push("--out-dir", a.out_dir);
			return { command: "wx", argv };
		},
		present: () => "解密微信本地数据库",
		timeoutMs: 600_000
	});

	def({
		name: "wechat_unread",
		title: "8 只看未读",
		description: "从解密后的 session.db 读未读计数，返回未读会话（名称、条数、最近时间、摘要），可选脱敏。需要先跑第 7 个工具解密。",
		parameters: {
			type: "object",
			additionalProperties: false,
			properties: {
				limit: { type: "number", description: "最多返回多少个会话，默认 50" },
				decrypted_dir: { type: "string", description: "解密目录（可选，默认自动找）" },
				redacted: { type: "boolean", description: "是否脱敏摘要" }
			}
		},
		build: (a) => {
			const argv = ["unread"];
			if (Number.isFinite(a.limit)) argv.push("--limit", String(a.limit));
			if (a.decrypted_dir) argv.push("--decrypted-dir", a.decrypted_dir);
			if (a.redacted === true) argv.push("--redacted");
			return { command: "wx", argv };
		},
		present: () => "列出未读会话"
	});
}
