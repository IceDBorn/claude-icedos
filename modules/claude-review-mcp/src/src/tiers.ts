/**
 * Permission tiers.
 *
 * `read` — Claude can look, and nothing else. Used for review and Q&A, where side
 * effects would be a bug.
 *
 * `work` — Claude can additionally run allowlisted commands and write under a scratch
 * directory, so it can actually verify a claim ("the tests pass") instead of taking the
 * caller's word for it.
 *
 * Be honest about what `work` is: once a test runner is allowlisted, the command
 * allowlist stops being a security boundary, because the tests it runs are code the
 * local model wrote. It remains useful for two other things — it stops *accidents*
 * (a stray `rm -rf`, a `sudo`), and it forces an attacker through an extra step. The
 * boundaries that still hold under `work` are the ones enforced structurally: no
 * network egress, no MCP servers, and no repo outside the allowed roots.
 */

export type Tier = "read" | "work";

const READ_TOOLS = ["Read", "Grep", "Glob"];

/**
 * Denied in every tier. Deny rules beat allow rules, so these hold even if a command
 * allowlist entry would otherwise match.
 */
const ALWAYS_DENY = [
  // Tools that would let Claude edit the repo. Implementation belongs to the caller;
  // Claude judges and verifies.
  "Edit",
  "MultiEdit",
  "NotebookEdit",
  // Subagents would inherit a fresh permission context — keep the blast radius flat.
  "Task",
];

/** Egress. The one boundary that still means something once Bash is available. */
const NETWORK_DENY = [
  "WebFetch",
  "WebSearch",
  "Bash(curl *)",
  "Bash(wget *)",
  "Bash(nc *)",
  "Bash(ncat *)",
  "Bash(ssh *)",
  "Bash(scp *)",
  "Bash(rsync *)",
  "Bash(git push *)",
  "Bash(npm publish *)",
];

/** Accident guards. Not a boundary — a seatbelt. */
const DESTRUCTIVE_DENY = [
  "Bash(rm *)",
  "Bash(rmdir *)",
  "Bash(sudo *)",
  "Bash(doas *)",
  "Bash(su *)",
  "Bash(dd *)",
  "Bash(mkfs *)",
  "Bash(chmod *)",
  "Bash(chown *)",
  "Bash(shutdown *)",
  "Bash(reboot *)",
  "Bash(systemctl start *)",
  "Bash(systemctl stop *)",
  "Bash(systemctl restart *)",
  "Bash(git reset *)",
  "Bash(git checkout *)",
  "Bash(git commit *)",
  "Bash(git clean *)",
  "Bash(nix-collect-garbage *)",
];

export interface TierRules {
  allowedTools: string;
  disallowedTools: string;
}

export interface TierInput {
  tier: Tier;
  /**
   * Absolute path of the scratch root. The `work` tier may write under it, and the
   * `read`-tier image tool stages per-call validated copies under its images/<pid>/
   * subdirectory and runs each session from the empty session/<pid>/ subdir (with a
   * repo, when given, granted as an additional directory). Never granted to a session
   * as a whole — only per-call staging dirs and that empty session cwd are.
   */
  scratchDir: string;
  /** Command prefixes for `work`, without the `Bash(...)` wrapper, e.g. `npm test *`. */
  bashAllow: string[];
  denyNetwork: boolean;
}

export function rulesFor(input: TierInput): TierRules {
  const allow = [...READ_TOOLS];

  if (input.tier === "work") {
    allow.push(`Write(${input.scratchDir}/**)`);
    for (const cmd of input.bashAllow) allow.push(`Bash(${cmd})`);
  }

  const deny = [
    ...ALWAYS_DENY,
    ...DESTRUCTIVE_DENY,
    ...(input.denyNetwork ? NETWORK_DENY : []),
  ];

  // The read tier grants no Bash rule at all, so Bash is denied by omission — headless
  // mode cannot prompt, so an unlisted tool is refused rather than escalated. Naming it
  // in the deny list as well makes that explicit rather than incidental.
  if (input.tier === "read") deny.push("Bash", "Write");

  return { allowedTools: allow.join(","), disallowedTools: deny.join(",") };
}
