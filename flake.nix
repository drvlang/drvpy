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
    name = "drvpy";
    extraChecks = system: { benchmark = import ./benchmarks {
      pkgs = nixpkgs.legacyPackages.${system}; inherit self system;
    }; };
    mkPackage = import ./package.nix inputs;
  };
}
