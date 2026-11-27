#!/usr/bin/env python3
"""Self-contained stdlib-only Nix daemon planner and PEP 427/405 builder for drvpy."""

import configparser
import glob
import hashlib
import json
import os
import pathlib
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tomllib
import zipfile

# --- Nix store path & protocol-1.21 daemon client ---

NIX_B32 = "0123456789abcdfghijklmnpqrsvwxyz"
CLIENT_MAGIC = 0x6E697863
SERVER_MAGIC = 0x6478696F
PROTOCOL_1_21 = (1 << 8) | 21
STDERR_NEXT = 0x6F6C6D67
STDERR_LAST = 0x616C7473
STDERR_ERROR = 0x63787470
WOP_ADD_TEXT_TO_STORE = 8
WOP_QUERY_VALID_PATHS = 31


def store_dir() -> str:
    d = os.environ.get("NIX_STORE", "/nix/store")
    if d.startswith("/"):
        return d.rstrip("/") or "/"
    return "/nix/store"


def nix_base32(data: bytes) -> str:
    out_len = (len(data) * 8 - 1) // 5 + 1
    out = []
    for n in range(out_len):
        b = (out_len - 1 - n) * 5
        i = b // 8
        j = b % 8
        c = data[i] >> j
        if j > 0 and i + 1 < len(data):
            c |= data[i + 1] << (8 - j)
        out.append(NIX_B32[c & 0x1F])
    return "".join(out)


def compress_hash(digest: bytes, size: int) -> bytes:
    out = bytearray(size)
    for i, b in enumerate(digest):
        out[i % size] ^= b
    return bytes(out)


def make_store_path(sdir: str, typ: str, sha256_hex: str, name: str) -> str:
    fp = f"{typ}:sha256:{sha256_hex}:{sdir}:{name}".encode()
    h = nix_base32(compress_hash(hashlib.sha256(fp).digest(), 20))
    return f"{sdir}/{h}-{name}"


def make_text_path(sdir: str, name: str, text: bytes, refs: list[str]) -> str:
    digest = hashlib.sha256(text).hexdigest()
    typ = "text" + "".join(f":{r}" for r in sorted(set(refs)))
    return make_store_path(sdir, typ, digest, name)


def make_fixed_output_path(sdir: str, name: str, sha256_hex: str) -> str:
    inner = hashlib.sha256(f"fixed:out:sha256:{sha256_hex}:".encode()).hexdigest()
    return make_store_path(sdir, "output:out", inner, name)


def hash_placeholder(output_name: str) -> str:
    return "/" + nix_base32(hashlib.sha256(f"nix-output:{output_name}".encode()).digest())


def downstream_placeholder(drv_path: str, output_name: str) -> str:
    base = os.path.basename(drv_path)
    hash_part = base[:32]
    drv_name = base[33:].removesuffix(".drv")
    out_name = drv_name if output_name == "out" else f"{drv_name}-{output_name}"
    digest = hashlib.sha256(f"nix-upstream-output:{hash_part}:{out_name}".encode()).digest()
    return "/" + nix_base32(digest)


def aterm_str(s: str) -> str:
    return (
        '"'
        + s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        + '"'
    )


def format_derivation(
    outputs: list[tuple[str, str, str, str]],
    input_drvs: dict[str, list[str]],
    input_srcs: set[str],
    platform: str,
    builder: str,
    args: list[str],
    env: dict[str, str],
) -> bytes:
    outs = ",".join(
        f"({aterm_str(n)},{aterm_str(p)},{aterm_str(a)},{aterm_str(h)})"
        for n, p, a, h in sorted(outputs, key=lambda x: x[0])
    )
    idrvs = ",".join(
        f"({aterm_str(d)},[{','.join(aterm_str(o) for o in sorted(set(os_)))}])"
        for d, os_ in sorted(input_drvs.items())
    )
    isrcs = ",".join(aterm_str(s) for s in sorted(input_srcs))
    a_str = ",".join(aterm_str(a) for a in args)
    e_str = ",".join(f"({aterm_str(k)},{aterm_str(v)})" for k, v in sorted(env.items()))
    return (
        f"Derive([{outs}],[{idrvs}],[{isrcs}],"
        f"{aterm_str(platform)},{aterm_str(builder)},[{a_str}],[{e_str}])"
    ).encode()


def make_fetchurl_drv(sdir: str, name: str, url: str, sha256_hex: str):
    out_path = make_fixed_output_path(sdir, name, sha256_hex)
    env = {
        "builder": "builtin:fetchurl",
        "executable": "",
        "impureEnvVars": "http_proxy https_proxy ftp_proxy all_proxy no_proxy",
        "name": name,
        "out": out_path,
        "outputHash": sha256_hex,
        "outputHashAlgo": "sha256",
        "outputHashMode": "flat",
        "preferLocalBuild": "1",
        "system": "builtin",
        "unpack": "",
        "url": url,
        "urls": url,
    }
    aterm = format_derivation(
        [("out", out_path, "sha256", sha256_hex)],
        {},
        set(),
        "builtin",
        "builtin:fetchurl",
        [],
        env,
    )
    drv_name = f"{name}.drv"
    drv_path = make_text_path(sdir, drv_name, aterm, [])
    return drv_path, out_path, aterm, []


