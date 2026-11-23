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
in pkgs.runCommand "check-workspace" {
  memberTests = builtins.attrValues workspace.checks.${system};
  workspaceTest = self.lib.mkTest {
    inherit system;
    src = self.lib.projectSource { inherit root; files = common ++ [ ./workspace/apps ]; };
  };
} ''
  test "$(${apps.api.program})" = 'api drvlang'
  test "$(${apps.worker.program})" = 'worker drvlang'
  touch "$out"
''
