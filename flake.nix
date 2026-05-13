{
  outputs =
    { ... }:
    {
      icedosModules =
        { icedosLib, ... }:
        icedosLib.scanModules {
          path = ./modules;
          filename = "icedos.nix";
        };
    };
}
