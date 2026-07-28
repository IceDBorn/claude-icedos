#!/usr/bin/env node
/**
 * claude-review-mcp
 *
 * Exposes Claude Code, running headless, to another model as a plan reviewer, code
 * reviewer, verifier, and helper. The local model implements; Claude judges, proves, and
 * unsticks.
 *
 * Two permission tiers (see tiers.ts): `read` for plan review, code review, Q&A and
 * image analysis, `work` for verifying and diagnosing, which adds allowlisted commands
 * and a scratch directory.
 */

import { spawn, type ChildProcess } from "node:child_process";
import { createHash } from "node:crypto";
import { rmSync } from "node:fs";
import {
  mkdir,
  mkdtemp,
  readdir,
  readFile,
  realpath,
  rm,
  stat,
  writeFile,
} from "node:fs/promises";
import { homedir } from "node:os";
import { basename, join, resolve, sep } from "node:path";

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

import {
  ASK_RUBRIC,
  IMAGE_RUBRIC,
  PLAN_RUBRIC,
  REVIEW_RUBRIC,
  buildImagePrompt,
  buildPlanPrompt,
  buildReviewPrompt,
  helpRubric,
  verifyRubric,
} from "./prompt.js";
import { type Tier, rulesFor } from "./tiers.js";

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const CLAUDE_BIN = process.env.CLAUDE_BIN ?? "claude";

/** Expand leading `~` or `~/` to the home directory (path.resolve does not). */
function expandHome(p: string): string {
  return p.startsWith("~" + sep) || p === "~" ? join(homedir(), p.slice(1)) : p;
}

/** Repos outside these roots are refused. `cwd` arrives from the local model. */
const ALLOWED_ROOTS = (process.env.CLAUDE_REVIEW_ALLOWED_ROOTS ?? `${homedir()}/.code`)
  .split(":")
  .filter(Boolean)
  .map((p) => resolve(expandHome(p)));

/**
 * Roots `claude_analyze_image` may read images from. Distinct from ALLOWED_ROOTS so a
 * user can add a screenshots directory without widening the repo roots — and thus the
 * `work` tier's Bash/Write reach. Empty env (the wrapper always exports it) = fall
 * back to ALLOWED_ROOTS. This is deliberate and differs from the fail-closed repo
 * roots above; it only extends Read reach for image analysis.
 */
const IMAGE_ALLOWED_ROOTS = (() => {
  const raw = process.env.CLAUDE_REVIEW_ALLOWED_IMAGE_ROOTS;
  const spec = raw && raw.trim() ? raw : (process.env.CLAUDE_REVIEW_ALLOWED_ROOTS ?? "");
  return spec
    .split(":")
    .filter(Boolean)
    .map((p) => resolve(expandHome(p)));
})();

const TIMEOUT_MS = numberEnv("CLAUDE_REVIEW_TIMEOUT_MS", 300_000);
const EFFORT = process.env.CLAUDE_REVIEW_EFFORT ?? "high";
const MAX_CALLS_PER_HOUR = numberEnv("CLAUDE_REVIEW_MAX_CALLS_PER_HOUR", 20);
const MAX_DIFF_BYTES = numberEnv("CLAUDE_REVIEW_MAX_DIFF_BYTES", 200_000);
const MAX_IMAGE_BYTES = numberEnv("CLAUDE_REVIEW_MAX_IMAGE_BYTES", 3_900_000);
const DENY_NETWORK = process.env.CLAUDE_REVIEW_ALLOW_NETWORK !== "1";

/**
 * Model policy. Off (the default) the tools offer only `opus` and `sonnet`, and the
 * model-override env vars are stripped from the child so an alias cannot silently
 * resolve elsewhere. On, `fable` becomes selectable and the strip is lifted.
 */
const ALLOW_FABLE = process.env.CLAUDE_REVIEW_ALLOW_FABLE === "1";

type ReviewModel = "opus" | "sonnet" | "fable";

const MODEL_CHOICES: [ReviewModel, ...ReviewModel[]] = ALLOW_FABLE
  ? ["opus", "sonnet", "fable"]
  : ["opus", "sonnet"];

const SCRATCH_DIR = resolve(
  expandHome(
    process.env.CLAUDE_REVIEW_SCRATCH_DIR ?? `~/.cache/claude-review-mcp/scratch`,
  ),
);

/**
 * Per-process staging root for `claude_analyze_image`. One directory per server
 * process (`<pid>`), with a fresh per-call directory under it per call, so no live
 * process ever shares a staging root with another.
 */
const IMAGE_STAGING_ROOT = join(SCRATCH_DIR, "images");
const PID_STAGING_ROOT = join(IMAGE_STAGING_ROOT, String(process.pid));

/**
 * Stable per-process session cwd for `claude_analyze_image` calls without a repo:
 * Claude Code keys sessions by project directory, so a fresh directory per call would
 * make the documented `session_id` follow-up unresumable. This dir holds no image
 * data — the staged copy is always granted via `--add-dir` — so giving it as the
 * session cwd stays inside the one-file containment.
 */
const SESSION_ROOT = join(SCRATCH_DIR, "session");
const PID_SESSION_ROOT = join(SESSION_ROOT, String(process.pid));

