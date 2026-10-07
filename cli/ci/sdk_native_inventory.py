"""Bounded native inspection for the Linux package-owned SDK helper.

ELF64 section, symbol and GNU version layouts follow the System V ABI and LSB
symbol-version specification. This module uses only the standard library inside
its frozen inspection entrypoint. The builder separately inspects the archive.
"""
from __future__ import annotations
import ast
from collections import deque
import base64
import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath
import re
import struct
import unicodedata

MAX_BYTES = 256 * 1024 * 1024
MAX_FILES = 128
MAX_METADATA = 8 * 1024 * 1024
SOURCE_PATHS = ('harnesses/agents-sdk/run.py', 'harnesses/agents-sdk/requirements-build.txt',
                'harnesses/agents-sdk/test_run.py', 'cli/ci/build_agents_sdk.py',
                'cli/ci/sdk_native_inventory.py')


def require(value, message):
    if not value:
        raise ValueError(message)


def load_json(data):
    def closed_pairs(pairs):
        result = {}
        for name, value in pairs:
            require(name not in result, 'Duplicate native metadata key')
            result[name] = value
        return result
    return json.loads(data, object_pairs_hook=closed_pairs)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def keys(value, expected, label):
    require(isinstance(value, dict) and set(value) == set(expected), 'Unsupported ' + label + ' shape')


def safe_path(value):
    require(isinstance(value, str) and value and '\\' not in value and '\x00' not in value,
            'Invalid native member path')
    path = PurePosixPath(value)
    require(not path.is_absolute() and '..' not in path.parts and str(path) == value,
            'Unsafe native member path')
    return value


def read_file(path, maximum=MAX_BYTES):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), 'Native input must be a regular file')
    size = path.stat().st_size
    require(0 < size <= maximum, 'Native input size is outside bounds')
    with path.open('rb') as stream:
        data = stream.read(maximum + 1)
    require(len(data) <= maximum and len(data) == size, 'Native input changed while reading')
    return data


def record(path, data):
    return {'path': safe_path(path), 'sha256': sha(data), 'byteLength': len(data)}


def elf_record(path, data):
    """Inspect ELF64 little-endian dynamic imports/exports, rejecting partial data."""
    require(64 <= len(data) <= MAX_BYTES and data[:7] == b'\x7fELF\x02\x01\x01', 'Unsupported ELF header')
    machine = struct.unpack_from('<H', data, 18)[0]
    require(machine in (62, 183), 'Unsupported ELF machine')
    require(struct.unpack_from('<H', data, 52)[0] == 64, 'Invalid ELF header size')
    shoff = struct.unpack_from('<Q', data, 40)[0]
    shsize, count, names_index = struct.unpack_from('<HHH', data, 58)
    require(shsize == 64 and 0 < shoff <= len(data) - 64, 'Missing ELF section table')
    zero = struct.unpack_from('<IIQQQQIIQQ', data, shoff)
    if count == 0: count = zero[5]
    if names_index == 0xffff: names_index = zero[6]
    require(0 < count <= 65536 and shoff + count * 64 <= len(data) and names_index < count,
            'Invalid ELF section count')
    sections = [struct.unpack_from('<IIQQQQIIQQ', data, shoff + i * 64) for i in range(count)]
    for section in sections[1:]:
        if section[1] != 8:
            require(section[4] + section[5] <= len(data), 'ELF section exceeds file')
    def payload(section):
        return data[section[4]:section[4] + section[5]]
    def string(table, offset):
        require(0 <= offset < len(table), 'ELF string offset exceeds table')
        end = table.find(b'\0', offset)
        require(end >= 0, 'Unterminated ELF string')
        try: value = table[offset:end].decode('ascii')
        except UnicodeDecodeError as error: raise ValueError('Non-ASCII ELF dynamic string') from error
        require(all(32 <= ord(c) < 127 for c in value), 'Invalid ELF dynamic string')
        return value
    def linked(section):
        require(0 < section[6] < count and sections[section[6]][1] == 3, 'Invalid ELF string table link')
        return payload(sections[section[6]])
    def single(kind):
        matches = [s for s in sections if s[1] == kind]
        require(len(matches) <= 1, 'Duplicate ELF dynamic section')
        return matches[0] if matches else None
    dynamic = single(6); symbols = single(11); versions = single(0x6fffffff)
    needs_section = single(0x6ffffffe); defs_section = single(0x6ffffffd)
    needed = []; dynamic_counts = {}
    if dynamic:
        require(dynamic[9] == 16 and dynamic[5] % 16 == 0, 'Invalid ELF dynamic entries')
        table = linked(dynamic); terminated = False
        for offset in range(0, dynamic[5], 16):
            tag, value = struct.unpack_from('<qQ', payload(dynamic), offset)
            if tag == 0: terminated = True; break
            if tag == 1: needed.append(string(table, value))
            if tag in (0x6ffffffd, 0x6fffffff):
                require(tag not in dynamic_counts, 'Duplicate ELF dynamic version count')
                dynamic_counts[tag] = value
        require(terminated, 'ELF dynamic entries lack terminator')
    require(len(set(needed)) == len(needed), 'Duplicate ELF needed library')
    need_indices = {}; def_indices = {}; need_rows = []
    def chains(section, definition):
        if not section: return 0
        blob = payload(section); table = linked(section); cursor = 0; visited = set(); total = 0
        while True:
            width = 20 if definition else 16
            require(cursor not in visited and cursor + width <= len(blob), 'Invalid ELF version chain')
            visited.add(cursor); total += 1
            require(total <= 65536, 'Oversized ELF version chain')
            if definition:
                revision, flags, index, auxiliary_count, _, aux, following = struct.unpack_from('<HHHHIII', blob, cursor)
                library = None
            else:
                revision, auxiliary_count, library_offset, aux, following = struct.unpack_from('<HHIII', blob, cursor)
                library = string(table, library_offset)
                require(library in needed, 'ELF version dependency is not DT_NEEDED')
            require(revision == 1 and 0 < auxiliary_count <= 65536 and aux >= width,
                    'Invalid ELF version auxiliary count')
            acursor = cursor + aux; avisited = set(); first = None
            for i in range(auxiliary_count):
                awidth = 8 if definition else 16
                require(acursor not in avisited and acursor + awidth <= len(blob), 'Invalid ELF version auxiliary chain')
                avisited.add(acursor)
                if definition:
                    name_offset, next_aux = struct.unpack_from('<II', blob, acursor)
                else:
                    _, flags, index, name_offset, next_aux = struct.unpack_from('<IHHII', blob, acursor)
                name = string(table, name_offset); require(bool(name), 'Empty ELF version name')
                if first is None: first = name
                if not definition:
                    key = index & 0x7fff
                    require(key > 1 and key not in need_indices, 'Duplicate ELF needed version index')
                    need_indices[key] = (library, name)
                    need_rows.append({'library': library, 'version': name, 'flags': flags})
                if i + 1 < auxiliary_count:
                    require(next_aux >= awidth, 'Short ELF auxiliary version chain')
                    acursor += next_aux
                else: require(next_aux == 0, 'ELF auxiliary count mismatch')
            if definition:
                index &= 0x7fff
                require(index > 0 and index not in def_indices, 'Duplicate ELF defined version index')
                def_indices[index] = first
            if following == 0: break
            require(following >= width, 'Invalid ELF version next offset')
            cursor += following
        require(section[7] == total, 'ELF version section count mismatch')
        return total
    nneeds = chains(needs_section, False); ndefs = chains(defs_section, True)
    for tag, actual in ((0x6fffffff, nneeds), (0x6ffffffd, ndefs)):
        if tag in dynamic_counts: require(dynamic_counts[tag] == actual, 'ELF dynamic version count mismatch')
    imported = []; defined = []
    if symbols:
        require(symbols[9] == 24 and symbols[5] % 24 == 0, 'Invalid ELF symbol table')
        sym_count = symbols[5] // 24; table = linked(symbols); blob = payload(symbols)
        require(sym_count <= 1000000, 'Oversized ELF dynamic symbol table')
        vblob = payload(versions) if versions else None
        if versions:
            require(versions[6] == sections.index(symbols) and versions[5] == sym_count * 2,
                    'ELF version and symbol tables disagree')
        for i in range(sym_count):
            name_offset, info, other, section_index, _, _ = struct.unpack_from('<IBBHQQ', blob, i * 24)
            if info >> 4 not in (1, 2): continue
            name = string(table, name_offset)
            if not name: continue
            version_index = struct.unpack_from('<H', vblob, i * 2)[0] if vblob is not None else 1
            version_key = version_index & 0x7fff
            if section_index == 0:
                require(version_key <= 1 or version_key in need_indices, 'Unknown ELF imported symbol version')
                library, version = need_indices.get(version_key, (None, None))
                imported.append({'symbol': name, 'library': library, 'version': version, 'weak': info >> 4 == 2})
            elif other & 3 in (0, 3):
                require(section_index != 0xffff, 'Unsupported extended ELF symbol index')
                require(version_key <= 1 or version_key in def_indices, 'Unknown ELF defined symbol version')
                defined.append({'symbol': name, 'version': def_indices.get(version_key),
                                'defaultVersion': not bool(version_index & 0x8000)})
    else: require(not versions and not needs_section and not defs_section, 'ELF versions without symbols')
    # Keep the established conservative raw-version gate in addition to actual imports.
    glibc = [tuple(int(x) for x in v.split(b'.')) for v in re.findall(rb'GLIBC_([0-9]+\.[0-9]+(?:\.[0-9]+)?)', data)]
    return {**record(path, data), 'machine': machine, 'needed': sorted(needed),
            'versionNeeds': sorted(need_rows, key=lambda r: (r['library'], r['version'], r['flags'])),
            'importedSymbols': sorted(imported, key=lambda r: (r['symbol'], r['library'] or '', r['version'] or '', r['weak'])),
            'definedSymbols': sorted(defined, key=lambda r: (r['symbol'], r['version'] or '', r['defaultVersion'])),
            'maximumRequiredGlibc': '.'.join(map(str, max(glibc))) if glibc else None}


