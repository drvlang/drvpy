# drvpy

Build a Python environment from a project's `uv.lock`. Nix caches individual packages, prefers compatible wheels, and exposes the project's console scripts in `result/bin`. An offline `uv lock --check` rejects stale manifests.

```sh
nix run github:drvlang/drvpy -- build -C ./app
./result/bin/python
```

The default contains runtime dependencies of workspace members. To include a locked extra or group, pass, for example, `--options '{"dependencies":{"flask":["tests"]}}'`. The flake also exposes `drvpy.lib.mkFlake { src = ./app; }` and `lib.mkPackage { system, src, ... }`. Nix callers can pass a compatible `python` interpreter. Wheel selection and package builds use [uv2nix](https://pyproject-nix.github.io/uv2nix/) with pinned build backends.

## Benchmark

The Rust benchmark runner builds Flask 3.1.3 and repeats its upstream request, routing, and session tests on the resulting environment. The pinned workload contains 130 tests. [Benchmark definition](benchmarks/default.nix) · [Flask source](https://github.com/pallets/flask/tree/22d924701a6ae2e4cd01e9a15bbaf3946094af65)

```sh
nix run github:drvlang/drvpy#bench -- --runs 5
```
