{ drvkit, self, test ? false }:
pkgs: { src, memberPath ? ".", python ? pkgs.python3, dependencies ? null, testArgs ? [] }:
let
  inherit (pkgs) lib;
  dynamic = import (drvkit + "/dynamic.nix") { inherit pkgs; };
  json = value: "builtins.fromJSON ${lib.strings.escapeNixString (builtins.toJSON value)}";
  pythonAttr = if builtins.isString python then python else python.pythonAttr;
  # Explicit dependency groups describe the same environment for build and
  # test. Test arguments only affect the runner once that environment exists.
  testGroups = test && dependencies == null;
  # Every project and test evaluates the same locked tools. Store their
  # expression once, preserving references to the pinned flake sources.
  lockedInputs = (pkgs.writeText "drvpy-locked-inputs.nix" (import (drvkit + "/flake-inputs.nix") {
    inputs = { inherit (self.inputs) nixpkgs uv2nix pyproject-nix pyproject-build-systems; };
  })).overrideAttrs { __contentAddressed = true; allowSubstitutes = true; };
  env = dynamic {
    name = if testGroups then "drvpy-test-env" else "drvpy-package";
    expression = ''
      let
        inputs = import ${lockedInputs};
        pkgs = inputs.nixpkgs.legacyPackages.${pkgs.stdenv.hostPlatform.system};
      in import ${./build.nix} inputs pkgs {
        src = builtins.storePath "${src}";
        memberPath = ${lib.strings.escapeNixString memberPath};
        python = pkgs.${lib.strings.escapeNixString pythonAttr};
        test = ${lib.boolToString testGroups};
        dependencies = ${json dependencies};
      }
    '';
  };
in
assert lib.assertMsg (builtins.isString python || pkgs.${pythonAttr}.drvPath == python.drvPath)
  "drvpy needs a Python interpreter from its pinned nixpkgs for recursive planning";
if test then import ./test.nix { inherit pkgs src memberPath env testArgs; } else env