class DaemonClient:
    def __init__(self, remote: str):
        if remote.startswith("unix://"):
            sock_path = remote[len("unix://") :].split("?", 1)[0]
        elif remote == "daemon":
            sock_path = "/nix/var/nix/daemon-socket/socket"
        else:
            raise RuntimeError(f"unsupported NIX_REMOTE={remote!r}")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(sock_path)
        self.buf = bytearray()
        self._handshake()

    def close(self):
        self.sock.close()

    def _send_u64(self, v: int):
        self.buf.extend(struct.pack("<Q", v))

    def _send_bytes(self, b: bytes):
        self._send_u64(len(b))
        self.buf.extend(b)
        pad = (8 - (len(b) % 8)) % 8
        if pad:
            self.buf.extend(b"\x00" * pad)

    def _send_str(self, s: str):
        self._send_bytes(s.encode())

    def _send_str_set(self, items: list[str]):
        self._send_u64(len(items))
        for s in items:
            self._send_str(s)

    def _flush(self):
        if self.buf:
            self.sock.sendall(self.buf)
            self.buf.clear()

    def _recv_exact(self, n: int) -> bytes:
        chunks = []
        rem = n
        while rem > 0:
            chunk = self.sock.recv(rem)
            if not chunk:
                raise RuntimeError("unexpected EOF from nix daemon")
            chunks.append(chunk)
            rem -= len(chunk)
        return b"".join(chunks)

    def _recv_u64(self) -> int:
        return struct.unpack("<Q", self._recv_exact(8))[0]

    def _recv_bytes(self) -> bytes:
        n = self._recv_u64()
        data = self._recv_exact(n)
        pad = (8 - (n % 8)) % 8
        if pad:
            self._recv_exact(pad)
        return data

    def _recv_str(self) -> str:
        return self._recv_bytes().decode()

    def _recv_str_set(self) -> list[str]:
        n = self._recv_u64()
        return [self._recv_str() for _ in range(n)]

    def _drain_stderr(self):
        while True:
            code = self._recv_u64()
            if code == STDERR_LAST:
                return
            if code == STDERR_NEXT:
                self._recv_bytes()
            elif code == STDERR_ERROR:
                msg = self._recv_str()
                self._recv_u64()
                raise RuntimeError(f"nix daemon error: {msg}")
            else:
                raise RuntimeError(f"unexpected nix daemon stderr code {code:#x}")

    def _handshake(self):
        self._send_u64(CLIENT_MAGIC)
        self._flush()
        magic = self._recv_u64()
        if magic != SERVER_MAGIC:
            raise RuntimeError(f"unexpected nix daemon magic {magic:#x}")
        self._recv_u64()  # server version
        self._send_u64(PROTOCOL_1_21)
        self._send_u64(0)  # cpuAffinity
        self._send_u64(0)  # reserveSpace
        self._flush()
        self._drain_stderr()

    def query_valid_paths(self, paths: list[str]) -> set[str]:
        if not paths:
            return set()
        self._send_u64(WOP_QUERY_VALID_PATHS)
        self._send_str_set(paths)
        self._flush()
        self._drain_stderr()
        return set(self._recv_str_set())

    def add_text_to_store(self, name: str, text: bytes, refs: list[str]) -> str:
        self._send_u64(WOP_ADD_TEXT_TO_STORE)
        self._send_str(name)
        self._send_bytes(text)
        self._send_str_set(refs)
        self._flush()
        self._drain_stderr()
        return self._recv_str()


def register_nodes(remote: str, nodes: list[tuple[str, str, bytes, list[str]]]):
    if not nodes:
        return
    client = DaemonClient(remote)
    try:
        valid = client.query_valid_paths([p for p, _, _, _ in nodes])
        for path, name, content, refs in nodes:
            if path in valid:
                continue
            got = client.add_text_to_store(name, content, refs)
            if got != path:
                raise RuntimeError(f"daemon returned {got}, expected {path}")
            valid.add(path)
    finally:
        client.close()


# --- PEP 503 / PEP 508 / PEP 425 helpers ---


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def sanitize_drv_name(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9+._?=-]", "-", name).lstrip(".")
    return s or "unknown"


def parse_version_tuple(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v)
    return tuple(int(x) for x in nums) if nums else (0,)


def make_marker_env(system: str) -> dict[str, str]:
    arch, os_name = system.split("-", 1)
    vi = sys.version_info
    return {
        "os_name": "posix",
        "sys_platform": "linux" if os_name == "linux" else os_name,
        "platform_machine": arch,
        "platform_system": "Linux" if os_name == "linux" else os_name,
        "platform_python_implementation": "CPython",
        "implementation_name": "cpython",
        "python_version": f"{vi.major}.{vi.minor}",
        "python_full_version": f"{vi.major}.{vi.minor}.{vi.micro}",
        "implementation_version": f"{vi.major}.{vi.minor}.{vi.micro}",
        "extra": "",
    }


