/**
 * Agent Operations plugin for OpenClaw.
 *
 * Maps OpenClaw plugin hooks to aops.v1 events and ships them to the local collector.
 *
 *   before_agent_start .. agent_end        -> run
 *   llm_input .. llm_output                -> llm span
 *   before_tool_call .. after_tool_call    -> tool span (child of the latest llm span)
 *   subagent_spawned .. subagent_ended     -> subagent span; the child session's runs link back to it
 *
 * Hook payload shapes differ across OpenClaw versions, so every field is read defensively
 * and every handler is wrapped: this plugin must never break the agent.
 */

type Dict = Record<string, any>;
type Api = {
  on: (hook: string, handler: (event: Dict, ctx: Dict) => unknown, opts?: Dict) => void;
  pluginConfig?: Dict;
  config?: Dict;
  logger?: { info?: (m: string) => void; warn?: (m: string) => void };
};

const DEFAULT_ENDPOINT = "http://127.0.0.1:4319";
const MAX_FIELD = 32_000;
const SECRET = /(sk-(?:ant-|proj-)?[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abposr]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{30,}|[Bb]earer\s+[A-Za-z0-9._\-]{20,})/g;
const SECRET_KEY = /^(api[_-]?key|token|secret|password|passwd|authorization|cookie|access[_-]?token|refresh[_-]?token|client[_-]?secret)$/i;
const CONTENT = new Set(["args", "result", "request", "response", "prompt", "user_message", "assistant_response", "child_goal", "child_summary"]);

