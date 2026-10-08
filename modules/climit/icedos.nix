{ icedosLib, lib, ... }:

{
  # Nested `climit` submodule for the claude-code per-user option in ../default.
  # MUST omit `default` — two `attrsOf submodule` decls with defaults do not type-merge.
  options.icedos.applications.claude-code.users =
    let
      inherit (lib) importTOML;

      inherit (icedosLib)
        mkBoolOption
        mkEnumOption
        mkIntBetweenOption
        mkNumberOption
        mkStrOption
        mkSubmoduleAttrsOption
        ;

      inherit ((importTOML ./config.toml).icedos.applications.claude-code.users.username.climit)
        interval
        alerts
        keybind
        widget
        statusLine
        alertUrgency
        alertTimeout
        alertTransient
        ;
    in
    mkSubmoduleAttrsOption { } {
      climit = {
        interval = mkNumberOption { default = interval; };
        alerts = mkBoolOption { default = alerts; };
        widget = mkBoolOption { default = widget; };

        # Claude Code status line: prompt segments plus usage; each redraw also records a sample.
        statusLine = mkBoolOption { default = statusLine; };

        # Plasma never expires critical urgency: alertUrgency = "critical" + alertTimeout = 0 is sticky.
        alertUrgency =
          mkEnumOption
            {
              path = "icedos.applications.claude-code.users.username.climit.alertUrgency";
              source = ./config.toml;
              default = alertUrgency;
            }
            [
              "critical"
              "low"
              "normal"
            ];

        # Seconds on screen; 0 means "until dismissed".
        alertTimeout = mkIntBetweenOption {
          path = "icedos.applications.claude-code.users.username.climit.alertTimeout";
          source = ./config.toml;
          default = alertTimeout;
        } 0 300;

        # Popup only — skip the notification history.
        alertTransient = mkBoolOption { default = alertTransient; };

        # Zed keybind to spawn the climit dashboard (ctrl-alt-v).
        keybind = mkStrOption { default = keybind; };
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

          climitPkg = pkgs.python3Packages.buildPythonApplication {
            pname = "climit";
            version = "0.1.0";
            pyproject = true;
            src = ./src;
            build-system = [ pkgs.python3Packages.setuptools ];
            doCheck = false;
            # notify-send for the timer's alerts; git as a fallback for the status line.
            makeWrapperArgs = [
              "--prefix"
              "PATH"
              ":"
              "${lib.makeBinPath [ pkgs.libnotify ]}"
              "--suffix"
              "PATH"
              ":"
              "${lib.makeBinPath [ pkgs.git ]}"
            ];
            meta = {
              description = "Track Claude usage limits (5h + weekly) and burn rate";
              mainProgram = "climit";
            };
          };

          # KDE Plasma 6 applet rendering `status --json` (store path baked in via
          # @climit@). It only reads the DB the timer and status line fill; no network.
          climitPlasmoid = pkgs.stdenvNoCC.mkDerivation {
            pname = "climit-plasmoid";
            version = "0.1.0";
            src = ./plasmoid;
            dontConfigure = true;
            dontBuild = true;
            installPhase = ''
              runHook preInstall
              dst="$out/share/plasma/plasmoids/org.icedos.climit"
              mkdir -p "$dst"
              cp -r ./* "$dst/"
              substituteInPlace "$dst/contents/ui/main.qml" \
                --replace-fail '@climit@' '${climitPkg}/bin/climit'
              runHook postInstall
            '';
            meta.description = "climit KDE Plasma 6 widget";
          };
        in
        {
          icedos.system.toolset.commands = lib.mkIf (claudeUsers != { }) [
            {
              command = "claude";
              help = "claude code tooling";

              commands = [
                {
                  command = "limits";
                  bin = "${climitPkg}/bin/climit";
                  help = "live Claude usage-limit dashboard (5h + weekly, burn rate)";
                }
              ];
            }
          ];

          home-manager.sharedModules = [
            (
              { config, lib, ... }:

              let
                userCfg = claudeUsers.${config.home.username}.climit or null;
              in
              lib.mkIf (userCfg != null) {
                # Refuse a poll interval below the endpoint's rate-limit floor.
                assertions = [
                  {
                    assertion = userCfg.interval >= 180;
                    message =
                      "climit: interval for ${config.home.username} is ${toString userCfg.interval}s;"
                      + " it must be ≥ 180 (the /api/oauth/usage rate-limit floor).";
                  }
                ];

                # plasmashell finds plasmoids via XDG_DATA_DIRS, which home.packages populates.
                # Add via "Add Widgets", or pin "org.icedos.climit" in icedos.desktop.kde.panel.widgets.
                home.packages = lib.optional userCfg.widget climitPlasmoid;

                # Live data comes from any running Claude Code via the status line;
                # this timer keeps samples coming (other clients, idle periods) while the login is valid.
                systemd.user.services.climit = {
                  Unit.Description = "climit: fetch Claude usage limits once";

                  Service = {
                    Type = "oneshot";
                    ExecStart =
                      "${climitPkg}/bin/climit poll"
                      + (
                        if !userCfg.alerts then
                          " --no-alerts"
                        else
                          " --alert-urgency ${userCfg.alertUrgency}"
                          + " --alert-timeout ${toString userCfg.alertTimeout}"
                          + lib.optionalString userCfg.alertTransient " --alert-transient"
                      );
                  };
                };

                systemd.user.timers.climit = {
                  Unit.Description = "climit: Claude usage-limit poll timer";

                  Timer = {
                    OnStartupSec = "30s";
                    OnUnitActiveSec = "${toString userCfg.interval}s";
                    AccuracySec = "1s";
                  };

                  Install.WantedBy = [ "timers.target" ];
                };

                programs.claude-code.settings.statusLine = lib.mkIf userCfg.statusLine {
                  type = "command";
                  command = "${climitPkg}/bin/climit statusline";
                  padding = 0;
                  # Redraw between model responses so timer samples and reset countdowns show up.
                  refreshInterval = 60;
                };

                # Live usage view as a Zed task (bottom terminal dock), when Zed is in use.
                programs.zed-editor.userTasks = lib.mkIf (config.programs.zed-editor.enable or false) [
                  {
                    label = "climit";
                    command = "icedos";
                    args = [
                      "claude"
                      "limits"
                    ];
                    use_new_terminal = false;
                    allow_concurrent_runs = false;
                    reveal = "always";
                    reveal_target = "dock";
                    hide = "never";
                  }
                ];

                # `ctrl-alt-v` (limits) spawns the climit dashboard from anywhere.
                programs.zed-editor.userKeymaps = lib.mkIf (config.programs.zed-editor.enable or false) [
                  {
                    context = "Workspace";
                    bindings = {
                      ${userCfg.keybind} = [
                        "task::Spawn"
                        {
                          task_name = "climit";
                        }
                      ];
                    };
                  }
                ];
              }
            )
          ];
        }
      )
    ];

  meta.name = "climit";
}