/** Command prefixes the `work` tier may run, as a JSON array of Bash rule bodies. */
const BASH_ALLOW: string[] = (() => {
  const raw = process.env.CLAUDE_REVIEW_BASH_ALLOW;
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === "string") : [];
  } catch {
    return [];
  }
})();

function numberEnv(name: string, fallback: number): number {
  const raw = process.env[name];
  if (!raw) return fallback;
  const parsed = Number(raw);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

// ---------------------------------------------------------------------------
// Guards
// ---------------------------------------------------------------------------

/**
 * Resolve `p` through symlinks and confirm it sits inside one of `roots`. `p` is
 * attacker-controlled in the threat model: it comes from the local model, which may be
 * acting on text it read from the web or from a repository.
 */
async function assertAllowedPath(p: string, label: string, roots: string[]): Promise<string> {
  let real: string;
  try {
    real = await realpath(resolve(p));
  } catch {
    throw new Error(`${label} does not exist or is not readable: ${p}`);
  }

  const permitted = roots.some((root) => real === root || real.startsWith(root + sep));
  if (!permitted) {
    throw new Error(
      `${label} ${real} is outside the allowed roots (${roots.join(", ")}). ` +
        `Widen the relevant allowed-roots option in the module config if this is intentional.`,
    );
  }
  return real;
}

const assertAllowedCwd = (cwd: string) => assertAllowedPath(cwd, "cwd", ALLOWED_ROOTS);
const assertAllowedImage = (image: string) =>
  assertAllowedPath(image, "image", IMAGE_ALLOWED_ROOTS);

const callTimestamps: number[] = [];

/**
 * Sliding-window cap, per server process — and stdio MCP is one process per client, so
 * per client. A local model in a retry loop can otherwise burn a weekly quota.
 */
function assertUnderRateLimit(): void {
  const now = Date.now();
  const hourAgo = now - 3_600_000;
  while (callTimestamps.length > 0 && callTimestamps[0]! < hourAgo) callTimestamps.shift();

  if (callTimestamps.length >= MAX_CALLS_PER_HOUR) {
    const retryAfterMs = callTimestamps[0]! + 3_600_000 - now;
    throw new Error(
      `Rate limit reached (${MAX_CALLS_PER_HOUR}/hour, shared across all tools). Retry ` +
        `in ${Math.ceil(retryAfterMs / 60_000)} minute(s). Each call spends from the ` +
        `Claude subscription's rolling window.`,
    );
  }
  callTimestamps.push(now);
}

function assertWorkTierConfigured(): void {
  if (BASH_ALLOW.length === 0) {
    throw new Error(
      "This tool needs the `work` tier, but no commands are allowlisted. Set " +
        "`bashAllow` in the module config (icedos.applications.claude-code.users." +
        "<name>.reviewMcp.bashAllow) — with an empty list Claude could not run anything, " +
        "so use claude_review_plan, claude_review or claude_ask instead.",
    );
  }
}

// ---------------------------------------------------------------------------
// Subprocess helpers
// ---------------------------------------------------------------------------

/**
 * Every child `run()` spawns, tracked so the shutdown handlers can kill an in-flight
 * `claude` (a billable session must not outlive the server). A Set, not a single
 * slot: `git()` shares this helper, `collectDiff` spawns two git children per review,
 * and tool calls can overlap.
 */
const activeChildren = new Set<ChildProcess>();

interface RunResult {
  stdout: string;
  stderr: string;
  code: number | null;
}

function run(
  cmd: string,
  args: string[],
  opts: { cwd: string; stdin?: string; timeoutMs: number; env?: NodeJS.ProcessEnv },
): Promise<RunResult> {
  return new Promise((resolvePromise, rejectPromise) => {
    const child = spawn(cmd, args, {
      cwd: opts.cwd,
      env: opts.env ?? process.env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    activeChildren.add(child);

    let stdout = "";
    let stderr = "";
    let settled = false;

    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.kill("SIGTERM");
      setTimeout(() => child.kill("SIGKILL"), 5_000).unref();
      rejectPromise(new Error(`${cmd} timed out after ${Math.round(opts.timeoutMs / 1000)}s`));
    }, opts.timeoutMs);

    child.stdout.on("data", (d: Buffer) => (stdout += d.toString()));
    child.stderr.on("data", (d: Buffer) => (stderr += d.toString()));

    child.on("error", (err) => {
      if (settled) return;
      settled = true;
      activeChildren.delete(child);
      clearTimeout(timer);
      rejectPromise(
        new Error(
          `failed to spawn ${cmd}: ${err.message}. ` +
            `Set CLAUDE_BIN to an absolute path if it is not on PATH.`,
        ),
      );
    });

    child.on("close", (code) => {
      // Delete before the settle check: the timeout branch has already set `settled`
      // by the time the child dies, so this is the only path that removes a
      // timed-out child from the Set.
      activeChildren.delete(child);
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolvePromise({ stdout, stderr, code });
    });

    if (opts.stdin !== undefined) child.stdin.write(opts.stdin);
    child.stdin.end();
  });
}

async function git(args: string[], cwd: string): Promise<string> {
  const { stdout, stderr, code } = await run("git", args, { cwd, timeoutMs: 30_000 });
  if (code !== 0) {
    throw new Error(`git ${args.join(" ")} failed (exit ${code}): ${stderr.trim()}`);
  }
  return stdout;
}

// ---------------------------------------------------------------------------
// Image staging
// ---------------------------------------------------------------------------

/**
 * The four media types Anthropic vision accepts. Anything else — BMP, TIFF, SVG,
 * text, markdown — is refused, so a non-image file can never be rendered by Read as
 * text and shipped to Anthropic's servers.
 */
function imageKind(bytes: Buffer): string | null {
  if (
    bytes.length >= 8 &&
    bytes.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))
  ) {
    return "png";
  }
  if (bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) {
    return "jpg";
  }
  if (bytes.length >= 6) {
    const gifHead = bytes.subarray(0, 6).toString("latin1");
    if (gifHead === "GIF87a" || gifHead === "GIF89a") {
      return "gif";
    }
  }
  if (
    bytes.length >= 12 &&
    bytes.subarray(0, 4).toString("latin1") === "RIFF" &&
    bytes.subarray(8, 12).toString("latin1") === "WEBP"
  ) {
    return "webp";
  }
  return null;
}

