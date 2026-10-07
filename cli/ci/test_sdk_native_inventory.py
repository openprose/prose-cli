import ast
import base64
import copy
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import sdk_native_inventory as native


def elf(*, imported=False, machine=62, version='GCC_3.0', symbol='_Unwind_Resume', glibc='2.17'):
    strings = b'\0libgcc_s.so.1\0' + version.encode() + b'\0' + symbol.encode() + b'\0'
    version_offset = 15; symbol_offset = version_offset + len(version) + 1
    symbols = bytes(24) + struct.pack('<IBBHQQ', symbol_offset, 0x12, 0, 0 if imported else 1, 0, 0)
    versions = struct.pack('<HH', 0, 2)
    if imported:
        chain = struct.pack('<HHIII', 1, 1, 1, 16, 0) + struct.pack('<IHHII', 0, 0, 2, version_offset, 0)
        dynamic = struct.pack('<qQqQqQ', 1, 1, 0x6fffffff, 1, 0, 0)
        kind = 0x6ffffffe
    else:
        chain = struct.pack('<HHHHIII', 1, 0, 2, 1, 0, 20, 0) + struct.pack('<II', version_offset, 0)
        dynamic = struct.pack('<qQqQ', 0x6ffffffd, 1, 0, 0)
        kind = 0x6ffffffd
    blob = bytearray(64)
    headers = [bytes(64)]
    for section_type, content, link, info, entry_size in ((3, strings, 0, 0, 0), (11, symbols, 1, 0, 24),
            (0x6fffffff, versions, 2, 0, 2), (6, dynamic, 1, 0, 16), (kind, chain, 1, 1, 0)):
        offset = len(blob); blob.extend(content)
        headers.append(struct.pack('<IIQQQQIIQQ', 0, section_type, 0, 0, offset, len(content), link, info, 1, entry_size))
    blob.extend(('GLIBC_' + glibc + '\0').encode()); shoff = len(blob)
    for header in headers: blob.extend(header)
    blob[:16] = b'\x7fELF\x02\x01\x01' + bytes(9)
    struct.pack_into('<HHIQQQIHHHHHH', blob, 16, 3, machine, 1, 0, 0, shoff, 0, 64, 0, 0, 64, len(headers), 0)
    return bytes(blob)


def macho_fixture(architecture='x86_64', kind=6):
    """Header-only, nonexecuted Mach-O parser fixture; no native success claim."""
    cpu = {'x86_64': 0x1000007, 'arm64': 0x100000c}[architecture]
    return struct.pack('<IIIIIIII', 0xfeedfacf, cpu, 3 if architecture == 'x86_64' else 0, kind, 0, 0, 0, 0)


def onedir_fixture(architecture='x86_64'):
    root = native.SDK_PAYLOAD_ROOT
    files = {native.SDK_HELPER: (macho_fixture(architecture, 2), 0o755),
             root + '/Python.framework/Versions/3.10/Python': (macho_fixture(architecture), 0o755),
             root + '/base_library.zip': (b'synthetic base library', 0o644),
             root + '/empty-data': (b'', 0o644)}
    directories = {p: 0o755 for p in (root, root + '/Python.framework',
                    root + '/Python.framework/Versions', root + '/Python.framework/Versions/3.10')}
    links = {root + '/Python.framework/Python': 'Versions/Current/Python',
             root + '/Python.framework/Versions/Current': '3.10'}
    entries = [{'path': p, 'type': 'file', 'sha256': native.sha(data), 'byteLength': len(data), 'mode': mode}
               for p, (data, mode) in files.items() if p != native.SDK_HELPER]
    entries += [{'path': p, 'type': 'directory', 'mode': mode} for p, mode in directories.items()]
    entries += [{'path': p, 'type': 'symlink', 'target': target,
                 'resolvedPath': root + '/Python.framework/Versions/3.10' + ('/Python' if p.endswith('/Python') else '')}
                for p, target in links.items()]
    payload = {'layout': 'pyinstaller-onedir/1', 'root': root, 'entries': sorted(entries, key=lambda r: r['path']),
               'totalRegularBytes': sum(len(data) for p, (data, _) in files.items() if p != native.SDK_HELPER),
               'builderSources': {'cli/ci/build_agents_sdk.py': 'a' * 64, 'cli/ci/sdk_native_inventory.py': 'b' * 64},
               'entrySourceSha256': 'c' * 64, 'collectTocSha256': 'd' * 64,
               'codeSignaturePaths': sorted([native.SDK_HELPER, root + '/Python.framework/Versions/3.10/Python'])}
    return {'payload': payload, 'files': files, 'directories': directories, 'symlinks': links, 'architecture': architecture}