def verify_closure(libraries, target):
    machine = {'linux-arm64-gnu': 183, 'linux-x64-gnu': 62}.get(target)
    require(machine is not None and 0 < len(libraries) <= MAX_FILES, 'Invalid native target or library count')
    require(len({r['path'] for r in libraries}) == len(libraries), 'Duplicate native member')
    require(all(r['machine'] == machine for r in libraries), 'Native library machine differs from target')
    suppliers = [r for r in libraries if r['path'] == 'libgcc_s.so.1']
    require(len(suppliers) == 1, 'Frozen helper must contain exactly one libgcc supplier')
    supplier = suppliers[0]
    exports = {(r['symbol'], r['version']) for r in supplier['definedSymbols']}
    supplied_versions = {r['version'] for r in supplier['definedSymbols'] if r['version'] is not None}
    for library in libraries:
        maximum = library['maximumRequiredGlibc']
        require(maximum is None or tuple(map(int, maximum.split('.'))) <= (2, 34),
                'Frozen SDK libraries require glibc newer than 2.34')
        for need in library['versionNeeds']:
            if need['library'] == 'libgcc_s.so.1' and not need['flags'] & 2:
                require(need['version'] in supplied_versions, 'libgcc does not supply a required version')
        for symbol in library['importedSymbols']:
            if symbol['library'] == 'libgcc_s.so.1' and not symbol['weak']:
                require((symbol['symbol'], symbol['version']) in exports, 'libgcc does not supply a required strong symbol')
    return True


def inspect_tree(root):
    root = Path(root); rows = []
    for path in sorted(root.rglob('*')):
        require(not path.is_symlink(), 'Unexpected symlink in frozen native inspection')
        if not path.is_file(): continue
        with path.open('rb') as stream: magic = stream.read(4)
        if magic != b'\x7fELF': continue
        rows.append(elf_record(path.relative_to(root).as_posix(), read_file(path)))
        require(len(rows) <= MAX_FILES, 'Too many frozen native libraries')
    maximum = max((tuple(map(int, r['maximumRequiredGlibc'].split('.'))) for r in rows
                   if r['maximumRequiredGlibc'] is not None), default=None)
    return {'schema': 'openprose.sdk-packaged-libraries/1', 'elfCount': len(rows),
            'requiredGlibcMaximum': '.'.join(map(str, maximum)) if maximum else None,
            'libraries': rows, 'modelCalls': 0}


def verify_analysis_toc(path, supplier):
    data = read_file(path, MAX_METADATA)
    try: value = ast.literal_eval(data.decode('utf-8'))
    except (ValueError, SyntaxError, RecursionError) as error: raise ValueError('Invalid PyInstaller analysis TOC') from error
    found = []
    def visit(node, depth=0):
        require(depth <= 64, 'PyInstaller TOC nesting exceeds bound')
        if isinstance(node, (list, tuple)):
            if len(node) == 3 and node[0] == 'libgcc_s.so.1' and node[2] == 'BINARY': found.append(node)
            else:
                for child in node: visit(child, depth + 1)
    visit(value)
    require(len(found) == 1, 'PyInstaller must select exactly one libgcc binary')
    require(Path(found[0][1]).resolve() == Path(supplier).resolve() and sha(read_file(found[0][1])) == sha(read_file(supplier)),
            'PyInstaller selected an ambient libgcc binary')
    return sha(data)


