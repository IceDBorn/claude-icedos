{ icedosLib, lib, ... }:

let
  inherit (lib) attrNames head importTOML;

  inherit (icedosLib)
    mkEnumOption
    mkStrOption
    mkSubmoduleAttrsOption
    mkSubmoduleListOption
    ;

  marketplaceTemplate = head (importTOML ./config.toml)
    .icedos.applications.claude-code.users.username.marketplaces;

  # Option path prefix shared by validation messages (source + path checks).
  mpOptPath = "icedos.applications.claude-code.users.<name>.marketplaces";

  # Fields each source consumes; unused fields are never forwarded. The enum
  # below derives from this, so adding a source without its fields is caught.
  requiredFor = {
    fetchFromGitHub = [
      "owner"
      "repo"
      "rev"
      "hash"
    ];
    fetchFromGitLab = [
      "owner"
      "repo"
      "rev"
      "hash"
    ];
    fetchgit = [
      "url"
      "rev"
      "hash"
    ];
    path = [ "path" ];
  };
in
{
  options.icedos.applications.claude-code.users = mkSubmoduleAttrsOption { default = { }; } {
    marketplaces = mkSubmoduleListOption { default = [ ]; } {
      name = mkStrOption { default = marketplaceTemplate.name; };

      # source names the pkgs fetcher attr (fetchFromGitHub/fetchFromGitLab/
      # fetchgit) or "path" for a local dir. Empty default aborts until set.
      source = mkEnumOption {
        path = "${mpOptPath}.source";
        source = ./config.toml;
        default = marketplaceTemplate.source;
      } (attrNames requiredFor);

      owner = mkStrOption { default = marketplaceTemplate.owner; };
      repo = mkStrOption { default = marketplaceTemplate.repo; };
      url = mkStrOption { default = marketplaceTemplate.url; };

      # Absolute path required once set; "" is the unused default.
      path =
        let
          check =
            v:
            v == ""
            || icedosLib.validate.str {
              regex = "/.+";
            } "${mpOptPath}.path" ./config.toml v;
        in
        lib.mkOption {
          type = lib.types.addCheck lib.types.str check;
          default = marketplaceTemplate.path;
        };

      domain = mkStrOption { default = marketplaceTemplate.domain; };
      rev = mkStrOption { default = marketplaceTemplate.rev; };
      hash = mkStrOption { default = marketplaceTemplate.hash; };
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
          icedosLib,
          ...
        }:

        let
          inherit (lib)
            all
            attrNames
            attrValues
            concatLists
            concatStringsSep
            filter
            findFirst
            listToAttrs
            mapAttrsToList
            mkIf
            optionalAttrs
            unique
            ;

          claudeUsers = config.icedos.applications.claude-code.users;

          # Fetcher per source enum value: pkgs fetchers named by the value.
          # "path" is copied into the store when hm serializes settings.json
          # (toJSON) — a snapshot at eval time; pure eval forbids host paths,
          # so store paths work and host dirs need --impure or nix store add-path.
          fetchMarketplace =
            m:
            {
              fetchFromGitHub = pkgs.fetchFromGitHub {
                inherit (m)
                  owner
                  repo
                  rev
                  hash
                  ;
              };

              fetchFromGitLab = pkgs.fetchFromGitLab (
                {
                  inherit (m)
                    owner
                    repo
                    rev
                    hash
                    ;
                }
                // optionalAttrs (m.domain != "") {
                  inherit (m) domain;
                }
              );

              fetchgit = pkgs.fetchgit {
                inherit (m) url rev hash;
              };

              path = /. + m.path;
            }
            .${m.source};

          # Fetch at build time; programs.claude-code.marketplaces installs it
          # and records it in settings.extraKnownMarketplaces + known_marketplaces.json.
          renderMarketplaces =
            userCfg:
            listToAttrs (
              map (m: {
                name = m.name;
                value = fetchMarketplace m;
              }) userCfg.marketplaces
            );
        in
        {
          # Rule 1 (core/AGENTS.md): fill every normal user so the nested
          # climit/reviewMcp submodules get their field defaults.
          icedos.applications.claude-code.users = icedosLib.users.genDefaults {
            inherit (config.icedos) users;
          };

          assertions =
            let
              # (user, marketplace) pairs so failures name the user too.
              entries = concatLists (
                mapAttrsToList (u: ms: map (m: { inherit u m; }) ms.marketplaces) claudeUsers
              );
              names = map (e: e.m.name) entries;
              missingFor = m: filter (f: m.${f} == "") (requiredFor.${m.source} or [ ]);
              findEntry = p: findFirst (e: p e) null entries;
              dupNames =
                cfg:
                let
                  ns = map (m: m.name) cfg.marketplaces;
                in
                ns != unique ns;
            in
            [
              {
                assertion = all (n: n != "") names;
                message =
                  let
                    offender = findEntry (e: e.m.name == "");
                  in
                  "claude-code: marketplace for user '${if offender == null then "?" else offender.u}'"
                  + " is missing a name.";
              }
              # Per user — renderMarketplaces builds one attrset per user, so two
              # users may share a marketplace name.
              {
                assertion = all (u: !dupNames u) (attrValues claudeUsers);
                message =
                  "claude-code: marketplace names must be unique per user"
                  + " ('${findFirst (u: dupNames claudeUsers.${u}) "" (attrNames claudeUsers)}').";
              }
            ]
            ++ map (
              e:
              let
                missing = missingFor e.m;
              in
              {
                assertion = missing == [ ];
                message =
                  "claude-code: marketplace '${e.m.name}' for user '${e.u}' (source"
                  + " ${e.m.source}) is missing: ${concatStringsSep ", " missing}";
              }
            ) (filter (e: e.m.name != "") entries);

          home-manager.sharedModules = [
            (
              { config, ... }:

              let
                userCfg = claudeUsers.${config.home.username} or null;
              in
              mkIf (userCfg != null) {
                programs.claude-code.marketplaces = renderMarketplaces userCfg;
              }
            )
            (
              { lib, ... }:

              {
                programs.claude-code = {
                  enable = lib.mkDefault true;
                  enableMcpIntegration = true;
                };
              }
            )
          ];
        }
      )
    ];

  meta.name = "default";
}