def eval_marker(expr: str, env: dict[str, str]) -> bool:
    if not expr or not expr.strip():
        return True
    tokens = re.findall(
        r"\(|\)|not\s+in|in|and|or|===|==|!=|<=|>=|<|>|'[^']*'|\"[^\"]*\"|[A-Za-z0-9_.-]+",
        expr,
    )
    pos = 0

    def parse_or() -> bool:
        nonlocal pos
        val = parse_and()
        while pos < len(tokens) and tokens[pos] == "or":
            pos += 1
            rhs = parse_and()
            val = val or rhs
        return val

    def parse_and() -> bool:
        nonlocal pos
        val = parse_atom()
        while pos < len(tokens) and tokens[pos] == "and":
            pos += 1
            rhs = parse_atom()
            val = val and rhs
        return val

    def read_operand() -> tuple[str, bool]:
        nonlocal pos
        tok = tokens[pos]
        pos += 1
        if (tok.startswith("'") and tok.endswith("'")) or (
            tok.startswith('"') and tok.endswith('"')
        ):
            return tok[1:-1], False
        return env.get(tok, tok), tok in (
            "python_version",
            "python_full_version",
            "implementation_version",
        )

    def parse_atom() -> bool:
        nonlocal pos
        if pos < len(tokens) and tokens[pos] == "(":
            pos += 1
            val = parse_or()
            if pos < len(tokens) and tokens[pos] == ")":
                pos += 1
            return val
        lhs, lhs_ver = read_operand()
        if pos >= len(tokens):
            return bool(lhs)
        op = tokens[pos]
        pos += 1
        rhs, rhs_ver = read_operand()
        is_ver = lhs_ver or rhs_ver
        if op in ("==", "==="):
            return (
                parse_version_tuple(lhs) == parse_version_tuple(rhs)
                if is_ver
                else lhs == rhs
            )
        if op == "!=":
            return (
                parse_version_tuple(lhs) != parse_version_tuple(rhs)
                if is_ver
                else lhs != rhs
            )
        if op == "<":
            return (
                parse_version_tuple(lhs) < parse_version_tuple(rhs)
                if is_ver
                else lhs < rhs
            )
        if op == "<=":
            return (
                parse_version_tuple(lhs) <= parse_version_tuple(rhs)
                if is_ver
                else lhs <= rhs
            )
        if op == ">":
            return (
                parse_version_tuple(lhs) > parse_version_tuple(rhs)
                if is_ver
                else lhs > rhs
            )
        if op == ">=":
            return (
                parse_version_tuple(lhs) >= parse_version_tuple(rhs)
                if is_ver
                else lhs >= rhs
            )
        if op == "in":
            return lhs in rhs
        if op.replace(" ", "") == "notin":
            return lhs not in rhs
        return False

    return parse_or()


def select_wheel(wheels: list[dict], system: str) -> dict | None:
    arch, os_name = system.split("-", 1)
    if os_name != "linux":
        return None
    vi = sys.version_info
    cp_tag = f"cp{vi.major}{vi.minor}"
    py_tag = f"py{vi.major}"
    py_minor_tag = f"py{vi.major}{vi.minor}"

    # Lower score = better match.
    best = None
    best_score = (999, 999, 999)

    for w in wheels:
        fname = w["url"].rsplit("/", 1)[-1]
        if not fname.endswith(".whl"):
            continue
        stem = fname[:-4]
        parts = stem.split("-")
        if len(parts) < 5:
            continue
        py_tags = parts[-3].split(".")
        abi_tags = parts[-2].split(".")
        plat_tags = parts[-1].split(".")

        # Check python + abi compatibility.
        py_abi_rank = None
        if cp_tag in py_tags and cp_tag in abi_tags:
            py_abi_rank = 0
        elif any(t.startswith("cp3") and int(t[3:] or "0") <= vi.minor for t in py_tags if t[3:].isdigit()) and "abi3" in abi_tags:
            py_abi_rank = 1
        elif (py_minor_tag in py_tags or py_tag in py_tags) and "none" in abi_tags:
            py_abi_rank = 2
        if py_abi_rank is None:
            continue

        # Check platform compatibility (reject musllinux on glibc NixOS).
        plat_rank = None
        for pt in plat_tags:
            if pt == "any":
                plat_rank = min(plat_rank, 10) if plat_rank is not None else 10
            elif pt.startswith("manylinux") and pt.endswith(f"_{arch}"):
                # Prefer manylinux_2_28 / manylinux2014 (glibc 2.42 supports all <= 2.42).
                m = re.match(r"manylinux_(\d+)_(\d+)_", pt)
                if m:
                    maj, minor = int(m.group(1)), int(m.group(2))
                    if (maj, minor) <= (2, 42):
                        r = 2
                        plat_rank = min(plat_rank, r) if plat_rank is not None else r
                elif pt.startswith(("manylinux2014_", "manylinux2010_", "manylinux1_")):
                    r = 3
                    plat_rank = min(plat_rank, r) if plat_rank is not None else r
            elif pt == f"linux_{arch}":
                plat_rank = min(plat_rank, 4) if plat_rank is not None else 4

        if plat_rank is None:
            continue
        score = (py_abi_rank, plat_rank, len(fname))
        if score < best_score:
            best_score = score
            best = w
    return best


