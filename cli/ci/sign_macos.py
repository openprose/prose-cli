#!/usr/bin/env python3
"""Sign copied CLI binaries and notarize their exact ZIP. Explicit opt-in only.

The caller supplies an unlocked, temporary signing keychain and cleans it up.
No credential is imported or persisted here. A receipt exists only after both
Developer ID signatures and Apple's matching Accepted log have been checked.
Bun entitlements follow https://bun.sh/guides/runtime/codesign-macos-executable
(consulted 2026-09-17); Rust receives no entitlement exceptions.
Raw Mach-O executables cannot carry a stapled ticket: online Gatekeeper lookup
is required. The notarization ZIP and signed binaries must remain unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import stat
import copy
import unicodedata
import subprocess
import time
import uuid
import zipfile

BUN_ENTITLEMENTS = {"com.apple.security.cs." + key: True for key in (
    "allow-jit", "allow-unsigned-executable-memory",
    "disable-executable-page-protection", "allow-dyld-environment-variables",
    "disable-library-validation",
)}


class SigningError(ValueError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def signing_json(path):
    path=regular_file(path)
    if not 0<path.stat().st_size<=2*1024*1024:raise SigningError('Signing receipt exceeds bounds')
    def pairs(rows):
        value={}
        for name,item in rows:
            if name in value:raise SigningError('Duplicate signing receipt field')
            value[name]=item
        return value
    with path.open('rb') as stream:data=stream.read(2*1024*1024+1)
    if len(data)>2*1024*1024:raise SigningError('Signing receipt exceeds bounds')
    return json.loads(data,object_pairs_hook=pairs)


def regular_file(path: Path) -> Path:
    if path.is_symlink() or not path.is_file():
        raise SigningError("Inputs must be regular files, not symbolic links")
    return path.resolve(strict=True)


class Commands:
    def __init__(self, timeout_seconds: int):
        self.deadline = time.monotonic() + timeout_seconds

    def __call__(self, args: list[str]) -> str:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise SigningError("Signing deadline exhausted")
        # Never forward provider credentials or print native diagnostic output:
        # notarytool diagnostics can include account or authentication details.
        env = {key: os.environ[key] for key in (
            "HOME", "PATH", "TMPDIR", "DEVELOPER_DIR", "SDKROOT"
        ) if key in os.environ}
        env["LANG"] = "en_US.UTF-8"
        try:
            result = subprocess.run(args, capture_output=True, text=True,
                                    env=env, timeout=remaining, check=False,
                                    stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as exc:
            raise SigningError("Signing command exceeded deadline") from exc
        if result.returncode:
            raise SigningError(f"{Path(args[0]).name} failed (exit {result.returncode}); no receipt issued")
        return result.stdout if result.stdout else result.stderr


def signature_details(text: str, identity: str, team_id: str) -> None:
    lines = text.splitlines()
    if f"Authority={identity}" not in lines or f"TeamIdentifier={team_id}" not in lines:
        raise SigningError("Developer ID identity or team does not match")
    if not any(line.startswith("Timestamp=") for line in lines):
        raise SigningError("Signature has no secure timestamp")
    if not any(re.search(r"flags=.*\bruntime\b", line) for line in lines):
        raise SigningError("Signature does not enable hardened runtime")
    if any("Signature=adhoc" in line for line in lines):
        raise SigningError("Ad-hoc signatures cannot be published")


def verify_entitlements(run, binary: Path, expected: dict) -> None:
    text = run(["/usr/bin/codesign", "--display", "--entitlements", "-", str(binary)])
    # codesign emits an Executable= diagnostic on stderr when no plist exists.
    if not text.strip() or all(line.startswith("Executable=") for line in text.splitlines()):
        actual = {}
    else:
        try:
            actual = plistlib.loads(text.encode())
        except (ValueError, plistlib.InvalidFileException) as exc:
            raise SigningError("Cannot verify signed entitlements") from exc
    if actual != expected:
        raise SigningError("Signed entitlements differ from release policy")


def sdk_source_sidecars(root,payload):
    """Adjacent producer evidence; never executable support or notarization input."""
    result={}
    for name,key,maximum in (('sdk-entry.py','entrySourceSha256',1024*1024),('collect.toc','collectTocSha256',2*1024*1024)):
        path=regular_file(root/name)
        if not 0<path.stat().st_size<=maximum:raise SigningError('SDK producer sidecar exceeds bounds')
        with path.open('rb') as source:data=source.read(maximum+1)
        if len(data)>maximum or hashlib.sha256(data).hexdigest()!=payload[key]:raise SigningError('SDK producer sidecar differs from build receipt')
        result[name]=data
    return result


def sdk_signature_paths(run,root,payload,identity,team_id):
    for name in payload['codeSignaturePaths']:
        target=regular_file(root/name)
        run(['/usr/bin/codesign','--verify','--strict',str(target)])
        signature_details(run(['/usr/bin/codesign','--display','--verbose=4',str(target)]),identity,team_id)
        verify_entitlements(run,target,{})


def notarization_table(archive,receipt):
    """Read the exact complete notarization ZIP without following aliases."""
    import sdk_native_inventory as native
    table={'files':{},'directories':{},'symlinks':{}}
    expected={record['file'] for record in receipt['binaries'].values()}
    payload=receipt.get('agentsSdkPayload');sdk='agents-sdk' in receipt['binaries']
    if sdk:
        if not isinstance(payload,dict) or receipt.get('agentsSdkArchitecture') not in ('arm64','x86_64'):
            raise SigningError('Current signed SDK requires complete onedir payload')
        native.validate_macos_payload_structure(payload,receipt['agentsSdkArchitecture'])
        expected.update(row['path'] for row in payload['entries'])
    elif payload is not None or 'agentsSdkArchitecture' in receipt:
        raise SigningError('Unexpected SDK signing payload')
    if archive.stat().st_size>512*1024*1024:raise SigningError('Notarization archive exceeds bounds')
    with zipfile.ZipFile(archive) as zipped:
        infos=zipped.infolist()
        if len(infos)>8192+3 or len(infos)!=len(expected):raise SigningError('Notarization archive member count differs')
        seen=set();total=0
        for info in infos:
            name=info.filename[:-1] if info.is_dir() else info.filename
            portable=unicodedata.normalize('NFC',unicodedata.normalize('NFC',name).casefold())
            if name not in expected or portable in seen:raise SigningError('Unexpected notarization member')
            seen.add(portable)
            mode=info.external_attr>>16;kind=stat.S_IFMT(mode);permissions=stat.S_IMODE(mode)
            if info.flag_bits&1 or info.file_size<0 or info.file_size>256*1024*1024:raise SigningError('Unsafe notarization member')
            total+=info.file_size
            if total>512*1024*1024:raise SigningError('Notarization expanded bytes exceed bounds')
            data=zipped.read(info)
            if len(data)!=info.file_size:raise SigningError('Notarization member length differs')
            if kind==stat.S_IFREG and not info.is_dir():table['files'][name]=(data,permissions)
            elif kind==stat.S_IFDIR and info.is_dir() and not data:table['directories'][name]=permissions
            elif kind==stat.S_IFLNK and not info.is_dir() and permissions==0o777:
                if len(data)>1024:raise SigningError('Notarization alias exceeds bounds')
                table['symlinks'][name]=data.decode('utf-8')
            else:raise SigningError('Unsafe notarization member type')
    for record in receipt['binaries'].values():
        value=table['files'].get(record['file'])
        if value is None or value[1]!=0o755 or hashlib.sha256(value[0]).hexdigest()!=record['signedSha256']:
            raise SigningError('Archive binary digest or mode mismatch')
    if sdk:
        scope={kind:{name:value for name,value in table[kind].items() if name not in ('prose-bun','prose-rust')} for kind in table}
        native.validate_macos_payload(payload,**scope,architecture=receipt['agentsSdkArchitecture'])
    return table


def write_notarization_archive(archive,binaries,output,sdk_view=None):
    table={'files':{record['file']:((output/record['file']).read_bytes(),0o755) for record in binaries.values()},'directories':{},'symlinks':{}}
    if sdk_view:
        for kind in table:table[kind].update(sdk_view[kind])
    if sum(len(data) for data,mode in table['files'].values())+sum(len(t.encode()) for t in table['symlinks'].values())>512*1024*1024:
        raise SigningError('Notarization expanded bytes exceed bounds')
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as zipped:
        for kind in ('files','directories','symlinks'):
            for name,value in sorted(table[kind].items()):
                info=zipfile.ZipInfo(name+'/' if kind=='directories' else name);info.create_system=3
                if kind=='files':data,mode=value;filetype=stat.S_IFREG
                elif kind=='directories':data=b'';mode=value;filetype=stat.S_IFDIR
                else:data=value.encode('utf-8');mode=0o777;filetype=stat.S_IFLNK
                info.external_attr=(filetype|mode)<<16;info.compress_type=zipfile.ZIP_DEFLATED
                zipped.writestr(info,data)
    if archive.stat().st_size>512*1024*1024:raise SigningError('Notarization archive exceeds bounds')


def verify_existing(output: Path, identity: str, team_id: str,
                    notary_key: Path, notary_key_id: str, notary_issuer: str,
                    timeout_seconds: int = 900, *, commands=None) -> dict:
    """Revalidate existing signed output, including a fresh authenticated log."""
    if platform.system() != "Darwin":
        raise SigningError("Developer ID verification requires macOS")
    if type(timeout_seconds) is not int or not 30 <= timeout_seconds <= 3600:
        raise SigningError("Timeout must be between 30 and 3600 seconds")
    if output.is_symlink() or not output.is_dir():
        raise SigningError("Signing directory must be a real directory")
    notary_key = regular_file(notary_key)
    run = commands if commands is not None else Commands(timeout_seconds)
    try:
        receipt = signing_json(output / "receipt.json")
        if (receipt["schema"] != "openprose.macos-signing/1"
                or receipt["identity"] != identity or receipt["teamId"] != team_id
                or receipt["bunEntitlements"] != BUN_ENTITLEMENTS
                or set(receipt["binaries"]) not in ({"bun", "rust"}, {"bun", "rust", "agents-sdk"})
                or receipt["notarization"]["archive"] != "notarization.zip"
                or receipt["notarization"]["status"] != "Accepted"):
            raise SigningError("Signing receipt does not match release policy")
        for name, record in receipt["binaries"].items():
            if record["file"] != f"prose-{name}":
                raise SigningError("Unexpected signed binary name")
            binary = regular_file(output / record["file"])
            if sha256(binary) != record["signedSha256"]:
                raise SigningError("Signed binary digest mismatch")
            run(["/usr/bin/codesign", "--verify", "--strict", str(binary)])
            signature_details(run(["/usr/bin/codesign", "--display", "--verbose=4", str(binary)]), identity, team_id)
            verify_entitlements(run, binary, BUN_ENTITLEMENTS if name == "bun" else {})
        archive = regular_file(output / "notarization.zip")
        archive_hash = sha256(archive)
        if archive_hash != receipt["notarization"]["sha256"]:
            raise SigningError("Notarization archive digest mismatch")
        table=notarization_table(archive,receipt)
        if 'agents-sdk' in receipt['binaries']:
            import sdk_native_inventory as native
            actual=native.read_macos_payload(output,receipt['agentsSdkPayload'],receipt['agentsSdkArchitecture'])
            scope={kind:{name:value for name,value in table[kind].items() if name not in ('prose-bun','prose-rust')} for kind in table}
            if actual!=scope:raise SigningError('Signed SDK tree differs from notarization archive')
            sdk_signature_paths(run,output,receipt['agentsSdkPayload'],identity,team_id)
        submission_id = str(uuid.UUID(receipt["notarization"]["submissionId"]))
        log = json.loads(run(["/usr/bin/xcrun", "notarytool", "log", submission_id,
                             "--key", str(notary_key), "--key-id", notary_key_id,
                             "--issuer", notary_issuer]))
        if (log.get("jobId") != submission_id or log.get("status") != "Accepted"
                or log.get("sha256") != archive_hash or log.get("statusCode") != 0):
            raise SigningError("Notarization log does not accept this exact archive")
    except (KeyError, TypeError, ValueError, zipfile.BadZipFile) as exc:
        if isinstance(exc, SigningError):
            raise
        raise SigningError("Invalid signing evidence") from exc
    return receipt


def sign(bun: Path, rust: Path, output: Path, identity: str, team_id: str,
         notary_key: Path, notary_key_id: str, notary_issuer: str,
         keychain: Path | None = None, timeout_seconds: int = 900,
         *, commands=None, agents_sdk: Path | None = None) -> dict:
    if platform.system() != "Darwin":
        raise SigningError("Developer ID signing requires macOS")
    if not re.fullmatch(r"[A-Z0-9]{10}", team_id):
        raise SigningError("Invalid Apple team ID")
    if not identity.startswith("Developer ID Application: ") or not identity.endswith(f" ({team_id})"):
        raise SigningError("A matching Developer ID Application identity is required")
    if not re.fullmatch(r"[A-Za-z0-9]{10,}", notary_key_id):
        raise SigningError("Invalid notary key ID")
    try:
        uuid.UUID(notary_issuer)
    except (ValueError, AttributeError) as exc:
        raise SigningError("Invalid notary issuer UUID") from exc
    if type(timeout_seconds) is not int or not 30 <= timeout_seconds <= 3600:
        raise SigningError("Timeout must be between 30 and 3600 seconds")
    inputs = {"bun": regular_file(bun), "rust": regular_file(rust)}
    sdk_build = None
    sdk_view = None
    sdk_sidecars = None
    if agents_sdk is not None:
        inputs["agents-sdk"] = regular_file(agents_sdk)
        if inputs["agents-sdk"].name!="prose-agents-sdk":raise SigningError("Canonical SDK helper name required")
        build_path = regular_file(agents_sdk.parent / 'agents-sdk-build.json')
        notices = regular_file(agents_sdk.parent / 'AGENTS-SDK-NOTICES.txt')
        if build_path.stat().st_size > 2 * 1024 * 1024 or notices.stat().st_size > 8 * 1024 * 1024:
            raise SigningError('SDK signing metadata exceeds bounds')
        try:
            sdk_build = signing_json(build_path)
        except ValueError as error:
            raise SigningError('SDK build receipt is malformed') from error
        if not isinstance(sdk_build, dict):
            raise SigningError('SDK build receipt must be an object')
        if sdk_build.get('embeddedSigning') != {'identity': identity, 'verification': 'pyinstaller-inner-binaries-and-frozen-self-tests'}:
            raise SigningError('SDK signing requires same-identity signed embedded binaries')
        if sdk_build.get('helper') != {'path': 'prose-agents-sdk', 'byteLength': agents_sdk.stat().st_size, 'sha256': sha256(agents_sdk)}:
            raise SigningError('SDK input differs from frozen build receipt')
        if sdk_build.get('notices') != {'path': notices.name, 'byteLength': notices.stat().st_size, 'sha256': sha256(notices)}:
            raise SigningError('SDK notices differ from frozen build receipt')
        import sdk_native_inventory as native
        try:
            sdk_view=native.read_macos_payload(agents_sdk.parent,sdk_build['payload'],sdk_build['architecture'])
            sdk_sidecars=sdk_source_sidecars(agents_sdk.parent,sdk_build['payload'])
        except (KeyError,ValueError,OSError) as error:
            raise SigningError('SDK signing requires complete valid onedir payload') from error
    notary_key = regular_file(notary_key)
    if keychain is not None:
        keychain = regular_file(keychain)
    # Refuse reuse, including a dangling symlink, so a failed earlier attempt
    # can never supply an apparently current receipt or stale signed binary.
    if output.exists() or output.is_symlink():
        raise SigningError("Signing output must be a new directory")
    output = output.absolute()
    output.mkdir(mode=0o700, parents=False)
    run = commands if commands is not None else Commands(timeout_seconds)
    entitlements = output / "bun-entitlements.plist"
    entitlements.write_bytes(plistlib.dumps(BUN_ENTITLEMENTS))
    binaries = {}
    if sdk_view is not None:
        native.materialize_macos_payload(output,sdk_build['payload'],**sdk_view,architecture=sdk_build['architecture'])
        for name,data in sdk_sidecars.items():
            with (output/name).open('xb') as sidecar:sidecar.write(data)
            (output/name).chmod(0o644)
        sdk_signature_paths(run,output,sdk_build['payload'],identity,team_id)
    for name, source in inputs.items():
        original_hash = sha256(source)
        target = output / f"prose-{name}"
        if name!="agents-sdk":
            shutil.copyfile(source, target)
            target.chmod(0o755)
        command = ["/usr/bin/codesign", "--force", "--sign", identity,
                   "--options", "runtime", "--timestamp"]
        if keychain is not None:
            command.extend(["--keychain", str(keychain)])
        if name == "bun":
            command.extend(["--entitlements", str(entitlements)])
        run(command + [str(target)])
        run(["/usr/bin/codesign", "--verify", "--strict", str(target)])
        details = run(["/usr/bin/codesign", "--display", "--verbose=4", str(target)])
        signature_details(details, identity, team_id)
        verify_entitlements(run, target, BUN_ENTITLEMENTS if name == "bun" else {})
        if name == 'agents-sdk':
            from kernel_rc_evidence import SDK_IMPORT_TEST, SDK_TOOL_TEST
            for flag, expected in (('--packaged-self-test', SDK_IMPORT_TEST), ('--packaged-tool-self-test', SDK_TOOL_TEST)):
                if json.loads(run([str(target), flag])) != expected:
                    raise SigningError('Signed SDK self-test differs from frozen tool qualification')
        if sha256(source) != original_hash:
            raise SigningError("Source binary changed during signing")
        binaries[name] = {"file": target.name, "inputSha256": original_hash,
                          "signedSha256": sha256(target)}
    archive = output / "notarization.zip"
    signed_sdk_view=None
    if sdk_view is not None:
        signed_sdk_view=native.read_macos_payload(output,sdk_build['payload'],sdk_build['architecture'])
        for kind in ('directories','symlinks'):
            if signed_sdk_view[kind]!=sdk_view[kind]:raise SigningError('SDK layout changed during signing')
        if any(value!=sdk_view['files'][name] for name,value in signed_sdk_view['files'].items() if name!='prose-agents-sdk'):
            raise SigningError('SDK support bytes changed during signing')
        sdk_signature_paths(run,output,sdk_build['payload'],identity,team_id)
    write_notarization_archive(archive,binaries,output,signed_sdk_view)
    archive_hash = sha256(archive)
    auth = ["--key", str(notary_key), "--key-id", notary_key_id, "--issuer", notary_issuer]
    try:
        submission = json.loads(run(["/usr/bin/xcrun", "notarytool", "submit", str(archive),
                                     *auth, "--wait", "--timeout", str(timeout_seconds),
                                     "--output-format", "json"]))
        submission_id = str(uuid.UUID(submission["id"]))
        if submission.get("status") != "Accepted":
            raise SigningError("Notarization was not Accepted")
        log = json.loads(run(["/usr/bin/xcrun", "notarytool", "log", submission_id, *auth]))
        if (log.get("jobId") != submission_id or log.get("status") != "Accepted"
                or log.get("sha256") != archive_hash or log.get("statusCode") != 0):
            raise SigningError("Notarization log does not accept this exact archive")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, SigningError):
            raise
        raise SigningError("Invalid notarization response; no receipt issued") from exc
    if sha256(archive) != archive_hash or any(
        sha256(output / record["file"]) != record["signedSha256"] for record in binaries.values()
    ):
        raise SigningError("Signed artifacts changed during notarization")
    if sdk_build is not None:
        if (signing_json(build_path) != sdk_build
                or sha256(notices) != sdk_build['notices']['sha256']):
            raise SigningError('SDK build metadata changed during signing')
        if sdk_source_sidecars(agents_sdk.parent,sdk_build['payload'])!=sdk_sidecars or sdk_source_sidecars(output,sdk_build['payload'])!=sdk_sidecars:
            raise SigningError('SDK producer sidecars changed during signing')
        if native.read_macos_payload(agents_sdk.parent,sdk_build['payload'],sdk_build['architecture'])!=sdk_view or native.read_macos_payload(output,sdk_build['payload'],sdk_build['architecture'])!=signed_sdk_view:
            raise SigningError('SDK tree changed during notarization')
    # Store a bounded projection, not the raw service log, which can contain
    # local paths/account data. Native service logs remain available by ID.
    receipt = {"schema": "openprose.macos-signing/1", "teamId": team_id,
               "identity": identity, "binaries": binaries,
               "bunEntitlements": BUN_ENTITLEMENTS,
               "notarization": {"submissionId": submission_id, "status": "Accepted",
                                "archive": archive.name, "sha256": archive_hash},
               "stapled": False}
    if sdk_build is not None:
        receipt['agentsSdkPayload']=copy.deepcopy(sdk_build['payload'])
        receipt['agentsSdkArchitecture']=sdk_build['architecture']
    notarization_table(archive,receipt)
    encoded_receipt=json.dumps(receipt,indent=2)+"\n"
    if len(encoded_receipt.encode())>2*1024*1024:raise SigningError("Signing receipt exceeds bounds")
    if sdk_build is not None:
        build = sdk_build
        signed = binaries['agents-sdk']
        build['unsignedHelper'] = build['helper']
        build['helper'] = {'path': 'prose-agents-sdk', 'byteLength': (output / signed['file']).stat().st_size,
                           'sha256': signed['signedSha256']}
        build['signing'] = 'developer-id-notarized'
        build['signingReceiptSha256'] = hashlib.sha256(encoded_receipt.encode()).hexdigest()
        encoded_build=json.dumps(build,indent=2)+'\n'
        if len(encoded_build.encode())>2*1024*1024:raise SigningError('SDK signing receipt exceeds bounds')
        (output/'agents-sdk-build.json').write_text(encoded_build)
        (output/'agents-sdk-build.json').chmod(0o644)
        shutil.copyfile(notices, output / notices.name)
        (output/notices.name).chmod(0o644)
    (output/'receipt.json').write_text(encoded_receipt)
    (output/'receipt.json').chmod(0o644)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("bun", "rust", "agents-sdk", "verify-existing"):
        parser.add_argument("--" + name, type=Path)
    for name in ("output", "notary-key"):
        parser.add_argument("--" + name, type=Path, required=name == "notary-key")
    for name in ("identity", "team-id", "notary-key-id", "notary-issuer"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--keychain", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    try:
        args = vars(parser.parse_args())
        existing = args.pop("verify_existing")
        if existing is not None:
            if any([args.pop(name) is not None for name in ("bun", "rust", "agents_sdk", "output")]):
                raise SigningError("Verification mode cannot accept build inputs")
            args.pop("keychain")
            verify_existing(existing, **args)
        else:
            if any(args[name] is None for name in ("bun", "rust", "output")):
                raise SigningError("Signing requires --bun, --rust and --output")
            sign(**args)
    except (SigningError, OSError) as exc:
        parser.exit(1, f"Signing failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