def input_fixture(root, target='linux-x64-gnu'):
    arch = 'x86_64' if target == 'linux-x64-gnu' else 'aarch64'
    machine = 62 if arch == 'x86_64' else 183
    supplier = root / 'supplier'; supplier.mkdir()
    library = supplier / 'libgcc_s.so.1'; library.write_bytes(elf(machine=machine))
    license_text = b'GNU GENERAL PUBLIC LICENSE\nVersion 3\nGCC RUNTIME LIBRARY EXCEPTION\nVersion 3.1\nfixture-only text'
    (supplier / 'license-0.txt').write_bytes(license_text)
    source = root / 'source'; source.mkdir()
    for name in native.SOURCE_PATHS:
        path = source / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('fixture ' + name)
    python = root / 'python'; python.mkdir()
    (python / 'LICENSE').write_text('Python fixture license')
    (python / 'libpython.so').write_bytes(elf(machine=machine, symbol='Py_Initialize'))
    info = {'version': '8', 'python_version': '3.10.20', 'target_triple': arch + '-unknown-linux-gnu', 'libpython_link_mode': 'shared',
            'licenses': ['Python-2.0'], 'license_path': 'LICENSE', 'build_info': {'core': {'shared_lib': 'libpython.so'}, 'extensions': {}}}
    metadata = python / 'PYTHON.json'; metadata.write_text(json.dumps(info))
    image = lambda floor: {'image': 'quay.io/pypa/manylinux_' + floor + '_' + arch + '@sha256:' + 'a' * 64,
                          'configSha256': 'b' * 64, 'metadataUrl': 'https://quay.io/fixture'}
    origin = {'schema': 'openprose.sdk-native-origin/2',
              'package': {'name': 'libgcc', 'epoch': '0', 'version': '8.5.0', 'release': 'fixture', 'architecture': arch,
                          'sourceRpm': 'gcc-8.5.0-fixture.src.rpm', 'license': 'GPLv3+ and GPLv3+ with exceptions'},
              'library': {**native.record('libgcc_s.so.1', library.read_bytes()), 'supplierPath': '/usr/lib64/libgcc_s-8.so.1', 'rpmFileDigestVerified': True,
                          'rpmPayloadPath': '/lib64/libgcc_s-8.so.1', 'rpmFileDigestAlgorithm': 8,
                          'rpmFileDigestSha256': native.sha(library.read_bytes())},
              'licenses': [{**native.record('license-0.txt', license_text), 'packagePath': '/usr/share/licenses/libgcc/COPYING'}]}
    # Canonical encoded URLs exactly match the approved v3 driver lock's full archive pins.
    version = '20260807'
    archive = {'url': 'https://github.com/astral-sh/python-build-standalone/releases/download/' + version + '/cpython-3.10.20%2B' + version + '-' + arch + '-unknown-linux-gnu-pgo%2Blto-full.tar.zst',
               # Synthetic provider metadata must not impersonate an inspected
               # archive whose exact PYTHON.json hash is part of the erratum.
               'sha256': 'e' * 64,
               'byteLength': 66186516 if arch == 'x86_64' else 65985139, 'targetTriple': arch + '-unknown-linux-gnu',
               'metadataUrl': 'https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/20260807'}
    value = {'schema': 'openprose.sdk-linux-native-input/1', 'target': target,
             'images': {'supplier': image('2_28'), 'freezer': image('2_34')}, 'pythonArchive': archive,
             'sourceSnapshot': {'sources': {name: native.sha((source / name).read_bytes()) for name in native.SOURCE_PATHS},
                                'driverSha256': 'c' * 64, 'lockSha256': 'd' * 64},
             'driverSha256': 'c' * 64, 'lockSha256': 'd' * 64, 'libgcc': origin,
             'pythonDistribution': {'root': str(python), 'metadataPath': str(metadata), 'metadataSha256': native.sha(metadata.read_bytes())}}
    path = root / 'native-input.json'; path.write_text(json.dumps(value))
    return value, path, library, source