const uid = () => (globalThis.crypto?.randomUUID?.() ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`);

export function scrub(v: unknown, capture = true, depth = 0): unknown {
  if (depth > 12) return "…";
  if (typeof v === "string") {
    if (!capture) return `<${v.length} chars>`;
    const s = v.replace(SECRET, "[REDACTED]");
    return s.length > MAX_FIELD ? `${s.slice(0, MAX_FIELD)}… [+${s.length - MAX_FIELD} chars]` : s;
  }
  if (Array.isArray(v)) return v.slice(0, 500).map((x) => scrub(x, capture, depth + 1));
  if (v && typeof v === "object") {
    const out: Dict = {};
    for (const [k, x] of Object.entries(v)) out[k] = SECRET_KEY.test(k) ? "[REDACTED]" : scrub(x, capture, depth + 1);
    return out;
  }
  return v ?? null;
}

export function toolKind(name: string, params: Dict = {}): string {
  const n = (name || "").toLowerCase();
  if (n === "exec" || n === "process" || n === "bash") return "terminal";
  if (n === "browser" || n.startsWith("browser_")) return "browser";
  if (n.startsWith("memory")) return "memory";
  if (n === "sessions_spawn" || n === "subagents") return "subagent";
  if (n.includes("__") || params.server || n.startsWith("mcp")) return "mcp";
  return "tool";
}

class Shipper {
  endpoint: string;
  capture: boolean;
  queue: Dict[] = [];
  timer: ReturnType<typeof setTimeout> | null = null;
  inflight: Promise<void> = Promise.resolve();
  failures = 0;

  constructor(endpoint: string, capture: boolean) {
    this.endpoint = endpoint.replace(/\/$/, "");
    this.capture = capture;
  }

  emit(type: string, run_id: string, fields: Dict = {}, attrs: Dict = {}): void {
    const clean: Dict = {};
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null) continue;
      clean[k] = CONTENT.has(k) ? scrub(v, this.capture) : scrub(v);
    }
    if (typeof fields.name === "string") fields = { ...fields, name: scrub(fields.name) };  // titles come from prompts
    this.queue.push({ id: uid(), ts: Date.now() / 1000, type, run_id, source: "openclaw", ...fields, attrs: clean });
    if (this.queue.length >= 200) this.flush();
    else if (!this.timer) this.timer = setTimeout(() => this.flush(), 500);
  }

  flush(): Promise<void> {
    if (this.timer) { clearTimeout(this.timer); this.timer = null; }
    const batch = this.queue.splice(0, this.queue.length);
    if (!batch.length) return this.inflight;
    this.inflight = this.inflight.then(async () => {
      try {
        const res = await fetch(`${this.endpoint}/v1/events`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ events: batch }),
          signal: AbortSignal.timeout(2000),
        });
        if (!res.ok) throw new Error(String(res.status));
        this.failures = 0;
      } catch {
        // Collector down: keep a bounded backlog and retry with the next batch.
        this.failures++;
        const room = Math.max(0, 5000 - this.queue.length);
        if (room > 0) this.queue.unshift(...batch.slice(Math.max(0, batch.length - room)));
        if (!this.timer) this.timer = setTimeout(() => this.flush(), Math.min(30_000, 1000 * 2 ** this.failures));
      }
    });
    return this.inflight;
  }
}

export class OpenClawObserver {
  ship: Shipper;
  sessionRun = new Map<string, string>();        // sessionKey -> current run id
  lastLlm = new Map<string, string>();           // run id -> latest llm span id
  openLlm = new Map<string, string>();           // run id -> llm span awaiting llm_output
  toolFifo = new Map<string, string[]>();        // `${run}|${tool}` -> open span ids
  childOf = new Map<string, [string, string]>(); // child sessionKey -> [parent run, subagent span]

  constructor(endpoint = DEFAULT_ENDPOINT, capture = true) {
    this.ship = new Shipper(endpoint, capture);
  }

  key(ctx: Dict, event: Dict = {}): string {
    return String(ctx?.sessionKey ?? event?.sessionKey ?? ctx?.sessionId ?? event?.sessionId ?? ctx?.agentId ?? "default");
  }

  runFor(ctx: Dict, event: Dict = {}): string {
    const k = this.key(ctx, event);
    let run = this.sessionRun.get(k);
    if (!run) {
      run = `oc-${uid()}`;
      this.sessionRun.set(k, run);
      this.ship.emit("run.start", run, { kind: "agent", name: "" }, { agent: this.agent(ctx), session_id: k, implicit: true });
    }
    return run;
  }

  agent(ctx: Dict): string {
    return ctx?.agentId ? `openclaw/${ctx.agentId}` : "openclaw";
  }

  // -- runs ---------------------------------------------------------------------------

  before_agent_start(event: Dict, ctx: Dict): void {
    const k = this.key(ctx, event);
    const run = `oc-${event?.runId ?? uid()}`;
    this.sessionRun.set(k, run);
    const prompt = typeof event?.prompt === "string" ? event.prompt : "";
    const parent = this.childOf.get(k);
    const title = prompt.trim().split("\n")[0].slice(0, 120);
    this.ship.emit("run.start", run, { kind: "agent", name: title }, {
      agent: this.agent(ctx), session_id: k, user: ctx?.senderId ?? ctx?.messageProvider, platform: ctx?.messageProvider,
      user_message: prompt, parent_run_id: parent?.[0], parent_run_span_id: parent?.[1],
    });
  }

  agent_end(event: Dict, ctx: Dict): void {
    const k = this.key(ctx, event);
    const run = this.sessionRun.get(k);
    if (!run) return;
    const ok = event?.success !== false && !event?.error;
    this.ship.emit("run.end", run, { kind: "agent" }, { status: ok ? "ok" : "error", error: event?.error, duration_ms: event?.durationMs });
    this.sessionRun.delete(k);
    this.ship.flush();
  }

  // -- model calls --------------------------------------------------------------------

  llm_input(event: Dict, ctx: Dict): void {
    const run = this.runFor(ctx, event);
    const span = `llm:${uid()}`;
    this.openLlm.set(run, span);
    this.lastLlm.set(run, span);
    const parent = this.childOf.get(this.key(ctx, event))?.[1];
    this.ship.emit("span.start", run, { span_id: span, parent_span_id: parent, kind: "llm", name: String(event?.model ?? "llm") }, {
      model: event?.model, provider: event?.provider,
      request: { prompt: event?.prompt, history_messages: Array.isArray(event?.historyMessages) ? event.historyMessages.length : undefined },
    });
  }

  llm_output(event: Dict, ctx: Dict): void {
    const run = this.runFor(ctx, event);
    const span = this.openLlm.get(run) ?? `llm:${uid()}`;
    this.openLlm.delete(run);
    const usage = event?.usage ?? event?.lastAssistant?.usage;
    this.ship.emit("span.end", run, { span_id: span, kind: "llm", name: String(event?.model ?? "llm") }, {
      status: "ok", model: event?.model, provider: event?.provider, usage,
      cost_usd: typeof usage?.cost === "number" ? usage.cost : usage?.cost?.total,
      response: Array.isArray(event?.assistantTexts) ? event.assistantTexts.join("\n") : undefined,
    });
  }

  // -- tools --------------------------------------------------------------------------

  before_tool_call(event: Dict, ctx: Dict): void {
    const run = this.runFor(ctx, event);
    const name = String(event?.toolName ?? ctx?.toolName ?? "tool");
    const params = event?.params ?? {};
    const span = `tool:${event?.toolCallId ?? uid()}`;
    if (!event?.toolCallId) {
      const q = this.toolFifo.get(`${run}|${name}`) ?? [];
      q.push(span);
      this.toolFifo.set(`${run}|${name}`, q);
    }
    this.ship.emit("span.start", run, { span_id: span, parent_span_id: this.lastLlm.get(run), kind: toolKind(name, params), name }, {
      args: params, mcp_server: params?.server,
    });
  }

  after_tool_call(event: Dict, ctx: Dict): void {
    const run = this.runFor(ctx, event);
    const name = String(event?.toolName ?? ctx?.toolName ?? "tool");
    const span = event?.toolCallId ? `tool:${event.toolCallId}` : (this.toolFifo.get(`${run}|${name}`)?.shift() ?? `tool:${uid()}`);
    const err = event?.error ? String(event.error?.message ?? event.error) : undefined;
    const status = !err ? "ok" : /time(d)?\s*out|timeout|ETIMEDOUT/i.test(err) ? "timeout" : "error";
    this.ship.emit("span.end", run, { span_id: span, kind: toolKind(name, event?.params), name }, {
      status, result: event?.result, error: err, duration_ms: event?.durationMs,
    });
  }

  // -- subagents ----------------------------------------------------------------------

  subagent_spawned(event: Dict, ctx: Dict): void {
    const run = this.runFor(ctx, event);
    const child = String(event?.childSessionKey ?? event?.targetSessionKey ?? event?.runId ?? uid());
    const span = `subagent:${child}`;
    this.childOf.set(child, [run, span]);
    this.ship.emit("span.start", run, { span_id: span, parent_span_id: this.lastLlm.get(run), kind: "subagent", name: String(event?.agentId ?? event?.label ?? "subagent") }, {
      child_session_id: child, child_goal: event?.task ?? event?.label,
    });
  }

  subagent_ended(event: Dict, ctx: Dict): void {
    const child = String(event?.childSessionKey ?? event?.targetSessionKey ?? event?.runId ?? "");
    const link = this.childOf.get(child);
    if (!link) return;
    const ok = !event?.error && event?.outcome !== "error" && event?.outcome !== "failed";
    this.ship.emit("span.end", link[0], { span_id: link[1], kind: "subagent" }, {
      status: ok ? "ok" : "error", error: event?.error, child_summary: event?.summary ?? event?.result,
    });
  }

  // -- messages -----------------------------------------------------------------------

  message_sent(event: Dict, ctx: Dict): void {
    const run = this.sessionRun.get(this.key(ctx, event));
    if (!run) return;
    this.ship.emit("span.event", run, { span_id: `msg:${uid()}`, kind: "message", name: `message to ${ctx?.channelId ?? event?.to ?? "channel"}` }, {
      direction: "outbound", system: ctx?.channelId, content: event?.content, status: event?.success === false ? "error" : "ok",
    });
  }
}

export const HOOKS = [
  "before_agent_start", "agent_end", "llm_input", "llm_output", "before_tool_call", "after_tool_call",
  "subagent_spawned", "subagent_ended", "message_sent",
] as const;

export function register(api: Api, observer?: OpenClawObserver): OpenClawObserver {
  const cfg = api.pluginConfig ?? {};
  const endpoint = cfg.endpoint ?? process.env.AOPS_ENDPOINT ?? DEFAULT_ENDPOINT;
  const capture = cfg.captureContent ?? process.env.AOPS_CAPTURE_CONTENT !== "0";
  const obs = observer ?? new OpenClawObserver(endpoint, capture);
  if (cfg.enabled === false || process.env.AOPS_DISABLED === "1") return obs;
  for (const hook of HOOKS) {
    try {
      api.on(hook, (event, ctx) => {
        try { (obs as any)[hook](event ?? {}, ctx ?? {}); } catch { /* never break the agent */ }
        return undefined; // observe only: never block or modify
      });
    } catch { /* hook not supported by this OpenClaw version */ }
  }
  api.logger?.info?.(`aops: recording to ${endpoint}`);
  return obs;
}

export default { id: "aops", name: "Agent Operations", register };
