{
  description = "Build uv-locked Python environments with Nix";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/6b5e5b7a6631f065bf6908986990b37d845f847f";
  inputs.drvkit = { url = "github:drvlang/drvkit"; flake = false; };

  inputs.pyproject-nix = {
    url = "github:pyproject-nix/pyproject.nix";
    inputs.nixpkgs.follows = "nixpkgs";
  };
  inputs.uv2nix = {
    url = "github:pyproject-nix/uv2nix";
    inputs.nixpkgs.follows = "nixpkgs";
    inputs.pyproject-nix.follows = "pyproject-nix";
  };
  inputs.pyproject-build-systems = {
    url = "github:pyproject-nix/build-system-pkgs";
    inputs.nixpkgs.follows = "nixpkgs";
    inputs.pyproject-nix.follows = "pyproject-nix";
    inputs.uv2nix.follows = "uv2nix";
  };

  outputs = { self, nixpkgs, drvkit, ... }@inputs: import drvkit {
    inherit self nixpkgs;
    toolSource = {
      root = ./.;
      files = [ ./build.nix ./package.nix ./test.nix ];
      benchmarkFiles = [ ./benchmarks/default.nix ./benchmarks/source.nix ];
    };
    name = "drvpy";
    extraChecks = system: let args = {
      pkgs = nixpkgs.legacyPackages.${system}; inherit self system;
    }; in {
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