class NativeInventoryTests(unittest.TestCase):
    def test_frozen_inspection_module_imports_only_standard_library(self):
        # PyInstaller scans function-body IMPORT_NAME too: keep build tooling out
        # of every static import in the actual module bundled for consumers.
        tree = ast.parse(Path(native.__file__).read_text('utf-8'))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                imports.add(node.module.split('.')[0])
        self.assertTrue(imports)
        self.assertEqual(imports - sys.stdlib_module_names, set())
        self.assertNotIn('PyInstaller', imports)

    def test_actual_elf_import_definition_and_needed_version_layout(self):
        supplier = native.elf_record('libgcc_s.so.1', elf())
        consumer = native.elf_record('extension.so', elf(imported=True))
        self.assertEqual(supplier['definedSymbols'], [{'symbol': '_Unwind_Resume', 'version': 'GCC_3.0', 'defaultVersion': True}])
        self.assertEqual(consumer['importedSymbols'], [{'symbol': '_Unwind_Resume', 'library': 'libgcc_s.so.1', 'version': 'GCC_3.0', 'weak': False}])
        self.assertEqual(consumer['needed'], ['libgcc_s.so.1'])
        self.assertEqual(consumer['versionNeeds'], [{'library': 'libgcc_s.so.1', 'version': 'GCC_3.0', 'flags': 0}])
        self.assertTrue(native.verify_closure([consumer, supplier], 'linux-x64-gnu'))

    def test_missing_strong_symbol_and_version_are_rejected_separately(self):
        supplier = native.elf_record('libgcc_s.so.1', elf())
        for data, reason in ((elf(imported=True, symbol='missing'), 'strong symbol'),
                             (elf(imported=True, version='GCC_99.0'), 'required version')):
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                native.verify_closure([supplier, native.elf_record('consumer.so', data)], 'linux-x64-gnu')

    def test_wrong_machine_duplicate_supplier_and_glibc_235_fail(self):
        supplier = native.elf_record('libgcc_s.so.1', elf())
        for rows in ([{**supplier, 'machine': 183}], [supplier, supplier],
                     [native.elf_record('libgcc_s.so.1', elf(glibc='2.35'))], [{**supplier, 'path': 'other.so'}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError): native.verify_closure(rows, 'linux-x64-gnu')

    def test_truncated_offsets_unknown_versions_and_auxiliary_count_fail(self):
        for data in (elf()[:63], elf()[:-1], b'not elf'):
            with self.assertRaises(ValueError): native.elf_record('x.so', data)
        original = elf(imported=True)
        shoff = struct.unpack_from('<Q', original, 40)[0]
        sections = [struct.unpack_from('<IIQQQQIIQQ', original, shoff + i * 64) for i in range(6)]
        for offset, width, value in ((sections[3][4] + 2, 'H', 99), (sections[5][4] + 2, 'H', 2),
                                     (sections[5][4] + 8, 'I', 0xffffffff)):
            poisoned = bytearray(original); struct.pack_into('<' + width, poisoned, offset, value)
            with self.subTest(offset=offset), self.assertRaises(ValueError): native.elf_record('x.so', bytes(poisoned))

    def test_frozen_tree_reports_exact_elf_hashes_and_no_model_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); data = elf(); (root / 'libgcc_s.so.1').write_bytes(data)
            (root / 'ignored').write_text('not ELF')
            report = native.inspect_tree(root)
            self.assertEqual(report['libraries'], [native.elf_record('libgcc_s.so.1', data)])
            self.assertEqual((report['elfCount'], report['requiredGlibcMaximum'], report['modelCalls']), (1, '2.17', 0))
            (root / 'unsafe').symlink_to(root / 'libgcc_s.so.1')
            with self.assertRaisesRegex(ValueError, 'symlink'): native.inspect_tree(root)

    def test_analysis_toc_must_select_exact_explicit_supplier(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); supplier = root / 'libgcc_s.so.1'; supplier.write_bytes(elf())
            ambient = root / 'ambient'; ambient.write_bytes(elf(symbol='other'))
            toc = root / 'Analysis-00.toc'
            toc.write_text(repr(([], [('libgcc_s.so.1', str(supplier), 'BINARY')])))
            self.assertEqual(native.verify_analysis_toc(toc, supplier), native.sha(toc.read_bytes()))
            for rows in ([], [('libgcc_s.so.1', str(ambient), 'BINARY')],
                         [('libgcc_s.so.1', str(supplier), 'BINARY')] * 2):
                toc.write_text(repr(([], rows)))
                with self.assertRaises(ValueError): native.verify_analysis_toc(toc, supplier)

    def test_valid_v3_encoded_full_archive_input_both_architectures(self):
        for target in ('linux-x64-gnu', 'linux-arm64-gnu'):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temp:
                value, path, library, source = input_fixture(Path(temp), target)
                actual, digest, texts = native.validate_input(path, library, source, target)
                self.assertEqual(actual, value); self.assertEqual(digest, native.sha(path.read_bytes()))
                self.assertIn('GCC RUNTIME LIBRARY EXCEPTION', texts[0])

    def test_origin_v2_aliases_and_closed_rpm_facts_in_inputs_and_receipts(self):
        for target in ('linux-x64-gnu', 'linux-arm64-gnu'):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temp:
                value, path, library, source = input_fixture(Path(temp), target)
                machine = 62 if target == 'linux-x64-gnu' else 183
                rows, origins = native.assign_origins(
                    [native.elf_record('libgcc_s.so.1', elf(machine=machine))], value, [], lambda _: None)
                receipt = {'libraries': rows, 'symbolClosureVerified': True,
                           'libgccSelection': {**native.record('libgcc_s.so.1', library.read_bytes()),
                                               'analysisTocSha256': 'a' * 64}, 'origins': origins}
                for parent in ('/lib64', '/usr/lib64'):
                    actual = copy.deepcopy(value)
                    actual['libgcc']['library']['rpmPayloadPath'] = parent + '/libgcc_s-8.so.1'
                    path.write_text(json.dumps(actual))
                    self.assertEqual(native.validate_input(path, library, source, target)[0], actual)
                    portable = copy.deepcopy(receipt)
                    portable['origins']['supplier']['library'] = actual['libgcc']['library']
                    self.assertEqual(native.validate_native_dependencies(portable, target), portable)
                mutations = [lambda o: o.update(schema='openprose.sdk-native-origin/1')]
                for field in ('rpmPayloadPath', 'rpmFileDigestAlgorithm', 'rpmFileDigestSha256'):
                    mutations.append(lambda o, field=field: o['library'].pop(field))
                mutations.append(lambda o: o['library'].update(extra=True))
                for algorithm in (True, '8', 1, 2, 9):
                    mutations.append(lambda o, algorithm=algorithm: o['library'].update(rpmFileDigestAlgorithm=algorithm))
                for digest in ('A' * 64, 'b' * 64, None):
                    mutations.append(lambda o, digest=digest: o['library'].update(rpmFileDigestSha256=digest))
                mutations.append(lambda o: o['library'].update(rpmFileDigestVerified=False))
                for field in ('supplierPath', 'rpmPayloadPath'):
                    bad_paths = ('libgcc_s-8.so.1', '/etc/libgcc_s-8.so.1',
                                 '/usr/lib64/../lib64/libgcc_s-8.so.1', '/usr/lib64/./libgcc_s-8.so.1',
                                 '/usr//lib64/libgcc_s-8.so.1', '/usr/lib64/nested/libgcc_s-8.so.1',
                                 '/usr/lib64/libgcc_s-9.so.1' if field == 'rpmPayloadPath' else '/lib64/libgcc_s-8.so.1',
                                 '/usr/lib64/libgcc_s-8.so.1\n', '/usr/lib64/not-libgcc.so.1',
                                 '/usr/lib64/libgcc_s-8.so.1/', '/usr/lib64/' + 'x' * 1025,
                                 '/usr/lib64/libgcc_s-8~.so.1')
                    for bad in bad_paths:
                        mutations.append(lambda o, field=field, bad=bad: o['library'].update({field: bad}))
                for index, mutate in enumerate(mutations):
                    with self.subTest(target=target, poison=index):
                        poisoned = copy.deepcopy(value); mutate(poisoned['libgcc'])
                        path.write_text(json.dumps(poisoned))
                        with self.assertRaises(ValueError): native.validate_input(path, library, source, target)
                        portable = copy.deepcopy(receipt); mutate(portable['origins']['supplier'])
                        with self.assertRaises(ValueError): native.validate_native_dependencies(portable, target)

    def test_source_module_mutation_encoded_pin_architecture_and_closed_shapes(self):
        with tempfile.TemporaryDirectory() as temp:
            value, path, library, source = input_fixture(Path(temp))
            mutations = [lambda v: v.update(extra=True), lambda v: v['sourceSnapshot']['sources'].pop('cli/ci/sdk_native_inventory.py'),
                         lambda v: v['pythonArchive'].update(url=v['pythonArchive']['url'].replace('%2B', '+')),
                         lambda v: v['images']['supplier'].update(image=v['images']['supplier']['image'].replace('2_28', '2_34')),
                         lambda v: v['libgcc']['library'].update(rpmFileDigestVerified=False)]
            for mutate in mutations:
                changed = copy.deepcopy(value); mutate(changed); path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError): native.validate_input(path, library, source, 'linux-x64-gnu')
            path.write_text(json.dumps(value)); (source / 'cli/ci/sdk_native_inventory.py').write_text('mutated')
            with self.assertRaisesRegex(ValueError, 'snapshot changed'): native.validate_input(path, library, source, 'linux-x64-gnu')

    def test_supplier_digest_and_actual_corresponding_license_bytes_required(self):
        with tempfile.TemporaryDirectory() as temp:
            value, path, library, source = input_fixture(Path(temp))
            library.write_bytes(elf(symbol='poisoned'))
            with self.assertRaisesRegex(ValueError, 'supplier receipt'): native.validate_input(path, library, source, 'linux-x64-gnu')
            library.write_bytes(elf()); (library.parent / 'license-0.txt').write_text('generic substitute')
            with self.assertRaisesRegex(ValueError, 'license digest'): native.validate_input(path, library, source, 'linux-x64-gnu')

    def test_python_owned_bytes_and_static_license_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            value, _, _, _ = input_fixture(Path(temp))
            root = Path(value['pythonDistribution']['root']); (root / 'zlib-license').write_text('zlib fixture')
            info = json.loads((root / 'PYTHON.json').read_text())
            info['build_info']['extensions']['zlib'] = [{'licenses': ['Zlib'], 'license_path': ['zlib-license']}]
            (root / 'PYTHON.json').write_text(json.dumps(info))
            origin, owned = native.python_origin(value)
            self.assertIn(native.sha((root / 'libpython.so').read_bytes()), owned)
            self.assertEqual(origin['declaredExtensionLicenses'], [{'name': 'zlib', 'licenses': ['Zlib'], 'licensePaths': ['zlib-license']}])
            self.assertEqual([r['path'] for r in origin['licenses']], ['LICENSE', 'zlib-license'])
            (root / 'zlib-license').unlink()
            with self.assertRaises(ValueError): native.python_origin(value)

    def test_python_metadata_requires_canonical_upstream_string_version_on_both_targets(self):
        for target in ('linux-x64-gnu', 'linux-arm64-gnu'):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temp:
                value, _, _, _ = input_fixture(Path(temp), target)
                root = Path(value['pythonDistribution']['root']); metadata = root / 'PYTHON.json'
                original = json.loads(metadata.read_text())
                origin, owned = native.python_origin(value)
                self.assertEqual(origin['archive']['targetTriple'], original['target_triple'])
                self.assertEqual(origin['metadataSha256'], native.sha(metadata.read_bytes()))
                self.assertIn(native.sha((root / 'libpython.so').read_bytes()), owned)
                for version in (8, 8.0, True, None, '08', '8.0', ' 8', '8 ', '9', [], {}):
                    changed = copy.deepcopy(original); changed['version'] = version
                    metadata.write_text(json.dumps(changed))
                    with self.subTest(version=version), self.assertRaisesRegex(ValueError, 'metadata version'):
                        native.python_origin(value)
                changed = copy.deepcopy(original); changed.pop('version'); metadata.write_text(json.dumps(changed))
                with self.assertRaisesRegex(ValueError, 'metadata version'): native.python_origin(value)

    def test_python_metadata_string_version_does_not_bypass_native_origin_guards(self):
        with tempfile.TemporaryDirectory() as temp:
            value, _, _, _ = input_fixture(Path(temp))
            metadata = Path(value['pythonDistribution']['metadataPath']); original = json.loads(metadata.read_text())
            mutations = [lambda v: v.update(python_version='3.10.19'),
                         lambda v: v.update(target_triple='aarch64-unknown-linux-gnu'),
                         lambda v: v.update(libpython_link_mode='static'),
                         lambda v: v.update(licenses=[]), lambda v: v.pop('license_path'),
                         lambda v: v['build_info']['core'].update(shared_lib='absent.so'),
                         lambda v: v['build_info'].update(extensions=[])]
            for mutate in mutations:
                changed = copy.deepcopy(original); mutate(changed); metadata.write_text(json.dumps(changed))
                with self.assertRaises(ValueError): native.python_origin(value)

    def test_wheel_native_and_license_members_are_checked_against_record(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); paths = ['fixture.dist-info/RECORD', 'fixture.dist-info/LICENSE', 'extension.so']
            files = []
            for name in paths:
                path = root / name; path.parent.mkdir(exist_ok=True); path.write_bytes(elf(imported=True) if name.endswith('.so') else b'fixture license or RECORD')
                digest = base64.urlsafe_b64encode(hashlib.sha256(path.read_bytes()).digest()).rstrip(b'=').decode()
                class Entry(str): pass
                entry = Entry(name); entry.hash = SimpleNamespace(mode='sha256', value=digest); entry.size = path.stat().st_size
                files.append(entry)
            dist = SimpleNamespace(version='1.0', files=files, locate_file=lambda e: root / str(e))
            package = {'name': 'fixture', 'version': '1.0', 'wheelSha256': ['a' * 64]}
            origins, owned = native.wheel_origins([package], lambda _: dist)
            self.assertEqual(origins[0]['wheelCandidateSha256s'], ['a' * 64])
            self.assertEqual(origins[0]['recordSha256'], native.sha((root / paths[0]).read_bytes()))
            self.assertEqual(owned[native.sha((root / 'extension.so').read_bytes())], 'fixture')
            (root / 'extension.so').write_bytes(elf(imported=True, symbol='changed'))
            with self.assertRaisesRegex(ValueError, 'wheel RECORD'): native.wheel_origins([package], lambda _: dist)

    def test_unknown_native_origin_fails_even_when_platform_closure_is_valid(self):
        with tempfile.TemporaryDirectory() as temp:
            value, _, _, _ = input_fixture(Path(temp))
            rows = [native.elf_record('libgcc_s.so.1', elf()), native.elf_record('unknown.so', elf(imported=True))]
            with self.assertRaisesRegex(ValueError, 'unknown origin'): native.assign_origins(rows, value, [], lambda _: None)

    def test_closed_native_receipt_and_origin_poison_controls(self):
        with tempfile.TemporaryDirectory() as temp:
            value, _, _, _ = input_fixture(Path(temp))
            rows, origins = native.assign_origins([native.elf_record('libgcc_s.so.1', elf())], value, [], lambda _: None)
            receipt = {'libraries': rows, 'symbolClosureVerified': True,
                       'libgccSelection': {**native.record('libgcc_s.so.1', elf()), 'analysisTocSha256': 'a' * 64}, 'origins': origins}
            self.assertEqual(native.validate_native_dependencies(receipt, 'linux-x64-gnu'), receipt)
            mutations = [lambda r: r.update(extra=True), lambda r: r.update(symbolClosureVerified=False),
                         lambda r: r['libraries'][0].update(origin='wheel:absent'),
                         lambda r: r['libraries'][0].update(byteLength=True),
                         lambda r: r['libgccSelection'].update(sha256='b' * 64),
                         lambda r: r['origins']['supplier']['library'].update(rpmFileDigestVerified=False),
                         lambda r: r['origins']['python'].update(licenses=[]),
                         lambda r: r['origins']['python']['licenses'][0].update(byteLength=1024 * 1024 + 1),
                         lambda r: r['origins']['supplier']['package'].update(license='x' * (2 * 1024 * 1024))]
            for mutate in mutations:
                poisoned = copy.deepcopy(receipt); mutate(poisoned)
                with self.subTest(mutate=mutate), self.assertRaises(ValueError): native.validate_native_dependencies(poisoned, 'linux-x64-gnu')

    def test_enclosing_linux_receipt_requires_source_dependency_and_runtime_agreement(self):
        with tempfile.TemporaryDirectory() as temp:
            value, path, _, _ = input_fixture(Path(temp))
            rows, origins = native.assign_origins([native.elf_record('libgcc_s.so.1', elf())], value, [], lambda _: None)
            deps = {'libraries': rows, 'symbolClosureVerified': True,
                    'libgccSelection': {**native.record('libgcc_s.so.1', elf()), 'analysisTocSha256': 'a' * 64}, 'origins': origins}
            receipt = {'platform': 'linux', 'architecture': 'x86_64', 'linuxBuildInputSha256': native.sha(path.read_bytes()),
                       'linuxBuildSourceSnapshot': value['sourceSnapshot'], 'nativeDependencies': deps, 'dependencies': [],
                       'sources': {name: value['sourceSnapshot']['sources'][name] for name in native.SOURCE_PATHS[:2]},
                       'linuxLibraries': {'schema': 'openprose.sdk-packaged-libraries/1', 'elfCount': 1,
                           'requiredGlibcMaximum': '2.17', 'libraries': [native.elf_record('libgcc_s.so.1', elf())], 'modelCalls': 0}}
            self.assertEqual(native.validate_linux_receipt(receipt), deps)
            mutations = [lambda r: r.update(architecture='aarch64'),
                         lambda r: r['sources'].update({'harnesses/agents-sdk/run.py': 'b' * 64}),
                         lambda r: r['linuxBuildSourceSnapshot']['sources'].pop('cli/ci/sdk_native_inventory.py'),
                         lambda r: r['linuxLibraries'].update(elfCount=2), lambda r: r['linuxLibraries'].update(modelCalls=False),
                         lambda r: r['linuxLibraries'].update(requiredGlibcMaximum='2.34'),
                         lambda r: r['linuxLibraries'].update(libraries=[]),
                         lambda r: r['dependencies'].append({'name': 'unbound', 'version': '1', 'wheelSha256': ['a' * 64]}),
                         lambda r: r['nativeDependencies']['origins']['python']['archive'].update(targetTriple='aarch64-unknown-linux-gnu')]
            for mutate in mutations:
                poisoned = copy.deepcopy(receipt); mutate(poisoned)
                with self.subTest(mutate=mutate), self.assertRaises(ValueError): native.validate_linux_receipt(poisoned)

    def test_duplicate_json_metadata_keys_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            native.load_json('{"target":"first","target":"second"}')