def validate_supplier_library(library):
    """Validate portable /2 RPM facts; the supplier proves identity and ownership.

    Directory aliases are recorded only after the pinned supplier process has
    proved a unique regular RPM payload row sharing the canonical inode/device.
    A portable receipt cannot itself repeat those original filesystem checks.
    """
    keys(library, ('path', 'sha256', 'byteLength', 'supplierPath', 'rpmFileDigestVerified',
                   'rpmPayloadPath', 'rpmFileDigestAlgorithm', 'rpmFileDigestSha256'), 'supplier library')
    require(library['path'] == 'libgcc_s.so.1' and library['rpmFileDigestVerified'] is True,
            'Invalid supplier library identity or RPM verification')
    require(isinstance(library['sha256'], str) and re.fullmatch('[0-9a-f]{64}', library['sha256']),
            'Invalid supplier library SHA256')
    require(type(library['byteLength']) is int and 0 < library['byteLength'] <= 8 * 1024 * 1024,
            'Supplier library size is outside bounds')
    require(type(library['rpmFileDigestAlgorithm']) is int and library['rpmFileDigestAlgorithm'] == 8,
            'Supplier RPM digest algorithm must be integer SHA256 (8)')
    require(isinstance(library['rpmFileDigestSha256'], str) and
            re.fullmatch('[0-9a-f]{64}', library['rpmFileDigestSha256']) and
            library['rpmFileDigestSha256'] == library['sha256'], 'Supplier RPM digest differs from measured SHA256')
    def direct_path(value, parents):
        require(isinstance(value, str) and 0 < len(value) <= 1024 and
                all(32 <= ord(c) < 127 for c in value) and '\\' not in value,
                'Invalid supplier RPM path')
        path = PurePosixPath(value)
        require(path.is_absolute() and str(path) == value and str(path.parent) in parents and
                re.fullmatch(r'libgcc_s(?:-[A-Za-z0-9._+\-]+)?[.]so[.]1', path.name),
                'Supplier RPM path must be a direct canonical libgcc leaf')
        return path
    canonical = direct_path(library['supplierPath'], {'/usr/lib64'})
    payload = direct_path(library['rpmPayloadPath'], {'/usr/lib64', '/lib64'})
    require(payload.name == canonical.name, 'Supplier RPM alias basename differs from canonical library')
    return library


def validate_input(path, supplier, source_root, target):
    data = read_file(path, MAX_METADATA)
    value = load_json(data)
    keys(value, ('schema', 'target', 'images', 'pythonArchive', 'sourceSnapshot', 'libgcc',
                 'driverSha256', 'lockSha256', 'pythonDistribution'), 'Linux native input')
    require(value['schema'] == 'openprose.sdk-linux-native-input/1' and value['target'] == target,
            'Native input target/schema mismatch')
    def hash_value(digest):
        require(isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest), 'Invalid native input digest')
    snapshot = value['sourceSnapshot']; keys(snapshot, ('sources', 'driverSha256', 'lockSha256'), 'source snapshot')
    keys(snapshot['sources'], SOURCE_PATHS, 'source snapshot files')
    for relative, expected in snapshot['sources'].items():
        hash_value(expected)
        require(sha(read_file(Path(source_root) / relative)) == expected, 'Linux build source snapshot changed')
    for name in ('driverSha256', 'lockSha256'):
        hash_value(value[name]); require(snapshot[name] == value[name], 'Native driver/lock binding differs')
    keys(value['images'], ('supplier', 'freezer'), 'native images')
    arch = 'aarch64' if target == 'linux-arm64-gnu' else 'x86_64'
    for role, floor in (('supplier', '2_28'), ('freezer', '2_34')):
        image = value['images'][role]; keys(image, ('image', 'configSha256', 'metadataUrl'), 'native image')
        require(re.fullmatch(r'quay\.io/pypa/manylinux_' + floor + '_' + arch + r'@sha256:[0-9a-f]{64}', image['image']),
                'Native image must be same-architecture digest-pinned PyPA image')
        hash_value(image['configSha256'])
        require(isinstance(image['metadataUrl'], str) and image['metadataUrl'].startswith('https://'), 'Missing native image metadata URL')
    archive = value['pythonArchive']
    keys(archive, ('url', 'sha256', 'byteLength', 'targetTriple', 'metadataUrl'), 'Python archive')
    hash_value(archive['sha256'])
    require(type(archive['byteLength']) is int and archive['byteLength'] > 0, 'Invalid Python archive size')
    triple = arch + '-unknown-linux-gnu'
    require(archive['targetTriple'] == triple and isinstance(archive['url'], str) and
            re.fullmatch(r'https://github\.com/astral-sh/python-build-standalone/releases/download/[0-9]+/cpython-3\.10\.20%2B[0-9]+-' +
                         triple + r'-pgo%2Blto-full\.tar\.zst', archive['url']), 'Expected pinned full shared Python archive')
    require(isinstance(archive['metadataUrl'], str) and archive['metadataUrl'].startswith('https://'), 'Missing Python archive metadata URL')
    origin = value['libgcc']; keys(origin, ('schema', 'package', 'library', 'licenses'), 'libgcc origin')
    require(origin['schema'] == 'openprose.sdk-native-origin/2', 'Unsupported libgcc origin schema')
    package = origin['package']; keys(package, ('name', 'epoch', 'version', 'release', 'architecture', 'sourceRpm', 'license'), 'supplier package')
    require(package['name'] == 'libgcc' and package['architecture'] == arch and
            all(isinstance(v, str) and v and not any(ord(c) < 32 for c in v) for v in package.values()) and
            re.fullmatch(r'[0-9]+', package['epoch']) and package['sourceRpm'].endswith('.src.rpm'), 'Invalid supplier package provenance')
    library = validate_supplier_library(origin['library'])
    actual = read_file(supplier, 8 * 1024 * 1024)
    require(library['sha256'] == sha(actual) and type(library['byteLength']) is int and library['byteLength'] == len(actual),
            'Explicit libgcc bytes differ from supplier receipt')
    licenses = origin['licenses']; require(isinstance(licenses, list) and 0 < len(licenses) <= 32, 'Missing supplier license evidence')
    texts = []; seen = set()
    for item in licenses:
        keys(item, ('packagePath', 'path', 'sha256', 'byteLength'), 'supplier license')
        safe_path(item['path']); require(item['path'] not in seen, 'Duplicate supplier license'); seen.add(item['path'])
        require(isinstance(item['packagePath'], str) and item['packagePath'].startswith('/usr/share/licenses/'), 'Supplier license is not RPM-owned')
        text = read_file(Path(supplier).parent / item['path'], 1024 * 1024)
        require(item['sha256'] == sha(text) and type(item['byteLength']) is int and item['byteLength'] == len(text), 'Supplier license digest mismatch')
        texts.append(text.decode('utf-8'))
    joined = '\n'.join(texts)
    require('GNU GENERAL PUBLIC LICENSE' in joined and 'Version 3' in joined and
            'GCC RUNTIME LIBRARY EXCEPTION' in joined and 'Version 3.1' in joined,
            'Supplier GPL and runtime exception texts are required')
    distribution = value['pythonDistribution']; keys(distribution, ('metadataPath', 'root', 'metadataSha256'), 'Python distribution')
    root = Path(distribution['root']); metadata_path = Path(distribution['metadataPath'])
    require(root.is_absolute() and metadata_path.is_absolute() and not root.is_symlink() and
            metadata_path == root / 'PYTHON.json', 'Python metadata must belong to supplied full distribution')
    hash_value(distribution['metadataSha256'])
    require(sha(read_file(metadata_path, MAX_METADATA)) == distribution['metadataSha256'], 'Python metadata digest mismatch')
    return value, sha(data), texts


