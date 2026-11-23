# drvpy

Build and test Python applications directly from a version 1 `uv.lock`. A dynamic Nix derivation resolves the locked packages, prefers compatible wheels and produces an environment with the project's console scripts. Updating dependencies only requires updating the uv lockfile. An offline `uv lock --check` catches stale manifests.

```sh
nix run github:drvlang/drvpy -- build -C ./app
nix run github:drvlang/drvpy -- test -C ./app
nix run github:drvlang/drvpy -- run -C ./app -- --help
```

Enable `ca-derivations`, `dynamic-derivations` and `recursive-nix` in Nix and the build daemon. The builder needs the `recursive-nix` system feature. The flake supports x86-64 and AArch64 Linux.

The build includes runtime dependencies. Tests also look for a locked group or extra named `test` or `tests`, then `dev`. They run pytest when installed and otherwise use unittest discovery. Use `--options '{"dependencies":{"flask":["tests"]}}'` to choose groups explicitly. Run selects a project's sole console script and otherwise starts Python. Use `--options '{"program":"flask"}'` to choose a script.

## Flakes and workspaces

For one Python project, use `drvpy.lib.mkFlake` with a source selection covering its manifests, Python packages, tests and packaging inputs. For a uv workspace, give each command the directory containing its `pyproject.toml`. Both commands can depend on shared workspace packages and use the root lock.

```nix
outputs = { drvpy, ... }: drvpy.lib.mkMonorepoFlake {
  srcForTarget = { target, ... }: let member = ./. + "/${target}";
  in drvpy.lib.projectSource {
    root = ./.;
    files = [
      ./pyproject.toml ./uv.lock ./apps/api/pyproject.toml
      ./apps/worker/pyproject.toml ./pkg/shared/pyproject.toml ./pkg/shared/shared.py
      (member + "/src") (member + "/tests")
    ];
  };
  commands = { api = "apps/api"; worker = "apps/worker"; };
};
```

The example includes every workspace manifest so uv can verify the root lock. The selected member and its shared package supply the implementation files. Include additional readmes or data files used by your build backend.

Use `nix build .#api`, `nix run .#worker` and `nix flake check`. For CLI target names, put `{"commands":{"api":"apps/api","worker":"apps/worker"}}` in `drvlang.json` and run `drvpy test api`. Nix callers can select a pinned nixpkgs interpreter with `python = pkgs.python313;`. [Working workspace](tests/workspace)

Read that same map in the flake with `commands = (builtins.fromJSON (builtins.readFile ./drvlang.json)).commands;`.

## Benchmark

The Rust runner builds Flask 3.1.3 and repeats 130 upstream request, routing and session tests on its environment. [Benchmark definition](benchmarks/default.nix) · [Flask source](https://github.com/pallets/flask/tree/22d924701a6ae2e4cd01e9a15bbaf3946094af65)

```sh
nix run github:drvlang/drvpy#bench -- --runs 5
```