class MacPayloadTests(unittest.TestCase):
    def test_exact_complete_view_and_relocated_tree_preserve_physical_aliases(self):
        for architecture in ('arm64', 'x86_64'):
            fixture = onedir_fixture(architecture)
            self.assertEqual(native.validate_macos_payload(**fixture), fixture['payload'])
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve(); first = root / 'first'; second = root / 'second'; first.mkdir(); second.mkdir()
                view = native.materialize_macos_payload(first, **fixture)
                self.assertEqual(view, {key: fixture[key] for key in ('files', 'directories', 'symlinks')})
                self.assertTrue((first / native.SDK_PAYLOAD_ROOT / 'Python.framework/Python').is_symlink())
                self.assertEqual(native.materialize_macos_payload(second, fixture['payload'], architecture=architecture, **view), view)
                self.assertEqual(native.inventory_macos_payload(second, architecture, fixture['payload']['builderSources'],
                    fixture['payload']['entrySourceSha256'], fixture['payload']['collectTocSha256']), fixture['payload'])
                with self.assertRaisesRegex(ValueError, 'absent SDK'):
                    native.materialize_macos_payload(second, **fixture)

    def test_declared_graph_rejects_escaping_dangling_cycles_and_physical_children(self):
        root = native.SDK_PAYLOAD_ROOT; link = root + '/Python.framework/Python'
        for target in ('/etc/passwd', '../../../outside', 'missing', 'Python', 'Versions/Current/../../../Python', 'bad\\target', 'bad\ntarget'):
            value = copy.deepcopy(onedir_fixture())
            value['symlinks'][link] = target
            next(r for r in value['payload']['entries'] if r['path'] == link)['target'] = target
            with self.subTest(target=target), self.assertRaises(ValueError): native.validate_macos_payload(**value)
        value = copy.deepcopy(onedir_fixture()); child = root + '/Python.framework/Versions/Current/injected'
        value['files'][child] = (b'', 0o644)
        value['payload']['entries'].append({'path': child, 'type': 'file', 'sha256': native.sha(b''), 'byteLength': 0, 'mode': 0o644})
        value['payload']['entries'].sort(key=lambda r: r['path'])
        with self.assertRaisesRegex(ValueError, 'beneath alias'): native.validate_macos_payload(**value)
        value = copy.deepcopy(onedir_fixture()); target = '../Versions/3.10/Python'
        value['symlinks'][link] = target
        next(r for r in value['payload']['entries'] if r['path'] == link)['target'] = target
        # Parent traversal is legitimate only when resolving to an existing member.
        with self.assertRaisesRegex(ValueError, 'dangling'): native.validate_macos_payload(**value)
        target = 'Versions/3.10/../3.10/Python'; value['symlinks'][link] = target
        next(r for r in value['payload']['entries'] if r['path'] == link)['target'] = target
        self.assertEqual(native.validate_macos_payload(**value), value['payload'])

    def test_incomplete_mutated_or_untyped_maps_are_rejected_before_materialization(self):
        root = native.SDK_PAYLOAD_ROOT
        controls = []
        v = onedir_fixture(); del v['files'][root + '/empty-data']; controls.append(v)
        v = onedir_fixture(); v['files'][root + '/extra'] = (b'', 0o644); controls.append(v)
        v = onedir_fixture(); v['files'][root + '/empty-data'] = (b'changed', 0o644); controls.append(v)
        v = onedir_fixture(); v['files'][root + '/empty-data'] = (b'', 0o755); controls.append(v)
        v = onedir_fixture(); v['directories'][root] = 0o700; controls.append(v)
        v = onedir_fixture(); v['symlinks'] = {}; controls.append(v)
        v = onedir_fixture(); v['files'][native.SDK_HELPER] = (macho_fixture('arm64', 2), 0o755); controls.append(v)
        v = onedir_fixture(); v['payload']['codeSignaturePaths'] = [native.SDK_HELPER]; controls.append(v)
        v = onedir_fixture(); v['payload']['builderSources']['unknown'] = 'a' * 64; controls.append(v)
        v = onedir_fixture(); v['payload']['entries'].append(v['payload']['entries'][0]); controls.append(v)
        v = onedir_fixture(); v['payload']['totalRegularBytes'] = True; controls.append(v)
        for index, value in enumerate(controls):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as raw:
                destination = Path(raw).resolve()
                with self.assertRaises(ValueError): native.materialize_macos_payload(destination, **value)
                self.assertEqual(list(destination.iterdir()), [])

    def test_real_tree_extra_link_missing_tree_and_mutated_bytes_are_observed(self):
        value = onedir_fixture()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); (root / native.SDK_HELPER).write_bytes(value['files'][native.SDK_HELPER][0]); (root / native.SDK_HELPER).chmod(0o755)
            with self.assertRaisesRegex(ValueError, 'support directory'): native.read_macos_payload(root, value['payload'], value['architecture'])
            (root / native.SDK_HELPER).unlink()
            native.materialize_macos_payload(root, **value)
            extra = root / native.SDK_PAYLOAD_ROOT / 'foreign'; extra.symlink_to('/etc/passwd')
            with self.assertRaises(ValueError): native.read_macos_payload(root, value['payload'], value['architecture'])
            extra.unlink(); (root / native.SDK_PAYLOAD_ROOT / 'empty-data').write_bytes(b'mutation')
            with self.assertRaisesRegex(ValueError, 'bytes/mode differ'): native.read_macos_payload(root, value['payload'], value['architecture'])

    def test_macho_thin_fat_command_and_cpu_poison_controls(self):
        self.assertIsNone(native.macho_architectures(b'ordinary data'))
        self.assertEqual(native.macho_architectures(macho_fixture()), ('x86_64',))
        thin = macho_fixture(); fat = struct.pack('>IIIIIII', 0xcafebabe, 1, 0x1000007, 3, 32, len(thin), 3) + bytes(4) + thin
        self.assertEqual(native.macho_architectures(fat), ('x86_64',))
        for poisoned in (thin[:10], struct.pack('<IIIIIIII', 0xfeedfacf, 7, 3, 6, 0, 0, 0, 0),
                         struct.pack('<IIIIIIII', 0xfeedfacf, 0x1000007, 3, 6, 1, 8, 0, 0) + bytes(8),
                         fat[:40], fat[:12] + struct.pack('>I', 0) + fat[16:]):
            with self.assertRaises(ValueError): native.macho_architectures(poisoned)

    def test_growing_alias_cycle_and_repeat_reuse_are_bounded_without_false_rejection(self):
        value = onedir_fixture(); root = native.SDK_PAYLOAD_ROOT; path = root + '/Python.framework/Python'
        value['symlinks'][path] = 'Python/Python'
        next(r for r in value['payload']['entries'] if r['path'] == path)['target'] = 'Python/Python'
        with mock.patch.object(native, 'deque', wraps=native.deque) as queue:
            with self.assertRaisesRegex(ValueError, 'alias cycle'): native.validate_macos_payload(**value)
            self.assertEqual(queue.call_count, 1)
        value = onedir_fixture(); target = 'Versions/Current/../Current/Python'
        value['symlinks'][path] = target
        next(r for r in value['payload']['entries'] if r['path'] == path)['target'] = target
        native.validate_macos_payload(**value)

    def test_portable_file_directory_alias_unicode_collisions_and_drive_targets_refuse(self):
        root = native.SDK_PAYLOAD_ROOT
        for kind in ('file', 'directory', 'symlink'):
            value = onedir_fixture(); row = {'path': root + '/EMPTY-DATA', 'type': kind}
            if kind == 'file': row.update(sha256=native.sha(b''), byteLength=0, mode=0o644)
            elif kind == 'directory': row.update(mode=0o755)
            else: row.update(target='empty-data', resolvedPath=root + '/empty-data')
            value['payload']['entries'].append(row); value['payload']['entries'].sort(key=lambda r: r['path'])
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'Portable SDK path collision'):
                native.validate_macos_payload_structure(value['payload'], value['architecture'])
        value = onedir_fixture()
        for name in ('\u00e9', 'e\u0301'):
            value['payload']['entries'].append({'path': root + '/' + name, 'type': 'directory', 'mode': 0o755})
        value['payload']['entries'].sort(key=lambda r: r['path'])
        with self.assertRaisesRegex(ValueError, 'Portable SDK path collision'):
            native.validate_macos_payload_structure(value['payload'], value['architecture'])
        for target in ('C:relative', './C:/other', 'Versions/C:'):
            value = onedir_fixture(); row = next(r for r in value['payload']['entries'] if r['type'] == 'symlink')
            row['target'] = target
            with self.assertRaisesRegex(ValueError, 'alias target'):
                native.validate_macos_payload_structure(value['payload'], value['architecture'])
        value = onedir_fixture(); value['payload']['entries'][0]['path'] = root + '/C:relative'
        with self.assertRaisesRegex(ValueError, 'payload path'):
            native.validate_macos_payload_structure(value['payload'], value['architecture'])

    def test_structure_api_reuses_closed_alias_rules_without_claiming_bytes(self):
        value = onedir_fixture()
        self.assertEqual(native.validate_macos_payload_structure(value['payload'], value['architecture']), value['payload'])
        # Structure can validate a report declaration, but the full validator
        # still rejects changed actual bytes or incomplete native coverage.
        value['files'][native.SDK_HELPER] = (b'not Mach-O', 0o755)
        native.validate_macos_payload_structure(value['payload'], value['architecture'])
        with self.assertRaisesRegex(ValueError, 'must be Mach-O'):
            native.validate_macos_payload(**value)
        old_name = copy.deepcopy(value['payload']); old_name['sources'] = old_name.pop('builderSources')
        with self.assertRaisesRegex(ValueError, 'payload.*shape'):
            native.validate_macos_payload_structure(old_name, value['architecture'])
        unknown_alias = copy.deepcopy(value['payload'])
        next(r for r in unknown_alias['entries'] if r['type'] == 'symlink')['target'] = '/outside'
        with self.assertRaises(ValueError): native.validate_macos_payload_structure(unknown_alias, value['architecture'])

    def test_sdk_only_member_aggregate_and_receipt_bounds(self):
        value = onedir_fixture()
        with mock.patch.object(native, 'SDK_PAYLOAD_MAX_ENTRIES', 2), self.assertRaisesRegex(ValueError, 'member count'):
            native.validate_macos_payload(**value)
        with mock.patch.object(native, 'SDK_PAYLOAD_MAX_BYTES', 2), self.assertRaisesRegex(ValueError, 'aggregate bytes'):
            native.validate_macos_payload(**value)
        with mock.patch.object(native, 'SDK_PAYLOAD_MAX_METADATA', 2), self.assertRaisesRegex(ValueError, 'metadata'):
            native.validate_macos_payload(**value)





