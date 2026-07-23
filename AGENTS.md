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
The claude-code per-user submodule is `icedos.applications.claude-code.users.<name>`
(declared in `default`, materialised there with `genDefaults` — see core's *Per-user
(`users`) options*). Sub-features **nest** under it rather than owning a `.users` tree:
- `default` — `enabledPlugins`, `extraSettings`, `skills`, `statusLine`, `mcpServers`,
  `marketplaces`.
- `climit` — `…users.<name>.climit` (`interval`, `alerts`, `widget`); the module adds
  only the nested submodule + its daemon/plasmoid, no `.users` of its own.
- `peonPing` — `…users.<name>.peonPing`, contributed by the **apps** repo's `peon-ping`
  module (not this repo). `default` detects it via `userCfg ? peonPing` to wire the
  Claude Code hooks; the audio integration itself lives in apps.