def verify_lock_matches_manifests(root: pathlib.Path, lock: dict):
    """Verify workspace manifests agree with uv.lock without invoking uv."""
    if lock.get("version") != 1:
        raise RuntimeError(f"unsupported uv.lock version: {lock.get('version')}")
    for pkg in lock.get("package", []):
        src_info = pkg.get("source", {})
        rel = src_info.get("editable") or src_info.get("directory")
        if rel is None:
            continue
        member_dir = root if rel == "." else root / rel
        pp_path = member_dir / "pyproject.toml"
        if not pp_path.is_file():
            continue
        pp = tomllib.loads(pp_path.read_text())
        proj = pp.get("project", {})
        if "name" in proj and normalize_name(proj["name"]) != normalize_name(pkg["name"]):
            raise RuntimeError(
                f"uv.lock mismatch: {pp_path} declares {proj['name']}, lock has {pkg['name']}"
            )
        if "version" in proj and proj["version"] != pkg.get("version"):
            raise RuntimeError(
                f"uv.lock mismatch: {pp_path} version {proj['version']} != lock {pkg.get('version')}"
            )
        meta = pkg.get("metadata", {})
        if "dependencies" in proj and "requires-dist" in meta:
            locked_names = {normalize_name(r["name"]) for r in meta["requires-dist"]}
            for dep in proj["dependencies"]:
                base = re.split(r"[\s\[<>=!~;@]", dep.strip(), maxsplit=1)[0]
                if base and normalize_name(base) not in locked_names:
                    raise RuntimeError(
                        f"uv.lock out of sync: {proj.get('name')} requires {base} not in uv.lock"
                    )


# --- Planner ---