def python_origin(value):
    distribution = value['pythonDistribution']; root = Path(distribution['root'])
    data = read_file(distribution['metadataPath'], MAX_METADATA); info = load_json(data)
    require(info.get('version') == 8, 'Unsupported full Python metadata version')
    require(info.get('python_version') == '3.10.20' and info.get('target_triple') == value['pythonArchive']['targetTriple'] and
            info.get('libpython_link_mode') == 'shared', 'Python metadata is not the target full shared distribution')
    license_paths = {info.get('license_path')} if info.get('license_path') else set()
    require(isinstance(info.get('licenses'), list) and info['licenses'] and license_paths,
            'Python distribution license metadata is absent')
    build_info = info.get('build_info'); require(isinstance(build_info, dict), 'Missing Python native build metadata')
    core = build_info.get('core'); require(isinstance(core, dict) and isinstance(core.get('shared_lib'), str), 'Python core shared library is absent')
    static = []
    extensions = build_info.get('extensions'); require(isinstance(extensions, dict), 'Missing Python extension metadata')
    for name, variants in sorted(extensions.items()):
        require(isinstance(variants, list), 'Invalid Python extension variants')
        for variant in variants:
            require(isinstance(variant, dict), 'Invalid Python extension metadata')
            declared = variant.get('licenses', [])
            paths = variant.get('license_paths', [])
            legacy_paths = variant.get('license_path', [])
            if isinstance(legacy_paths, str): legacy_paths = [legacy_paths]
            require(isinstance(legacy_paths, list), 'Invalid Python extension license paths')
            paths = [*paths, *legacy_paths]
            require(isinstance(declared, list) and isinstance(paths, list), 'Invalid Python extension license metadata')
            if declared:
                require(paths, 'Declared Python native license lacks corresponding text')
                license_paths.update(paths)
                static.append({'name': name, 'licenses': declared, 'licensePaths': sorted(paths)})
    licenses = []
    for relative in sorted(license_paths):
        safe_path(relative); text = read_file(root / relative, 1024 * 1024)
        text.decode('utf-8')
        licenses.append(record(relative, text))
    # Actual full-provider member bytes establish ownership, not a basename guess.
    owned = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or not path.is_file(): continue
        with path.open('rb') as stream: magic = stream.read(4)
        if magic == b'\x7fELF': owned[sha(read_file(path))] = path.relative_to(root).as_posix()
    core_path = (root / safe_path(core['shared_lib'])).resolve()
    require(core_path.is_relative_to(root.resolve()) and sha(read_file(core_path)) in owned, 'Python shared core bytes are absent')
    # These are full-provider declarations, not proof that each extension shipped.
    return {'archive': value['pythonArchive'], 'metadataSha256': sha(data), 'licenses': licenses,
            'declaredExtensionLicenses': static}, owned


def wheel_origins(packages, distributions):
    origins = []; owned = {}
    for package in packages:
        dist = distributions(package['name'])
        require(dist.version == package['version'], 'Installed wheel differs from lock')
        files = dist.files or []
        record_files = [entry for entry in files if str(entry).endswith('.dist-info/RECORD')]
        require(len(record_files) == 1, 'Wheel RECORD is absent or ambiguous')
        record_bytes = read_file(dist.locate_file(record_files[0]), MAX_METADATA)
        licenses = []
        for entry in files:
            path = Path(dist.locate_file(entry))
            if path.is_symlink() or not path.is_file(): continue
            is_license = '.dist-info/' in str(entry) and ('license' in str(entry).lower() or 'copying' in str(entry).lower())
            with path.open('rb') as stream: native = stream.read(4) == b'\x7fELF'
            if not native and not is_license: continue
            data = read_file(path, 1024 * 1024 if is_license else MAX_BYTES)
            require(entry.hash is not None and entry.hash.mode == 'sha256' and
                    base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode() == entry.hash.value and
                    entry.size == len(data), 'Installed native/license bytes differ from wheel RECORD')
            if native:
                digest = sha(data)
                require(digest not in owned, 'Ambiguous installed native wheel origin')
                owned[digest] = package['name']
            if is_license:
                data.decode('utf-8'); licenses.append(record(str(entry), data))
        if any(name == package['name'] for name in owned.values()):
            require(licenses, 'Native wheel lacks corresponding license texts')
        origins.append({'name': package['name'], 'version': package['version'], 'wheelCandidateSha256s': package['wheelSha256'],
                        'recordSha256': sha(record_bytes), 'licenses': sorted(licenses, key=lambda r: r['path'])})
    return sorted(origins, key=lambda r: r['name']), owned


def assign_origins(libraries, value, packages, distributions):
    python, python_members = python_origin(value)
    wheels, wheel_members = wheel_origins(packages, distributions)
    result = []
    for row in libraries:
        if row['path'] == 'libgcc_s.so.1':
            require(row['sha256'] == value['libgcc']['library']['sha256'], 'Frozen libgcc differs from selected supplier')
            origin = 'supplier'
        elif row['sha256'] in python_members: origin = 'python'
        elif row['sha256'] in wheel_members: origin = 'wheel:' + wheel_members[row['sha256']]
        else: raise ValueError('Frozen native member has unknown origin: ' + row['path'])
        result.append({**row, 'origin': origin})
    return result, {'supplier': {**value['libgcc'], 'image': value['images']['supplier']}, 'python': python, 'wheels': wheels}