interface StagedImage {
  /** Absolute path of the staged copy Claude is told to Read. */
  stagedPath: string;
  /** Per-call directory, the only thing granted to the session. */
  dir: string;
}

/**
 * Validate the source image and copy it into a fresh per-call directory under the
 * staging root. The source is read ONCE into a buffer; size, magic bytes and the copy
 * all come from that buffer, so a caller swapping the file mid-call cannot smuggle a
 * non-image into the session. The source directory is never granted to the session —
 * only this one-file directory is, so only the image's actual pixels leave the machine.
 */
async function stageImage(src: string): Promise<StagedImage> {
  const info = await stat(src);
  if (!info.isFile()) {
    throw new Error(
      `${src} is not a regular file — refused. Only image files (PNG/JPEG/GIF/WebP) may be analyzed.`,
    );
  }
  if (info.size > MAX_IMAGE_BYTES) {
    throw new Error(
      `${src} is ${info.size} bytes, over the ${MAX_IMAGE_BYTES}-byte image limit.`,
    );
  }

  const bytes = await readFile(src);
  if (bytes.length > MAX_IMAGE_BYTES) {
    throw new Error(
      `${src} is ${bytes.length} bytes, over the ${MAX_IMAGE_BYTES}-byte image limit.`,
    );
  }
  const kind = imageKind(bytes);
  if (!kind) {
    throw new Error(
      `${src} is not a supported image file (PNG/JPEG/GIF/WebP). Non-image files are ` +
        `refused so only actual pixels leave the machine.`,
    );
  }

  await mkdir(PID_STAGING_ROOT, { recursive: true });
  const dir = await mkdtemp(join(PID_STAGING_ROOT, "img-"));

  // Extension from the magic bytes, not the source name: Read selects image rendering
  // from the extension, and a hash name alone would read back as binary text.
  const hash = createHash("sha256").update(bytes).digest("hex").slice(0, 16);
  const stagedPath = join(dir, `${hash}.${kind}`);
  await writeFile(stagedPath, bytes);

  return { stagedPath, dir };
}

/**
 * Reclaim pid-scoped roots left by dead server processes — both the staged images
 * under `images/` and the session cwds under `session/`. An entry is removed only
 * when its pid is NOT live (ESRCH; EPERM means the pid exists — treated as live) AND
 * it has been idle longer than TIMEOUT_MS, so a live but idle server's root is never
 * deleted by a sibling client. Cleanup must never abort startup, so every failure
 * here degrades to skipping.
 */
async function sweepPidRoots(parentDir: string): Promise<void> {
  let entries: string[] = [];
  try {
    entries = await readdir(parentDir);
  } catch {
    return;
  }
  for (const entry of entries) {
    if (!/^\d+$/.test(entry)) continue;
    const pid = Number(entry);
    let live = true;
    try {
      process.kill(pid, 0);
    } catch (err) {
      live = (err as NodeJS.ErrnoException).code === "EPERM";
    }
    const root = join(parentDir, entry);
    let mtime = 0;
    try {
      mtime = (await stat(root)).mtimeMs;
    } catch {
      mtime = 0;
    }
    if (!live && Date.now() - mtime > TIMEOUT_MS) {
      await rm(root, { recursive: true, force: true }).catch(() => {});
    }
  }
}

async function sweepStagingRoots(): Promise<void> {
  try {
    await sweepPidRoots(IMAGE_STAGING_ROOT);
    await sweepPidRoots(SESSION_ROOT);
  } catch {
    // never abort startup
  }
}

// ---------------------------------------------------------------------------
// Claude invocation
// ---------------------------------------------------------------------------

interface ClaudeJsonResult {
  result?: string;
  is_error?: boolean;
  session_id?: string;
  num_turns?: number;
  subtype?: string;
  permission_denials?: unknown[];
}

