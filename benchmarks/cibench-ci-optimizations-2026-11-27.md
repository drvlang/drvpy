# Optimizing drvpy for CI

Across all seven CIBench CI cases, replacing `drvkit/dynamic.nix`, `nix-instantiate`, `uv2nix`, `pyproject-nix`, and `pyproject-build-systems` with a self-contained Python stdlib Nix daemon ATerm planner and PEP 427/405 wheel/virtualenv builder reduced cold builds from 54.62 s to 1.92 s (28.44× faster; 2.00× faster than Nixpkgs `buildPythonPackage`), remote cache restores from 11.53 s / 288.2 MiB to 2.07 s / 93.8 MiB (5.58× faster, 67.5% less traffic), local warm checks from 1.31 s to 0.56 s (2.32× faster; faster than `buildPythonPackage`), remote edits from 48.36 s / 287.8 MiB to 2.56 s / 93.8 MiB (18.90× faster; 1.77× faster than `buildPythonPackage` with 55.6% less traffic), local edits from 31.80 s to 1.21 s (26.36× faster; 1.74× faster than `buildPythonPackage`), remote dependency updates from 47.78 s / 295.1 MiB to 2.73 s / 93.7 MiB (17.48× faster; 1.95× faster than `buildPythonPackage` with 55.6% less traffic), and local dependency updates from 32.21 s to 1.33 s (24.14× faster; 2.37× faster than `buildPythonPackage`).

CIBench builds a Python package (`format-demo` depending on `idna`) and checks the output of its installed console script `format-demo`. The source edit modifies `format_demo.py` and the expected output without changing `pyproject.toml` or `uv.lock`. The independent dependency update upgrades `idna` from `3.10` to `3.11` in `pyproject.toml` and `uv.lock` without any Nix-specific lockfile hashes.

| CIBench case | drvpy `f2d1019` (s) | drvpy optimized (s) | `buildPythonPackage` (s) | drvpy cache transfer (before → after) | drvpy builds (before → after) | drvpy peak RAM (before → after) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| cold | 54.62 | 1.92 | 3.85 | 0.0 → 0.0 MiB | 44 → 6 | 2146 → 557 MiB |
| remote-warm | 11.53 | 2.07 | 1.95 | 288.2 → 93.8 MiB | 0 → 0 | 1979 → 546 MiB |
| local-warm | 1.31 | 0.56 | 0.62 | 0.0 → 0.0 MiB | 0 → 0 | 443 → 203 MiB |
| edit-remote | 48.36 | 2.56 | 4.54 | 287.8 → 93.8 MiB | 7 → 5 | 2006 → 561 MiB |
| edit-local | 31.80 | 1.21 | 2.10 | 0.0 → 0.0 MiB | 7 → 5 | 601 → 221 MiB |
| dependency-remote | 47.78 | 2.73 | 5.33 | 295.1 → 93.7 MiB | 9 → 7 | 2050 → 556 MiB |
| dependency-local | 32.21 | 1.33 | 3.17 | 0.0 → 0.0 MiB | 9 → 7 | 600 → 223 MiB |

Times are medians of two trials for `f2d1019`, three trials for the optimized tree, and five trials for the shared Nixpkgs `buildPythonPackage` comparator, run in alternating order (`before-1`, `after-1` ×2, `before-2`, `after-2`) with no competing host workloads. Both builders used Python 3.14.7. Each sample timed evaluation and realization of the package and check together. [Individual timings, build counts, cache traffic, peak memory, and realized outputs](cibench-ci-optimizations-2026-11-27.csv)

## Summary of optimizations

