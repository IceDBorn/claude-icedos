{ icedosLib, lib, ... }:

let
  # Defaults from config.toml seed the option declarations; wrappers use each user's *evaluated* config.
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
  # Nested `reviewMcp` submodule under the claude-code per-user option in ../default.
  # MUST omit `default` — only ../default sets it. Values here are system-wide defaults.
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

        # Image analysis roots. Empty = fall back to allowedRoots (extends Read reach, not the work tier).
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

        # Network egress. Off by default — the one boundary that holds once Claude can run tests.
        allowNetwork = mkBoolOption { default = allowNetwork; };

        # Allow fable-tier models. Off: only opus/sonnet are offered and model-override env vars are stripped.
        allowFable = mkBoolOption { default = allowFable; };

        # Command prefixes the `work` tier may run. Empty disables claude_verify
        # and claude_help; the read-only tools are unaffected.
        bashAllow = mkStrListOption { default = bashAllow; };

        # Extra nixpkgs attrs on PATH so bashAllow entries resolve in minimal environments.
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

          # Stdio MCP server: read-only Claude Code session as a reviewer. Containment via `claude -p` flags.
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

          # Per-user wrapper with all policy/tuning baked in — a misconfigured client cannot widen roots or re-enable egress.
          mkReviewMcpBin =
            cfg:
            pkgs.writeShellScriptBin "claude-review-mcp" ''
              export CLAUDE_REVIEW_ALLOWED_ROOTS=${esc (lib.concatStringsSep ":" cfg.allowedRoots)}
              # Always exported (even when empty) so a client cannot override it.
              # Empty = fall back to ALLOWED_ROOTS at runtime — widens Read reach for images only.
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

          # One system-wide leaf (core/modules/toolset.nix); per-user dispatch via runtime `id -un`.
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