/**
 * `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` outrank the OAuth login in Claude
 * Code's credential chain, and in non-interactive mode an API key is used without any
 * prompt — so leaving either set would silently move this workload onto metered API
 * billing, the exact thing this server exists to avoid. Strip both.
 */
function claudeEnv(): NodeJS.ProcessEnv {
  const env = { ...process.env };
  if (process.env.CLAUDE_REVIEW_KEEP_API_KEY !== "1") {
    delete env.ANTHROPIC_API_KEY;
    delete env.ANTHROPIC_AUTH_TOKEN;
  }

  // Same reasoning as the API-key strip above, applied to model selection. `--model` is
  // always passed explicitly, so nothing legitimate needs these; left set, an alias remap
  // (ANTHROPIC_DEFAULT_SONNET_MODEL=claude-fable-5) would silently redirect the session to
  // a model the tool schema never offered. Matched by pattern rather than by a fixed list
  // so a future claude-code release cannot introduce a name that slips through.
  if (!ALLOW_FABLE) {
    for (const key of Object.keys(env)) {
      if (/^ANTHROPIC_.*MODEL$/.test(key) || key === "CLAUDE_CODE_SUBAGENT_MODEL") {
        delete env[key];
      }
    }
  }

  return env;
}

interface ClaudeCallOptions {
  prompt: string;
  cwd: string;
  systemPrompt: string;
  model: string;
  tier: Tier;
  sessionId?: string;
  /** JSON Schema handed to `--json-schema`, so the CLI enforces the reply shape. */
  jsonSchema?: unknown;
  /** Additional directories granted via `--add-dir`, beyond `cwd`. */
  extraDirs?: string[];
}

async function callClaude(opts: ClaudeCallOptions): Promise<ClaudeJsonResult> {
  // The zod enum already refuses anything else at the MCP boundary; this is the same
  // check one layer down, so a future caller that builds options directly cannot reach
  // `--model` with a model the policy never permitted.
  if (!MODEL_CHOICES.includes(opts.model as ReviewModel)) {
    throw new Error(
      `model ${opts.model} is not permitted (allowed: ${MODEL_CHOICES.join(", ")}). ` +
        `Set reviewMcp.allowFable = true to enable fable.`,
    );
  }

  const { allowedTools, disallowedTools } = rulesFor({
    tier: opts.tier,
    scratchDir: SCRATCH_DIR,
    bashAllow: BASH_ALLOW,
    denyNetwork: DENY_NETWORK,
  });

  const args = [
    "-p",
    "--output-format",
    "json",
    "--model",
    opts.model,
    "--allowedTools",
    allowedTools,
    "--disallowedTools",
    disallowedTools,
    "--add-dir",
    opts.cwd,
    // Depth/spend lever. `--max-turns` was removed in claude-code 2.x; effort replaces it.
    "--effort",
    EFFORT,
    // No --mcp-config alongside this: the session gets zero MCP servers. Without it it
    // would inherit whatever this machine configures for Claude Code — on a dev box that
    // can mean live client databases and a browser, inside a session another model drives.
    "--strict-mcp-config",
    "--append-system-prompt",
    opts.systemPrompt,
  ];

  if (opts.tier === "work") args.push("--add-dir", SCRATCH_DIR);
  for (const dir of opts.extraDirs ?? []) args.push("--add-dir", dir);
  if (opts.sessionId) args.push("--resume", opts.sessionId);
  if (opts.jsonSchema) args.push("--json-schema", JSON.stringify(opts.jsonSchema));

  const { stdout, stderr, code } = await run(CLAUDE_BIN, args, {
    cwd: opts.cwd,
    stdin: opts.prompt,
    timeoutMs: TIMEOUT_MS,
    env: claudeEnv(),
  });

  if (code !== 0 && stdout.trim().length === 0) {
    throw new Error(`claude exited ${code}: ${stderr.trim() || "(no stderr)"}`);
  }

  let parsed: ClaudeJsonResult;
  try {
    parsed = JSON.parse(stdout) as ClaudeJsonResult;
  } catch {
    throw new Error(
      `could not parse claude --output-format json output. ` +
        `stdout: ${stdout.slice(0, 500)} stderr: ${stderr.slice(0, 500)}`,
    );
  }

  if (parsed.is_error) {
    throw new Error(
      `claude reported an error (${parsed.subtype ?? "unknown"}): ` +
        `${parsed.result ?? "(no message)"}`,
    );
  }
  return parsed;
}

// ---------------------------------------------------------------------------
// Reply schemas and parsing
// ---------------------------------------------------------------------------

const FINDING = {
  type: "object",
  properties: {
    file: { type: "string" },
    line: { anyOf: [{ type: "integer" }, { type: "null" }] },
    severity: { type: "string", enum: ["high", "medium", "low"] },
    issue: { type: "string" },
    fix: { type: "string" },
  },
  required: ["file", "line", "severity", "issue", "fix"],
  additionalProperties: false,
} as const;

const REVIEW_SCHEMA = {
  type: "object",
  properties: {
    verdict: { type: "string", enum: ["done", "changes_requested"] },
    findings: { type: "array", items: FINDING },
    tips: { type: "array", items: { type: "string" } },
    summary: { type: "string" },
  },
  required: ["verdict", "findings", "tips", "summary"],
  additionalProperties: false,
} as const;

