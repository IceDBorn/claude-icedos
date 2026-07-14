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
        statusLine
        ;

      mcpTemplate = head (fromTOML (readFile ./mcp-servers.toml))
        .icedos.applications.claude-code.users.username.mcpServers;

      marketplaceTemplate = head (fromTOML (readFile ./marketplaces.toml))
        .icedos.applications.claude-code.users.username.marketplaces;
    in
    mkSubmoduleAttrsOption { default = { }; } {
      enabledPlugins = mkStrListOption { default = enabledPlugins; };

      extraSettings = mkAttrsOption { default = extraSettings; };

      statusLine = {
        type = mkStrOption { default = statusLine.type; };
        command = mkStrOption { default = statusLine.command; };
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
            optionalAttrs
            ;

          claudeUsers = config.icedos.applications.claude-code.users;

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
            // optionalAttrs (userCfg.statusLine.command != "") {
              statusLine = {
                inherit (userCfg.statusLine) type command;
              };
            }
            // userCfg.extraSettings;

          renderMcpServers =
            userCfg: listToAttrs (map renderMcp (filter (m: m.name != "") userCfg.mcpServers));
        in
        {
          home-manager.sharedModules = [
            (
              { config, lib, ... }:

              let
                userCfg = claudeUsers.${config.home.username} or null;
              in
              lib.mkIf (userCfg != null) {
                home.file.".claude/settings.json".source = pkgs.writeText "claude-settings.json" (
                  builtins.toJSON (renderSettings userCfg)
                );

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