1. **Self-contained Python stdlib Nix daemon ATerm planner (`drvpy.py`, `package.nix`, `flake.nix`)**: Replaced `drvkit/dynamic.nix`, `nix-instantiate`, `uv2nix`, `pyproject-nix`, `pyproject-build-systems`, and `pkgs.uv` with a zero-dependency Python 3 script using `tomllib`, `zipfile`, `hashlib`, and a direct protocol-1.21 Nix daemon Unix-socket client (`wopQueryValidPaths`, `wopAddTextToStore`). Removed `pkgs.nix`, `nixpkgsSource` (202 MiB), `uv` (23 MiB), `gcc-15.3.0` (98.6 MiB), `binutils` (10.5 MiB), `stdenv`, and 38 build-system derivations (`hatchling`, `flit-core`, `setuptools`, `poetry-core`, and hooks) from consumer builds.
2. **Per-wheel `builtin:fetchurl` + floating CA PEP 427/405 builders (`drvpy.py`)**: Evaluates PEP 508 environment markers and PEP 425 wheel tags natively, downloads each locked wheel via `builtin:fetchurl` (preserving stock cache paths), unpacks each wheel into its own floating CA derivation (applying `patchelf` only to manylinux wheels containing `.so` extensions), builds local workspace packages directly into deterministic `.dist-info` outputs, and assembles a standard PEP 405 `pyvenv.cfg` virtual environment.
3. **Removed unused heavy flake inputs (`flake.nix`, `flake.lock`)**: Dropped `uv2nix`, `pyproject-nix`, and `pyproject-build-systems` flake inputs, cutting `local-warm` evaluation time by more than half (1.31 s → 0.56 s).

## Correctness checks

- Realized package and check outputs were deterministic across all three optimized trials and matched between `cold`, `remote-warm`, and `local-warm`, between `edit-remote` and `edit-local`, and between `dependency-remote` and `dependency-local`.
- `nix flake check -L --option allow-import-from-derivation false` passed all four checks:
  - `checks.x86_64-linux.unit-tests` (`<nix/fetchurl.nix>` path parity for `idna-3.10-py3-none-any.whl` and PEP 508 marker evaluation)
  - `checks.x86_64-linux.workspace` (multi-member uv workspace, shared planner across test shards and explicit deps, CA byte-identity across a comment-only `pyproject.toml` edit, behavior change on `api.py` edit, and root workspace unittest discovery)
  - `checks.x86_64-linux.benchmark` (Flask 3.1.3 with 21 resolved packages including `markupsafe` and `greenlet` manylinux C extensions passing `pytest tests/test_basic.py`)
  - `checks.x86_64-linux.cli`
- `nix flake show --all-systems` (`x86_64-linux`, `aarch64-linux`) and CLI end-to-end checks (`drvpy build`, `drvpy test`, `drvpy run`, and non-zero exit propagation on a failing test) passed.

## Reproduction

The runner used x86_64 Linux, an Intel Core i7-1280P, Nix 2.35.3, four logical CPUs (16–19), four jobs, four cores per builder, 8 GiB RAM, and zero swap. Sandboxing was enabled and IFD disabled. The simulated project cache used 5 ms latency and 100 MiB/s aggregate bandwidth. Both builders used the same local mirror of signed NixOS packages populated from `drvkit/cibench/results/bootstrap/py.txt`. Cold jobs started with an empty local store and project cache. Subsequent cases independently used the original job's local store or its published project cache. The recorded runner was `/nix/store/vpysz8ih9lhm154cjaamrncwjn08njp6-cibench-0.1.0`.

```sh
work=$(mktemp -d)
stock="file://$work/stock"
nix copy --from https://cache.nixos.org --to "$stock?compression=zstd&compression-level=1" \
  --stdin < ../drvkit/cibench/results/bootstrap/py.txt
runner="$(nix build --impure --no-link --print-out-paths -f ../drvkit/cibench)/bin/cibench"
mkdir "$work/before"
git archive f2d1019 | tar -x -C "$work/before"
run() {
  taskset -c 16-19 "$runner" py --builder "$1" --out "$work/$2" --trials "$3" \
    --cpus 4 --memory-mib 8192 --latency-ms 5 --bandwidth-mib-s 100 \
    --upstream "$stock?priority=40"
}
run "$work/before" before-1 1
run "$PWD" after-1 2
run "$work/before" before-2 1
run "$PWD" after-2 1
```