const VERIFY_SCHEMA = {
  type: "object",
  properties: {
    verdict: { type: "string", enum: ["done", "changes_requested"] },
    checks: {
      type: "array",
      items: {
        type: "object",
        properties: {
          command: { type: "string" },
          result: { type: "string", enum: ["pass", "fail", "inconclusive"] },
          detail: { type: "string" },
        },
        required: ["command", "result", "detail"],
        additionalProperties: false,
      },
    },
    findings: { type: "array", items: FINDING },
    summary: { type: "string" },
  },
  required: ["verdict", "checks", "findings", "summary"],
  additionalProperties: false,
} as const;

const HELP_SCHEMA = {
  type: "object",
  properties: {
    root_cause: { type: "string" },
    confidence: { type: "string", enum: ["high", "medium", "low"] },
    steps: {
      type: "array",
      items: {
        type: "object",
        properties: { action: { type: "string" }, why: { type: "string" } },
        required: ["action", "why"],
        additionalProperties: false,
      },
    },
    evidence: { type: "array", items: { type: "string" } },
    summary: { type: "string" },
  },
  required: ["root_cause", "confidence", "steps", "evidence", "summary"],
  additionalProperties: false,
} as const;

/**
 * Like FINDING, but a plan defect is not always tied to a file — and when it is tied to
 * one, the line does not exist yet. `step` locates it inside the plan instead.
 */
const PLAN_FINDING = {
  type: "object",
  properties: {
    step: { anyOf: [{ type: "string" }, { type: "null" }] },
    file: { anyOf: [{ type: "string" }, { type: "null" }] },
    severity: { type: "string", enum: ["high", "medium", "low"] },
    issue: { type: "string" },
    fix: { type: "string" },
  },
  required: ["step", "file", "severity", "issue", "fix"],
  additionalProperties: false,
} as const;

const PLAN_SCHEMA = {
  type: "object",
  properties: {
    verdict: { type: "string", enum: ["done", "changes_requested"] },
    findings: { type: "array", items: PLAN_FINDING },
    gaps: { type: "array", items: { type: "string" } },
    risks: { type: "array", items: { type: "string" } },
    summary: { type: "string" },
  },
  required: ["verdict", "findings", "gaps", "risks", "summary"],
  additionalProperties: false,
} as const;

/** Like FINDING, but an image defect is located by `region` (where in the image). */
const IMAGE_FINDING = {
  type: "object",
  properties: {
    region: { anyOf: [{ type: "string" }, { type: "null" }] },
    severity: { type: "string", enum: ["high", "medium", "low"] },
    issue: { type: "string" },
    fix: { type: "string" },
  },
  required: ["region", "severity", "issue", "fix"],
  additionalProperties: false,
} as const;

/**
 * `verdict` is pinned to the exact enum `hasVerdict` checks, so a schema-valid reply
 * parses rather than falling through to the `unparsed` branch.
 */
const IMAGE_SCHEMA = {
  type: "object",
  properties: {
    verdict: { type: "string", enum: ["done", "changes_requested"] },
    description: { type: "string" },
    findings: { type: "array", items: IMAGE_FINDING },
    observations: { type: "array", items: { type: "string" } },
    summary: { type: "string" },
  },
  required: ["verdict", "description", "findings", "observations", "summary"],
  additionalProperties: false,
} as const;

/**
 * `--json-schema` normally makes the whole reply a JSON object, so try that first. The
 * fenced-block and first-brace paths are fallbacks for a reply that arrived as prose
 * anyway — the content is still useful to a human when the contract slips, so this
 * never throws.
 */
function parseStructured<T>(text: string, isValid: (o: unknown) => o is T): T | null {
  const candidates: string[] = [];

  const trimmed = text.trim();
  if (trimmed.startsWith("{")) candidates.push(trimmed);
  for (const m of text.matchAll(/```json\s*([\s\S]*?)```/g)) candidates.push(m[1]!.trim());
  const firstBrace = text.indexOf("{");
  if (firstBrace !== -1) candidates.push(text.slice(firstBrace));

  for (const candidate of candidates) {
    try {
      const obj: unknown = JSON.parse(candidate);
      if (isValid(obj)) return obj;
    } catch {
      // try the next candidate
    }
  }
  return null;
}

const hasVerdict = (o: unknown): o is { verdict: string } =>
  typeof o === "object" &&
  o !== null &&
  ((o as { verdict?: unknown }).verdict === "done" ||
    (o as { verdict?: unknown }).verdict === "changes_requested");

const hasRootCause = (o: unknown): o is { root_cause: string } =>
  typeof o === "object" && o !== null && typeof (o as { root_cause?: unknown }).root_cause === "string";

