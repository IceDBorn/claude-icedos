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
`modules/{default,peon-ping}/{icedos.nix,config.toml}`; `flake.nix` exposes them via
`icedosLib.scanModules { path = ./modules; filename = "icedos.nix"; }`.

## Module shape here
Standard IceDOS module. Modules here may declare external `inputs` (e.g. `peon-ping`
pins `github:PeonPing/peon-ping` with `inputs.nixpkgs.follows = "nixpkgs"`).

## Test a change to this repo
In the config root's `config.toml`, point this repo's `overrideUrl` at your local
checkout (`path:/abs/path/to/claude-icedos`), then `icedos rebuild --build` (no activation).

## Notable modules / gotchas
- `peon-ping` — Warcraft-peon-style audio notifications for agent events (per-user
  submodule config).
