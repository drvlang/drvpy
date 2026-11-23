{ self, system, pkgs }:
let
  root = ./workspace;
  commands = (builtins.fromJSON (builtins.readFile ./workspace/drvlang.json)).commands;
  common = [
    ./workspace/pyproject.toml
    ./workspace/uv.lock
    ./workspace/apps/api/pyproject.toml
    ./workspace/apps/worker/pyproject.toml
    ./workspace/pkg/shared/pyproject.toml
    ./workspace/pkg/shared/shared.py
  ];
  srcForTarget = { target, ... }: let member = root + "/${target}";
  in self.lib.projectSource {
    inherit root;
    files = common ++ [ (member + "/src") (member + "/tests") ];
  };
  workspace = self.lib.mkMonorepoFlake { inherit commands srcForTarget; };
  apps = workspace.apps.${system};
  api = workspace.packages.${system}.api;
  apiArgs = { inherit system commands srcForTarget; target = "api"; };
  explicitDeps = apiArgs // { dependencies.api = []; };
  shard = self.lib.mkTest (apiArgs // { testArgs = [ "-p" "test_command.py" ]; });
  editedApi = edit: self.lib.mkPackage {
    inherit system commands;
    target = "api";
    src = pkgs.runCommand "project-source" { __contentAddressed = true; } ''
      cp -r ${srcForTarget { target = commands.api; }} "$out"
      chmod -R u+w "$out"
      cd "$out"
      ${edit}
    '';
  };
  manifestEdit = editedApi ''echo "# edited manifest" >> apps/api/pyproject.toml'';
  programEdit = editedApi ''sed -i 's/"api"/"edited"/' apps/api/src/api.py'';
in
assert shard.planner.drvPath == workspace.checks.${system}.api.planner.drvPath;
assert (self.lib.mkPackage explicitDeps).planner.drvPath == (self.lib.mkTest explicitDeps).planner.drvPath;
pkgs.runCommand "check-workspace" {
  __contentAddressed = true;
  memberTests = builtins.attrValues workspace.checks.${system};
  workspaceTest = self.lib.mkTest {
    inherit system;
    src = self.lib.projectSource { inherit root; files = common ++ [ ./workspace/apps ]; };
  };
} ''
  test "$(${apps.api.program})" = 'api drvlang'
  test "$(${apps.worker.program})" = 'worker drvlang'
  test "${api}" = "${manifestEdit}"
  test "${api}" != "${programEdit}"
  test "$(${programEdit}/bin/api)" = 'edited drvlang'
  touch "$out"
''
