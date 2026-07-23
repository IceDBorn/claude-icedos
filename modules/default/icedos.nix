{ icedosLib, lib, ... }:

{
  options.icedos.applications.claude-code.users =
    let
      inherit (lib) head readFile;

      inherit (icedosLib)
        mkAttrsOption
        mkNumberOption
        mkStrListOption
        mkStrOption
        mkSubmoduleAttrsOption
        mkSubmoduleListOption
        ;

      inherit ((fromTOML (readFile ./config.toml)).icedos.applications.claude-code.users.username)
        enabledPlugins
        extraSettings
        skills
        status-line
        ;

      mcpTemplate = head (fromTOML (readFile ./mcp-servers.toml))
        .icedos.applications.claude-code.users.username.mcpServers;

      marketplaceTemplate = head (fromTOML (readFile ./marketplaces.toml))
        .icedos.applications.claude-code.users.username.marketplaces;
    in
    mkSubmoduleAttrsOption { default = { }; } {
      enabledPlugins = mkStrListOption { default = enabledPlugins; };

      extraSettings = mkAttrsOption { default = extraSettings; };

      skills = mkAttrsOption { default = skills; };

      status-line = {
        type = mkStrOption { default = status-line.type; };
        command = mkStrOption { default = status-line.command; };
      };

      mcpServers = mkSubmoduleListOption { default = [ ]; } {
        name = mkStrOption { default = mcpTemplate.name; };
        command = mkStrOption { default = mcpTemplate.command; };
        args = mkStrListOption { default = mcpTemplate.args; };
        env = mkAttrsOption { default = mcpTemplate.env; };
        timeout = mkNumberOption { default = mcpTemplate.timeout; };
      };

      marketplaces = mkSubmoduleListOption { default = [ ]; } {
        name = mkStrOption { default = marketplaceTemplate.name; };
        source = mkStrOption { default = marketplaceTemplate.source; };
        repo = mkStrOption { default = marketplaceTemplate.repo; };
        url = mkStrOption { default = marketplaceTemplate.url; };
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
          inherit (lib)
            filter
            listToAttrs
            mapAttrs'
            nameValuePair
            optionalAttrs
            ;

          claudeUsers = config.icedos.applications.claude-code.users;

          # Standalone apps `peon-ping` module (icedos.applications.peon-ping.users);
          # we only consume it to register the Claude Code hooks when a user has it.
          peonPingUsers = config.icedos.applications.peon-ping.users or { };

          # peon.sh + hook-handle-*.sh are staged by the apps peon-ping module
          # (~/.claude/hooks/peon-ping/, ~/.openpeon/scripts/); we just wire them up.
          peonCmd = {
            type = "command";
            command = "$HOME/.claude/hooks/peon-ping/peon.sh";
            timeout = 10;
          };

          peonCmdAsync = peonCmd // {
            async = true;
          };

          syncEntry = {
            matcher = "";
            hooks = [ peonCmd ];
          };

          asyncEntry = {
            matcher = "";
            hooks = [ peonCmdAsync ];
          };

          peonHooks = {
            SessionStart = [ syncEntry ];
            SessionEnd = [ asyncEntry ];
            SubagentStart = [ asyncEntry ];
            SubagentStop = [ asyncEntry ];
            Stop = [ asyncEntry ];
            Notification = [ asyncEntry ];
            PermissionRequest = [ asyncEntry ];
            PreToolUse = [ asyncEntry ];
            PostToolUseFailure = [
              {
                matcher = "Bash";
                hooks = [ peonCmdAsync ];
              }
            ];
            PreCompact = [ asyncEntry ];
            UserPromptSubmit = [
              asyncEntry
              {
                matcher = "";
                hooks = [
                  {
                    type = "command";
                    command = "bash $HOME/.openpeon/scripts/hook-handle-use.sh";
                    timeout = 5;
                  }
                  {
                    type = "command";
                    command = "bash $HOME/.openpeon/scripts/hook-handle-rename.sh";
                    timeout = 5;
                  }
                ];
              }
            ];
          };

          renderMcp = m: {
            name = m.name;
            value = {
              inherit (m) command args;
            }
            // optionalAttrs (m.env != { }) { env = m.env; }
            // optionalAttrs (m.timeout > 0) { timeout = m.timeout; };
          };

          renderMarketplace = m: {
            name = m.name;
            value.source = {
              source = m.source;
            }
            // optionalAttrs (m.repo != "") { repo = m.repo; }
            // optionalAttrs (m.url != "") { url = m.url; };
          };

          renderEnabledPlugins =
            plugins:
            listToAttrs (
              map (p: {
                name = p;
                value = true;
              }) plugins
            );

          renderSettings =
            userCfg:
            {
              enabledPlugins = renderEnabledPlugins userCfg.enabledPlugins;

              extraKnownMarketplaces = listToAttrs (
                map renderMarketplace (filter (m: m.name != "") userCfg.marketplaces)
              );
            }
            // optionalAttrs (userCfg.status-line.command != "") {
              statusLine = {
                inherit (userCfg.status-line) type command;
              };
            }
            // userCfg.extraSettings;

          renderMcpServers =
            userCfg: listToAttrs (map renderMcp (filter (m: m.name != "") userCfg.mcpServers));
        in
        {
          icedos.applications.claude-code.users = icedosLib.users.genDefaults {
            inherit (config.icedos) users;
          };

          home-manager.sharedModules = [
            (
              { config, lib, ... }:

              let
                userCfg = claudeUsers.${config.home.username} or null;

                # Merge peon-ping Claude Code hooks in when the apps peon-ping
                # module is enabled for this user (user extraSettings.hooks win).
                peonEnabled = builtins.hasAttr config.home.username peonPingUsers;
                rendered = renderSettings userCfg;
                finalSettings =
                  rendered
                  // optionalAttrs peonEnabled {
                    hooks = peonHooks // (rendered.hooks or { });
                  };
              in
              lib.mkIf (userCfg != null) {
                home.file = {
                  ".claude/settings.json".source = pkgs.writeText "claude-settings.json" (
                    builtins.toJSON finalSettings
                  );
                }
                // mapAttrs' (
                  name: content: nameValuePair ".claude/skills/${name}/SKILL.md" { text = content; }
                ) userCfg.skills;

                home.activation.icedosClaudeJsonMcpServers =
                  let
                    mcpJson = pkgs.writeText "claude-mcp-servers.json" (builtins.toJSON (renderMcpServers userCfg));
                  in
                  lib.hm.dag.entryAfter [ "writeBoundary" ] ''
                    set -eu
                    claude_json="$HOME/.claude.json"
                    if [ ! -f "$claude_json" ]; then
                      echo '{}' > "$claude_json"
                    fi
                    tmp=$(${pkgs.coreutils}/bin/mktemp)
                    ${pkgs.jq}/bin/jq \
                      --slurpfile mcps ${mcpJson} \
                      '.mcpServers = $mcps[0]' \
                      "$claude_json" > "$tmp"
                    mv "$tmp" "$claude_json"
                  '';
              }
            )
          ];
        }
      )
    ];

  meta.name = "default";
}