def validate_native_dependencies(value, target):
    """Pure closed receipt validation; artifact consumers also compare actual ELF bytes.

This verifies the receipt's internal proof and origin references. It does not
replace CArchive/extracted-byte custody or establish missing supplier facts.
"""
    keys(value, ('libraries', 'symbolClosureVerified', 'libgccSelection', 'origins'), 'native dependencies')
    require(value['symbolClosureVerified'] is True, 'Native symbol closure was not established')
    libraries = value['libraries']; require(isinstance(libraries, list), 'Invalid native libraries')
    require(0 < len(libraries) <= MAX_FILES and all(isinstance(row, dict) and isinstance(row.get('path'), str) for row in libraries), 'Invalid native library rows')
    require(libraries == sorted(libraries, key=lambda row: row['path']), 'Native libraries must be sorted')
    def digest(value):
        require(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value), 'Invalid native receipt digest')
    def file_row(row, maximum=1024 * 1024):
        keys(row, ('path', 'sha256', 'byteLength'), 'native license file')
        safe_path(row['path']); digest(row['sha256'])
        require(type(row['byteLength']) is int and 0 < row['byteLength'] <= maximum, 'Invalid native receipt size')
    origins = value['origins']; keys(origins, ('supplier', 'python', 'wheels'), 'native origins')
    supplier = origins['supplier']; keys(supplier, ('schema', 'package', 'library', 'licenses', 'image'), 'supplier origin')
    require(supplier['schema'] == 'openprose.sdk-native-origin/2', 'Invalid supplier origin schema')
    keys(supplier['package'], ('name', 'epoch', 'version', 'release', 'architecture', 'sourceRpm', 'license'), 'supplier package')
    arch = {'linux-x64-gnu': 'x86_64', 'linux-arm64-gnu': 'aarch64'}.get(target)
    require(arch is not None, 'Unsupported native receipt target')
    require(supplier['package']['architecture'] == arch and supplier['package']['name'] == 'libgcc' and all(isinstance(x, str) and x for x in supplier['package'].values()),
            'Invalid supplier package metadata')
    validate_supplier_library(supplier['library'])
    keys(supplier['image'], ('image', 'configSha256', 'metadataUrl'), 'supplier image')
    digest(supplier['image']['configSha256'])
    require(re.fullmatch(r'quay\.io/pypa/manylinux_2_28_' + arch + r'@sha256:[0-9a-f]{64}', supplier['image']['image']), 'Supplier image target/floor mismatch')
    require(re.fullmatch(r'[0-9]+', supplier['package']['epoch']) and supplier['package']['sourceRpm'].endswith('.src.rpm'), 'Supplier RPM identity is incomplete')
    require(isinstance(supplier['licenses'], list) and supplier['licenses'], 'Missing supplier licenses')
    for row in supplier['licenses']:
        keys(row, ('path', 'sha256', 'byteLength', 'packagePath'), 'supplier license')
        file_row({k: row[k] for k in ('path', 'sha256', 'byteLength')})
    python = origins['python']; keys(python, ('archive', 'metadataSha256', 'licenses', 'declaredExtensionLicenses'), 'Python origin')
    keys(python['archive'], ('url', 'sha256', 'byteLength', 'targetTriple', 'metadataUrl'), 'Python origin archive')
    digest(python['metadataSha256']); digest(python['archive']['sha256'])
    require(python['archive']['targetTriple'] == arch + '-unknown-linux-gnu' and
            re.fullmatch(r'https://github\.com/astral-sh/python-build-standalone/releases/download/[0-9]+/cpython-3\.10\.20%2B[0-9]+-' + arch + r'-unknown-linux-gnu-pgo%2Blto-full\.tar\.zst', python['archive']['url']), 'Python archive target/type mismatch')
    require(type(python['archive']['byteLength']) is int and python['archive']['byteLength'] > 0, 'Invalid Python archive size')
    require(isinstance(python['licenses'], list) and python['licenses'], 'Missing Python license evidence')
    for row in python['licenses']: file_row(row)
    require(isinstance(python['declaredExtensionLicenses'], list), 'Invalid Python static dependencies')
    for row in python['declaredExtensionLicenses']:
        keys(row, ('name', 'licenses', 'licensePaths'), 'Python static dependency')
        require(isinstance(row['name'], str) and row['name'] and isinstance(row['licenses'], list) and row['licenses'] and
                isinstance(row['licensePaths'], list) and row['licensePaths'] and
                all(path in {r['path'] for r in python['licenses']} for path in row['licensePaths']),
                'Unbound Python static dependency licenses')
    wheels = origins['wheels']; require(isinstance(wheels, list) and wheels == sorted(wheels, key=lambda r: r['name']), 'Unsorted native wheel origins')
    names = set()
    for row in wheels:
        keys(row, ('name', 'version', 'wheelCandidateSha256s', 'recordSha256', 'licenses'), 'native wheel origin')
        require(isinstance(row['name'], str) and re.fullmatch('[a-z0-9]+(?:-[a-z0-9]+)*', row['name']) and row['name'] not in names,
                'Duplicate/invalid wheel origin name'); names.add(row['name'])
        require(isinstance(row['version'], str) and row['version'], 'Invalid wheel origin version')
        digest(row['recordSha256'])
        require(isinstance(row['wheelCandidateSha256s'], list) and row['wheelCandidateSha256s'] and row['wheelCandidateSha256s'] == sorted(set(row['wheelCandidateSha256s'])), 'Invalid locked wheel candidates')
        for candidate in row['wheelCandidateSha256s']: digest(candidate)
        require(isinstance(row['licenses'], list), 'Invalid wheel licenses')
        for license_row in row['licenses']: file_row(license_row)
    for row in libraries:
        keys(row, ('path', 'sha256', 'byteLength', 'machine', 'needed', 'versionNeeds', 'importedSymbols',
                   'definedSymbols', 'maximumRequiredGlibc', 'origin'), 'native library')
        file_row({k: row[k] for k in ('path', 'sha256', 'byteLength')}, MAX_BYTES)
        require(type(row['machine']) is int and isinstance(row['needed'], list) and
                all(isinstance(x, str) and x for x in row['needed']), 'Invalid ELF receipt machine/needed libraries')
        require(row['maximumRequiredGlibc'] is None or (isinstance(row['maximumRequiredGlibc'], str) and
                re.fullmatch(r'[0-9]+\.[0-9]+(?:\.[0-9]+)?', row['maximumRequiredGlibc'])), 'Invalid GLIBC requirement')
        require(all(isinstance(row[field], list) and len(row[field]) <= 100000 for field in ('needed', 'versionNeeds', 'importedSymbols', 'definedSymbols')), 'Invalid ELF receipt arrays')
        for need in row['versionNeeds']:
            keys(need, ('library', 'version', 'flags'), 'native version need')
            require(need['library'] in row['needed'] and isinstance(need['version'], str) and need['version'] and
                    type(need['flags']) is int and 0 <= need['flags'] <= 65535, 'Invalid native version need')
        for symbol in row['importedSymbols']:
            keys(symbol, ('symbol', 'library', 'version', 'weak'), 'native imported symbol')
            require(isinstance(symbol['symbol'], str) and symbol['symbol'] and type(symbol['weak']) is bool and
                    ((symbol['library'] is None and symbol['version'] is None) or
                     (symbol['library'] in row['needed'] and isinstance(symbol['version'], str) and symbol['version'])), 'Invalid native imported symbol')
        for symbol in row['definedSymbols']:
            keys(symbol, ('symbol', 'version', 'defaultVersion'), 'native defined symbol')
            require(isinstance(symbol['symbol'], str) and symbol['symbol'] and type(symbol['defaultVersion']) is bool and
                    (symbol['version'] is None or (isinstance(symbol['version'], str) and symbol['version'])), 'Invalid native defined symbol')
        reference = row['origin']
        require(reference in ('python', 'supplier') or (isinstance(reference, str) and reference.startswith('wheel:') and reference[6:] in names), 'Unknown native origin reference')
        if reference.startswith('wheel:'):
            require(next(r for r in wheels if r['name'] == reference[6:])['licenses'], 'Native wheel has no license evidence')
    selection = value['libgccSelection']; keys(selection, ('path', 'sha256', 'byteLength', 'analysisTocSha256'), 'libgcc selection')
    digest(selection['analysisTocSha256'])
    actual_supplier = next((r for r in libraries if r['path'] == 'libgcc_s.so.1'), None)
    require(actual_supplier is not None and actual_supplier['origin'] == 'supplier' and
            all(selection[k] == actual_supplier[k] == supplier['library'][k] for k in ('path', 'sha256', 'byteLength')),
            'libgcc selection differs from supplier bytes')
    verify_closure(libraries, target)
    require(len(json.dumps(value, sort_keys=True).encode('utf-8')) <= 2 * 1024 * 1024, 'Native receipt exceeds size bound')
    return value


