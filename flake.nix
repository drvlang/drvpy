{
  description = "Build uv-locked Python environments with Nix";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/6b5e5b7a6631f065bf6908986990b37d845f847f";
  inputs.drvkit = { url = "github:drvlang/drvkit"; flake = false; };

  outputs = { self, nixpkgs, drvkit, ... }@inputs: import drvkit {
    inherit self nixpkgs;
    toolSource = {
      root = ./.;
      files = [ ./drvpy.py ./package.nix ./test.nix ];
      benchmarkFiles = [ ./benchmarks/default.nix ./benchmarks/source.nix ];
    };
    name = "drvpy";
    extraChecks = system: let
      pkgs = nixpkgs.legacyPackages.${system};
      args = { inherit pkgs self system; };
    in {
      unit-tests = pkgs.runCommand "drvpy-unit-tests" { __contentAddressed = true; } ''
        ${pkgs.python3}/bin/python3 ${./drvpy.py} self-test
        mkdir "$out"
      '';
      benchmark = import ./benchmarks args;
      workspace = import ./tests args;
    };
    mkPackage = import ./package.nix { inherit self drvkit; };
    mkTest = import ./package.nix { inherit self drvkit; test = true; };
    selectTarget = { src, target, ... }: { inherit src; memberPath = target; };
    mkApp = pkgs: args: package: let name = args.program or null;
    in assert pkgs.lib.assertMsg (name == null ||
      (builtins.match "[^/]+" name != null && !(builtins.elem name [ "." ".." ])))
      "program must be a basename in bin/";
      {
        type = "app";
        program = if name == null then "${package}/nix-support/drvpy-run" else "${package}/bin/${name}";
      };
  };
}
