{ drvkit, self, test ? false }:
pkgs: { src, memberPath ? ".", python ? pkgs.python3, dependencies ? null, testArgs ? [] }:
let
  inherit (pkgs) lib;
  system = pkgs.stdenv.hostPlatform.system;
  ca = { __contentAddressed = true; outputHashMode = "recursive"; outputHashAlgo = "sha256"; };
  pythonAttr = if builtins.isString python then python else python.pythonAttr;
  selectedPython = pkgs.${pythonAttr};
  # Explicit dependency groups describe the same environment for build and
  # test. Test arguments only affect the runner once that environment exists.
  testGroups = test && dependencies == null;
  drvpyScript = ./drvpy.py;

  # Detect whether any platform wheels (manylinux C extensions) might be needed
  # so pure-Python projects omit patchelf when possible.
  planner = builtins.derivation {
    name = if testGroups then "drvpy-test-env-plan.drv" else "drvpy-package-plan.drv";
    inherit system;
    builder = "${selectedPython}/bin/python3";
    args = [ drvpyScript "internal-plan" ];
    __contentAddressed = true;
    outputHashMode = "text";
    outputHashAlgo = "sha256";
    requiredSystemFeatures = [ "recursive-nix" ];
    preferLocalBuild = true;
    allowSubstitutes = true;
    inherit src memberPath;
    pythonOut = selectedPython;
    drvpyScript = drvpyScript;
    patchelfOut = pkgs.patchelf;
    glibcOut = pkgs.glibc;
    gccLibOut = pkgs.stdenv.cc.cc.lib;
    testGroups = if testGroups then "1" else "0";
    dependenciesJson = builtins.toJSON dependencies;
  };

  env = builtins.derivation (ca // {
    name = if testGroups then "drvpy-test-env" else "drvpy-package";
    inherit system;
    preferLocalBuild = true;
    allowSubstitutes = true;
    builder = "${selectedPython}/bin/python3";
    args = [ drvpyScript "internal-symlink" (builtins.outputOf planner.outPath "out") (builtins.placeholder "out") ];
  }) // {
    inherit planner;
    passthru = { inherit planner; };
  };
in
assert lib.assertMsg (builtins.isString python || selectedPython.drvPath == python.drvPath)
  "drvpy needs a Python interpreter from its pinned nixpkgs for recursive planning";
if test then import ./test.nix { inherit pkgs src memberPath env testArgs; } else env
