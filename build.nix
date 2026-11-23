{ uv2nix, pyproject-nix, pyproject-build-systems, ... }:
pkgs: { src, memberPath, python, dependencies, test, testArgs }:
let
  inherit (pkgs) lib;
  project = src + "/${memberPath}";
  # An independent member can carry its own lock. A uv workspace keeps the
  # lock at the root and resolves member-to-member dependencies there.
  root = if builtins.pathExists (src + "/uv.lock") then src else project;
  workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = root; };
  manifest = builtins.fromTOML (builtins.readFile (project + "/pyproject.toml"));
  member = pyproject-nix.lib.pypa.normalizePackageName manifest.project.name;
  members = if memberPath == "." then builtins.attrNames workspace.deps.default
    else if builtins.hasAttr member workspace.deps.default then [ member ]
    else throw "drvpy: selected project ${member} is absent from the uv workspace";
  depSpec = if dependencies != null then dependencies else lib.genAttrs members (name:
    let groups = workspace.deps.all.${name};
        testGroups = builtins.filter (g: builtins.elem g groups) [ "test" "tests" ];
    in if !test then [] else if testGroups != [] then testGroups else lib.optional (builtins.elem "dev" groups) "dev");
  # pyproject.nix uses this stdenv for every locked package and virtualenv.
  pythonSet = (pkgs.callPackage pyproject-nix.build.packages {
    inherit python;
    stdenv = pkgs.stdenvAdapters.addAttrsToDerivation { __contentAddressed = true; } pkgs.stdenv;
  }).overrideScope
    (lib.composeManyExtensions [
      pyproject-build-systems.overlays.wheel
      (workspace.mkPyprojectOverlay { sourcePreference = "wheel"; dependencies = depSpec; })
    ]);
  env = pythonSet.mkVirtualEnv "drvpy-env" depSpec;
  verifiedLock = pkgs.runCommand "drvpy-check-uv-lock" { __contentAddressed = true; } ''
    cp -r ${root} project
    chmod -R u+w project
    cd project
    export HOME="$TMPDIR" UV_PYTHON_DOWNLOADS=never
    ${pkgs.uv}/bin/uv lock --check --offline --python ${python.interpreter}
    touch "$out"
  '';
  scripts = builtins.attrNames (manifest.project.scripts or {});
  script = if builtins.length scripts == 1 then builtins.head scripts else "python";
  testRoots = if memberPath == "." && (manifest.tool.uv.workspace.members or []) != []
    then map (dir: "." + dir) (uv2nix.lib.workspace.discoverWorkspace { workspaceRoot = root; })
    else [ "." ];
  checkedEnv = env.overrideAttrs (old: {
    inherit verifiedLock;
    postFixup = (old.postFixup or "") + ''
      mkdir -p "$out/nix-support"
      ln -s ${lib.escapeShellArg "../bin/${script}"} "$out/nix-support/drvpy-run"
    '';
  });
in
assert lib.assertMsg (builtins.pathExists (root + "/uv.lock")) "drvpy requires a checked-in uv.lock";
if !test then checkedEnv else pkgs.runCommand "drvpy-tests" { __contentAddressed = true; } ''
  cp -r ${src} project
  chmod -R u+w project
  cd project/${lib.escapeShellArg memberPath}
  export HOME="$TMPDIR"
  if [[ -x ${checkedEnv}/bin/pytest ]]; then
    ${checkedEnv}/bin/python -m pytest ${lib.escapeShellArgs testArgs}
  else
    args=(${lib.escapeShellArgs testArgs})
    if [[ ''${#args[@]} != 0 ]]; then
      ${checkedEnv}/bin/python -m unittest discover "''${args[@]}"
    else
      tested=false
      for member in ${lib.escapeShellArgs testRoots}; do
        if (
          cd "$member"
          if [[ -d tests ]]; then args=(-s tests); fi
          ${checkedEnv}/bin/python -m unittest discover "''${args[@]}"
        ); then
          tested=true
        else
          status=$?
          # unittest returns 5 when a member has no tests to discover.
          if [[ $status != 5 ]]; then exit "$status"; fi
        fi
      done
      if [[ $tested == false ]]; then exit 5; fi
    fi
  fi
  mkdir "$out"
''