def validate_linux_receipt(receipt):
    """Validate Linux additions against the enclosing unchanged SDK receipt.

Consumers separately hash the exact helper/notices bytes. The driver's pinned
input and before/after snapshots bind build custody; this pure check requires
all source, dependency and extracted-library claims to agree internally.
"""
    require(receipt.get('platform') == 'linux', 'Linux native receipt expected')
    target = {'aarch64': 'linux-arm64-gnu', 'x86_64': 'linux-x64-gnu'}.get(receipt.get('architecture'))
    require(target is not None, 'Unsupported Linux receipt architecture')
    require(isinstance(receipt.get('linuxBuildInputSha256'), str) and
            re.fullmatch('[0-9a-f]{64}', receipt['linuxBuildInputSha256']), 'Missing Linux build input binding')
    snapshot = receipt.get('linuxBuildSourceSnapshot')
    keys(snapshot, ('sources', 'driverSha256', 'lockSha256'), 'Linux receipt source snapshot')
    keys(snapshot['sources'], SOURCE_PATHS, 'Linux receipt source files')
    require(all(isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest)
                for digest in [*snapshot['sources'].values(), snapshot['driverSha256'], snapshot['lockSha256']]),
            'Invalid Linux receipt source digest')
    sources = receipt.get('sources'); keys(sources, ('harnesses/agents-sdk/run.py', 'harnesses/agents-sdk/requirements-build.txt'), 'SDK receipt sources')
    require(all(snapshot['sources'][name] == digest for name, digest in sources.items()), 'SDK source scopes disagree')
    deps = validate_native_dependencies(receipt.get('nativeDependencies'), target)
    packages = receipt.get('dependencies'); require(isinstance(packages, list), 'SDK dependencies are absent')
    expected = {row['name']: (row['version'], row['wheelSha256']) for row in packages}
    require(len(expected) == len(packages), 'Duplicate SDK dependencies')
    actual = {row['name']: (row['version'], row['wheelCandidateSha256s']) for row in deps['origins']['wheels']}
    require(actual == expected, 'Native wheel origins differ from locked SDK candidates')
    runtime = receipt.get('linuxLibraries')
    keys(runtime, ('schema', 'elfCount', 'requiredGlibcMaximum', 'libraries', 'modelCalls'), 'extracted Linux inspection')
    raw = [{key: value for key, value in row.items() if key != 'origin'} for row in deps['libraries']]
    require(runtime['schema'] == 'openprose.sdk-packaged-libraries/1' and type(runtime['elfCount']) is int and
            runtime['elfCount'] == len(raw) and type(runtime['modelCalls']) is int and runtime['modelCalls'] == 0 and
            runtime['libraries'] == raw, 'Extracted Linux inspection differs from native receipt')
    maximum = max((tuple(map(int, row['maximumRequiredGlibc'].split('.'))) for row in raw
                   if row['maximumRequiredGlibc'] is not None), default=None)
    require(maximum is not None and runtime['requiredGlibcMaximum'] == '.'.join(map(str, maximum)),
            'Extracted GLIBC aggregate differs from native receipt')
    return deps


# This allowance is restricted to the package-owned Mac SDK support tree. Generic
# publication archives keep their separate 128-member and no-link policies.
SDK_PAYLOAD_ROOT = 'prose-agents-sdk-runtime'
SDK_HELPER = 'prose-agents-sdk'
SDK_PAYLOAD_MAX_ENTRIES = 8192
SDK_PAYLOAD_MAX_BYTES = 512 * 1024 * 1024
SDK_PAYLOAD_MAX_METADATA = 2 * 1024 * 1024
SDK_BUILDER_SOURCES = ('cli/ci/build_agents_sdk.py', 'cli/ci/sdk_native_inventory.py')


def _payload_path(value):
    safe_path(value)
    require(len(value.encode('utf-8')) <= 1024 and
            ':' not in value and all(unicodedata.category(c) not in ('Cc', 'Cf', 'Cs') for c in value), 'Invalid SDK payload path')
    return value


def _payload_target(value):
    require(isinstance(value, str) and 0 < len(value.encode('utf-8')) <= 1024 and
            not value.startswith('/') and '\\' not in value and
            ':' not in value and all(unicodedata.category(c) not in ('Cc', 'Cf', 'Cs') for c in value), 'Invalid SDK alias target')
    return value


def _resolve_payload_alias(path, entries):
    """Resolve declared POSIX links with active frames and bounded work.

    Active expansions reject A->A/A immediately; completion frames allow a
    legitimate alias to be reused later, such as Current/../Current/Python.
    """
    pending = deque(PurePosixPath(path).parts); resolved = []; active = set(); steps = 0
    pending_bytes = sum(len(p.encode('utf-8')) for p in pending); work = pending_bytes
    while pending:
        steps += 1
        require(steps <= SDK_PAYLOAD_MAX_ENTRIES * 4, 'SDK alias resolution exceeds bound')
        part = pending.popleft()
        if isinstance(part, tuple):
            active.remove(part[0]); continue
        pending_bytes -= len(part.encode('utf-8'))
        if part == '.': continue
        if part == '..':
            require(len(resolved) > 1, 'SDK alias escapes support root')
            resolved.pop(); continue
        candidate = '/'.join([*resolved, part])
        require(candidate in entries, 'SDK alias is dangling')
        row = entries[candidate]
        if row['type'] == 'symlink':
            require(candidate not in active, 'SDK alias cycle')
            require(len(active) < 128, 'SDK alias nesting exceeds bound')
            target = PurePosixPath(row['target']).parts
            size = sum(len(p.encode('utf-8')) for p in target); work += size
            require(len(pending) + len(target) + 1 <= 4096 and pending_bytes + size <= 8192 and
                    work <= 1024 * 1024, 'SDK alias expansion work exceeds bound')
            active.add(candidate); pending.appendleft((candidate,))
            pending.extendleft(reversed(target)); pending_bytes += size
            continue
        require(not any(isinstance(p, str) for p in pending) or row['type'] == 'directory',
                'SDK alias traverses a regular file')
        resolved.append(part)
    result = '/'.join(resolved)
    require(result in entries and entries[result]['type'] in ('file', 'directory'),
            'SDK alias does not resolve to a real member')
    return result