/** Uniform tool reply: structured payload when parseable, raw text plus a flag when not. */
function reply(parsed: unknown, res: ClaudeJsonResult, extra: Record<string, unknown> = {}) {
  const payload =
    parsed !== null
      ? { ...(parsed as object), ...extra, session_id: res.session_id, turns: res.num_turns }
      : {
          unparsed: true,
          note:
            "Claude did not return the expected JSON object. Treat this as advisory and " +
            "do not branch on it; the full reply is in `raw_reply`.",
          raw_reply: res.result ?? "",
          ...extra,
          session_id: res.session_id,
          turns: res.num_turns,
        };

  if (res.permission_denials && res.permission_denials.length > 0) {
    (payload as Record<string, unknown>).permission_denials = res.permission_denials;
  }

  return { content: [{ type: "text" as const, text: JSON.stringify(payload, null, 2) }] };
}

function toolError(name: string, err: unknown) {
  return {
    isError: true,
    content: [{ type: "text" as const, text: `${name} failed: ${(err as Error).message}` }],
  };
}

// ---------------------------------------------------------------------------
// Diff collection
// ---------------------------------------------------------------------------

interface DiffBundle {
  diff: string;
  untracked: string[];
  truncated: boolean;
}

async function collectDiff(repo: string, baseRef: string, explicit?: string): Promise<DiffBundle> {
  let diff = explicit;
  let untracked: string[] = [];

  if (diff === undefined) {
    diff = await git(["--no-pager", "diff", "--no-color", baseRef], repo);
    const lsFiles = await git(["ls-files", "--others", "--exclude-standard"], repo);
    untracked = lsFiles.split("\n").filter(Boolean).slice(0, 100);
  }

  let truncated = false;
  if (Buffer.byteLength(diff, "utf8") > MAX_DIFF_BYTES) {
    diff = diff.slice(0, MAX_DIFF_BYTES);
    truncated = true;
  }

  return { diff, untracked, truncated };
}

// ---------------------------------------------------------------------------
// Server
// ---------------------------------------------------------------------------

const server = new McpServer({ name: "claude-review-mcp", version: "2.0.0" });

// Built from MODEL_CHOICES rather than refined after the fact: the MCP client is handed
// this as JSON Schema, and a `.refine()` would not appear there — the client would still
// believe a disallowed model was valid and only find out when the call failed.
const modelSchema = z
  .enum(MODEL_CHOICES)
  .default("sonnet")
  .describe("Reviewer model. `sonnet` for routine checks, `opus` for the final gate.");

const cwdSchema = z.string().describe("Absolute path to the repository. Must be inside an allowed root.");

const sessionSchema = z
  .string()
  .optional()
  .describe("Resume a previous round (from a prior response) so context is not re-read.");

server.registerTool(
  "claude_review_plan",
  {
    title: "Review an implementation plan with Claude (read-only)",
    description:
      "Send a plan to Claude before you implement it. Claude reads the repository — no " +
      "shell, no writes — and checks the plan against what is actually there: files and " +
      "helpers it assumes exist, work it omits, steps in the wrong order. Returns `done` " +
      "or `changes_requested` plus findings, gaps, and risks. Cheapest place to catch a " +
      "wrong approach. Re-call with the returned `session_id` after revising. Use " +
      "claude_ask when you have a design question and no plan yet, claude_review once the " +
      "change is written.",
    inputSchema: {
      cwd: cwdSchema,
      plan: z.string().describe("The plan, as you would execute it. Name the files it touches."),
      goal: z
        .string()
        .optional()
        .describe("What the plan is meant to achieve, if the plan alone does not say."),
      focus: z.string().optional().describe("Which part you are least sure about."),
      session_id: sessionSchema,
      model: modelSchema,
    },
  },
  async ({ cwd, plan, goal, focus, session_id, model }) => {
    try {
      const repo = await assertAllowedCwd(cwd);
      assertUnderRateLimit();

      // Same cap and the same warning path as a diff: a plan pasted from a generated
      // document can be arbitrarily long.
      let planText = plan;
      let truncated = false;
      if (Buffer.byteLength(planText, "utf8") > MAX_DIFF_BYTES) {
        planText = planText.slice(0, MAX_DIFF_BYTES);
        truncated = true;
      }

      const res = await callClaude({
        prompt: buildPlanPrompt({
          plan: planText,
          goal,
          focus,
          isFollowUp: Boolean(session_id),
          truncated,
        }),
        cwd: repo,
        systemPrompt: PLAN_RUBRIC,
        model,
        tier: "read",
        sessionId: session_id,
        jsonSchema: PLAN_SCHEMA,
      });

      return reply(parseStructured(res.result ?? "", hasVerdict), res, {
        plan_truncated: truncated,
      });
    } catch (err) {
      return toolError("claude_review_plan", err);
    }
  },
);

