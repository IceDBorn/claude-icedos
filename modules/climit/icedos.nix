{ icedosLib, lib, ... }:

{
  options.icedos.applications.claude-code.climit.users =
    let
      inherit (lib) readFile;

      inherit (icedosLib)
        mkBoolOption
        mkNumberOption
        mkSubmoduleAttrsOption
        ;

      inherit ((fromTOML (readFile ./config.toml)).icedos.applications.claude-code.climit.users.username)
        interval
        alerts
        widget
        ;
    in
    mkSubmoduleAttrsOption { default = { }; } {
      interval = mkNumberOption { default = interval; };
      alerts = mkBoolOption { default = alerts; };
      widget = mkBoolOption { default = widget; };
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
          climitUsers = config.icedos.applications.claude-code.climit.users;

          climitPkg = pkgs.python3Packages.buildPythonApplication {
            pname = "climit";
            version = "0.1.0";
            pyproject = true;
            src = ./src;
            build-system = [ pkgs.python3Packages.setuptools ];
            doCheck = false;
            # Make notify-send reachable from the systemd user service's PATH.
            makeWrapperArgs = [
              "--prefix"
              "PATH"
              ":"
              "${lib.makeBinPath [ pkgs.libnotify ]}"
            ];
            meta = {
              description = "Track Claude usage limits (5h + weekly) and burn rate";
              mainProgram = "climit";
            };
          };

          # KDE Plasma 6 applet that renders climit's `status --json` on the panel
          # or desktop. It shells out to the CLI above (absolute store path baked in
          # via @climit@) with --no-poll, so it only reads the DB the daemon fills —
          # no network, no rate-limit exposure.
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
          icedos.system.toolset.commands = lib.mkIf (climitUsers != { }) [
            {
              command = "claude";
              help = "Claude Code tooling";

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
                userCfg = climitUsers.${config.home.username} or null;
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

                # Plasma 6 widget. plasmashell discovers plasmoids from any
                # XDG_DATA_DIRS/plasma/plasmoids/, which home.packages populates.
                # Add it via "Add Widgets" (panel or desktop), or pin it to the KDE
                # panel by adding "org.icedos.climit" to icedos.desktop.kde.panel.widgets.
                home.packages = lib.optional userCfg.widget climitPlasmoid;

                systemd.user.services.climit = {
                  Unit.Description = "climit — Claude usage-limit poller";

                  Service = {
                    ExecStart =
                      "${climitPkg}/bin/climit daemon --interval ${toString userCfg.interval}"
                      + lib.optionalString (!userCfg.alerts) " --no-alerts";

                    Restart = "on-failure";
                    RestartSec = 30;
                  };

                  Install.WantedBy = [ "default.target" ];
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
              }
            )
          ];
        }
      )
    ];

  meta.name = "climit";
}