# Actual pinned provider license bytes; source/archive identity is separately tested.
PBS_ZLIB_LICENSE = b"\n  Copyright (C) 1995-2017 Jean-loup Gailly and Mark Adler\n\n  This software is provided 'as-is', without any express or implied\n  warranty.  In no event will the authors be held liable for any damages\n  arising from the use of this software.\n\n  Permission is granted to anyone to use this software for any purpose,\n  including commercial applications, and to alter it and redistribute it\n  freely, subject to the following restrictions:\n\n  1. The origin of this software must not be misrepresented; you must not\n     claim that you wrote the original software. If you use this software\n     in a product, an acknowledgment in the product documentation would be\n     appreciated but is not required.\n  2. Altered source versions must be plainly marked as such, and must not be\n     misrepresented as being the original software.\n  3. This notice may not be removed or altered from any source distribution.\n\n  Jean-loup Gailly        Mark Adler\n  jloup@gzip.org          madler@alumni.caltech.edu\n"


class PythonMetadataErratumTests(unittest.TestCase):
    def declaration(self):
        return {'name': 'zlib', 'licenses': ['Zlib'],
                'licensePaths': ['licenses/LICENSE.zlib-ng.txt', 'licenses/LICENSE.zlib.txt']}

    def constructor_fixture(self, root, target):
        value, _, _, _ = input_fixture(root, target)
        python = Path(value['pythonDistribution']['root']); metadata = python / 'PYTHON.json'
        info = json.loads(metadata.read_bytes())
        info['build_info']['extensions']['zlib'] = [{'variant': 'default',
            'links': [{'name': 'z', 'path_static': 'build/lib/libz.a'}], 'licenses': ['Zlib'],
            'license_paths': self.declaration()['licensePaths']}]
        metadata.write_text(json.dumps(info))
        (python / 'licenses').mkdir(); (python / 'licenses/LICENSE.zlib.txt').write_bytes(PBS_ZLIB_LICENSE)
        archive_sha, metadata_sha = native.PBS_ZLIB_PARENTS[value['pythonArchive']['targetTriple']]
        value['pythonArchive']['sha256'] = archive_sha
        # Controlled metadata authentication seam models a verified upstream
        # record without embedding two entire94KB upstream JSON files here.
        original_sha = native.sha; metadata_bytes = metadata.read_bytes()
        digest = lambda data: metadata_sha if data == metadata_bytes else original_sha(data)
        return value, python, mock.patch.object(native, 'sha', side_effect=digest)

    def portable_fixture(self, root, target):
        value, _, _, _ = input_fixture(root, target)
        machine = 62 if target == 'linux-x64-gnu' else 183
        rows, origins = native.assign_origins([native.elf_record('libgcc_s.so.1', elf(machine=machine))], value, [], lambda _: None)
        python = origins['python']; archive_sha, metadata_sha = native.PBS_ZLIB_PARENTS[python['archive']['targetTriple']]
        python['archive']['sha256'] = archive_sha; python['metadataSha256'] = metadata_sha
        python['declaredExtensionLicenses'] = [self.declaration()]
        python['licenses'].append(native.record('licenses/LICENSE.zlib.txt', PBS_ZLIB_LICENSE))
        python['metadataErrata'] = native.python_metadata_errata(python['archive'], metadata_sha, python['declaredExtensionLicenses'])
        return {'libraries': rows, 'symbolClosureVerified': True,
                'libgccSelection': {**native.record('libgcc_s.so.1', elf(machine=machine)), 'analysisTocSha256': 'a' * 64}, 'origins': origins}

    def test_both_pinned_constructor_mappings_keep_original_declaration(self):
        for target in ('linux-x64-gnu', 'linux-arm64-gnu'):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temp:
                value, _, authenticated = self.constructor_fixture(Path(temp), target)
                with authenticated: origin, _ = native.python_origin(value)
                self.assertEqual(origin['declaredExtensionLicenses'], [self.declaration()])
                self.assertEqual(len(origin['metadataErrata']), 1)
                self.assertEqual(origin['metadataErrata'][0]['extension']['effectiveLicensePaths'], ['licenses/LICENSE.zlib.txt'])
                self.assertEqual({r['path'] for r in origin['licenses']}, {'LICENSE', 'licenses/LICENSE.zlib.txt'})

    def test_known_parent_cannot_downgrade_or_mix_identities(self):
        archive_sha, metadata_sha = native.PBS_ZLIB_PARENTS['x86_64-unknown-linux-gnu']
        archive = {'sha256': archive_sha, 'targetTriple': 'x86_64-unknown-linux-gnu'}
        for changed_archive, changed_meta in [({**archive, 'sha256': '0' * 64}, metadata_sha),
                (archive, '0' * 64), ({**archive, 'targetTriple': 'aarch64-unknown-linux-gnu'}, metadata_sha)]:
            with self.assertRaises(ValueError): native.python_metadata_errata(changed_archive, changed_meta, [self.declaration()])
        for declarations in ([], [self.declaration(), self.declaration()], [None], ['zlib']):
            with self.assertRaises(ValueError): native.python_metadata_errata(archive, metadata_sha, declarations)

    def test_constructor_requires_absent_omitted_leaf_and_exact_actual_text(self):
        for poison in ('present', 'dangling', 'retained-alias', 'retained-bytes', 'missing-other'):
            with self.subTest(poison=poison), tempfile.TemporaryDirectory() as temp:
                value, root, authenticated = self.constructor_fixture(Path(temp), 'linux-x64-gnu')
                retained = root / 'licenses/LICENSE.zlib.txt'; omitted = root / 'licenses/LICENSE.zlib-ng.txt'
                if poison == 'present': omitted.write_bytes(PBS_ZLIB_LICENSE)
                elif poison == 'dangling': omitted.symlink_to('absent')
                elif poison == 'retained-alias': retained.rename(root / 'text'); retained.symlink_to('../text')
                elif poison == 'retained-bytes': retained.write_bytes(b'wrong license')
                else: (root / 'LICENSE').unlink()
                with authenticated, self.assertRaises(ValueError): native.python_origin(value)

    def test_portable_mapping_and_every_fixed_field_are_closed_on_both_targets(self):
        for target in ('linux-x64-gnu', 'linux-arm64-gnu'):
            with tempfile.TemporaryDirectory() as temp:
                original = self.portable_fixture(Path(temp), target)
                self.assertEqual(native.validate_native_dependencies(original, target), original)
                mutations = [lambda p: p.update(metadataErrata=[]), lambda p: p.pop('metadataErrata'),
                    lambda p: p.update(declaredExtensionLicenses=[]),
                    lambda p: p['declaredExtensionLicenses'].append(self.declaration()),
                    lambda p: p['declaredExtensionLicenses'][0].update(licenses=['MIT']),
                    lambda p: p['metadataErrata'].append(copy.deepcopy(p['metadataErrata'][0])),
                    lambda p: p['metadataErrata'][0].update(extra=True),
                    lambda p: p['metadataErrata'][0].update(upstreamCommit='0' * 40),
                    lambda p: p['metadataErrata'][0]['sourceSha256s'].update({'pythonbuild/utils.py': '0' * 64}),
                    lambda p: p['metadataErrata'][0]['extension'].update(variant='other'),
                    lambda p: p['metadataErrata'][0]['extension'].update(links=[]),
                    lambda p: p['metadataErrata'][0]['extension'].update(effectiveLicensePaths=[]),
                    lambda p: p['metadataErrata'][0]['retainedLicense'].update(byteLength=995.0),
                    lambda p: p['licenses'][-1].update(sha256='0' * 64),
                    lambda p: p['licenses'][-1].update(byteLength=994),
                    lambda p: p.update(declaredExtensionLicenses=[None])]
                for mutate in mutations:
                    changed = copy.deepcopy(original); mutate(changed['origins']['python'])
                    with self.assertRaises(ValueError): native.validate_native_dependencies(changed, target)

    def test_unknown_provider_never_skips_missing_declared_text(self):
        with tempfile.TemporaryDirectory() as temp:
            value, _, _, _ = input_fixture(Path(temp))
            root = Path(value['pythonDistribution']['root']); metadata = root / 'PYTHON.json'
            origin, _ = native.python_origin(value); self.assertEqual(origin['metadataErrata'], [])
            info = json.loads(metadata.read_bytes()); info['build_info']['extensions']['zlib'] = [
                {'licenses': ['Zlib'], 'license_paths': self.declaration()['licensePaths']}]
            metadata.write_text(json.dumps(info))
            with self.assertRaises(ValueError): native.python_origin(value)


if __name__ == '__main__':
    unittest.main()