server.registerTool(
  "claude_review",
  {
    title: "Review a change with Claude (read-only)",
    description:
      "Send a completed change to Claude for review. Claude reads the repository — no " +
      "shell, no writes — and returns `done` or `changes_requested` plus findings and " +
      "tips. Call this when you believe your work is finished. Re-call with the returned " +
      "`session_id` after revising. Use claude_verify instead when you want the change " +
      "actually exercised rather than read.",
    inputSchema: {
      cwd: cwdSchema,
      base_ref: z.string().default("HEAD").describe("Git ref to diff against. Default `HEAD`."),
      diff: z.string().optional().describe("Explicit unified diff, instead of the working tree."),
      focus: z.string().optional().describe("What to scrutinise, e.g. 'the retry logic in fetchAll'."),
      session_id: sessionSchema,
      model: modelSchema,
    },
  },
  async ({ cwd, base_ref, diff, focus, session_id, model }) => {
    try {
      const repo = await assertAllowedCwd(cwd);
      assertUnderRateLimit();
      const bundle = await collectDiff(repo, base_ref, diff);

      const res = await callClaude({
        prompt: buildReviewPrompt({
          diff: bundle.diff,
          untracked: bundle.untracked,
          focus,
          baseRef: base_ref,
          isFollowUp: Boolean(session_id),
          truncated: bundle.truncated,
        }),
        cwd: repo,
        systemPrompt: REVIEW_RUBRIC,
        model,
        tier: "read",
        sessionId: session_id,
        jsonSchema: REVIEW_SCHEMA,
      });

      return reply(parseStructured(res.result ?? "", hasVerdict), res, {
        diff_truncated: bundle.truncated,
      });
    } catch (err) {
      return toolError("claude_review", err);
    }
  },
);

server.registerTool(
  "claude_verify",
  {
    title: "Have Claude prove the change works (runs commands)",
    description:
      "Hand Claude a finished change and have it actually exercise it — run the test " +
      "suite, build, start services, check logs — rather than reading it. Returns a " +
      "verdict plus a `checks` list of what was run and what each result was. Claude can " +
      "run allowlisted commands and write scratch files, but cannot edit your repository. " +
      "Slower and more expensive than claude_review; use it as the final gate.",
    inputSchema: {
      cwd: cwdSchema,
      task: z
        .string()
        .describe("What the change is supposed to do, in your words. This is what gets verified."),
      base_ref: z.string().default("HEAD").describe("Git ref to diff against. Default `HEAD`."),
      session_id: sessionSchema,
      model: modelSchema,
    },
  },
  async ({ cwd, task, base_ref, session_id, model }) => {
    try {
      const repo = await assertAllowedCwd(cwd);
      assertWorkTierConfigured();
      assertUnderRateLimit();
      const bundle = await collectDiff(repo, base_ref, undefined);

      const prompt = [
        `The author says this change does the following:\n\n<task>\n${task}\n</task>`,
        `Diff against \`${base_ref}\`:\n\n<diff>\n${bundle.diff || "(no tracked changes)"}\n</diff>`,
        bundle.untracked.length > 0
          ? `Untracked new files (read them directly):\n${bundle.untracked.map((f) => `- ${f}`).join("\n")}`
          : "",
        bundle.truncated
          ? "WARNING: the diff was truncated at a byte limit. Read the affected files " +
            "directly, and do not return `done` if you cannot establish what the full " +
            "change does."
          : "",
        "Verify it. Run the project's own checks, then probe the behaviour the change claims.",
      ]
        .filter(Boolean)
        .join("\n\n");

      const res = await callClaude({
        prompt,
        cwd: repo,
        systemPrompt: verifyRubric(SCRATCH_DIR),
        model,
        tier: "work",
        sessionId: session_id,
        jsonSchema: VERIFY_SCHEMA,
      });

      return reply(parseStructured(res.result ?? "", hasVerdict), res, {
        diff_truncated: bundle.truncated,
      });
    } catch (err) {
      return toolError("claude_verify", err);
    }
  },
);

server.registerTool(
  "claude_help",
  {
    title: "Ask Claude to unstick you (runs commands)",
    description:
      "Call this when you are stuck: a failure you cannot explain, a test that will not " +
      "pass, a service that will not start, or you do not know how to approach something. " +
      "Claude reproduces the problem itself — reading code, running allowlisted commands, " +
      "checking logs — and returns a root cause, a confidence level, and concrete ordered " +
      "steps. Say what you already tried; it saves Claude repeating it.",
    inputSchema: {
      cwd: cwdSchema,
      problem: z
        .string()
        .describe("What is happening, what you expected, and the exact error if there is one."),
      tried: z
        .string()
        .optional()
        .describe("What you have already attempted and what each attempt did."),
      session_id: sessionSchema,
      model: modelSchema,
    },
  },
  async ({ cwd, problem, tried, session_id, model }) => {
    try {
      const repo = await assertAllowedCwd(cwd);
      assertWorkTierConfigured();
      assertUnderRateLimit();

      const prompt = [
        `<problem>\n${problem}\n</problem>`,
        tried ? `<already_tried>\n${tried}\n</already_tried>` : "",
        "Reproduce it before diagnosing. If you cannot reproduce it, say so and give the " +
          "commands that would distinguish the possibilities.",
      ]
        .filter(Boolean)
        .join("\n\n");

      const res = await callClaude({
        prompt,
        cwd: repo,
        systemPrompt: helpRubric(SCRATCH_DIR),
        model,
        tier: "work",
        sessionId: session_id,
        jsonSchema: HELP_SCHEMA,
      });

      return reply(parseStructured(res.result ?? "", hasRootCause), res);
    } catch (err) {
      return toolError("claude_help", err);
    }
  },
);

