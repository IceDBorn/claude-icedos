{ icedosLib, lib, ... }:

{
  inputs.peon-ping = {
    url = "github:PeonPing/peon-ping";
    inputs.nixpkgs.follows = "nixpkgs";
  };

  options.icedos.applications.claude-code.peon-ping.users =
    let
      inherit (lib) head readFile;

      inherit (icedosLib)
        mkAttrsOption
        mkBoolOption
        mkFloatBetweenOption
        mkStrListOption
        mkStrOption
        mkSubmoduleAttrsOption
        mkSubmoduleListOption
        ;

      inherit
        ((fromTOML (readFile ./config.toml)).icedos.applications.claude-code.peon-ping.users.username)
        categories
        defaultPack
        desktopNotifications
        packs
        suppressSubagentComplete
        volume
        ;

      customPackTemplate = head (fromTOML (readFile ./custom-packs.toml))
        .icedos.applications.claude-code.peon-ping.users.username.customPacks;
    in
    mkSubmoduleAttrsOption { default = { }; } {
      defaultPack = mkStrOption { default = defaultPack; };

      volume = mkFloatBetweenOption {
        path = "icedos.applications.claude-code.peon-ping.users.<u>.volume";
        source = ./config.toml;
        default = volume;
      } 0.0 1.0;

      desktopNotifications = mkBoolOption { default = desktopNotifications; };
      suppressSubagentComplete = mkBoolOption { default = suppressSubagentComplete; };
      categories = mkAttrsOption { default = categories; };
      packs = mkStrListOption { default = packs; };

      customPacks = mkSubmoduleListOption { default = [ ]; } {
        name = mkStrOption { default = customPackTemplate.name; };
        owner = mkStrOption { default = customPackTemplate.owner; };
        repo = mkStrOption { default = customPackTemplate.repo; };
        rev = mkStrOption { default = customPackTemplate.rev; };
        hash = mkStrOption { default = customPackTemplate.hash; };
      };
    };

  outputs.nixosModules =
    { inputs, ... }:
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
            mapAttrs
            optionalAttrs
            ;

          peonUsers = config.icedos.applications.claude-code.peon-ping.users;
          peonPkg = inputs.peon-ping.packages.${pkgs.system}.default;

          renderCustomPack = cp: {
            inherit (cp) name;
            src = pkgs.fetchFromGitHub {
              inherit (cp)
                owner
                repo
                rev
                hash
                ;
            };
          };

          renderInstallPacks =
            u: u.packs ++ (map renderCustomPack (filter (cp: cp.name != "") u.customPacks));

          renderPeonSettings =
            u:
            {
              default_pack = u.defaultPack;
              volume = u.volume;
              desktop_notifications = u.desktopNotifications;
              suppress_subagent_complete = u.suppressSubagentComplete;
            }
            // optionalAttrs (u.categories != { }) { categories = u.categories; };

          peonCmd = {
            type = "command";
            command = "${peonPkg}/bin/peon";
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
        in
        {
          home-manager.sharedModules = [
            inputs.peon-ping.homeManagerModules.default
            (
              { config, lib, ... }:

              let
                peonUserCfg = peonUsers.${config.home.username} or null;
              in
              lib.mkIf (peonUserCfg != null) {
                programs.peon-ping = {
                  enable = true;
                  package = peonPkg;
                  claudeCodeIntegration = false;
                  settings = renderPeonSettings peonUserCfg;
                  installPacks = renderInstallPacks peonUserCfg;
                };
              }
            )
          ];

          icedos.applications.claude-code.users = mapAttrs (_: _: {
            extraSettings.hooks = peonHooks;
          }) peonUsers;
        }
      )
    ];

  meta.name = "peon-ping";
}