def macho_architectures(data):
    """Inspect bounded thin/fat Mach-O headers and load-command layout.

    CPU values/layouts follow Apple mach-o/loader.h and fat.h. Ordinary resource
    bytes return None; a recognized malformed native header is never data.
    """
    require(isinstance(data, bytes) and len(data) <= MAX_BYTES, 'Oversized Mach-O inspection')
    magic = data[:4]
    thin = {b'\xcf\xfa\xed\xfe': ('<', 32), b'\xfe\xed\xfa\xcf': ('>', 32),
            b'\xce\xfa\xed\xfe': ('<', 28), b'\xfe\xed\xfa\xce': ('>', 28)}
    fat = {b'\xca\xfe\xba\xbe': ('>', False), b'\xbe\xba\xfe\xca': ('<', False),
           b'\xca\xfe\xba\xbf': ('>', True), b'\xbf\xba\xfe\xca': ('<', True)}
    if magic not in thin and magic not in fat:
        return None
    cpu_names = {0x1000007: 'x86_64', 0x100000c: 'arm64'}
    if magic in thin:
        endian, size = thin[magic]
        require(size == 32 and len(data) >= size, 'Unsupported Mach-O header')
        _, cpu, _, kind, count, commands_size, _, _ = struct.unpack_from(endian + 'IIIIIIII', data)
        require(cpu in cpu_names and kind in (2, 6, 8), 'Unsupported Mach-O target or file type')
        require(count <= 65536 and commands_size <= len(data) - size, 'Invalid Mach-O command table')
        position = size
        for _ in range(count):
            require(position + 8 <= size + commands_size, 'Truncated Mach-O command')
            _, length = struct.unpack_from(endian + 'II', data, position)
            require(length >= 8 and length % 4 == 0 and position + length <= size + commands_size,
                    'Invalid Mach-O command size')
            position += length
        require(position == size + commands_size, 'Mach-O command count differs')
        return (cpu_names[cpu],)
    endian, wide = fat[magic]
    require(len(data) >= 8, 'Truncated fat Mach-O header')
    count = struct.unpack_from(endian + 'I', data, 4)[0]; row_size = 32 if wide else 20
    require(0 < count <= 16 and 8 + count * row_size <= len(data), 'Invalid fat Mach-O count')
    result = []; ranges = []; identities = set()
    for index in range(count):
        values = struct.unpack_from(endian + ('IIQQII' if wide else 'IIIII'), data, 8 + index * row_size)
        cpu, subtype, offset, length, alignment = values[:5]
        require(cpu in cpu_names and (cpu, subtype) not in identities, 'Duplicate or unsupported fat Mach-O target')
        identities.add((cpu, subtype))
        require(alignment <= 31 and offset % (1 << alignment) == 0 and
                offset >= 8 + count * row_size and length >= 32 and offset + length <= len(data),
                'Invalid fat Mach-O slice')
        require(all(offset + length <= a or offset >= b for a, b in ranges), 'Overlapping fat Mach-O slices')
        ranges.append((offset, offset + length))
        require(data[offset:offset + 4] in thin, 'Fat Mach-O slice must be thin')
        slice_endian = thin[data[offset:offset + 4]][0]
        require(struct.unpack_from(slice_endian + 'II', data, offset + 4) == (cpu, subtype),
                'Fat Mach-O subtype differs from slice')
        actual = macho_architectures(data[offset:offset + length])
        require(actual == (cpu_names[cpu],), 'Fat Mach-O CPU differs from slice')
        result.extend(actual)
    return tuple(sorted(set(result)))


def _macos_payload_structure(payload, architecture):
    """Shared closed metadata/alias validation without claiming native bytes."""
    keys(payload, ('layout', 'root', 'entries', 'totalRegularBytes', 'builderSources',
                   'entrySourceSha256', 'collectTocSha256', 'codeSignaturePaths'), 'Mac SDK payload')
    require(payload['layout'] == 'pyinstaller-onedir/1' and payload['root'] == SDK_PAYLOAD_ROOT and
            architecture in ('arm64', 'x86_64'), 'Unsupported Mac SDK layout/target')
    keys(payload['builderSources'], SDK_BUILDER_SOURCES, 'Mac SDK builder sources')
    hashes = [*payload['builderSources'].values(), payload['entrySourceSha256'], payload['collectTocSha256']]
    require(all(isinstance(h, str) and re.fullmatch('[0-9a-f]{64}', h) for h in hashes), 'Invalid Mac SDK source digest')
    require(isinstance(payload['entries'], list) and 0 < len(payload['entries']) <= SDK_PAYLOAD_MAX_ENTRIES,
            'Invalid Mac SDK member count')
    require(len(json.dumps(payload, sort_keys=True).encode('utf-8')) <= SDK_PAYLOAD_MAX_METADATA,
            'Mac SDK payload metadata exceeds bound')
    entries = {}; portable_names = set(); expected_files = {SDK_HELPER}; expected_dirs = set(); expected_links = set(); total = 0
    for row in payload['entries']:
        require(isinstance(row, dict), 'Invalid SDK payload entry')
        kind = row.get('type')
        shape = {'file': ('path', 'type', 'sha256', 'byteLength', 'mode'),
                 'directory': ('path', 'type', 'mode'), 'symlink': ('path', 'type', 'target', 'resolvedPath')}
        require(isinstance(kind, str) and kind in shape, 'Unsupported SDK payload member type'); keys(row, shape[kind], 'SDK payload entry')
        path = _payload_path(row['path'])
        require((path == SDK_PAYLOAD_ROOT or path.startswith(SDK_PAYLOAD_ROOT + '/')) and path not in entries,
                'Foreign or duplicate SDK payload member')
        portable = unicodedata.normalize('NFC', unicodedata.normalize('NFC', path).casefold())
        require(portable not in portable_names, 'Portable SDK path collision')
        portable_names.add(portable); entries[path] = row
        if kind == 'file':
            require(type(row['mode']) is int and row['mode'] in (0o644, 0o755) and
                    type(row['byteLength']) is int and 0 <= row['byteLength'] <= MAX_BYTES and
                    isinstance(row['sha256'], str) and re.fullmatch('[0-9a-f]{64}', row['sha256']),
                    'Invalid SDK regular file metadata')
            expected_files.add(path); total += row['byteLength']
        elif kind == 'directory':
            require(type(row['mode']) is int and row['mode'] == 0o755, 'Invalid SDK directory mode'); expected_dirs.add(path)
        else:
            _payload_target(row['target']); _payload_path(row['resolvedPath']); expected_links.add(path)
    require(list(entries) == sorted(entries), 'SDK payload entries are not sorted')
    require(SDK_PAYLOAD_ROOT in entries and entries[SDK_PAYLOAD_ROOT]['type'] == 'directory', 'Missing SDK support root')
    require(type(payload['totalRegularBytes']) is int and total == payload['totalRegularBytes'] and
            total <= SDK_PAYLOAD_MAX_BYTES, 'SDK payload aggregate bytes differ/exceed bound')
    for path, row in entries.items():
        if path != SDK_PAYLOAD_ROOT:
            parent = str(PurePosixPath(path).parent)
            require(parent in entries and entries[parent]['type'] == 'directory', 'SDK physical member beneath alias/missing directory')
        if row['type'] == 'symlink':
            require(_resolve_payload_alias(path, entries) == row['resolvedPath'], 'SDK alias resolved member differs')
    signatures = payload['codeSignaturePaths']
    require(isinstance(signatures, list) and signatures and all(isinstance(p, str) for p in signatures) and
            signatures == sorted(set(signatures)) and SDK_HELPER in signatures and
            set(signatures) <= expected_files, 'Invalid SDK signature path declaration')
    return entries, expected_files, expected_dirs, expected_links


def validate_macos_payload_structure(payload, architecture):
    """Validate metadata only; native bytes/signatures remain unverified here."""
    _macos_payload_structure(payload, architecture)
    return payload