server.registerTool(
  "claude_ask",
  {
    title: "Ask Claude a question about this codebase (read-only)",
    description:
      "Free-form question about the repository — what a function does, which of two " +
      "approaches fits the existing code, where something is handled. Claude reads and " +
      "answers in prose; it runs nothing. Cheapest of the six. Use claude_help instead " +
      "when you are stuck on a failure that needs reproducing.",
    inputSchema: {
      cwd: cwdSchema,
      question: z.string().describe("The question. Be specific; name files if you can."),
      session_id: sessionSchema,
      model: modelSchema,
    },
  },
  async ({ cwd, question, session_id, model }) => {
    try {
      const repo = await assertAllowedCwd(cwd);
      assertUnderRateLimit();

      const res = await callClaude({
        prompt: question,
        cwd: repo,
        systemPrompt: ASK_RUBRIC,
        model,
        tier: "read",
        sessionId: session_id,
      });

      return {
        content: [
          {
            type: "text" as const,
            text: JSON.stringify(
              { answer: res.result ?? "", session_id: res.session_id, turns: res.num_turns },
              null,
              2,
            ),
          },
        ],
      };
    } catch (err) {
      return toolError("claude_ask", err);
    }
  },
);

server.registerTool(
  "claude_analyze_image",
  {
    title: "Analyze an image with Claude (read-only)",
    description:
      "Hand an image to Claude for analysis — a screenshot of an error or a UI, a " +
      "mockup, a diagram. Claude stages a validated copy of the image, reads it, and " +
      "returns a description, severity-tagged findings, and a verdict. The file must " +
      "be PNG/JPEG/GIF/WebP and inside an allowed image root. Optionally pass a repo " +
      "as `cwd` for code context, and a `question` to steer the analysis. Re-call " +
      "with the returned `session_id` to follow up.",
    inputSchema: {
      image: z
        .string()
        .describe("Absolute path to the image file (PNG/JPEG/GIF/WebP). Must be inside an allowed root."),
      question: z
        .string()
        .optional()
        .describe("What to look for — a defect to hunt for, or a question about the image."),
      cwd: z
        .string()
        .optional()
        .describe("Optional repo to add as context. Must be inside an allowed root."),
      session_id: sessionSchema,
      model: modelSchema,
    },
  },
  async ({ image, question, cwd, session_id, model }) => {
    let staged: StagedImage | undefined;
    try {
      const realImage = await assertAllowedImage(image);
      let repo: string | undefined;
      if (cwd) repo = await assertAllowedCwd(cwd);
      assertUnderRateLimit();

      staged = await stageImage(realImage);

      // The session cwd is always the stable per-process session dir, never a repo:
      // Claude Code keys sessions by project directory, so a cwd that varies between
      // rounds (or a per-call dir) would make `session_id` follow-ups unresumable.
      // The repo and the staged one-file dir are granted via `--add-dir` below.
      const sessionCwd = PID_SESSION_ROOT;

      const res = await callClaude({
        prompt: buildImagePrompt({
          imagePath: staged.stagedPath,
          repo,
          question,
          isFollowUp: Boolean(session_id),
        }),
        cwd: sessionCwd,
        systemPrompt: IMAGE_RUBRIC,
        model,
        tier: "read",
        sessionId: session_id,
        jsonSchema: IMAGE_SCHEMA,
        extraDirs: repo ? [staged.dir, repo] : [staged.dir],
      });

      return reply(parseStructured(res.result ?? "", hasVerdict), res, {
        image: basename(realImage),
      });
    } catch (err) {
      return toolError("claude_analyze_image", err);
    } finally {
      if (staged) {
        await rm(staged.dir, { recursive: true, force: true }).catch(() => {});
      }
    }
  },
);

// Registered before any startup I/O — mkdir, sweep, connect — so a signal arriving
// at any point (even mid-sweep) terminates cleanly rather than leaving half-created
// staging roots or a half-connected stdio process.
function shutdown(signal: NodeJS.Signals): void {
  // Kill in-flight claude children: a billable session must not outlive its server
  // (and continue reading a staging dir we are about to delete).
  for (const child of activeChildren) {
    try {
      child.kill("SIGKILL");
    } catch {
      // already gone
    }
  }
  try {
    rmSync(PID_STAGING_ROOT, { recursive: true, force: true });
    rmSync(PID_SESSION_ROOT, { recursive: true, force: true });
  } finally {
    // 128 + signal, so an abandoned in-flight call is not reported as clean exit.
    process.exit(signal === "SIGINT" ? 130 : 143);
  }
}
process.on("SIGINT", () => shutdown("SIGINT"));
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("exit", () => {
  try {
    rmSync(PID_STAGING_ROOT, { recursive: true, force: true });
    rmSync(PID_SESSION_ROOT, { recursive: true, force: true });
  } catch {
    // best-effort
  }
});

await mkdir(SCRATCH_DIR, { recursive: true });
await sweepStagingRoots();
await mkdir(PID_STAGING_ROOT, { recursive: true });
await mkdir(PID_SESSION_ROOT, { recursive: true });

const transport = new StdioServerTransport();
await server.connect(transport);
