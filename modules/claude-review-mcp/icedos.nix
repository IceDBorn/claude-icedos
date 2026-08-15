{ icedosLib, lib, ... }:

let
  # Defaults read once from config.toml, used only to seed the option declarations
  # below. The wrappers are built from each user's *evaluated* config, so a
  # per-user override in `.claude.toml` reaches the running server.
  inherit ((lib.importTOML ./config.toml).icedos.applications.claude-code.users.username.reviewMcp)
    allowedRoots
    allowedImageRoots
    maxImageBytes
    maxCallsPerHour
    effort
    timeout
    maxDiffBytes
    scratchDir
    allowNetwork
    allowFable
    bashAllow
    extraPackages
    ;

in
{
  # Contributes a nested `reviewMcp` submodule to the claude-code per-user option
  # declared in ../default, so config lives at
  # `icedos.applications.claude-code.users.<name>.reviewMcp` and materialises via
  # ../default's `genDefaults`. NOTE: this child declaration MUST omit `default`
  # — only the always-loaded owner (../default) sets `default = {}`.
  #
  # Every value here is genuinely per-user: each configured user gets its own
  # wrapper with its own policy and tuning values baked in, and `icedos claude mcp`
  # picks the right one at runtime from `id -un`. The values below are only the
  # system-wide *defaults*, read from this module's `config.toml`.
  options.icedos.applications.claude-code.users =
    let
      inherit (icedosLib)
        mkBoolOption
        mkEnumOption
        mkNumberOption
        mkStrOption
        mkStrListOption
        mkSubmoduleAttrsOption
        ;
    in
    mkSubmoduleAttrsOption { } {
      reviewMcp = {
        allowedRoots = mkStrListOption { default = allowedRoots; };

        # Roots claude_analyze_image may read images from. Empty = fall back to
        # allowedRoots at runtime — a deliberate contrast with the fail-closed repo
        # roots: this extends only Read reach for image analysis, never the work tier.
        allowedImageRoots = mkStrListOption { default = allowedImageRoots; };

        # Byte cap for images handed to claude_analyze_image.
        maxImageBytes = mkNumberOption { default = maxImageBytes; };

        maxCallsPerHour = mkNumberOption { default = maxCallsPerHour; };

        # Depth/spend lever for each review. `--max-turns` was removed in
        # claude-code 2.x; effort replaces it.
        effort =
          mkEnumOption
            {
              path = "icedos.applications.claude-code.users.<name>.reviewMcp.effort";
              source = ./config.toml;
              default = effort;
            }
            [
              "low"
              "medium"
              "high"
              "xhigh"
              "max"
            ];

        timeout = mkNumberOption { default = timeout; };
        maxDiffBytes = mkNumberOption { default = maxDiffBytes; };

        # Where the `work` tier (claude_verify / claude_help) may write. The
        # repository itself stays read-only — implementation belongs to the caller.
        scratchDir = mkStrOption { default = scratchDir; };

        # Egress. Off by default: it is the one boundary that still holds once
        # Claude can run the caller's test suite, so a review cannot become an
        # exfiltration.
        allowNetwork = mkBoolOption { default = allowNetwork; };

        # Model policy. Off: only `opus`/`sonnet` are offered and the model-override
        # env vars are stripped from the child, so an alias cannot silently resolve
        # to fable. One switch covers both — turning it on lifts the strip too.
        allowFable = mkBoolOption { default = allowFable; };

        # Command prefixes the `work` tier may run. Empty disables claude_verify
        # and claude_help; the read-only tools are unaffected.
        bashAllow = mkStrListOption { default = bashAllow; };

        # nixpkgs attribute names prepended to the server's PATH, so `bashAllow`
        # entries resolve even when the MCP client was launched with a minimal
        # environment.
        extraPackages = mkStrListOption { default = extraPackages; };
      };
    };

  outputs.nixosModules =
    { ... }:
    [
      (
        {
          config,
          lib,
          pkgs,
          ...
        }:

        let
          claudeUsers = config.icedos.applications.claude-code.users;

          # Stdio MCP server that hands a *read-only* Claude Code session to another
          # model as a reviewer. The containment lives in the flags this server passes
          # to `claude -p` (Read/Grep/Glob only, no MCP servers of its own) — see
          # src/src/index.ts.
          reviewMcpPkg = pkgs.buildNpmPackage {
            pname = "claude-review-mcp";
            version = "1.0.0";
            src = ./src;

            # Hashless: deps come from the checked-in package-lock.json, so there is no
            # npmDepsHash to churn on every dependency bump.
            npmDeps = pkgs.importNpmLock { npmRoot = ./src; };
            inherit (pkgs.importNpmLock) npmConfigHook;

            meta = {
              description = "Claude Code as a read-only reviewer, over MCP";
              mainProgram = "claude-review-mcp";
            };
          };

          toolchainPkgs =
            extra:
            map (
              name:
              pkgs.${name}
                or (throw "claude-review-mcp: extraPackages entry '${name}' is not a nixpkgs attribute")
            ) ([ "git" ] ++ extra);

          esc = lib.escapeShellArg;

          # One wrapper per configured user, built from that user's *evaluated*
          # config. Every policy AND tuning value is baked in, so a misconfigured
          # MCP client cannot widen the allowed roots, re-enable network egress,
          # repoint CLAUDE_BIN, or raise the rate limit.
          #
          # CLAUDE_REVIEW_ALLOWED_ROOTS is always exported (even when empty) so the
          # server never falls back to its hardcoded default — empty = fail-closed.
          #
          # `timeout` is seconds in config.toml (matching the other human-facing
          # units); the server's env var is milliseconds. Convert here, not there.
          mkReviewMcpBin =
            cfg:
            pkgs.writeShellScriptBin "claude-review-mcp" ''
              export CLAUDE_REVIEW_ALLOWED_ROOTS=${esc (lib.concatStringsSep ":" cfg.allowedRoots)}
              # Always exported (even when empty) so a client cannot override it. Empty
              # = fall back to ALLOWED_ROOTS at runtime — deliberate, unlike the
              # fail-closed repo roots above: this only widens Read reach for image
              # analysis, never the `work` tier.
              export CLAUDE_REVIEW_ALLOWED_IMAGE_ROOTS=${esc (lib.concatStringsSep ":" cfg.allowedImageRoots)}
              export CLAUDE_REVIEW_MAX_IMAGE_BYTES=${esc (toString cfg.maxImageBytes)}
              export CLAUDE_REVIEW_ALLOW_NETWORK=${esc (if cfg.allowNetwork then "1" else "0")}
              export CLAUDE_REVIEW_ALLOW_FABLE=${esc (if cfg.allowFable then "1" else "0")}
              export CLAUDE_REVIEW_KEEP_API_KEY=0
              export CLAUDE_REVIEW_BASH_ALLOW=${esc (builtins.toJSON cfg.bashAllow)}
              export CLAUDE_REVIEW_SCRATCH_DIR=${esc cfg.scratchDir}
              export CLAUDE_REVIEW_EFFORT=${esc cfg.effort}
              export CLAUDE_REVIEW_TIMEOUT_MS=${esc (toString (cfg.timeout * 1000))}
              export CLAUDE_REVIEW_MAX_CALLS_PER_HOUR=${esc (toString cfg.maxCallsPerHour)}
              export CLAUDE_REVIEW_MAX_DIFF_BYTES=${esc (toString cfg.maxDiffBytes)}
              # claude-code is always installed by the `default` module; pin the
              # store path so PATH resolution can't drift.
              export CLAUDE_BIN=${esc "${lib.getExe pkgs.claude-code}"}
              export PATH="${lib.makeBinPath (toolchainPkgs cfg.extraPackages)}:$PATH"
              exec ${reviewMcpPkg}/bin/claude-review-mcp "$@"
            '';

          userBins = lib.mapAttrs (_: user: mkReviewMcpBin user.reviewMcp) claudeUsers;

          # The toolset registers one system-wide leaf (see core/modules/toolset.nix
          # — `mergeCommands` aborts on duplicate leaf definitions), so the per-user
          # wrapper is selected at *runtime* instead. `id -un`, not `$USER`: the
          # latter goes stale under su.
          dispatchScript = ''
            case "$(id -un)" in
            ${
              lib.concatStrings (
                lib.mapAttrsToList (name: bin: ''
                  ${esc name})
                      exec ${bin}/bin/claude-review-mcp "$@"
                      ;;
                '') userBins
              )
            }  *)
                die "no reviewMcp config for user $(id -un)"
                ;;
            esac
          '';
        in
        {
          icedos.system.toolset.commands = lib.mkIf (claudeUsers != { }) [
            {
              command = "claude";
              help = "claude code tooling";
              commands = [
                {
                  command = "mcp";
                  script = dispatchScript;
                  help = "MCP server — read-only Claude Code reviewer over stdio";
                }
              ];
            }
          ];
        }
      )
    ];

  meta.name = "claude-review-mcp";
}
