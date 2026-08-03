{ icedosLib, lib, ... }:

{
  # Registers the TencentDB Agent Memory knowledge service (code graph + wiki)
  # as a Claude Code MCP server. The containers themselves are the apps repo's
  # `agent-memory` module; this side only wires Claude Code to them.
  options.icedos.applications.claude-code.agentMemory =
    let
      inherit (lib) importTOML;

      inherit (icedosLib)
        mkNumberOption
        mkStrListOption
        mkStrOption
        ;

      inherit ((importTOML ./config.toml).icedos.applications.claude-code.agentMemory)
        containerName
        knowledgePort
        users
        ;
    in
    {
      containerName = mkStrOption { default = containerName; };
      knowledgePort = mkNumberOption { default = knowledgePort; };
      users = mkStrListOption { default = users; };
    };

  outputs.nixosModules =
    { ... }:
    [
      (
        {
          config,
          lib,
          ...
        }:

        let
          inherit (lib) listToAttrs nameValuePair;

          cfg = config.icedos.applications.claude-code.agentMemory;

          entry = {
            name = "agent-memory";
            # podman, not docker: the `docker` shim only exists because some
            # other module happens to enable virtualisation.podman.dockerCompat.
            command = "podman";

            args = [
              "exec"
              "-i"
              # Mandatory. The image defaults to LOG_LEVEL=info and the server
              # logs startup lines through console.log — i.e. stdout, the same
              # channel as JSON-RPC — which corrupts the MCP handshake.
              # warn/error go to stderr and are safe.
              "-e"
              "LOG_LEVEL=warn"
              # Server defaults to :8421; the client appends /v3 itself, so this
              # is the bare origin.
              "-e"
              "KNOWLEDGE_API_URL=http://127.0.0.1:${toString cfg.knowledgePort}"
              cfg.containerName
              "node"
              "/app/knowledge/dist/mcp/server.mjs"
            ];
          };
        in
        {
          # mcpServers is a listOf submodule, so this concatenates with whatever
          # the user declares in claude.toml rather than replacing it.
          icedos.applications.claude-code.users = listToAttrs (
            map (user: nameValuePair user { mcpServers = lib.mkAfter [ entry ]; }) cfg.users
          );
        }
      )
    ];

  meta.name = "agent-memory-mcp";
}