def internal_plan():
    out_file = os.environ["out"]
    system = os.environ["system"]
    remote = os.environ["NIX_REMOTE"]
    src = pathlib.Path(os.environ["src"])
    member_path = os.environ["memberPath"]
    python_out = os.environ["pythonOut"]
    script_path = os.environ["drvpyScript"]
    patchelf_out = os.environ.get("patchelfOut", "")
    glibc_out = os.environ["glibcOut"]
    gcc_lib_out = os.environ["gccLibOut"]
    test_groups_flag = os.environ.get("testGroups") == "1"
    dependencies_arg = json.loads(os.environ.get("dependenciesJson", "null"))

    sdir = store_dir()
    py_bin = f"{python_out}/bin/python3"
    out_ph = hash_placeholder("out")

    project = src if member_path == "." else src / member_path
    root = src if (src / "uv.lock").is_file() else project
    lock_path = root / "uv.lock"
    if not lock_path.is_file():
        raise RuntimeError("drvpy requires a checked-in uv.lock")

    lock = tomllib.loads(lock_path.read_text())
    verify_lock_matches_manifests(root, lock)

    manifest = tomllib.loads((project / "pyproject.toml").read_text())
    root_manifest = tomllib.loads((root / "pyproject.toml").read_text())

    # Index packages in uv.lock by normalized name.
    by_name: dict[str, list[dict]] = {}
    workspace_members: dict[str, tuple[dict, pathlib.Path]] = {}
    for p in lock.get("package", []):
        norm = normalize_name(p["name"])
        by_name.setdefault(norm, []).append(p)
        src_info = p.get("source", {})
        rel = src_info.get("editable") or src_info.get("directory")
        if rel is not None:
            mdir = root if rel == "." else root / rel
            workspace_members[norm] = (p, mdir)

    if member_path == ".":
        if "project" in manifest and "name" in manifest["project"]:
            root_norm = normalize_name(manifest["project"]["name"])
            selected_members = [root_norm] if root_norm in workspace_members else sorted(workspace_members)
        else:
            selected_members = sorted(workspace_members)
    else:
        member_norm = normalize_name(manifest["project"]["name"])
        if member_norm not in workspace_members:
            raise RuntimeError(f"drvpy: selected project {member_norm} is absent from the uv workspace")
        selected_members = [member_norm]

    # Determine dependency groups per selected member (matching drvpy's contract).
    if dependencies_arg is not None:
        dep_spec = {normalize_name(k): list(v) for k, v in dependencies_arg.items()}
    else:
        dep_spec = {}
        for m in selected_members:
            pkg, _ = workspace_members[m]
            dev_groups = set((pkg.get("dev-dependencies") or {}).keys())
            opt_groups = set((pkg.get("optional-dependencies") or {}).keys())
            all_groups = dev_groups | opt_groups
            if not test_groups_flag:
                dep_spec[m] = []
            else:
                tgroups = [g for g in ("test", "tests") if g in all_groups]
                if tgroups:
                    dep_spec[m] = tgroups
                elif "dev" in all_groups:
                    dep_spec[m] = ["dev"]
                else:
                    dep_spec[m] = []

    marker_env = make_marker_env(system)

    # Resolve reachable packages via BFS respecting PEP 508 markers & groups.
    resolved: dict[tuple[str, str], dict] = {}
    queue: list[tuple[dict, list[str]]] = []
    for m, groups in dep_spec.items():
        if m not in workspace_members:
            raise RuntimeError(f"drvpy: unknown dependency target {m}")
        pkg, _ = workspace_members[m]
        queue.append((pkg, groups))

    def match_dep_pkg(dep: dict) -> dict | None:
        dname = normalize_name(dep["name"])
        candidates = by_name.get(dname, [])
        if not candidates:
            return None
        if "version" in dep:
            for c in candidates:
                if c["version"] == dep["version"]:
                    return c
        if "source" in dep:
            for c in candidates:
                if c.get("source") == dep["source"]:
                    return c
        # If multiple versions exist without explicit pin, pick highest compatible.
        return candidates[-1]

    while queue:
        pkg, active_groups = queue.pop(0)
        key = (normalize_name(pkg["name"]), pkg["version"])
        if key in resolved:
            continue
        resolved[key] = pkg

        dep_lists = [pkg.get("dependencies", [])]
        for g in active_groups:
            if g in (pkg.get("dev-dependencies") or {}):
                dep_lists.append(pkg["dev-dependencies"][g])
            if g in (pkg.get("optional-dependencies") or {}):
                dep_lists.append(pkg["optional-dependencies"][g])

        for dlist in dep_lists:
            for dep in dlist:
                marker = dep.get("marker", "")
                if marker and not eval_marker(marker, marker_env):
                    continue
                target = match_dep_pkg(dep)
                if target is None:
                    continue
                extras = list(dep.get("extra", [])) if isinstance(dep.get("extra"), list) else []
                if isinstance(dep.get("extra"), str):
                    extras = [dep["extra"]]
                queue.append((target, extras))

    # Build per-package CA derivations + fetchurl derivations.
    nodes: list[tuple[str, str, bytes, list[str]]] = []
    env_input_drvs: dict[str, list[str]] = {}
    pkg_out_placeholders: list[str] = []

    for (norm, version), pkg in sorted(resolved.items()):
        src_info = pkg.get("source", {})
        rel = src_info.get("editable") or src_info.get("directory")
        drv_base = sanitize_drv_name(f"{norm}-{version}")

        if rel is not None:
            # Local workspace package -> CA derivation building site-packages & entry_points.
            mdir = root if rel == "." else root / rel
            rel_from_src = os.path.relpath(mdir, src)
            pkg_dir_str = str(src) if rel_from_src == "." else f"{src}/{rel_from_src}"
            env = {
                "__contentAddressed": "1",
                "builder": py_bin,
                "name": drv_base,
                "out": out_ph,
                "outputHashAlgo": "sha256",
                "outputHashMode": "recursive",
                "system": system,
                "PKG_DIR": pkg_dir_str,
                "PKG_NAME": pkg["name"],
                "PKG_VERSION": version,
            }
            srcs = {python_out, script_path, str(src)}
            aterm = format_derivation(
                [("out", "", "r:sha256", "")],
                {},
                srcs,
                system,
                py_bin,
                [script_path, "build-local"],
                env,
            )
            refs = sorted(srcs)
            drv_path = make_text_path(sdir, f"{drv_base}.drv", aterm, refs)
            nodes.append((drv_path, f"{drv_base}.drv", aterm, refs))
            env_input_drvs[drv_path] = ["out"]
            pkg_out_placeholders.append(downstream_placeholder(drv_path, "out"))
        else:
            # Registry package -> builtin:fetchurl wheel + CA unpack derivation.
            wheels = pkg.get("wheels", [])
            wheel = select_wheel(wheels, system)
            if wheel is None:
                raise RuntimeError(
                    f"drvpy: no compatible wheel for {pkg['name']}=={version} on {system}"
                )
            whl_name = sanitize_drv_name(wheel["url"].rsplit("/", 1)[-1])
            sha256_hex = wheel["hash"].removeprefix("sha256:")
            fetch_drv, fetch_out, fetch_aterm, fetch_refs = make_fetchurl_drv(
                sdir, whl_name, wheel["url"], sha256_hex
            )
            nodes.append((fetch_drv, f"{whl_name}.drv", fetch_aterm, fetch_refs))

            is_plat_wheel = not whl_name.endswith("-any.whl")
            srcs = {python_out, script_path}
            env = {
                "__contentAddressed": "1",
                "builder": py_bin,
                "name": drv_base,
                "out": out_ph,
                "outputHashAlgo": "sha256",
                "outputHashMode": "recursive",
                "system": system,
                "WHEEL_PATH": fetch_out,
                "PKG_NAME": pkg["name"],
                "PKG_VERSION": version,
            }
            if is_plat_wheel and patchelf_out:
                srcs.add(patchelf_out)
                srcs.add(glibc_out)
                srcs.add(gcc_lib_out)
                env["PATCHELF"] = f"{patchelf_out}/bin/patchelf"
                env["RPATH"] = f"{glibc_out}/lib:{gcc_lib_out}/lib"

            idrvs = {fetch_drv: ["out"]}
            aterm = format_derivation(
                [("out", "", "r:sha256", "")],
                idrvs,
                srcs,
                system,
                py_bin,
                [script_path, "unpack-wheel"],
                env,
            )
            refs = sorted(srcs | set(idrvs.keys()))
            drv_path = make_text_path(sdir, f"{drv_base}.drv", aterm, refs)
            nodes.append((drv_path, f"{drv_base}.drv", aterm, refs))
            env_input_drvs[drv_path] = ["out"]
            pkg_out_placeholders.append(downstream_placeholder(drv_path, "out"))

    # Determine drvpy-run target and drvpy-test-roots.
    scripts = sorted((manifest.get("project", {}).get("scripts") or {}).keys())
    default_script = scripts[0] if len(scripts) == 1 else "python"

    ws_member_patterns = (
        root_manifest.get("tool", {}).get("uv", {}).get("workspace", {}).get("members") or []
    )
    test_roots = []
    if member_path == "." and ws_member_patterns:
        for pat in ws_member_patterns:
            for hit in sorted(glob.glob(str(root / pat))):
                hp = pathlib.Path(hit)
                if hp.is_dir() and (hp / "pyproject.toml").is_file():
                    test_roots.append("./" + os.path.relpath(hp, root))
    if not test_roots:
        test_roots = ["."]

    # Final CA virtualenv derivation.
    env_srcs = {python_out, script_path}
    env_vars = {
        "__contentAddressed": "1",
        "builder": py_bin,
        "name": "drvpy-env",
        "out": out_ph,
        "outputHashAlgo": "sha256",
        "outputHashMode": "recursive",
        "system": system,
        "PYTHON_OUT": python_out,
        "PKG_DIRS": "\n".join(pkg_out_placeholders),
        "DEFAULT_SCRIPT": default_script,
        "TEST_ROOTS": "\n".join(test_roots),
    }
    final_aterm = format_derivation(
        [("out", "", "r:sha256", "")],
        env_input_drvs,
        env_srcs,
        system,
        py_bin,
        [script_path, "build-venv"],
        env_vars,
    )

    register_nodes(remote, nodes)
    pathlib.Path(out_file).write_bytes(final_aterm)


