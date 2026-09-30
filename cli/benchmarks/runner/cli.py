"""Command-line entry point for trusted local benchmark collection and analysis."""

from __future__ import annotations

import argparse
import copy
import ctypes
import errno
import hashlib
import json
import os
import platform
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from runner.rig import CommandDriver, canonical_json_bytes, run_benchmark, summarize


BENCHMARKS = Path(__file__).resolve().parents[1]
CLI_ROOT = BENCHMARKS.parent
DEFAULT_PROFILE = BENCHMARKS / "policy" / "local-smoke.profile.json"
DEFAULT_EVIDENCE = BENCHMARKS / "evidence" / "local-smoke-example"
PROFILE_SCHEMA = BENCHMARKS / "schemas" / "benchmark-profile.schema.json"
POLICY_SCHEMA = CLI_ROOT / "shared" / "schemas" / "benchmark-policy.schema.json"
COMMON_SCHEMA = CLI_ROOT / "shared" / "schemas" / "common.schema.json"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="OpenProse trusted provider-free benchmark rig"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser(
        "run", help="collect raw trials and derive a summary"
    )
    run_parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    run_parser.add_argument("--output-dir", type=Path, default=DEFAULT_EVIDENCE)
    run_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="accepted only for an absent destination; existing evidence is immutable",
    )
    verify_parser = subparsers.add_parser(
        "verify", help="validate frozen inputs and digests"
    )
    verify_parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    analyze_parser = subparsers.add_parser(
        "analyze", help="reproduce a summary from raw evidence"
    )
    analyze_parser.add_argument("--raw", type=Path, required=True)
    analyze_parser.add_argument("--policy", type=Path, required=True)
    analyze_parser.add_argument("--summary", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "verify":
            prepared = load_and_verify(arguments.profile)
            print(
                json.dumps(
                    {
                        "schema": "openprose.benchmark-input-verification/1",
                        "status": "pass",
                        "profile": prepared["profile"]["profileId"],
                        "policy": prepared["policy"]["policyId"],
                        "providerCallsMade": False,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "analyze":
            raw_bytes = arguments.raw.read_bytes()
            raw = json.loads(raw_bytes)
            raw["_rawArtifactSha256"] = hashlib.sha256(raw_bytes).hexdigest()
            policy = _load_json(arguments.policy)
            summary = summarize(raw, policy)
            _write_atomic_file(
                arguments.summary, canonical_json_bytes(summary), replace=True
            )
            return 0
        return collect(
            arguments.profile, arguments.output_dir, overwrite=arguments.overwrite
        )
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(
            json.dumps(
                {
                    "schema": "openprose.benchmark-error/1",
                    "code": "BENCHMARK_ADMISSION_FAILED",
                    "message": str(error),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


def collect(profile_path: Path, output_dir: Path, *, overwrite: bool) -> int:
    _assert_new_evidence_destination(output_dir, overwrite=overwrite)
    prepared = load_and_verify(profile_path)
    with tempfile.TemporaryDirectory(prefix="openprose-benchmark-") as temporary_text:
        temporary = Path(temporary_text)
        custody = _snapshot_execution_inputs(prepared, temporary / "execution-custody")
        workspace = temporary / "workspace"
        config = temporary / "config"
        home = temporary / "home"
        cache = temporary / "cache"
        for directory in (workspace, config, home, cache):
            directory.mkdir()
        fixture = prepared["fixture"]
        environment = {
            "PATH": os.defpath,
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(config),
            "XDG_CACHE_HOME": str(cache),
            "OPENPROSE_CONFORMANCE_FAKE_HARNESS": str(fixture),
            "OPENPROSE_CONFORMANCE_FAKE_SCENARIO": str(
                prepared["profile"]["fixture"]["scenario"]
            ),
        }
        if os.name == "nt":
            for name in ("SystemRoot", "WINDIR"):
                if name in os.environ:
                    environment[name] = os.environ[name]
        machine = qualify_machine(prepared["policy"])
        raw = run_benchmark(
            prepared["profile"],
            prepared["policy"],
            CommandDriver(),
            machine=machine,
            context={
                "cwd": str(workspace),
                "environment": environment,
                "redactionLiterals": [
                    str(temporary),
                    str(profile_path.resolve().parents[3]),
                ],
            },
        )
        final_custody = _reauthenticate_execution_inputs(custody)
    raw["inputVerification"] = {
        **prepared["verification"],
        "executionCustody": final_custody,
    }
    raw_bytes = canonical_json_bytes(raw)
    raw_digest = hashlib.sha256(raw_bytes).hexdigest()
    analysis_input = copy.deepcopy(raw)
    analysis_input["_rawArtifactSha256"] = raw_digest
    summary = summarize(analysis_input, prepared["policy"])
    summary_bytes = canonical_json_bytes(summary)
    manifest = {
        "schema": "openprose.benchmark-evidence-manifest/1",
        "releaseEligible": False,
        "semanticStatus": prepared["profile"]["semanticStatus"],
        "sentinelOnly": True,
        "providerCallsMade": False,
        "artifacts": [
            {"path": "raw.json", "sha256": raw_digest, "bytes": len(raw_bytes)},
            {
                "path": "summary.json",
                "sha256": hashlib.sha256(summary_bytes).hexdigest(),
                "bytes": len(summary_bytes),
            },
        ],
    }
    _publish_evidence_directory(
        output_dir,
        {
            "raw.json": raw_bytes,
            "summary.json": summary_bytes,
            "manifest.json": canonical_json_bytes(manifest),
        },
        overwrite=overwrite,
    )
    print(
        json.dumps(
            {
                "schema": "openprose.benchmark-run-completed/1",
                "output": str(output_dir),
                "plannedTrials": raw["plannedTrials"],
                "failures": sum(
                    trial["status"] != "success" for trial in raw["trials"]
                ),
                "releaseEligible": False,
                "semanticStatus": prepared["profile"]["semanticStatus"],
                "providerCallsMade": False,
            },
            sort_keys=True,
        )
    )
    return 0


def load_profile_contract(profile_path: Path) -> dict[str, Any]:
    """Validate frozen declarations without consulting mutable build outputs.

    Checked evidence binds the exact bytes that were admitted when it was
    collected. A later Cargo or Bun gate may legitimately replace a shared
    development output path, so evidence verification must compare recorded
    identities with this frozen contract rather than relabeling current bytes.
    Collection admission remains the stricter load_and_verify operation below.
    """

    profile_path = profile_path.resolve()
    original_profile = _load_json(profile_path)
    _validate_schema(original_profile, _load_json(PROFILE_SCHEMA))
    policy_path = (profile_path.parent / original_profile["policyArtifact"]).resolve()
    policy_bytes = policy_path.read_bytes()
    if _sha256(policy_bytes) != original_profile["policyArtifactSha256"]:
        raise ValueError("frozen policy digest mismatch")
    policy_schema_bytes = POLICY_SCHEMA.read_bytes()
    if _sha256(policy_schema_bytes) != original_profile["benchmarkPolicySchemaSha256"]:
        raise ValueError("shared benchmark-policy schema digest mismatch")
    policy = json.loads(policy_bytes)
    _validate_policy(policy)

    profile = copy.deepcopy(original_profile)
    profile["_profileArtifactSha256"] = _sha256(profile_path.read_bytes())
    verified_artifacts: list[dict[str, Any]] = []
    for identity, path_key, digest_key in (
        (profile["image"], "manifestArtifact", "manifestSha256"),
        (profile["program"], "artifact", "sha256"),
        (profile["validator"], "artifact", "sha256"),
    ):
        artifact = (profile_path.parent / identity[path_key]).resolve()
        digest = _sha256(artifact.read_bytes())
        if digest != identity[digest_key]:
            raise ValueError(f"digest mismatch for frozen input: {artifact.name}")
        verified_artifacts.append({"path": _portable_path(artifact), "sha256": digest})
    image_manifest = _load_json(
        (profile_path.parent / profile["image"]["manifestArtifact"]).resolve()
    )
    aggregate = image_manifest.get("aggregateSha256", {})
    if aggregate.get("sha256") != profile["image"]["sha256"]:
        raise ValueError("image content digest differs from frozen profile")
    if image_manifest.get("releaseEligible") is not False:
        raise ValueError("local smoke accepts only the non-release sentinel image")

    fixture = (profile_path.parent / profile["fixture"]["path"]).resolve()
    fixture_bytes = _read_admitted_file(
        fixture,
        expected_sha256=profile["fixture"]["sha256"],
        expected_bytes=profile["fixture"]["bytes"],
        label="fixture",
        require_executable=True,
    )
    fixture_identity = {
        "path": _portable_path(fixture),
        "sha256": _sha256(fixture_bytes),
        "bytes": len(fixture_bytes),
    }
    # Admission uses resolved private paths above; exported identities are portable.
    for identity, path_key in (
        (profile["image"], "manifestArtifact"),
        (profile["program"], "artifact"),
        (profile["validator"], "artifact"),
    ):
        identity[path_key] = _portable_path((profile_path.parent / identity[path_key]).resolve())
    for target in profile["targets"]:
        artifact = (profile_path.parent / target["artifact"]).resolve()
        target["_sourceArtifact"] = str(artifact)
        target["artifact"] = _portable_path(artifact)
    verification_contract = {
        "policySchema": {
            "path": "cli/shared/schemas/benchmark-policy.schema.json",
            "sha256": _sha256(policy_schema_bytes),
        },
        "policyArtifactSha256": _sha256(policy_bytes),
        "profileArtifactSha256": profile["_profileArtifactSha256"],
        "frozenInputs": verified_artifacts,
        "fixtureArtifact": fixture_identity,
        "targetArtifacts": [
            {
                "id": target["id"],
                "sha256": target["artifactSha256"],
                "bytes": target["artifactBytes"],
            }
            for target in profile["targets"]
        ],
        "providerCallsMade": False,
    }
    return {
        "profile": profile,
        "policy": policy,
        "fixture": fixture,
        "verificationContract": verification_contract,
    }


def load_and_verify(profile_path: Path) -> dict[str, Any]:
    """Admit an imminent collection only after verifying live target bytes."""

    prepared = load_profile_contract(profile_path)
    profile = prepared["profile"]
    for target in profile["targets"]:
        _read_admitted_file(
            Path(target["_sourceArtifact"]),
            expected_sha256=target["artifactSha256"],
            expected_bytes=target["artifactBytes"],
            label=f"installed-development artifact {target['id']}",
            require_executable=True,
        )
    verification = {
        "schema": "openprose.benchmark-input-verification/2",
        "status": "pass",
        **prepared["verificationContract"],
    }
    prepared["verification"] = verification
    return prepared


def _snapshot_execution_inputs(
    prepared: dict[str, Any], custody_root: Path
) -> dict[str, Any]:
    """Copy admitted executable inputs to private, read-only run custody."""

    try:
        custody_root.mkdir(mode=0o700)
        targets_root = custody_root / "targets"
        targets_root.mkdir(mode=0o700)
    except OSError as error:
        raise ValueError("cannot create private benchmark execution custody") from error

    fixture_contract = prepared["verificationContract"]["fixtureArtifact"]
    fixture_source = Path(prepared["fixture"])
    fixture_bytes = _read_admitted_file(
        fixture_source,
        expected_sha256=fixture_contract["sha256"],
        expected_bytes=fixture_contract["bytes"],
        label="fixture",
        require_executable=True,
    )
    fixture_snapshot = custody_root / "fixture"
    _write_snapshot(fixture_snapshot, fixture_bytes, 0o500)
    prepared["fixture"] = fixture_snapshot

    target_contracts = {
        item["id"]: item for item in prepared["verificationContract"]["targetArtifacts"]
    }
    private_artifacts: list[dict[str, Any]] = [
        {
            "kind": "fixture",
            "path": fixture_snapshot,
            "sha256": fixture_contract["sha256"],
            "bytes": fixture_contract["bytes"],
            "mode": 0o500,
        }
    ]
    public_targets: list[dict[str, Any]] = []
    for target in prepared["profile"]["targets"]:
        target_id = target["id"]
        contract = target_contracts[target_id]
        source = Path(target["_sourceArtifact"])
        artifact_bytes = _read_admitted_file(
            source,
            expected_sha256=contract["sha256"],
            expected_bytes=contract["bytes"],
            label=f"installed-development artifact {target_id}",
            require_executable=True,
        )
        snapshot = targets_root / target_id
        _write_snapshot(snapshot, artifact_bytes, 0o500)
        target["_executionArtifact"] = str(snapshot)
        identity = {
            "id": target_id,
            "sha256": contract["sha256"],
            "bytes": contract["bytes"],
        }
        public_targets.append(identity)
        private_artifacts.append(
            {
                "kind": f"target {target_id}",
                "path": snapshot,
                "sha256": contract["sha256"],
                "bytes": contract["bytes"],
                "mode": 0o500,
            }
        )

    os.chmod(targets_root, 0o500)
    os.chmod(custody_root, 0o500)
    return {
        "schema": "openprose.benchmark-execution-custody/1",
        "status": "snapshot-admitted",
        "mechanism": "private-read-only-snapshots",
        "initialAdmission": "pass",
        "fixtureArtifact": {
            "sha256": fixture_contract["sha256"],
            "bytes": fixture_contract["bytes"],
        },
        "targetArtifacts": public_targets,
        "_root": custody_root,
        "_targetsRoot": targets_root,
        "_artifacts": private_artifacts,
    }


def _reauthenticate_execution_inputs(custody: Mapping[str, Any]) -> dict[str, Any]:
    """Fail unless every snapshot remains the exact initially admitted file."""

    root = Path(custody["_root"])
    targets_root = Path(custody["_targetsRoot"])
    for directory in (root, targets_root):
        try:
            details = directory.lstat()
        except OSError as error:
            raise ValueError("execution snapshot custody directory changed") from error
        if not stat.S_ISDIR(details.st_mode) or stat.S_IMODE(details.st_mode) != 0o500:
            raise ValueError("execution snapshot custody directory changed")
    for artifact in custody["_artifacts"]:
        _read_admitted_file(
            Path(artifact["path"]),
            expected_sha256=artifact["sha256"],
            expected_bytes=artifact["bytes"],
            expected_mode=artifact["mode"],
            label=f"execution snapshot {artifact['kind']}",
        )
    return {
        str(key): value
        for key, value in custody.items()
        if not str(key).startswith("_")
    } | {"status": "pass", "finalReauthentication": "pass"}


def _read_admitted_file(
    path: Path,
    *,
    expected_sha256: str,
    expected_bytes: int,
    label: str,
    expected_mode: int | None = None,
    require_executable: bool = False,
) -> bytes:
    try:
        path_details = path.lstat()
    except OSError as error:
        raise ValueError(f"{label} is unavailable or is not a regular file") from error
    if not stat.S_ISREG(path_details.st_mode):
        raise ValueError(f"{label} is unavailable or is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(f"{label} is unavailable or is not a regular file") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{label} is unavailable or is not a regular file")
        if (path_details.st_dev, path_details.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError(f"{label} changed while being admitted")
        if require_executable and os.name != "nt" and before.st_mode & 0o111 == 0:
            raise ValueError(f"{label} is not executable")
        if expected_mode is not None and stat.S_IMODE(before.st_mode) != expected_mode:
            raise ValueError(f"{label} mode changed")
        chunks: list[bytes] = []
        observed = 0
        while True:
            chunk = os.read(descriptor, min(1_048_576, expected_bytes + 1 - observed))
            if not chunk:
                break
            chunks.append(chunk)
            observed += len(chunk)
            if observed > expected_bytes:
                break
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
        stat.S_IMODE(before.st_mode),
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        stat.S_IMODE(after.st_mode),
    ):
        raise ValueError(f"{label} changed while being admitted")
    value = b"".join(chunks)
    if len(value) != expected_bytes:
        raise ValueError(f"{label} size mismatch")
    if _sha256(value) != expected_sha256:
        raise ValueError(f"{label} digest mismatch")
    return value


def _write_snapshot(path: Path, value: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(value)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise ValueError("cannot write execution snapshot")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, mode)


def qualify_machine(policy: Mapping[str, Any]) -> dict[str, Any]:
    cores = os.cpu_count() or 1
    try:
        load = list(os.getloadavg())
    except (AttributeError, OSError):
        load = [None, None, None]
    one_minute_per_core = None if load[0] is None else float(load[0]) / cores
    threshold = float(policy["machineQualification"]["maximumLoadAveragePerCore"])
    memory = None
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        memory = int(pages * page_size)
    except (AttributeError, OSError, ValueError):
        pass
    qualified = one_minute_per_core is not None and one_minute_per_core <= threshold
    return {
        "os": platform.system(),
        "osRelease": platform.release(),
        "architecture": platform.machine(),
        "cpu": platform.processor() or platform.machine(),
        "logicalCores": cores,
        "memoryBytes": memory,
        "loadAverage": load,
        "loadAveragePerCore1m": one_minute_per_core,
        "maximumLoadAveragePerCore": threshold,
        "qualified": qualified,
        "qualificationReason": "within-load-budget"
        if qualified
        else "load-unavailable-or-over-budget",
    }


def _validate_policy(policy: Mapping[str, Any]) -> None:
    schema = _load_json(POLICY_SCHEMA)
    common = _load_json(COMMON_SCHEMA)
    try:
        from jsonschema import Draft202012Validator, FormatChecker
        from referencing import Registry, Resource
    except ImportError as error:
        raise ValueError(
            "jsonschema and referencing are required to validate benchmark policy"
        ) from error
    registry = Registry().with_resource(common["$id"], Resource.from_contents(common))
    errors = sorted(
        Draft202012Validator(
            schema,
            registry=registry,
            format_checker=FormatChecker(),
        ).iter_errors(policy),
        key=lambda item: list(item.path),
    )
    if errors:
        raise ValueError(f"benchmark policy schema violation: {errors[0].message}")


def _validate_schema(value: Mapping[str, Any], schema: Mapping[str, Any]) -> None:
    try:
        from jsonschema import Draft202012Validator
    except ImportError as error:
        raise ValueError(
            "jsonschema is required to validate benchmark profile"
        ) from error
    errors = sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda item: list(item.path),
    )
    if errors:
        raise ValueError(f"benchmark profile schema violation: {errors[0].message}")


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _portable_path(path: Path) -> str:
    try:
        return path.relative_to(CLI_ROOT.parent).as_posix()
    except ValueError:
        return path.name


def _assert_new_evidence_destination(path: Path, *, overwrite: bool) -> None:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise ValueError(f"cannot inspect evidence destination: {path}") from error
    kind = (
        "directory" if stat.S_ISDIR(details.st_mode) else "symlink or non-regular path"
    )
    suffix = " --overwrite cannot replace evidence atomically" if overwrite else ""
    raise ValueError(
        f"evidence directories are immutable; choose a new --output-dir "
        f"({path} is an existing {kind}).{suffix}"
    )


def _publish_evidence_directory(
    output_dir: Path, artifacts: Mapping[str, bytes], *, overwrite: bool
) -> None:
    """Publish one complete immutable evidence generation or publish nothing."""

    required = ("raw.json", "summary.json", "manifest.json")
    if set(artifacts) != set(required) or not all(
        isinstance(artifacts[name], bytes) for name in required
    ):
        raise ValueError("evidence publication requires the exact closed artifact set")
    _assert_new_evidence_destination(output_dir, overwrite=overwrite)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    _assert_new_evidence_destination(output_dir, overwrite=overwrite)

    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}.staging-", dir=str(output_dir.parent)
        )
    )
    os.chmod(staging, 0o700)
    staging_details = staging.lstat()
    created: list[dict[str, Any]] = []
    published = False
    try:
        for name in required:
            created.append(_write_exclusive_file(staging / name, artifacts[name]))
        _reauthenticate_publication_staging(staging, staging_details, created)
        _fsync_directory(staging)
        try:
            _rename_directory_noreplace(staging, output_dir)
        except FileExistsError as error:
            raise ValueError(
                "evidence destination appeared; nothing was published"
            ) from error
        published = True
        _fsync_directory(output_dir.parent)
    finally:
        if not published:
            _cleanup_owned_staging(staging, staging_details, created)


def _write_exclusive_file(path: Path, value: bytes) -> dict[str, Any]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    details = os.fstat(descriptor)
    try:
        view = memoryview(value)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise OSError("cannot write evidence staging file")
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        _unlink_owned_regular(path, details.st_dev, details.st_ino)
        raise
    os.close(descriptor)
    try:
        os.chmod(path, 0o600)
    except OSError:
        _unlink_owned_regular(path, details.st_dev, details.st_ino)
        raise
    return {
        "path": path,
        "device": details.st_dev,
        "inode": details.st_ino,
        "sha256": _sha256(value),
        "bytes": len(value),
    }


def _reauthenticate_publication_staging(
    staging: Path, staging_details: os.stat_result, created: Sequence[Mapping[str, Any]]
) -> None:
    current = staging.lstat()
    if (
        current.st_dev,
        current.st_ino,
        stat.S_IMODE(current.st_mode),
    ) != (staging_details.st_dev, staging_details.st_ino, 0o700):
        raise ValueError("evidence staging directory changed before publication")
    expected_names = {Path(item["path"]).name for item in created}
    if {entry.name for entry in os.scandir(staging)} != expected_names:
        raise ValueError("evidence staging directory contains an unexpected entry")
    for item in created:
        path = Path(item["path"])
        details = path.lstat()
        if (details.st_dev, details.st_ino) != (item["device"], item["inode"]):
            raise ValueError("evidence staging artifact was substituted")
        _read_admitted_file(
            path,
            expected_sha256=item["sha256"],
            expected_bytes=item["bytes"],
            expected_mode=0o600,
            label=f"evidence staging artifact {path.name}",
        )


def _rename_directory_noreplace(source: Path, destination: Path) -> None:
    if os.name == "nt":
        os.rename(source, destination)
        return
    library = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform == "darwin":
        rename = getattr(library, "renamex_np", None)
        if rename is None:
            raise ValueError("atomic no-replace directory publication is unavailable")
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(source_bytes, destination_bytes, 0x00000004)
    elif sys.platform.startswith("linux"):
        rename = getattr(library, "renameat2", None)
        if rename is None:
            raise ValueError("atomic no-replace directory publication is unavailable")
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(-100, source_bytes, -100, destination_bytes, 0x00000001)
    else:
        raise ValueError("atomic no-replace directory publication is unavailable")
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error_number, os.strerror(error_number), destination)
    raise OSError(error_number, os.strerror(error_number), destination)


def _write_atomic_file(path: Path, value: bytes, *, replace: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    initial = _lstat_optional(path)
    if initial is not None:
        if not stat.S_ISREG(initial.st_mode):
            raise ValueError(
                f"refusing to replace a symlink or non-regular file: {path}"
            )
        if not replace:
            raise ValueError(f"refusing to overwrite evidence: {path}")

    descriptor, temporary_text = tempfile.mkstemp(
        prefix=f".{path.name}.staging-", dir=str(path.parent)
    )
    temporary = Path(temporary_text)
    temporary_details = os.fstat(descriptor)
    published = False
    try:
        view = memoryview(value)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise OSError("cannot write atomic evidence file")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.chmod(temporary, 0o600)
        _read_admitted_file(
            temporary,
            expected_sha256=_sha256(value),
            expected_bytes=len(value),
            expected_mode=0o600,
            label="atomic evidence staging file",
        )
        current = _lstat_optional(path)
        if initial is None:
            if current is not None:
                raise ValueError("evidence output appeared during atomic publication")
            _rename_directory_noreplace(temporary, path)
        else:
            if (
                current is None
                or not stat.S_ISREG(current.st_mode)
                or (
                    current.st_dev,
                    current.st_ino,
                )
                != (initial.st_dev, initial.st_ino)
            ):
                raise ValueError("evidence output changed during atomic publication")
            os.replace(temporary, path)
        published = True
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not published:
            _unlink_owned_regular(
                temporary, temporary_details.st_dev, temporary_details.st_ino
            )


def _lstat_optional(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _unlink_owned_regular(path: Path, device: int, inode: int) -> None:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISREG(details.st_mode) and (details.st_dev, details.st_ino) == (
        device,
        inode,
    ):
        try:
            path.unlink()
        except OSError:
            pass


def _cleanup_owned_staging(
    staging: Path, staging_details: os.stat_result, created: Sequence[Mapping[str, Any]]
) -> None:
    for item in reversed(created):
        _unlink_owned_regular(
            Path(item["path"]), int(item["device"]), int(item["inode"])
        )
    try:
        current = staging.lstat()
        if stat.S_ISDIR(current.st_mode) and (
            current.st_dev,
            current.st_ino,
        ) == (staging_details.st_dev, staging_details.st_ino):
            staging.rmdir()
    except (FileNotFoundError, OSError):
        pass


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