def validate_macos_payload(payload, files, directories, symlinks, architecture):
    """Validate COMPLETE SDK-scoped typed tables before any byte projection."""
    entries, expected_files, expected_dirs, expected_links = _macos_payload_structure(payload, architecture)
    require(all(isinstance(table, dict) for table in (files, directories, symlinks)), 'Invalid SDK typed member view')
    require(set(files) == expected_files and set(directories) == expected_dirs and set(symlinks) == expected_links,
            'SDK complete typed member inventory differs')
    native_paths = []
    for path in sorted(files):
        item = files[path]
        require(type(item) is tuple and len(item) == 2 and isinstance(item[0], bytes) and
                type(item[1]) is int and item[1] in (0o644, 0o755) and len(item[0]) <= MAX_BYTES,
                'Invalid SDK file view')
        data, mode = item
        if path == SDK_HELPER:
            require(mode == 0o755 and data, 'Invalid SDK helper mode/bytes')
        else:
            row = entries[path]
            require(mode == row['mode'] and len(data) == row['byteLength'] and sha(data) == row['sha256'],
                    'SDK support file bytes/mode differ')
        actual = macho_architectures(data)
        if actual is not None:
            require(actual == (architecture,), 'SDK Mach-O architecture differs from target')
            native_paths.append(path)
        elif path == SDK_HELPER:
            require(False, 'SDK helper must be Mach-O')
    require(payload['codeSignaturePaths'] == native_paths, 'SDK code signature path closure differs')
    require(all(type(mode) is int and mode == entries[path]['mode'] for path, mode in directories.items()),
            'SDK directory table differs')
    require(all(target == entries[path]['target'] for path, target in symlinks.items()), 'SDK literal alias target differs')
    return payload


def _owned_payload_root(root):
    root = Path(root)
    require(root.is_dir() and not root.is_symlink() and root == root.resolve(), 'SDK sibling directory must be canonical')
    require(root.stat().st_uid == os.getuid(), 'SDK output directory must be owned')
    for parent in root.parents:
        require(not parent.is_symlink(), 'SDK sibling directory has symlink ancestor')
    return root


def _payload_file(path):
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and 0 <= before.st_size <= MAX_BYTES,
            'SDK payload file is not bounded regular bytes')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        require((opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino), 'SDK payload identity changed before read')
        data = stream.read(MAX_BYTES + 1)
    after = path.lstat()
    require(len(data) == before.st_size and
            (before.st_dev, before.st_ino, before.st_size, before.st_mode, before.st_mtime_ns) ==
            (after.st_dev, after.st_ino, after.st_size, after.st_mode, after.st_mtime_ns), 'SDK payload changed while reading')
    return data, stat.S_IMODE(before.st_mode)


def _read_payload_view(root):
    root = _owned_payload_root(root); files = {SDK_HELPER: _payload_file(root / SDK_HELPER)}; directories = {}; links = {}
    support = root / SDK_PAYLOAD_ROOT
    regular_bytes = [0]
    require(support.is_dir() and not support.is_symlink(), 'Missing/aliased SDK support directory')
    def walk(path):
        relative = path.relative_to(root).as_posix(); _payload_path(relative); before = path.lstat()
        require(len(files) + len(directories) + len(links) <= SDK_PAYLOAD_MAX_ENTRIES + 1, 'SDK tree exceeds member bound')
        if stat.S_ISLNK(before.st_mode):
            links[relative] = os.readlink(path); _payload_target(links[relative]); return
        if stat.S_ISREG(before.st_mode):
            require(regular_bytes[0] + before.st_size <= SDK_PAYLOAD_MAX_BYTES, 'SDK physical tree exceeds aggregate bytes')
            files[relative] = _payload_file(path); regular_bytes[0] += len(files[relative][0]); return
        require(stat.S_ISDIR(before.st_mode), 'Unsupported physical SDK member')
        directories[relative] = stat.S_IMODE(before.st_mode)
        children = []
        for child in path.iterdir():
            children.append(child)
            require(len(children) <= SDK_PAYLOAD_MAX_ENTRIES, 'SDK directory exceeds member bound')
        children.sort()
        for child in children: walk(child)
        after = path.lstat()
        require((before.st_dev, before.st_ino, before.st_mode, before.st_mtime_ns) ==
                (after.st_dev, after.st_ino, after.st_mode, after.st_mtime_ns), 'SDK tree changed while enumerating')
    walk(support)
    return {'files': files, 'directories': directories, 'symlinks': links}


def read_macos_payload(root, payload, architecture):
    validate_macos_payload_structure(payload, architecture)
    view = _read_payload_view(root)
    validate_macos_payload(payload, architecture=architecture, **view)
    return view


def materialize_macos_payload(destination, payload, files, directories, symlinks, architecture):
    validate_macos_payload(payload, files, directories, symlinks, architecture)
    destination = _owned_payload_root(destination)
    require(not os.path.lexists(destination / SDK_HELPER) and not os.path.lexists(destination / SDK_PAYLOAD_ROOT),
            'SDK materialization requires absent SDK paths')
    # Parent directories are physical, never links. Links are created last and no
    # file is ever opened through a declared alias. Partial failures are retained.
    for relative in sorted(directories, key=lambda p: (len(PurePosixPath(p).parts), p)):
        path = destination / relative; path.mkdir(mode=0o755); path.chmod(directories[relative])
    for relative in sorted(files):
        path = destination / relative
        require(not any(parent.is_symlink() for parent in path.parents), 'SDK file parent became aliased')
        with path.open('xb') as stream: stream.write(files[relative][0])
        path.chmod(files[relative][1])
    for relative in sorted(symlinks):
        path = destination / relative
        require(not any(parent.is_symlink() for parent in path.parents), 'SDK alias parent became aliased')
        path.symlink_to(symlinks[relative])
    return read_macos_payload(destination, payload, architecture)



def inventory_macos_payload(root, architecture, builder_sources, entry_source_sha256, collect_toc_sha256):
    """Builder-only construction from actual final bytes; consumers validate it."""
    view = _read_payload_view(root); entries = []
    for path, (data, mode) in view['files'].items():
        if path != SDK_HELPER:
            entries.append({'path': path, 'type': 'file', 'sha256': sha(data), 'byteLength': len(data), 'mode': mode})
    entries.extend({'path': p, 'type': 'directory', 'mode': mode} for p, mode in view['directories'].items())
    entries.extend({'path': p, 'type': 'symlink', 'target': target, 'resolvedPath': ''} for p, target in view['symlinks'].items())
    by_path = {r['path']: r for r in entries}
    for row in entries:
        if row['type'] == 'symlink': row['resolvedPath'] = _resolve_payload_alias(row['path'], by_path)
    result = {'layout': 'pyinstaller-onedir/1', 'root': SDK_PAYLOAD_ROOT,
              'entries': sorted(entries, key=lambda r: r['path']),
              'totalRegularBytes': sum(len(data) for p, (data, _) in view['files'].items() if p != SDK_HELPER),
              'builderSources': builder_sources, 'entrySourceSha256': entry_source_sha256,
              'collectTocSha256': collect_toc_sha256,
              'codeSignaturePaths': sorted(p for p, (data, _) in view['files'].items() if macho_architectures(data) is not None)}
    validate_macos_payload(result, architecture=architecture, **view)
    return result
