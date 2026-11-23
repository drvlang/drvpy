{ uv2nix, pyproject-nix, pyproject-build-systems, ... }:
pkgs: { src, python ? pkgs.python3, dependencies ? null }:
let
  workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = src; };
  pythonSet = (pkgs.callPackage pyproject-nix.build.packages { inherit python; }).overrideScope
    (pkgs.lib.composeManyExtensions [
      pyproject-build-systems.overlays.wheel
      (workspace.mkPyprojectOverlay { sourcePreference = "wheel"; })
    ]);
  runtime = pkgs.lib.mapAttrs (_: _: []) workspace.deps.default;
  env = pythonSet.mkVirtualEnv "drvpy-env" (if dependencies == null then runtime else dependencies);
  verifiedLock = pkgs.runCommand "drvpy-check-uv-lock" {} ''
    cp -r ${src} project
    chmod -R u+w project
    cd project
    export HOME="$TMPDIR" UV_PYTHON_DOWNLOADS=never
    ${pkgs.uv}/bin/uv lock --check --offline --python ${python.interpreter}
    touch "$out"
  '';
in
assert pkgs.lib.assertMsg (builtins.pathExists (src + "/uv.lock")) "drvpy requires a checked-in uv.lock";
env.overrideAttrs (_: { inherit verifiedLock; })
