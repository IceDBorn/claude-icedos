# AGENTS.md — IceDOS **claude-icedos**

> Utilizes the **IceDOS** framework. The full bible — module structure, config flow,
> the `icedos rebuild --build` test loop, `validate.*` helpers, dep loading — lives in
> **core**: <https://github.com/IceDOS/core/blob/main/AGENTS.md> — this file only
> covers what is specific to **claude-icedos**.

## Non-negotiable rules (full detail in core)
- Build/test only via the `icedos` CLI — **never `sudo nixos-rebuild`**.
- **Never** `git commit/stash/reset/pull` — the user manages git.
- Every option uses a `validate.*`/`mk*Option` helper; **no untyped options**.
- A module's `config.toml` defaults must mirror its `icedos.nix` defaults.
- Format with `icedos nixf .` after editing any `.nix`.
- If a repo or the config root you need isn't checked out locally, **ask the user** for
  its path or permission to `git clone` it — don't guess or clone unprompted.

## Purpose
Claude Code / Claude-specific integration modules for IceDOS — namespaced under
`icedos.applications.claude-code.*`.

## Layout
`modules/{climit,default}/{icedos.nix,config.toml}`; `flake.nix` exposes them via
`icedosLib.scanModules { path = ./modules; filename = "icedos.nix"; }`.

## Module shape here
Standard IceDOS module. Modules here may declare external `inputs`.

## Test a change to this repo
In the config root's `config.toml`, point this repo's `overrideUrl` at your local
checkout (`path:/abs/path/to/claude-icedos`), then `icedos rebuild --build` (no activation).

## Per-user config

The `default` module owns the per-user option
`icedos.applications.claude-code.users` (an `attrsOf submodule` declared with
`mkSubmoduleAttrsOption`, materialised per normal user via `icedosLib.users.genDefaults`
per core/AGENTS.md Rule 1). Per-user values come from
`[icedos.applications.claude-code.users.<name>.*]` TOML stanzas, which merge on top of
the defaults. claude-code itself is enabled for every hm user
(`enable = lib.mkDefault true`).
- `settings` / `skills` — `programs.claude-code.settings` / `.skills`, TOML-friendly
  via the raw NixOS passthrough (`[home-manager.users.<name>.programs.claude-code.*]`).
- `marketplaces` — fetcher results are not TOML-expressible; list them under
  `[[icedos.applications.claude-code.users.<name>.marketplaces]]` with a `source` enum
  (`fetchFromGitHub`/`fetchFromGitLab`/`fetchgit`/`path`) and that source's fields
  (`owner`+`repo`+`rev`+`hash`; fetchFromGitLab also takes `domain` for self-hosted;
  fetchgit takes `url`+`rev`+`hash`; `path` takes an absolute `path`).
  `path` is copied into the store when hm serializes `settings.json` (snapshot at
  eval time), and pure eval forbids host paths there — use a store path (e.g.
  `nix store add-path`) or `--impure`; the rest fetch at build time.
- `enableMcpIntegration` — set `true` by `default`; MCP servers come from the shared
  `programs.mcp.servers` registry (config root's `configs/mcp.toml`), never a per-user
  `mcpServers` option.

`default` leaves `package` at the upstream default (the unfree default `pkgs.claude-code`
evals fine because core sets `home-manager.useGlobalPkgs = true`, so hm follows the
system's pkgs incl. `allowUnfree`). It declares no hooks — peon-ping registers its own
via upstream `programs.peon-ping.claudeCodeIntegration`
(`icedos.applications.peon-ping.users.<u>.claudeCodeIntegration`). Modules needing
per-user config (`climit`, `claude-review-mcp`) declare
`icedos.applications.claude-code.users.<name>` themselves.

- `climit` — `…users.<name>.climit` (`interval`, `alerts`, `widget`, `statusLine`); the module
  adds only the nested submodule + its poll timer, plasmoid and Claude Code status line, no
  `.users` of its own. Every surface reads one SQLite DB, filled by the status line (each
  redraw) and the timer (`/api/oauth/usage`). climit only reads Claude Code's token and never
  refreshes it, so logins stay with the claude CLI.
- `peon-ping` — a **standalone apps module** (`icedos.applications.peon-ping.users`, not part
  of claude-code). It wires its own Claude Code hooks via
  `programs.peon-ping.claudeCodeIntegration`; the audio integration lives in the apps repo.