# --- Package / Wheel / Venv Builders ---


def site_packages_rel() -> str:
    return f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"


def build_local():
    out = pathlib.Path(os.environ["out"])
    pkg_dir = pathlib.Path(os.environ["PKG_DIR"])
    pkg_name = os.environ["PKG_NAME"]
    pkg_version = os.environ["PKG_VERSION"]

    pp = tomllib.loads((pkg_dir / "pyproject.toml").read_text())
    sp = out / site_packages_rel()
    sp.mkdir(parents=True, exist_ok=True)

    # Locate Python modules/packages according to PEP 621 / hatchling / flit / src-layout.
    copied = False
    hatch_wheel = (
        pp.get("tool", {}).get("hatch", {}).get("build", {}).get("targets", {}).get("wheel", {})
    )
    if hatch_wheel.get("packages"):
        for rel in hatch_wheel["packages"]:
            src_item = pkg_dir / rel
            if src_item.is_dir():
                shutil.copytree(src_item, sp / src_item.name, dirs_exist_ok=True)
                copied = True
            elif src_item.is_file():
                shutil.copy2(src_item, sp / src_item.name)
                copied = True
    elif hatch_wheel.get("include"):
        for pat in hatch_wheel["include"]:
            for hit in sorted(glob.glob(str(pkg_dir / pat.lstrip("/")))):
                hp = pathlib.Path(hit)
                if hp.is_dir():
                    shutil.copytree(hp, sp / hp.name, dirs_exist_ok=True)
                    copied = True
                elif hp.is_file():
                    shutil.copy2(hp, sp / hp.name)
                    copied = True

    if not copied:
        flit_mod = pp.get("tool", {}).get("flit", {}).get("module", {}).get("name")
        if flit_mod:
            for base in (pkg_dir / "src", pkg_dir):
                if (base / flit_mod).is_dir():
                    shutil.copytree(base / flit_mod, sp / flit_mod, dirs_exist_ok=True)
                    copied = True
                    break
                if (base / f"{flit_mod}.py").is_file():
                    shutil.copy2(base / f"{flit_mod}.py", sp / f"{flit_mod}.py")
                    copied = True
                    break

    if not copied and (pkg_dir / "src").is_dir():
        for item in sorted((pkg_dir / "src").iterdir()):
            if item.name.startswith(".") or item.name == "__pycache__":
                continue
            if item.is_dir():
                shutil.copytree(item, sp / item.name, dirs_exist_ok=True)
                copied = True
            elif item.suffix == ".py":
                shutil.copy2(item, sp / item.name)
                copied = True

    if not copied:
        mod_name = normalize_name(pkg_name).replace("-", "_")
        if (pkg_dir / mod_name).is_dir():
            shutil.copytree(pkg_dir / mod_name, sp / mod_name, dirs_exist_ok=True)
            copied = True
        elif (pkg_dir / f"{mod_name}.py").is_file():
            shutil.copy2(pkg_dir / f"{mod_name}.py", sp / f"{mod_name}.py")
            copied = True

    # Write deterministic PEP 427 .dist-info metadata.
    dist_name = re.sub(r"[-_.]+", "_", pkg_name)
    dist_info = sp / f"{dist_name}-{pkg_version}.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)

    proj = pp.get("project", {})
    meta_lines = [
        "Metadata-Version: 2.1",
        f"Name: {proj.get('name', pkg_name)}",
        f"Version: {pkg_version}",
    ]
    if proj.get("description"):
        meta_lines.append(f"Summary: {proj['description']}")
    for dep in proj.get("dependencies", []):
        meta_lines.append(f"Requires-Dist: {dep}")
    (dist_info / "METADATA").write_text("\n".join(meta_lines) + "\n")
    (dist_info / "WHEEL").write_text(
        "Wheel-Version: 1.0\nGenerator: drvpy\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )

    ep_sections: dict[str, dict[str, str]] = {}
    if proj.get("scripts"):
        ep_sections["console_scripts"] = dict(proj["scripts"])
    if proj.get("gui-scripts"):
        ep_sections["gui_scripts"] = dict(proj["gui-scripts"])
    for grp, mapping in (proj.get("entry-points") or {}).items():
        if isinstance(mapping, dict):
            ep_sections[grp] = dict(mapping)
    if ep_sections:
        lines = []
        for sec, mapping in sorted(ep_sections.items()):
            lines.append(f"[{sec}]")
            for k, v in sorted(mapping.items()):
                lines.append(f"{k} = {v}")
            lines.append("")
        (dist_info / "entry_points.txt").write_text("\n".join(lines))

    # Strip any __pycache__ for deterministic CA outputs.
    for pc in sp.rglob("__pycache__"):
        shutil.rmtree(pc, ignore_errors=True)


def unpack_wheel():
    out = pathlib.Path(os.environ["out"])
    whl_path = pathlib.Path(os.environ["WHEEL_PATH"])
    patchelf = os.environ.get("PATCHELF", "")
    rpath = os.environ.get("RPATH", "")

    sp = out / site_packages_rel()
    sp.mkdir(parents=True, exist_ok=True)
    bin_dir = out / "bin"

    so_files: list[pathlib.Path] = []
    with zipfile.ZipFile(whl_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            # Handle PEP 427 <dist>-<ver>.data/{purelib,platlib,scripts,data}/...
            parts = name.split("/")
            if len(parts) >= 3 and parts[0].endswith(".data"):
                cat = parts[1]
                sub = pathlib.PurePosixPath(*parts[2:])
                if cat in ("purelib", "platlib"):
                    dest = sp / sub
                elif cat == "scripts":
                    dest = bin_dir / sub
                else:
                    dest = out / sub
            else:
                dest = sp / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            data = zf.read(info)
            dest.write_bytes(data)
            unix_mode = (info.external_attr >> 16) & 0o777
            if unix_mode & 0o111:
                dest.chmod(unix_mode | 0o555)
            if dest.suffix == ".so" or ".so." in dest.name:
                so_files.append(dest)

    if so_files and patchelf and rpath:
        for so in so_files:
            so.chmod(so.stat().st_mode | 0o200)
            subprocess.run([patchelf, "--add-rpath", rpath, str(so)], check=True)


def parse_entry_points(ep_path: pathlib.Path) -> list[tuple[str, str]]:
    cp = configparser.ConfigParser()
    cp.optionxform = str  # preserve case
    cp.read(ep_path)
    out = []
    for sec in ("console_scripts", "gui_scripts"):
        if cp.has_section(sec):
            for name, target in cp.items(sec):
                out.append((name, target))
    return out


def build_venv():
    out = pathlib.Path(os.environ["out"])
    python_out = pathlib.Path(os.environ["PYTHON_OUT"])
    pkg_dirs = [pathlib.Path(p) for p in os.environ.get("PKG_DIRS", "").splitlines() if p]
    default_script = os.environ.get("DEFAULT_SCRIPT", "python")
    test_roots = [r for r in os.environ.get("TEST_ROOTS", ".").splitlines() if r]

    bin_dir = out / "bin"
    sp = out / site_packages_rel()
    bin_dir.mkdir(parents=True, exist_ok=True)
    sp.mkdir(parents=True, exist_ok=True)

    # PEP 405 virtual environment layout.
    (out / "pyvenv.cfg").write_text(
        f"home = {python_out}/bin\ninclude-system-site-packages = false\n"
    )
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    py_target = python_out / "bin" / f"python{py_ver}"
    if not py_target.exists():
        py_target = python_out / "bin" / "python3"
    os.symlink(py_target, bin_dir / "python")
    os.symlink("python", bin_dir / "python3")
    os.symlink("python", bin_dir / f"python{py_ver}")

    venv_py = f"{out}/bin/python"
    console_scripts: dict[str, str] = {}

    for pdir in pkg_dirs:
        psp = pdir / site_packages_rel()
        if psp.is_dir():
            for item in sorted(psp.iterdir()):
                dest = sp / item.name
                if not dest.exists() and not dest.is_symlink():
                    os.symlink(item, dest)
                elif dest.is_symlink() and item.is_dir() and dest.is_dir():
                    # Merge namespace packages (e.g. sphinxcontrib/) into a real dir.
                    prev = pathlib.Path(os.readlink(dest))
                    dest.unlink()
                    dest.mkdir()
                    for sub in sorted(prev.iterdir()):
                        os.symlink(sub, dest / sub.name)
                    for sub in sorted(item.iterdir()):
                        if not (dest / sub.name).exists():
                            os.symlink(sub, dest / sub.name)
                elif dest.is_dir() and not dest.is_symlink() and item.is_dir():
                    for sub in sorted(item.iterdir()):
                        if not (dest / sub.name).exists():
                            os.symlink(sub, dest / sub.name)
                if item.name.endswith(".dist-info"):
                    ep = item / "entry_points.txt"
                    if ep.is_file():
                        for sname, target in parse_entry_points(ep):
                            console_scripts[sname] = target

        pbin = pdir / "bin"
        if pbin.is_dir():
            for script in sorted(pbin.iterdir()):
                dest = bin_dir / script.name
                if dest.exists():
                    continue
                data = script.read_bytes()
                if data.startswith(b"#!") and b"python" in data.splitlines()[0]:
                    rest = data.split(b"\n", 1)[1] if b"\n" in data else b""
                    dest.write_bytes(f"#!{venv_py}\n".encode() + rest)
                    dest.chmod(0o755)
                else:
                    os.symlink(script, dest)

    for sname, target in sorted(console_scripts.items()):
        dest = bin_dir / sname
        if dest.exists():
            continue
        mod, attr = target.split(":", 1)
        # Handle dotted attributes like "cli:main" or "pkg.mod:Class.method"
        parts = attr.split(".")
        body = (
            f"#!{venv_py}\n"
            "import sys\n"
            f"from {mod} import {parts[0]}\n"
            "if __name__ == '__main__':\n"
            f"    sys.exit({'.'.join(parts)}())\n"
        )
        dest.write_text(body)
        dest.chmod(0o755)

    nix_support = out / "nix-support"
    nix_support.mkdir(parents=True, exist_ok=True)
    os.symlink(f"../bin/{default_script}", nix_support / "drvpy-run")
    if test_roots != ["."]:
        (nix_support / "drvpy-test-roots").write_bytes(
            b"".join(r.encode() + b"\x00" for r in test_roots)
        )


def run_unit_tests():
    # Verify exact <nix/fetchurl.nix> store path for idna-3.10-py3-none-any.whl!
    drv, out, _, _ = make_fetchurl_drv(
        "/nix/store",
        "idna-3.10-py3-none-any.whl",
        "https://files.pythonhosted.org/packages/76/c6/c88e154df9c4e1a2a66ccf0005a88dfb2650c1dffb6f5ce603dfbd452ce3/idna-3.10-py3-none-any.whl",
        "946d195a0d259cbba61165e88e65941f16e9b36ea6ddb97f00452bae8b1287d3",
    )
    assert out == "/nix/store/q32ggr2b310j4k98fg5s7gc3pw41v48l-idna-3.10-py3-none-any.whl", out

    env = make_marker_env("x86_64-linux")
    assert eval_marker("python_full_version >= '3.10'", env)
    assert not eval_marker("python_full_version < '3.10'", env)
    assert not eval_marker("sys_platform == 'win32'", env)
    assert eval_marker("sys_platform == 'linux' and platform_machine == 'x86_64'", env)
    assert eval_marker("(sys_platform == 'win32' or python_version >= '3.11')", env)
    print("drvpy unit tests passed!")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "internal-plan":
        internal_plan()
    elif cmd == "internal-symlink":
        os.symlink(sys.argv[2], sys.argv[3])
    elif cmd == "build-local":
        build_local()
    elif cmd == "unpack-wheel":
        unpack_wheel()
    elif cmd == "build-venv":
        build_venv()
    elif cmd == "self-test":
        run_unit_tests()
    else:
        raise SystemExit(f"unknown drvpy command: {cmd!r}")


if __name__ == "__main__":
    main()
