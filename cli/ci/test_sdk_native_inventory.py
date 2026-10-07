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
    info = {'version': 8, 'python_version': '3.10.20', 'target_triple': arch + '-unknown-linux-gnu', 'libpython_link_mode': 'shared',
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
               'sha256': '9e57324fd5e25f485fa5c8c587a2fe02f34a869092abd463e8e816c3cb1d48f2' if arch == 'x86_64' else '9201b2d72f8ea0250d594716cfefd8cd85f7b21a8741ba473fafcde58776c619',
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


if __name__ == '__main__':
    unittest.main()
