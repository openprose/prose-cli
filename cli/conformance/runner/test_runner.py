#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("run.py")
SPEC = importlib.util.spec_from_file_location(
    "openprose_conformance_runner", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


class ContractRegistryTest(unittest.TestCase):
    def test_all_corpus_output_contracts_are_registered(self):
        contracts = runner.ContractRegistry()
        for path in runner.CASES.rglob("*.json"):
            case = json.loads(path.read_text("utf-8"))
            for stream in ("stdout", "stderr"):
                rule = case.get("expected", {}).get(stream, {})
                if "schema" in rule:
                    self.assertIn(rule["schema"], contracts.by_contract, str(path))
        # The regression must validate the actual schema, not merely recognize it.
        valid = {"schema": "openprose.service-operation/1", "operation": "auth.status",
                 "interaction": "cli.auth_status",
                 "result": {"authenticated": False, "credentialSource": "none"}, "problem": None}
        self.assertEqual([], contracts.errors(valid["schema"], valid))
        self.assertTrue(contracts.errors(valid["schema"], {**valid, "result": None}))

    def test_discovers_new_contracts_and_fails_closed_on_unknown_or_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schema = {"$schema": "https://json-schema.org/draft/2020-12/schema",
                      "$id": "https://example.test/future.schema.json", "type": "object",
                      "required": ["schema"],
                      "properties": {"schema": {"const": "openprose.future-output/1"}}}
            (root / "future.schema.json").write_text(json.dumps(schema))
            with patch.object(runner, "SCHEMAS", root):
                contracts = runner.ContractRegistry()
                self.assertEqual([], contracts.errors("openprose.future-output/1", {"schema": "openprose.future-output/1"}))
                self.assertTrue(contracts.errors("openprose.unknown/1", {}))
                duplicate = {**schema, "$id": "https://example.test/duplicate.schema.json"}
                (root / "duplicate.schema.json").write_text(json.dumps(duplicate))
                with self.assertRaisesRegex(ValueError, "Duplicate contract discriminator"):
                    runner.ContractRegistry()


class RunnerUnitTest(unittest.TestCase):
    def npm_fixture(self, root):
        prefix=root.resolve()/'installed'
        base='lib/node_modules/@openprose/prose-cli'
        child=base+'-darwin-arm64'
        rows={'launcher':base+'/bin/prose.js','metaManifest':base+'/package.json','platformManifest':child+'/package.json','native':child+'/bin/prose'}
        launcher=b"const fs=require('node:fs'); const path=require('node:path'); process.stdout.write(JSON.stringify({argv:process.argv.slice(1),child:fs.existsSync(path.resolve(__dirname,'../../prose-cli-darwin-arm64/bin/prose'))}));"
        launcher+=b" if(process.env.SDK_FIXTURE_EXECUTE){const cp=require('node:child_process'); const child=path.resolve(__dirname,'../../prose-cli-darwin-arm64/bin/prose'); const native=cp.spawnSync(child,[],{encoding:'utf8'}); if(native.status!==0)process.exit(91); const helper=cp.spawnSync(process.env.SDK_FIXTURE_PYTHON,[path.join(path.dirname(child),'prose-agents-sdk'),'--cwd',process.cwd(),'--instructions',path.join(process.cwd(),'instructions'),'--model','gpt-6.1-sol','--prompt',JSON.stringify({argv:['prose','run','input.prose.md']})],{encoding:'utf8'}); if(helper.status!==0){process.stderr.write(helper.stderr);process.exit(92);} process.stderr.write(helper.stdout); }"
        native=Path('/usr/bin/true').read_bytes()
        cohort={'schema':'openprose.npm-cohort/3','version':'0.16.0-rc.1','sourceRevision':'a'*40,'admittedPlatforms':['darwin-arm64']}
        meta={'name':'@openprose/prose-cli','version':cohort['version'],'type':'commonjs','bin':{'prose':'bin/prose.js'},'openproseCohort':cohort,'optionalDependencies':{'@openprose/prose-cli-darwin-arm64':'npm:@openprose/prose-cli@0.16.0-0.rc.1-darwin-arm64'},'openproseLauncher':{'path':'bin/prose.js','sha256':runner.sha256(launcher),'byteLength':len(launcher)}}
        platform={'name':'@openprose/prose-cli','version':'0.16.0-0.rc.1-darwin-arm64','openproseCohort':cohort,'openprosePlatform':'darwin-arm64','openproseSourceRevision':'a'*40,'openproseBinary':'bin/prose','openproseBinarySha256':runner.sha256(native),'openproseBinaryByteLength':len(native)}
        contents={'launcher':launcher,'native':native,'metaManifest':json.dumps(meta).encode(),'platformManifest':json.dumps(platform).encode()}
        files={}
        for key,relative in rows.items():
            path=prefix/relative; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(contents[key]); path.chmod(0o700)
            files[key]={'path':relative,'sha256':runner.sha256(contents[key]),'byteLength':len(contents[key])}
        return {'prefix':str(prefix),'platform':'darwin-arm64','files':files}

    def test_npm_context_rejects_malformed_escape_symlink_and_custody_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            context=self.npm_fixture(Path(temporary))
            self.assertEqual(4,len(runner.npm_context_paths(context)))
            for mutation in ('null','missing','escape','length','unknown','platform','files-list','row-list','prefix-type'):
                changed=json.loads(json.dumps(context))
                if mutation=='null': changed=None
                elif mutation=='missing': del changed['files']['native']
                elif mutation=='escape': changed['files']['native']['path']='../native'
                elif mutation=='length': changed['files']['native']['byteLength']=True
                elif mutation=='unknown': changed['secret']='unaccepted'
                elif mutation=='files-list': changed['files']=[]
                elif mutation=='row-list': changed['files']['native']=[]
                elif mutation=='prefix-type': changed['prefix']=None
                else: changed['platform']='linux-x64-musl'
                with self.subTest(mutation=mutation), self.assertRaises(ValueError): runner.npm_context_paths(changed)
            path=Path(context['prefix'])/context['files']['native']['path']
            data=path.read_bytes(); path.write_bytes(data+b'changed')
            with self.assertRaisesRegex(ValueError,'bytes changed'): runner.npm_context_paths(context)
            path.write_bytes(data); foreign=path.with_name('foreign'); path.rename(foreign); path.symlink_to(foreign)
            with self.assertRaisesRegex(ValueError,'symlink'): runner.npm_context_paths(context)

    def test_npm_context_rejects_metadata_rebound_to_foreign_cohort(self):
        for field in ('name','version','openprosePlatform','openproseSourceRevision','openproseCohort','openproseBinarySha256'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                context=self.npm_fixture(Path(temporary)); row=context['files']['platformManifest']; path=Path(context['prefix'])/row['path']
                payload=json.loads(path.read_text()); payload[field]={'schema':'foreign'} if field=='openproseCohort' else 'foreign'
                path.write_text(json.dumps(payload)); row.update(sha256=runner.sha256(path.read_bytes()),byteLength=path.stat().st_size)
                with self.assertRaises(ValueError): runner.npm_context_paths(context)

    def test_npm_context_rejects_unbound_interpreter_labels_and_metadata_shapes(self):
        with tempfile.TemporaryDirectory() as temporary:
            context=self.npm_fixture(Path(temporary)); launcher=Path(context['prefix'])/context['files']['launcher']['path']
            product=runner.Product('fixture',launcher,'bun',Path(sys.executable))
            for specifications in ([['unknown',json.dumps(context)]],[['fixture',json.dumps(context)],['fixture',json.dumps(context)]],[['fixture','[]']]):
                with self.assertRaises(ValueError): runner.attach_npm_contexts([product],specifications)
            with self.assertRaisesRegex(ValueError,'explicit Bun launcher'):
                runner.attach_npm_contexts([runner.Product('fixture',launcher,'bun')],[['fixture',json.dumps(context)]])
        for key in ('metaManifest','platformManifest'):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temporary:
                context=self.npm_fixture(Path(temporary)); row=context['files'][key]; path=Path(context['prefix'])/row['path']; path.write_bytes(b'[]')
                row.update(sha256=runner.sha256(b'[]'),byteLength=2)
                with self.assertRaises(ValueError): runner.npm_context_paths(context)

    @unittest.skipUnless(shutil.which('node'),'Node is required for npm closure fixture')
    def test_sdk_npm_executes_immutable_snapshot_with_minimal_relocated_closure(self):
        for number in (4,8):
            with self.subTest(case=number), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary); context=self.npm_fixture(root)
                launcher=Path(context['prefix'])/context['files']['launcher']['path']
                product=runner.Product('npm-launcher',launcher,'bun',Path(shutil.which('node')))
                product=runner.attach_npm_contexts([product],[['npm-launcher',json.dumps(context)]])[0]
                snapshot=runner.snapshot_product(product,runner.capture_candidate_identity(product),root/'snapshot')
                environment,workspace=runner.product_roots(root/'case','npm-launcher'); runner.isolate_workspace(workspace)
                case=json.loads((runner.CASES/'adapters'/f'sdk-production-{number:02}.json').read_text())
                execution,fixture,poison=runner.prepare_sdk_installation(snapshot,case,workspace,environment)
                (workspace/'instructions').write_bytes(b'OPENPROSE_SENTINEL_IMAGE_V1')
                process_environment={'PATH':str(poison),'SDK_FIXTURE_EXECUTE':'1','SDK_FIXTURE_PYTHON':str(Path(sys.executable).resolve())}
                self.assertEqual(snapshot.execution_executable,execution.execution_executable)
                self.assertEqual(number==8,execution.execution_launch_path.is_symlink())
                native=Path(fixture['nativePath']); self.assertTrue((native.parent/'prose-agents-sdk').is_file())
                self.assertEqual(4,len(fixture['npmCloneFiles']))
                result=runner.run_owned_process(execution.execution_argv(['opaque']),cwd=workspace,environment=process_environment,timeout_seconds=3)
                self.assertEqual(0,result.exit_code,result.stderr)
                records=[json.loads(line) for line in result.stderr.splitlines()]; self.assertEqual('final',records[-1]['type']); self.assertTrue((workspace/'.sdk-harness-started').is_file())
                payload=json.loads(result.stdout); self.assertTrue(payload['child']); self.assertEqual([str(execution.execution_launch_path),'opaque'],payload['argv'])
                observation=runner.Observation(product,case,0,b'',b'',workspace=workspace,sdk_fixture=fixture)
                self.assertEqual([],runner.validate_sdk_effects(observation))
                execution.execution_source_context.write_text("throw Error('mutable clone executed')")
                result=runner.run_owned_process(execution.execution_argv(['opaque']),cwd=workspace,environment=process_environment,timeout_seconds=3)
                self.assertEqual(0,result.exit_code,result.stderr)
                self.assertIn('SDK npm relocated closure bytes changed',runner.validate_sdk_effects(observation))

    def test_host_oracle_keeps_admitted_expectations_and_requires_rejection(self):
        for path in runner.case_paths(7, set()):
            case = json.loads(path.read_text("utf-8"))
            original = json.loads(json.dumps(case))
            self.assertEqual(case["expected"], runner.expected_for_host(case, "darwin", "arm64"))
            for os_name, arch in [("linux", "aarch64"), ("linux", "x86_64"), ("darwin", "x86_64")]:
                expected = runner.expected_for_host(case, os_name, arch)
                adapter = case.get("controls", {}).get("installedAdapter", {}).get("adapterId")
                rejected = adapter in {"claude/print-stream-json", "prime/rpc"} or (
                    adapter == "omp/rpc" and (os_name, arch) != ("linux", "x86_64"))
                if rejected:
                    self.assertEqual(10, expected["exitCode"])
                    self.assertFalse(expected["startedHarness"])
                    self.assertNotIn("forwardedTask", expected)
                    self.assertIn("HARNESS_INCOMPATIBLE", json.dumps(expected))
                elif case["id"] not in {
                    "adapters.codex-doctor-probe-failed", "adapters.codex-list-probe-failed"
                }:
                    self.assertEqual(case["expected"], expected)
            self.assertEqual(original, case)

    def test_inventory_oracle_freezes_all_harnesses_and_codex_blocked_on_each_host(self):
        for name in ("codex-doctor-probe-failed", "codex-list-probe-failed"):
            case = json.loads((runner.CASES / f"adapters/{name}.json").read_text())
            for os_name, arch, incompatible in [
                ("darwin", "arm64", set()),
                ("darwin", "x86_64", {"prime", "omp", "claude"}),
                ("linux", "aarch64", {"prime", "omp", "claude"}),
                ("linux", "x86_64", {"prime", "claude"}),
            ]:
                wanted = json.loads(json.dumps(case["expected"]))
                for harness in wanted["resultMatches"]["harnesses"]:
                    if harness["id"] in incompatible:
                        harness["availability"] = "incompatible"
                actual = runner.expected_for_host(case, os_name, arch)
                self.assertEqual(wanted, actual)
                codex = next(h for h in actual["resultMatches"]["harnesses"] if h["id"] == "codex")
                self.assertEqual("blocked", codex["availability"])
                self.assertFalse(actual["startedHarness"])


    def test_sdk_inventory_host_oracle_matches_four_platform_frozen_contracts(self):
        host=json.loads((runner.CLI/'conformance/fixtures/adapter-host-expectations.json').read_text())
        recipe=json.loads((runner.CLI/'shared/capabilities/adapters/recipes/agents-sdk-jsonl.v1.json').read_text())
        production=json.loads((runner.CLI/'shared/fixtures/adapters/sdk-production.json').read_text())
        supported=['darwin-arm64','darwin-x64','linux-arm64-gnu','linux-x64-gnu']
        normalized=['darwin-arm64','darwin-x64','linux-arm64','linux-x64']
        self.assertEqual(supported,recipe['support']['platforms'])
        self.assertEqual(supported,production['discovery']['admissionPlatforms'])
        self.assertEqual(supported,host['supportedPlatforms']['agents-sdk/jsonl'])
        self.assertEqual(normalized,production['discovery']['platforms'])
        self.assertEqual(normalized,host['admittedHosts']['agents-sdk/jsonl'])
        for name in ('codex-doctor-probe-failed','codex-list-probe-failed'):
            case=json.loads((runner.CASES/f'adapters/{name}.json').read_text())
            original=json.loads(json.dumps(case))
            for os_name,arch in [('darwin','arm64'),('darwin','x86_64'),('linux','aarch64'),('linux','x86_64')]:
                with self.subTest(case=name,os=os_name,arch=arch):
                    expected=runner.expected_for_host(case,os_name,arch)
                    sdk=next(row for row in expected['resultMatches']['harnesses'] if row['id']=='agents-sdk')
                    self.assertEqual(next(row for row in case['expected']['resultMatches']['harnesses'] if row['id']=='agents-sdk'),sdk)
                    self.assertEqual('missing',sdk['availability'])
            self.assertEqual(original,case)
        self.assertEqual(100,len(list(runner.case_paths(7,set()))))

    def test_inventory_host_oracle_preserves_complete_arrays_on_unsupported_hosts(self):
        for name in ('codex-doctor-probe-failed','codex-list-probe-failed'):
            case=json.loads((runner.CASES/f'adapters/{name}.json').read_text())
            for os_name,arch in [('freebsd','x86_64'),('win32','AMD64'),('darwin','riscv64'),('linux','riscv64')]:
                with self.subTest(case=name,os=os_name,arch=arch):
                    wanted=json.loads(json.dumps(case['expected']))
                    for harness in wanted['resultMatches']['harnesses']:
                        if harness['runtime']=='installed-process': harness['availability']='incompatible'
                    self.assertEqual(wanted,runner.expected_for_host(case,os_name,arch))
                    self.assertFalse(wanted['startedHarness'])

    def test_host_oracle_rejects_unsupported_host_without_skipping_case(self):
        case = json.loads((runner.CASES / "adapters/claude-functional-alpha.json").read_text())
        wanted = runner.expected_for_host(case, "linux", "aarch64")
        self.assertEqual("arm64", wanted["resultMatches"]["error"]["details"]["hostArchitecture"])
        self.assertTrue(runner.deep_subset({"terminal": {"classification": "success"}}, wanted["resultMatches"]))
        self.assertEqual(100, len(list(runner.case_paths(7, set()))))

    def test_global_cwd_macro_expansion_respects_arity_and_language_freeze(self):
        workspace=Path('/fixture workspace')
        token='{{WORKSPACE}}/sub'
        for original,wanted in [
            (['--output','json','--cwd',token,'run','--cwd',token],['--output','json','--cwd','/fixture workspace/sub','run','--cwd',token]),
            (['--no-color','--cwd='+token,'--','--cwd',token],['--no-color','--cwd=/fixture workspace/sub','--','--cwd',token]),
            (['--model','--cwd','run',token],['--model','--cwd','run',token]),
            (['--unknown','--cwd',token],['--unknown','--cwd',token]),
            (['--native-add-dir',token,'--verbose','--cwd',token],['--native-add-dir',token,'--verbose','--cwd','/fixture workspace/sub']),
            (['--cwd'],['--cwd']),
            (['--help','--cwd',token],['--help','--cwd',token]),
        ]:
            with self.subTest(original=original):
                snapshot=list(original)
                self.assertEqual(wanted,runner.expand_fixture_global_cwd(original,workspace))
                self.assertEqual(snapshot,original)

    def test_sdk_installation_fixture_executes_canonical_clone_and_keeps_helper_off_path(self):
        for number in range(1,18):
            case = json.loads((runner.CASES/'adapters'/f'sdk-production-{number:02}.json').read_text())
            with self.subTest(case=case['id']), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment, workspace = runner.product_roots(root, 'fixture')
                runner.isolate_workspace(workspace)
                product = runner.Product('fixture', Path('/usr/bin/true'))
                execution, fixture, poison = runner.prepare_sdk_installation(product,case,workspace,environment)
                native = Path(fixture['nativePath'])
                self.assertEqual(product.executable.read_bytes(), native.read_bytes())
                argv = execution.execution_argv(case['invocation']['argv'])
                self.assertEqual(Path(argv[0]).resolve(), native)
                self.assertEqual(case['controls']['installedAdapter']['sdkInstallation']=='symlink', Path(argv[0]).is_symlink())
                self.assertTrue((native.parent/'prose-agents-sdk').is_file())
                self.assertNotEqual(native.parent,poison)
                self.assertTrue((poison/'prose-agents-sdk').is_file())
                observation = runner.Observation(product,case,0,b'',b'',workspace=workspace,sdk_fixture=fixture)
                self.assertEqual([],runner.validate_sdk_effects(observation))
                (workspace/'.sdk-wrong-helper-used').touch()
                self.assertIn('SDK discovery used a foreign PATH helper',runner.validate_sdk_effects(observation))

    def test_sdk_fixture_refuses_unfrozen_controls_and_interpreted_candidates(self):
        case = json.loads((runner.CASES/'adapters/sdk-production-04.json').read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError,'native compiled'):
                runner.prepare_sdk_installation(runner.Product('fixture',Path('/usr/bin/true'),interpreter=Path(sys.executable)),case,root,root/'environment')
            case['controls']['installedAdapter']['sdkScenario']='trap'
            with self.assertRaisesRegex(ValueError,'differ from'):
                runner.prepare_sdk_installation(runner.Product('fixture',Path('/usr/bin/true')),case,root,root/'environment')

    def test_sdk_fake_wire_is_provider_free_and_reuses_frozen_observations(self):
        oracle = json.loads((runner.CLI/'shared/fixtures/adapters/sdk-production.json').read_text())
        for number in (4,5,6,9,10,11,12,13,14,15,16,17):
            case = json.loads((runner.CASES/'adapters'/f'sdk-production-{number:02}.json').read_text())
            with self.subTest(case=case['id']), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary); environment,workspace=runner.product_roots(root,'fixture')
                runner.isolate_workspace(workspace)
                _,fixture,poison=runner.prepare_sdk_installation(runner.Product('fixture',Path('/usr/bin/true')),case,workspace,environment)
                helper=Path(fixture['nativePath']).parent/'prose-agents-sdk'
                image=workspace/'instructions';image.write_bytes(b'OPENPROSE_SENTINEL_IMAGE_V1')
                argv=[sys.executable,str(helper),'--cwd',str(workspace),'--instructions',str(image),'--model','gpt-6.1-sol','--prompt',json.dumps({'argv':['prose','run','input.prose.md']})]
                result=runner.run_owned_process(argv,cwd=workspace,environment=runner.hermetic_environment(environment,{'PATH':str(poison)}),timeout_seconds=3)
                self.assertEqual(1 if number in (5,9,10,11,12,13,14,15,16,17) else 0,result.exit_code,result.stderr)
                records=[json.loads(line) for line in result.stdout.splitlines()]
                self.assertEqual('start',records[0]['type'])
                self.assertEqual('error' if number in (5,9,10,11,12,13,14,15,16,17) else 'final',records[-1]['type'])
                if number!=6:
                    self.assertEqual(oracle['observation']['failureUsage' if number in (5,9,10,11,12,13,14,15,16,17) else 'completedUsage'],records[-1]['usageObservation'])
                    self.assertEqual(oracle['observation']['modelIdentity'],records[-1]['modelIdentity'])
                else:
                    self.assertEqual('sdk-unrecognized-secret-sentinel',records[-1]['usageObservation']['unknown'])
                if number >= 11:
                    control=next(row for row in oracle['cases'] if row['id']==case['id'])
                    self.assertEqual('SetupError',records[-1]['error_type'])
                    self.assertEqual(control['rawSetupReason'],records[-1]['setup_reason'])
                    self.assertIn(b'sdk-provider-body-secret-sentinel',result.stdout)
                    self.assertEqual([], result.stderr.splitlines())
                if number == 5:
                    self.assertIn(b'"maxTurns":20.0', result.stdout)
                    self.assertIn(b'"maxOutputTokens":12000e0', result.stdout)
                    self.assertIn(b'"maxChildren":8.0', result.stdout)
                    self.assertIn(b'"maxChildDepth":1e0', result.stdout)
                if number == 6:
                    self.assertIn(b'"completedResponseCount":3e0', result.stdout)
                    self.assertIn(b'"duplicateResponseCallbackCount":-0.0', result.stdout)
                    self.assertIn(b'"input_tokens_details.cached_tokens":-0.0', result.stdout)
                if number in (9,10):
                    key = 'maxChildren' if number == 9 else 'maxChildDepth'
                    self.assertEqual(9 if number == 9 else 2, records[-1]['limits'][key])
                    other = 'maxChildDepth' if number == 9 else 'maxChildren'
                    self.assertEqual(oracle['nativeLimits'][other], records[-1]['limits'][other])
                self.assertTrue((workspace/'.sdk-harness-started').is_file())
                if number not in (5,9,10,11,12,13,14,15,16,17):
                    terminal=json.loads(records[-1]['output'])
                    self.assertEqual('OPENPROSE_SENTINEL_TERMINAL_V1',terminal['marker'])

    def test_sdk_malformed_groups_must_be_absent_without_relaxing_usage_checks(self):
        case=json.loads((runner.CASES/'adapters/sdk-production-06.json').read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);environment,workspace=runner.product_roots(root,'fixture');runner.isolate_workspace(workspace)
            _,fixture,_=runner.prepare_sdk_installation(runner.Product('fixture',Path('/usr/bin/true')),case,workspace,environment)
            observation=runner.Observation(runner.Product('fixture',Path('/usr/bin/true')),case,0,b'',b'',workspace=workspace,sdk_fixture=fixture)
            for parsed in ({'modelIdentity':{}},{'error':{'details':{'modelIdentity':{}}}}):
                observation.parsed=parsed
                self.assertIn('SDK malformed observation group was retained: modelIdentity',runner.validate_sdk_effects(observation))
            for parsed in ({'error':None},{'error':'malformed'},{}):
                observation.parsed=parsed
                self.assertEqual([],runner.validate_sdk_effects(observation))
            observation.stdout=b'sdk-unrecognized-secret-sentinel'
            self.assertEqual(['SDK output exposed a forbidden raw-observation sentinel'],runner.validate_sdk_effects(observation))

    def test_sdk_setup_human_actions_and_body_suppression_are_independent_guards(self):
        for number in range(11,18):
            case=json.loads((runner.CASES/'adapters'/f'sdk-production-{number:02}.json').read_text())
            with self.subTest(case=case['id']),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);environment,workspace=runner.product_roots(root,'fixture');runner.isolate_workspace(workspace)
                _,fixture,_=runner.prepare_sdk_installation(runner.Product('fixture',Path('/usr/bin/true')),case,workspace,environment)
                observation=runner.Observation(runner.Product('fixture',Path('/usr/bin/true')),case,22,b'',b'',workspace=workspace,sdk_fixture=fixture)
                self.assertEqual([],runner.validate_sdk_effects(observation))
                for stream in ('stdout','stderr'):
                    setattr(observation,stream,b'sdk-provider-body-secret-sentinel')
                    self.assertIn('SDK output exposed a forbidden raw-observation sentinel',runner.validate_sdk_effects(observation))
                    setattr(observation,stream,b'')
                if 14<=number<=16:
                    (workspace/'.sdk-harness-started').touch()
                    observation.fake_observation=workspace/'.sdk-harness-started'
                    observation.stderr=('\n'.join(case['expected']['stderr']['contains'])+'\n').encode()
                    self.assertEqual([],runner.validate_output(observation,runner.ContractRegistry()))
                    observation.stderr=b'HARNESS_FAILED\nAction: wrong\n'
                    self.assertTrue(any('Action:' in error for error in runner.validate_output(observation,runner.ContractRegistry())))
                if number==17:
                    observation.parsed={'error':{'details':{'nativeFailure':{'kind':'execution','setupReason':'local-input'}}}}
                    self.assertIn('SDK malformed result field was retained: $.error.details.nativeFailure.setupReason',runner.validate_sdk_effects(observation))

    def test_sdk_invalid_child_limits_require_nested_absence_even_if_null(self):
        for number in (9,10):
            case=json.loads((runner.CASES/'adapters'/f'sdk-production-{number:02}.json').read_text())
            with self.subTest(case=case['id']), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);environment,workspace=runner.product_roots(root,'fixture');runner.isolate_workspace(workspace)
                _,fixture,_=runner.prepare_sdk_installation(runner.Product('fixture',Path('/usr/bin/true')),case,workspace,environment)
                observation=runner.Observation(runner.Product('fixture',Path('/usr/bin/true')),case,22,b'',b'',workspace=workspace,sdk_fixture=fixture)
                for retained in (None, {}, {'maxChildren':9}):
                    observation.parsed={'error':{'details':{'nativeFailure':{'kind':'execution','limits':retained}}}}
                    self.assertIn('SDK malformed result field was retained: $.error.details.nativeFailure.limits',runner.validate_sdk_effects(observation))
                observation.parsed={'error':{'details':{'nativeFailure':{'kind':'execution'}}}}
                self.assertEqual([],runner.validate_sdk_effects(observation))
                self.assertEqual([],runner.deep_subset(observation.parsed,{'error':{'details':{'nativeFailure':{'kind':'execution'}}}}))
                self.assertTrue(runner.deep_subset(observation.parsed,{'error':{'details':{'nativeFailure':{'kind':'timeout'}}}}))

    def test_sdk_setup_errors_and_pure_operations_have_independent_marker_guards(self):
        for number,marker,message in ((1,'.sdk-harness-probed','probed'),(2,'.sdk-harness-started','started'),(7,'.sdk-harness-started','started')):
            case=json.loads((runner.CASES/'adapters'/f'sdk-production-{number:02}.json').read_text())
            with self.subTest(case=case['id']), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);environment,workspace=runner.product_roots(root,'fixture');runner.isolate_workspace(workspace)
                _,fixture,_=runner.prepare_sdk_installation(runner.Product('fixture',Path('/usr/bin/true')),case,workspace,environment)
                observation=runner.Observation(runner.Product('fixture',Path('/usr/bin/true')),case,0,b'',b'',workspace=workspace,sdk_fixture=fixture)
                self.assertEqual([],runner.validate_sdk_effects(observation))
                (workspace/marker).touch()
                self.assertTrue(any(message in failure for failure in runner.validate_sdk_effects(observation)))
                Path(fixture['nativePath']).write_bytes(b'changed candidate')
                self.assertIn('SDK relocated candidate bytes changed during execution',runner.validate_sdk_effects(observation))

    def test_configuration_corpus_prepares_each_product_independently_and_checks_effects(self):
        for identifier in runner.CONFIGURATION_CASE_IDS:
            case = json.loads((runner.CASES / "operations" / f"{identifier.split('.')[-1]}.json").read_text())
            setup = runner.load_configuration_setup(case)
            with self.subTest(case=identifier), tempfile.TemporaryDirectory() as temporary:
                case_root = Path(temporary)
                workspaces = []
                for name in ("rust", "bun"):
                    environment_root, workspace = runner.product_roots(case_root, name)
                    def product_call(argv, *, cwd, environment, timeout_seconds):
                        self.assertEqual(15, timeout_seconds)
                        self.assertTrue((workspace / ".git").is_dir())
                        self.assertEqual(str(workspace.resolve() / "home"), environment["HOME"])
                        for relative, value in setup["files"].items():
                            self.assertEqual(value.encode(), (workspace / relative).read_bytes())
                        for relative, value in setup["checks"].get("files", {}).items():
                            destination = workspace / relative
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            destination.write_bytes(value.encode())
                        return runner.OwnedProcessResult(0, b"{}", b"", False, True, False, False)
                    with patch.object(runner, "run_owned_process", side_effect=product_call):
                        observation = runner.execute(runner.Product(name, Path(sys.executable)), case, environment_root, workspace)
                    self.assertEqual([], runner.validate_configuration_effects(observation))
                    self.assertEqual(observation.configuration_before[".git"], ("directory", ""))
                    workspaces.append(workspace)
                self.assertNotEqual(workspaces[0], workspaces[1])
                (workspaces[0] / "unexpected-state").write_bytes(b"product-specific")
                self.assertFalse((workspaces[1] / "unexpected-state").exists())

    def configuration_observation(self, number, workspace):
        case = json.loads((runner.CASES / "operations" / f"config-production-{number:02}.json").read_text())
        setup = runner.load_configuration_setup(case)
        before = runner.prepare_configuration(workspace, setup)
        return runner.Observation(runner.Product("fixture", Path(sys.executable)), case, 0, b"", b"", workspace=workspace, configuration_before=before)

    def test_configuration_tree_oracle_detects_additions_deletion_types_and_bytes(self):
        for alteration in ("addition", "directory", "deletion", "bytes", "symlink"):
            with self.subTest(alteration=alteration), tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary)
                observation = self.configuration_observation(3, workspace)
                protected = workspace / "home/.prose/cli.toml"
                if alteration == "addition":
                    (workspace / "unexpected").write_bytes(b"unexpected")
                elif alteration == "directory":
                    (workspace / "unexpected").mkdir()
                elif alteration == "deletion":
                    protected.unlink()
                elif alteration == "bytes":
                    protected.write_bytes(b"changed")
                else:
                    protected.unlink()
                    protected.symlink_to(workspace / ".prose/cli.toml")
                self.assertTrue(runner.validate_configuration_effects(observation))

    def test_configuration_selected_source_and_exact_destination_are_separate_oracles(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            observation = self.configuration_observation(6, workspace)
            self.assertTrue(runner.validate_configuration_effects(observation))
            source = workspace / "config/openprose/cli.toml"
            destination = workspace / "home/.prose/cli.toml"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(source.read_bytes())
            self.assertEqual([], runner.validate_configuration_effects(observation))
            destination.write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))
            self.assertTrue(runner.validate_configuration_effects(observation))
            destination.write_bytes(source.read_bytes())
            source.write_bytes(b"changed source")
            self.assertTrue(any("protected file" in failure for failure in runner.validate_configuration_effects(observation)))

    def test_configuration_absence_and_redaction_check_both_streams(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            observation = self.configuration_observation(1, workspace)
            destination = workspace / "home/.prose/cli.toml"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"")
            self.assertTrue(any("absent path" in failure for failure in runner.validate_configuration_effects(observation)))
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream), tempfile.TemporaryDirectory() as temporary:
                observation = self.configuration_observation(9, Path(temporary))
                setattr(observation, stream, b"fixture-secret-do-not-print")
                failures = runner.validate_configuration_effects(observation)
                self.assertEqual(["configuration output exposed a forbidden fixture sentinel"], failures)
                self.assertNotIn("fixture-secret-do-not-print", str(failures))

    def test_configuration_setup_refuses_escape_links_and_existing_files_before_writing(self):
        for relative in ("../escape", "/escape", "home/../escape", "C:/escape", "home\\escape", "home//escape", "home/./escape"):
            with self.subTest(path=relative), tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary)
                setup = {"files": {"would-write": "safe", relative: "bad"}, "directories": [], "checks": {}}
                with self.assertRaises(ValueError):
                    runner.prepare_configuration(workspace, setup)
                self.assertFalse((workspace / "would-write").exists())
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "linked").symlink_to(workspace, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                runner.prepare_configuration(workspace, {"files": {"linked/file": "bad"}, "directories": [], "checks": {}})
            (workspace / "existing").write_bytes(b"original")
            with self.assertRaises(FileExistsError):
                runner.prepare_configuration(workspace, {"files": {"existing": "bad"}, "directories": [], "checks": {}})
            self.assertEqual(b"original", (workspace / "existing").read_bytes())

    def test_configuration_effects_participate_in_full_output_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            observation = self.configuration_observation(1, Path(temporary))
            observation.case["expected"] = {"exitCode": 0, "stdout": {"kind": "empty"}, "stderr": {"kind": "empty"}, "startedHarness": False}
            self.assertEqual([], runner.validate_output(observation, runner.ContractRegistry()))
            (observation.workspace / "unexpected").write_bytes(b"unexpected")
            self.assertIn("configuration effects changed the protected workspace tree", runner.validate_output(observation, runner.ContractRegistry()))

    def test_workspace_boundary_preserves_nested_project_discovery_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            nested = workspace / "work space"
            nested.mkdir(parents=True)
            (nested / ".prose").mkdir()
            (nested / ".prose/cli.toml").write_bytes(b'timeout = "6m"\n')
            (workspace / ".prose").mkdir()
            (workspace / ".prose/cli.toml").write_bytes(b'timeout = "5m"\n')
            runner.isolate_workspace(workspace)
            self.assertTrue((workspace / ".git").is_dir())
            self.assertFalse((nested / ".git").exists())
            self.assertEqual(b'timeout = "6m"\n', (nested / ".prose/cli.toml").read_bytes())
            self.assertEqual(b'timeout = "5m"\n', (workspace / ".prose/cli.toml").read_bytes())
            # A supplied nested Git boundary and worktree-file boundary stay explicit.
            (nested / ".git").mkdir()
            (workspace / ".git").rmdir()
            (workspace / ".git").write_bytes(b'gitdir: fixture-only\n')
            runner.isolate_workspace(workspace)
            self.assertEqual(b'gitdir: fixture-only\n', (workspace / ".git").read_bytes())
            self.assertTrue((nested / ".git").is_dir())

    def test_configuration_snapshot_bounds_entries_and_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            large = workspace / "large"
            with large.open("wb") as output:
                output.truncate(runner.MAX_CAPTURE_BYTES + 1)
            with self.assertRaisesRegex(ValueError, "bounded snapshot bytes"):
                runner.configuration_snapshot(workspace)
            large.unlink()
            for number in range(1025):
                (workspace / str(number)).touch()
            with self.assertRaisesRegex(ValueError, "bounded snapshot entries"):
                runner.configuration_snapshot(workspace)

    def test_configuration_cwd_cannot_escape_product_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            case_root = Path(temporary)
            case = json.loads((runner.CASES / "operations/config-production-01.json").read_text())
            case["invocation"]["cwd"] = str(case_root / "escape")
            with patch.object(runner, "run_owned_process") as start:
                with self.assertRaisesRegex(ValueError, "cwd leaves product workspace"):
                    runner.execute(runner.Product("fixture", Path(sys.executable)), case, case_root / "environment", case_root / "workspace")
                start.assert_not_called()
            self.assertFalse((case_root / "escape").exists())

    def test_configuration_references_and_corpus_fail_closed(self):
        case = json.loads((runner.CASES / "operations/config-production-01.json").read_text())
        for reference in ("unknown", "operations.config-production-02"):
            with self.subTest(reference=reference):
                case["controls"]["configurationFixture"] = reference
                with self.assertRaisesRegex(ValueError, "matching closed case"):
                    runner.load_configuration_setup(case)
        case["controls"]["configurationFixture"] = case["id"]
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "corpus.json"
            corpus = json.loads(runner.CONFIGURATION_FIXTURE.read_text())
            for mutation in (lambda value: value.update(extra=True), lambda value: value["cases"].pop(), lambda value: value["cases"][0]["setup"]["checks"].update(extra=True)):
                value = json.loads(json.dumps(corpus))
                mutation(value)
                fixture.write_text(json.dumps(value))
                with patch.object(runner, "CONFIGURATION_FIXTURE", fixture), self.assertRaises(ValueError):
                    runner.load_configuration_setup(case)
        case["controls"]["fakeHarness"] = {"scenario": "success"}
        with self.assertRaisesRegex(ValueError, "cannot start"):
            runner.load_configuration_setup(case)

    def test_hosted_transport_and_missing_selection_cases_freeze_dx_precedence(
        self,
    ) -> None:
        transport_cases = {
            "core/openprose-invalid-transport-run.json": {
                "harness": "openprose",
                "requested": "unsupported",
                "supported": ["hosted"],
            },
            "operations/openprose-invalid-transport-doctor.json": {
                "harness": "openprose",
                "requested": "unsupported",
                "supported": ["hosted"],
            },
            "core/codex-invalid-transport-run.json": {
                "harness": "codex",
                "requested": "rpc",
                "supported": ["exec-json"],
            },
            "operations/prime-invalid-transport-doctor.json": {
                "harness": "prime",
                "requested": "exec-json",
                "supported": ["rpc"],
            },
        }
        for relative, details in transport_cases.items():
            case = json.loads((runner.CASES / relative).read_text("utf-8"))
            expected = case["expected"]
            self.assertEqual(20, expected["exitCode"])
            self.assertEqual(
                "openprose.runner-error/1", expected["stdout"]["schema"]
            )
            self.assertEqual(
                details,
                expected["resultMatches"]["details"],
            )

        missing = json.loads(
            (
                runner.CASES
                / "operations/harness-use-prime-missing-bundle.json"
            ).read_text("utf-8")
        )["expected"]["resultMatches"]
        self.assertEqual("invocation", missing["boundary"])
        self.assertEqual(
            ["--model", "--auth-profile"], missing["details"]["requiredOptions"]
        )
        self.assertIn("credential route", missing["details"]["reason"])
        self.assertNotIn("billing", missing["details"]["reason"])

    def test_duplicate_output_cases_retain_the_first_machine_channel(self) -> None:
        json_case = json.loads(
            (runner.CASES / "core/duplicate-output-json-invalid.json").read_text(
                "utf-8"
            )
        )["expected"]
        self.assertEqual("json", json_case["stdout"]["kind"])
        self.assertEqual("INVOCATION_INVALID", json_case["resultMatches"]["code"])
        self.assertEqual("invocation", json_case["resultMatches"]["boundary"])

        jsonl_case = json.loads(
            (runner.CASES / "core/duplicate-output-jsonl-invalid.json").read_text(
                "utf-8"
            )
        )["expected"]
        self.assertEqual("jsonl", jsonl_case["stdout"]["kind"])
        self.assertEqual(["runner.failed"], jsonl_case["stdout"]["eventTypes"])
        self.assertEqual("INVOCATION_INVALID", jsonl_case["errorCode"])

    def test_invocation_and_configuration_cases_freeze_distinct_recovery_boundaries(
        self,
    ) -> None:
        invocation = json.loads(
            (
                runner.CASES / "operations/invocation-invalid-runner-command.json"
            ).read_text("utf-8")
        )
        configuration = json.loads(
            (
                runner.CASES / "operations/config-invalid-harness-environment.json"
            ).read_text("utf-8")
        )
        expected = invocation["expected"]
        self.assertEqual(2, expected["exitCode"])
        self.assertEqual("INVOCATION_INVALID", expected["resultMatches"]["code"])
        self.assertEqual("invocation", expected["resultMatches"]["boundary"])
        self.assertEqual(
            "Runner invocation is invalid.",
            expected["resultMatches"]["message"],
        )
        self.assertEqual(
            "Review the runner syntax with the --help option, place global options "
            "before cli, and retry the command.",
            expected["resultMatches"]["action"],
        )
        self.assertEqual(
            "configuration",
            configuration["expected"]["resultMatches"]["boundary"],
        )

    def test_installed_harness_fixture_emits_the_active_image_terminal_contract(
        self,
    ) -> None:
        spec = importlib.util.spec_from_file_location(
            "openprose_installed_harness_fixture",
            runner.INSTALLED_ADAPTER_HARNESS,
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        task = {"argv": ["prose", "write", "Hello world"]}

        sentinel = json.loads(
            fixture.terminal_for_image([], b"OPENPROSE_SENTINEL_IMAGE_V1", [], task)
        )
        self.assertEqual(
            {
                "schema": "openprose.sentinel-terminal-envelope/1",
                "semanticStatus": "not-applicable",
                "marker": "OPENPROSE_SENTINEL_TERMINAL_V1",
            },
            sentinel,
        )

        echo = json.loads(
            fixture.terminal_for_image([], b"OPENPROSE_ECHO_IMAGE_V0", [], task)
        )
        self.assertEqual("openprose.echo-terminal/1", echo["schema"])
        self.assertEqual(task, echo["task"])

    def test_installed_adapter_control_installs_only_the_selected_frozen_harness(
        self,
    ) -> None:
        case = {
            "controls": {"installedAdapter": {"adapterId": "omp/rpc"}},
            "invocation": {
                "argv": ["--version"],
                "cwd": "{{WORKSPACE}}",
                "environment": {},
            },
        }
        product = runner.Product("fixture", Path(sys.executable))
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            environment_root = root / "environment"
            workspace = root / "workspace"
            captured: dict[str, object] = {}
            original = runner.run_owned_process

            def record(argv, *, cwd, environment, timeout_seconds):
                captured.update(
                    argv=list(argv),
                    cwd=cwd,
                    environment=dict(environment),
                    timeout_seconds=timeout_seconds,
                )
                return runner.OwnedProcessResult(0, b"", b"", False, True)

            runner.run_owned_process = record
            try:
                runner.execute(product, case, environment_root, workspace)
            finally:
                runner.run_owned_process = original

            path = Path(captured["environment"]["PATH"])
            self.assertEqual(
                ["bun", "omp", "python3"],
                sorted(item.name for item in path.iterdir()),
            )
            self.assertEqual(
                runner.INSTALLED_ADAPTER_HARNESS.read_bytes(),
                (path / "omp").read_bytes(),
            )
            self.assertEqual(
                runner.BUN_RUNTIME_FIXTURE,
                (path / "bun").read_bytes(),
            )
            self.assertEqual(
                "fixture-provider-free-openrouter-key",
                captured["environment"]["OPENROUTER_API_KEY"],
            )

    def test_installed_adapter_wrong_stream_control_installs_only_the_probe_fixture(
        self,
    ) -> None:
        case = {
            "controls": {
                "installedAdapter": {
                    "adapterId": "codex/exec-json",
                    "versionProbeScenario": "wrong-stream-success",
                }
            },
            "invocation": {
                "argv": ["--version"],
                "cwd": "{{WORKSPACE}}",
                "environment": {},
            },
        }
        product = runner.Product("fixture", Path(sys.executable))
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            environment_root = root / "environment"
            workspace = root / "workspace"
            captured: dict[str, object] = {}
            original = runner.run_owned_process

            def record(argv, *, cwd, environment, timeout_seconds):
                captured.update(
                    argv=list(argv),
                    cwd=cwd,
                    environment=dict(environment),
                    timeout_seconds=timeout_seconds,
                )
                return runner.OwnedProcessResult(0, b"", b"", False, True)

            runner.run_owned_process = record
            try:
                runner.execute(product, case, environment_root, workspace)
            finally:
                runner.run_owned_process = original

            path = Path(captured["environment"]["PATH"])
            self.assertEqual(
                ["codex", "python3"],
                sorted(item.name for item in path.iterdir()),
            )
            self.assertEqual(
                runner.WRONG_STREAM_VERSION_HARNESS,
                (path / "codex").read_bytes(),
            )
            self.assertNotEqual(
                runner.INSTALLED_ADAPTER_HARNESS.read_bytes(),
                (path / "codex").read_bytes(),
            )

    def test_installed_adapter_and_fake_process_controls_are_mutually_exclusive(
        self,
    ) -> None:
        case = {
            "controls": {
                "fakeHarness": {"scenario": "success"},
                "installedAdapter": {"adapterId": "codex/exec-json"},
            },
            "invocation": {
                "argv": ["--version"],
                "cwd": "{{WORKSPACE}}",
                "environment": {},
            },
        }
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with self.assertRaisesRegex(ValueError, "cannot select both"):
                runner.execute(
                    runner.Product("fixture", Path(sys.executable)),
                    case,
                    root / "environment",
                    root / "workspace",
                )

    def test_build_products_uses_explicit_sentinel_test_profile(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []
        original_run = runner.subprocess.run
        original_key = os.environ.get("OPENAI_API_KEY")
        original_override = os.environ.get("OPENPROSE_IMAGE_SOURCE_DIR")

        def record(argv, **kwargs):
            calls.append((list(argv), dict(kwargs)))
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        runner.subprocess.run = record
        os.environ["OPENAI_API_KEY"] = "must-not-enter-build"
        os.environ["OPENPROSE_IMAGE_SOURCE_DIR"] = "/hostile/ambient/image"
        try:
            runner.build_products()
        finally:
            runner.subprocess.run = original_run
            if original_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = original_key
            if original_override is None:
                os.environ.pop("OPENPROSE_IMAGE_SOURCE_DIR", None)
            else:
                os.environ["OPENPROSE_IMAGE_SOURCE_DIR"] = original_override

        self.assertEqual(len(calls), 3)
        generator, rust, bun = calls
        self.assertIn("image_bundle.py", " ".join(generator[0]))
        self.assertEqual(rust[0][0], "cargo")
        self.assertEqual(bun[0][0], "bun")
        self.assertEqual(
            rust[0][rust[0].index("--features") + 1],
            "prose-cli/test-seams",
        )
        self.assertIn("--test-seams", bun[0])
        self.assertEqual(bun[0][bun[0].index("--image-dir") + 1], str(runner.SENTINEL))
        for _argv, options in calls:
            environment = options["env"]
            self.assertNotIn("OPENAI_API_KEY", environment)
            self.assertNotEqual(
                environment.get("OPENPROSE_IMAGE_SOURCE_DIR"),
                "/hostile/ambient/image",
            )
        for _argv, options in (rust, bun):
            environment = options["env"]
            self.assertEqual(
                environment["OPENPROSE_IMAGE_SOURCE_DIR"], str(runner.SENTINEL)
            )
            self.assertTrue(
                environment["OPENPROSE_IMAGE_BUNDLE"].endswith("sentinel.bundle.bin")
            )

    def test_candidate_identity_rejects_oversized_bytes_before_hashing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            candidate = Path(raw) / "oversized"
            with candidate.open("wb") as output:
                output.truncate(runner.MAX_CANDIDATE_BYTES + 1)
            candidate.chmod(0o700)
            with self.assertRaisesRegex(ValueError, "candidate byte limit"):
                runner.capture_candidate_identity(
                    runner.Product("oversized", candidate, "rust")
                )

    def test_all_exact_dx_fixtures_validate_the_declared_machine_contract(self) -> None:
        contracts = runner.ContractRegistry()
        fixture_root = runner.CLI / "shared/fixtures/dx"
        for fixture in sorted(fixture_root.glob("*.json")):
            with self.subTest(fixture=fixture.name):
                value = json.loads(fixture.read_text("utf-8"))
                self.assertEqual([], contracts.errors(value["schema"], value))

    def test_exact_json_fixture_freezes_order_redaction_and_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            fixture = runner.CLI / "shared/fixtures/dx/dry-run-mock.json"
            expected = json.loads(fixture.read_text("utf-8"))
            actual = json.loads(
                json.dumps(expected).replace("{{WORKSPACE}}", str(workspace))
            )
            observation = runner.Observation(
                runner.Product("fixture", Path(sys.executable)),
                {
                    "expected": {
                        "exitCode": 0,
                        "stdout": {
                            "kind": "json",
                            "schema": "openprose.runner-dry-run-report/1",
                        },
                        "stderr": {"kind": "empty"},
                        "resultFixture": "cli/shared/fixtures/dx/dry-run-mock.json",
                    }
                },
                0,
                json.dumps(actual).encode(),
                b"",
                workspace=workspace,
            )
            self.assertEqual(
                [], runner.validate_output(observation, runner.ContractRegistry())
            )
            actual["configuration"][-1]["redacted"] = True
            observation.stdout = json.dumps(actual).encode()
            failures = runner.validate_output(observation, runner.ContractRegistry())
            self.assertTrue(
                any("exact JSON fixture" in failure for failure in failures)
            )

    def test_bare_runner_error_checks_exact_code_and_action(self) -> None:
        oracle = json.loads((runner.CLI / "shared/fixtures/adapters/sdk-production.json").read_text())
        action = oracle["credentialAbsence"]["action"]
        error = {
            "schema": "openprose.runner-error/1", "code": "HARNESS_NEEDS_AUTH",
            "boundary": "authentication", "exitCode": 10, "retryable": False,
            "message": "The Agents SDK requires OPENAI_API_KEY.", "action": action,
            "details": {"authProfile": "openai-api-key"},
        }
        observation = runner.Observation(
            runner.Product("fixture", Path(sys.executable)),
            {"expected": {
                "exitCode": 10,
                "stdout": {"kind": "json", "schema": "openprose.runner-error/1"},
                "stderr": {"kind": "empty"},
                "errorCode": "HARNESS_NEEDS_AUTH", "errorAction": action,
            }}, 10, json.dumps(error).encode(), b"",
        )
        contracts = runner.ContractRegistry()
        self.assertEqual([], runner.validate_output(observation, contracts))
        error["code"] = "HARNESS_FAILED"
        observation.stdout = json.dumps(error).encode()
        self.assertTrue(any("HARNESS_NEEDS_AUTH" in failure for failure in runner.validate_output(observation, contracts)))
        error["code"] = "HARNESS_NEEDS_AUTH"
        error["action"] = "wrong"
        observation.stdout = json.dumps(error).encode()
        self.assertTrue(any(action in failure for failure in runner.validate_output(observation, contracts)))

    def test_dry_run_blocking_error_is_the_error_validation_surface(self) -> None:
        fixture = runner.load_exact_result_fixture(
            "cli/shared/fixtures/dx/dry-run-default-hosted.json"
        )
        action = fixture["blockingError"]["action"]
        observation = runner.Observation(
            runner.Product("fixture", Path(sys.executable)),
            {
                "expected": {
                    "exitCode": 10,
                    "stdout": {
                        "kind": "json",
                        "schema": "openprose.runner-dry-run-report/1",
                    },
                    "stderr": {"kind": "empty"},
                    "errorCode": "HOSTED_UNAVAILABLE",
                    "errorAction": action,
                }
            },
            10,
            json.dumps(fixture).encode(),
            b"",
        )
        self.assertEqual(
            [], runner.validate_output(observation, runner.ContractRegistry())
        )
        fixture["blockingError"]["action"] = "wrong"
        observation.stdout = json.dumps(fixture).encode()
        failures = runner.validate_output(observation, runner.ContractRegistry())
        self.assertTrue(any(action in failure for failure in failures))

    def test_started_harness_expectation_is_checked_from_machine_evidence(self) -> None:
        fixture = runner.load_exact_result_fixture(
            "cli/shared/fixtures/dx/dry-run-mock.json"
        )
        observation = runner.Observation(
            runner.Product("fixture", Path(sys.executable)),
            {
                "expected": {
                    "exitCode": 0,
                    "stdout": {
                        "kind": "json",
                        "schema": "openprose.runner-dry-run-report/1",
                    },
                    "stderr": {"kind": "empty"},
                    "startedHarness": False,
                }
            },
            0,
            json.dumps(fixture).encode(),
            b"",
        )
        self.assertEqual(
            [], runner.validate_output(observation, runner.ContractRegistry())
        )
        observation.case["expected"]["startedHarness"] = True
        failures = runner.validate_output(observation, runner.ContractRegistry())
        self.assertIn("harness start: expected True, observed False", failures)

    def test_file_effects_are_bounded_to_workspace_and_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            selection = workspace / "config/openprose/cli.toml"
            selection.parent.mkdir(parents=True)
            selection.write_text('harness = "claude"\n', encoding="utf-8")
            value = {
                "schema": "openprose.harness-selection/1",
                "harness": "claude",
                "scope": "user",
                "path": str(selection),
                "changed": True,
            }
            expected = {
                "exitCode": 0,
                "stdout": {
                    "kind": "json",
                    "schema": "openprose.harness-selection/1",
                },
                "stderr": {"kind": "empty"},
                "fileEffects": [
                    {
                        "path": "{{WORKSPACE}}/config/openprose/cli.toml",
                        "utf8": 'harness = "claude"\n',
                    }
                ],
            }
            observation = runner.Observation(
                runner.Product("fixture", Path(sys.executable)),
                {"expected": expected},
                0,
                json.dumps(value).encode(),
                b"",
                workspace=workspace,
            )
            self.assertEqual(
                [], runner.validate_output(observation, runner.ContractRegistry())
            )
            selection.write_text('harness = "codex"\n', encoding="utf-8")
            failures = runner.validate_output(observation, runner.ContractRegistry())
            self.assertTrue(any("file effect" in failure for failure in failures))
            expected["fileEffects"][0]["path"] = "{{WORKSPACE}}/../escape"
            failures = runner.validate_output(observation, runner.ContractRegistry())
            self.assertTrue(
                any("leaves product workspace" in failure for failure in failures)
            )

    def test_result_subset_normalizes_the_product_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            result = {
                "schema": "openprose.harness-selection/1",
                "harness": "claude",
                "scope": "user",
                "path": str(workspace / "config/openprose/cli.toml"),
                "changed": True,
            }
            observation = runner.Observation(
                runner.Product("fixture", Path(sys.executable)),
                {
                    "expected": {
                        "exitCode": 0,
                        "stdout": {
                            "kind": "json",
                            "schema": "openprose.harness-selection/1",
                        },
                        "stderr": {"kind": "empty"},
                        "resultMatches": {
                            "path": "{{WORKSPACE}}/config/openprose/cli.toml"
                        },
                    }
                },
                0,
                json.dumps(result).encode(),
                b"",
                workspace=workspace,
            )
            self.assertEqual(
                [], runner.validate_output(observation, runner.ContractRegistry())
            )

    def test_jsonl_terminal_payload_is_the_result_validation_surface(self) -> None:
        error = {
            "schema": "openprose.runner-error/1",
            "code": "HOSTED_UNAVAILABLE",
            "boundary": "hosted-service",
            "exitCode": 10,
            "retryable": False,
            "message": "Programs run here only with a local harness; hosted runs use `prose cli run submit`.",
            "action": (
                "To use the hosted service, run `cli run submit FILE --preview`; running "
                "programs on this machine needs a local harness (`cli harness list`)."
            ),
            "details": {"billingOwner": "openprose", "fallbackSelected": False},
        }
        event = {
            "schema": "openprose.normalized-event/1",
            "sequence": 0,
            "timestamp": "2025-01-01T00:00:00Z",
            "invocationId": "fixture-invocation-0001",
            "type": "runner.failed",
            "payload": {"kind": "runner.failed", "error": error},
        }
        observation = runner.Observation(
            runner.Product("fixture", Path(sys.executable)),
            {
                "expected": {
                    "exitCode": 10,
                    "stdout": {
                        "kind": "jsonl",
                        "eventTypes": ["runner.failed"],
                        "terminalType": "runner.failed",
                    },
                    "stderr": {"kind": "empty"},
                    "errorCode": "HOSTED_UNAVAILABLE",
                    "errorAction": error["action"],
                }
            },
            10,
            (json.dumps(event) + "\n").encode(),
            b"",
        )
        self.assertEqual(
            [], runner.validate_output(observation, runner.ContractRegistry())
        )
        event["payload"]["error"]["code"] = "CANCELLED"
        observation.stdout = (json.dumps(event) + "\n").encode()
        failures = runner.validate_output(observation, runner.ContractRegistry())
        self.assertTrue(any("HOSTED_UNAVAILABLE" in failure for failure in failures))

    def test_jsonl_difference_normalization_removes_only_declared_event_volatility(
        self,
    ) -> None:
        left = [
            {
                "schema": "openprose.normalized-event/1",
                "sequence": 0,
                "timestamp": "left",
                "invocationId": "left-id",
                "type": "runner.started",
                "payload": {
                    "kind": "runner.started",
                    "runnerName": "rust",
                    "runnerVersion": "1",
                },
            }
        ]
        right = [
            {
                "schema": "openprose.normalized-event/1",
                "sequence": 0,
                "timestamp": "right",
                "invocationId": "right-id",
                "type": "runner.started",
                "payload": {
                    "kind": "runner.started",
                    "runnerName": "bun",
                    "runnerVersion": "1",
                },
            }
        ]
        self.assertEqual(
            runner.normalized_for_difference(left),
            runner.normalized_for_difference(right),
        )

    def test_candidate_specs_preserve_surface_label_and_expected_runner(self) -> None:
        products = runner.parse_candidate_specs(
            [
                ["rust-installed", "rust", "/tmp/rust/prose"],
                ["bun-installed", "bun", "/tmp/bun/prose"],
                ["npm-launcher", "bun", "/tmp/npm/prose"],
            ]
        )
        self.assertEqual(
            [
                ("rust-installed", "rust", Path("/tmp/rust/prose")),
                ("bun-installed", "bun", Path("/tmp/bun/prose")),
                ("npm-launcher", "bun", Path("/tmp/npm/prose")),
            ],
            [
                (item.name, item.expected_runner_name, item.executable)
                for item in products
            ],
        )
        with self.assertRaisesRegex(ValueError, "duplicate candidate label"):
            runner.parse_candidate_specs(
                [["same", "rust", "/tmp/a"], ["same", "bun", "/tmp/b"]]
            )
        with_interpreter = runner.attach_candidate_interpreters(
            products, [["npm-launcher", "/usr/bin/node"]]
        )
        self.assertEqual(Path("/usr/bin/node"), with_interpreter[2].interpreter)
        with self.assertRaisesRegex(ValueError, "duplicate candidate interpreter"):
            runner.attach_candidate_interpreters(
                products,
                [
                    ["npm-launcher", "/usr/bin/node"],
                    ["npm-launcher", "/opt/node"],
                ],
            )

    @unittest.skipUnless(os.name == "posix", "symlink custody fixture requires POSIX")
    def test_symlink_candidate_binds_link_and_resolved_target_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "launcher.py"
            target.write_bytes(b"#!/usr/bin/env python3\nprint('ok')\n")
            target.chmod(0o755)
            leaf = root / "prose"
            leaf.symlink_to(target.name)
            product = runner.Product("npm-launcher", leaf, "bun")
            identity = runner.capture_candidate_identity(product)
            self.assertEqual("symlink", identity.leaf_kind)
            self.assertEqual(
                runner.sha256(target.name.encode("utf-8")),
                identity.leaf_sha256,
            )
            self.assertEqual(runner.sha256(target.read_bytes()), identity.target_sha256)
            self.assertFalse(identity.leaf_and_target_same_bytes)
            record = runner.candidate_report_record(product, identity)
            self.assertNotIn(str(root), json.dumps(record))
            self.assertIsNone(runner.candidate_identity_difference(product, identity))

    def test_tampered_candidate_bytes_fail_custody(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "prose"
            executable.write_bytes(b"#!/usr/bin/env python3\nprint('one')\n")
            executable.chmod(0o755)
            product = runner.Product("rust-installed", executable, "rust")
            identity = runner.capture_candidate_identity(product)
            executable.write_bytes(b"#!/usr/bin/env python3\nprint('two')\n")
            difference = runner.candidate_identity_difference(product, identity)
            self.assertEqual("resolved target bytes changed", difference)

    def test_machine_report_is_closed_canonical_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "prose"
            executable.write_bytes(b"fixture executable")
            executable.chmod(0o755)
            product = runner.Product("rust-installed", executable, "rust")
            identity = runner.capture_candidate_identity(product)
            report = runner.make_report(
                phase=7,
                case_ids=["a.case", "b.case"],
                candidates=[(product, identity)],
                candidate_passed=1,
                candidate_failed=1,
                differential_passed=0,
                differential_failed=0,
                failures=[
                    {
                        "caseId": "b.case",
                        "candidateLabels": ["rust-installed"],
                        "code": "OUTPUT_VALIDATION_FAILED",
                        "detail": "bounded detail",
                    }
                ],
            )
            encoded = runner.render_report(report)
            self.assertEqual(encoded, runner.render_report(report))
            self.assertTrue(encoded.endswith(b"\n"))
            self.assertEqual(
                encoded,
                runner.canonical_json(json.loads(encoded)) + b"\n",
            )
            self.assertEqual(
                {
                    "schema",
                    "phase",
                    "caseIds",
                    "candidates",
                    "validations",
                    "failures",
                    "claims",
                },
                set(report),
            )
            self.assertEqual(2, report["validations"]["total"])
            self.assertEqual("fail", report["validations"]["status"])
            self.assertFalse(report["claims"]["semanticConformance"])
            self.assertFalse(report["claims"]["releaseAdmission"])
            self.assertFalse(report["claims"]["detachedDescendantContainment"])
            self.assertNotIn(str(Path(temporary)), encoded.decode("utf-8"))
            self.assertEqual([], runner.validate_report(report))
            changed = json.loads(encoded)
            changed["validations"]["candidateCases"]["unknown"] = 1
            self.assertTrue(runner.validate_report(changed))
            malformed = json.loads(encoded)
            malformed["validations"]["candidateCases"] = None
            malformed["caseIds"] = 7
            malformed["candidates"][0]["executableLeaf"] = None
            self.assertTrue(runner.validate_report(malformed))
            overclaim = json.loads(encoded)
            overclaim["claims"]["detachedDescendantContainment"] = True
            self.assertTrue(
                any(
                    "overstates" in failure
                    for failure in runner.validate_report(overclaim)
                )
            )

            destination = Path(temporary) / "exclusive-report.json"
            runner.write_report(destination, report)
            original = destination.read_bytes()
            with self.assertRaises(FileExistsError):
                runner.write_report(destination, report)
            self.assertEqual(original, destination.read_bytes())

    def test_report_option_runs_exact_candidates_without_building(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            help_bytes = (runner.CASES / "fixtures/runner-help.txt").read_bytes()
            executable = root / "fake-prose"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                f"sys.stdout.buffer.write({help_bytes!r})\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            reports = [root / "first.json", root / "second.json"]
            outputs: list[str] = []
            for report_path in reports:
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                    stderr
                ):
                    status = runner.main(
                        [
                            "--phase",
                            "7",
                            "--case",
                            "core.initial-help",
                            "--candidate",
                            "fixture",
                            "rust",
                            str(executable),
                            "--candidate-interpreter",
                            "fixture",
                            sys.executable,
                            "--report-json",
                            str(report_path),
                        ]
                    )
                self.assertEqual(0, status, stderr.getvalue())
                outputs.append(stdout.getvalue())
            self.assertEqual(reports[0].read_bytes(), reports[1].read_bytes())
            self.assertEqual(outputs[0], outputs[1])
            report = json.loads(reports[0].read_bytes())
            interpreter = report["candidates"][0]["interpreter"]
            self.assertIsNotNone(interpreter)
            self.assertEqual(
                {
                    "mode": "resolved-original-path",
                    "preAndPostByteCustody": True,
                    "ownedSnapshot": False,
                    "relocatableClosureCaptured": False,
                    "execBoundaryToctouProtection": "not-enforced",
                },
                interpreter["execution"],
            )
            overclaim = json.loads(reports[0].read_bytes())
            overclaim["candidates"][0]["interpreter"]["execution"][
                "execBoundaryToctouProtection"
            ] = "enforced"
            self.assertTrue(
                any(
                    "overstates interpreter custody" in failure
                    for failure in runner.validate_report(overclaim)
                )
            )

    def test_report_rejects_candidate_attempt_to_mutate_owned_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            help_bytes = (runner.CASES / "fixtures/runner-help.txt").read_bytes()
            executable = root / "mutating-prose"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "import sys\n"
                f"sys.stdout.buffer.write({help_bytes!r})\n"
                "with Path(__file__).open('ab') as changed:\n"
                "    changed.write(b'# mutation\\n')\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            source_bytes = executable.read_bytes()
            report_path = root / "report.json"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                status = runner.main(
                    [
                        "--phase",
                        "7",
                        "--case",
                        "core.initial-help",
                        "--candidate",
                        "fixture",
                        "rust",
                        str(executable),
                        "--report-json",
                        str(report_path),
                    ]
                )
            self.assertEqual(1, status)
            report = json.loads(report_path.read_bytes())
            self.assertEqual("fail", report["validations"]["status"])
            self.assertEqual(1, report["validations"]["candidateCases"]["failed"])
            self.assertEqual(
                ["OUTPUT_VALIDATION_FAILED"],
                [failure["code"] for failure in report["failures"]],
            )
            self.assertEqual(source_bytes, executable.read_bytes())

    def test_owned_snapshot_executes_captured_bytes_after_source_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.py"
            source.write_text(
                "#!/usr/bin/env python3\nprint('captured')\n", encoding="utf-8"
            )
            source.chmod(0o755)
            product = runner.Product("fixture", source, "rust", Path(sys.executable))
            identity = runner.capture_candidate_identity(product)
            snapshot = runner.snapshot_product(
                product, identity, root / "owned-snapshot"
            )
            source.write_text(
                "#!/usr/bin/env python3\nprint('changed')\n", encoding="utf-8"
            )
            result = runner.run_owned_process(
                snapshot.execution_argv([]),
                cwd=root,
                environment={"PATH": os.defpath},
                timeout_seconds=2.0,
            )
            self.assertEqual(0, result.exit_code)
            self.assertEqual(b"captured\n", result.stdout)
            self.assertIsNone(runner.snapshot_identity_difference(snapshot, identity))

    @unittest.skipUnless(
        os.name == "posix", "relative interpreter fixture requires POSIX"
    )
    def test_interpreter_executes_in_original_location_with_relative_runtime(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime_root = root / "runtime"
            interpreter = runtime_root / "bin" / "fake-node"
            sibling = runtime_root / "lib" / "closure-marker"
            interpreter.parent.mkdir(parents=True)
            sibling.parent.mkdir(parents=True)
            sibling.write_text("closure-present\n", encoding="utf-8")
            interpreter.write_text(
                "#!/bin/sh\n"
                'root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)\n'
                'test "$(cat "$root/lib/closure-marker")" = closure-present || exit 91\n'
                'exec "$@"\n',
                encoding="utf-8",
            )
            interpreter.chmod(0o755)
            candidate = root / "launcher"
            candidate.write_text("#!/bin/sh\nprintf 'portable\\n'\n", encoding="utf-8")
            candidate.chmod(0o755)
            product = runner.Product("fixture", candidate, "bun", interpreter)
            identity = runner.capture_candidate_identity(product)
            snapshot = runner.snapshot_product(
                product, identity, root / "owned-snapshot"
            )
            self.assertEqual(interpreter.resolve(), snapshot.execution_interpreter)
            self.assertFalse((root / "owned-snapshot" / "interpreter").exists())
            result = runner.run_owned_process(
                snapshot.execution_argv([]),
                cwd=root,
                environment={"PATH": os.defpath},
                timeout_seconds=2.0,
            )
            self.assertEqual(0, result.exit_code, result.stderr)
            self.assertEqual(b"portable\n", result.stdout)
            self.assertIsNone(runner.snapshot_identity_difference(snapshot, identity))

    @unittest.skipUnless(shutil.which("node"), "Node is required for CommonJS fixture")
    def test_node_executes_snapshot_bytes_with_original_package_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "node_modules" / "@openprose" / "prose-cli"
            launcher = package / "bin" / "prose.js"
            launcher.parent.mkdir(parents=True)
            (package / "closure-marker").write_text(
                "package-context\n", encoding="utf-8"
            )
            launcher.write_text(
                "const fs=require('node:fs'); const path=require('node:path');\n"
                "process.stdout.write(fs.readFileSync(path.resolve(__dirname,'..','closure-marker'),'utf8'));\n",
                encoding="utf-8",
            )
            launcher.chmod(0o755)
            node = Path(shutil.which("node") or "")
            product = runner.Product("npm-launcher", launcher, "bun", node)
            identity = runner.capture_candidate_identity(product)
            snapshot = runner.snapshot_product(
                product, identity, root / "owned-snapshot"
            )
            result = runner.run_owned_process(
                snapshot.execution_argv(["opaque", "--flag"]),
                cwd=root,
                environment={"PATH": os.defpath},
                timeout_seconds=2.0,
            )
            self.assertEqual(0, result.exit_code, result.stderr)
            self.assertEqual(b"package-context\n", result.stdout)

    def test_owned_snapshot_tampering_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.write_bytes(b"#!/bin/sh\nexit 0\n")
            source.chmod(0o755)
            product = runner.Product("fixture", source, "rust")
            identity = runner.capture_candidate_identity(product)
            snapshot = runner.snapshot_product(
                product, identity, root / "owned-snapshot"
            )
            assert snapshot.execution_executable is not None
            snapshot.execution_executable.chmod(0o755)
            snapshot.execution_executable.write_bytes(b"#!/bin/sh\nexit 1\n")
            self.assertEqual(
                "owned executable snapshot bytes changed",
                runner.snapshot_identity_difference(snapshot, identity),
            )

    def test_owned_snapshot_refuses_source_bytes_changed_after_identity_capture(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.write_bytes(b"#!/bin/sh\nexit 0\n")
            source.chmod(0o755)
            product = runner.Product("fixture", source, "rust")
            identity = runner.capture_candidate_identity(product)
            source.write_bytes(b"#!/bin/sh\nexit 1\n")
            with self.assertRaisesRegex(ValueError, "changed before snapshot"):
                runner.snapshot_product(product, identity, root / "owned-snapshot")

    def test_failure_detail_redacts_paths_tokens_and_bounds_length(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            suite = Path(temporary)
            product = runner.Product(
                "rust-installed", suite / "owned/bin/prose", "rust"
            )
            detail = (
                f"workspace={suite}/case token=super-secret "
                f"candidate={product.executable} " + ("x" * 5000)
            )
            sanitized = runner.sanitize_failure_detail(detail, suite, [product])
            self.assertNotIn(str(suite), sanitized)
            self.assertNotIn("super-secret", sanitized)
            self.assertLessEqual(
                len(sanitized.encode("utf-8")), runner.MAX_FAILURE_BYTES
            )

    def test_candidate_expected_runner_identity_is_checked_before_differential(
        self,
    ) -> None:
        value = json.loads(
            (
                runner.CLI / "shared/fixtures/transport/runner-result-success.json"
            ).read_text("utf-8")
        )
        value["runner"]["name"] = "rust"
        observation = runner.Observation(
            runner.Product("npm-launcher", Path(sys.executable), "bun"),
            {
                "expected": {
                    "exitCode": 0,
                    "stdout": {
                        "kind": "json",
                        "schema": "openprose.runner-result/1",
                    },
                    "stderr": {"kind": "empty"},
                }
            },
            0,
            json.dumps(value).encode(),
            b"",
        )
        failures = runner.validate_output(observation, runner.ContractRegistry())
        self.assertIn("runner identity: expected 'bun', got 'rust'", failures)

    def test_process_groups_cannot_claim_detached_descendant_authority(self) -> None:
        self.assertFalse(runner.timeout_cleanup_has_descendant_authority("posix"))
        self.assertFalse(runner.timeout_cleanup_has_descendant_authority("nt"))

    def test_configuration_sourced_cwd_is_not_treated_as_runner_result_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            value = json.loads(
                (
                    runner.CLI
                    / "shared/fixtures/operations/configuration-explanation.json"
                ).read_text("utf-8")
            )
            encoded = json.dumps(value).replace("/workspace", str(workspace)).encode()
            observation = runner.Observation(
                runner.Product("fixture", Path(sys.executable)),
                {
                    "expected": {
                        "exitCode": 0,
                        "stdout": {
                            "kind": "json",
                            "schema": "openprose.configuration-explanation/1",
                        },
                        "stderr": {"kind": "empty"},
                    }
                },
                0,
                encoded,
                b"",
                workspace=workspace,
            )
            failures = runner.validate_output(
                observation,
                runner.ContractRegistry(),
            )
            self.assertEqual([], failures)

    def test_product_roots_use_distinct_workspaces_and_environment_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            case_root = Path(temporary)
            rust_environment, rust_workspace = runner.product_roots(case_root, "rust")
            bun_environment, bun_workspace = runner.product_roots(case_root, "bun")
            self.assertNotEqual(rust_workspace, bun_workspace)
            self.assertNotEqual(rust_environment, bun_environment)
            rust_workspace.mkdir(parents=True)
            bun_workspace.mkdir(parents=True)
            (rust_workspace / "product-state").write_text("rust", encoding="utf-8")
            self.assertFalse((bun_workspace / "product-state").exists())

    def test_execute_forwards_only_each_products_own_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            case_root = Path(temporary)
            executable = case_root / "fake-product"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json,os,pathlib\n"
                "pathlib.Path('state').write_text(os.environ['PRODUCT'])\n"
                "print(json.dumps({'cwd':os.getcwd(),'forwarded':os.environ['FORWARDED']}))\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            case = {
                "controls": {},
                "invocation": {
                    "argv": [],
                    "cwd": "{{WORKSPACE}}",
                    "environment": {
                        "FORWARDED": "{{WORKSPACE}}/nested",
                        "PRODUCT": "fixture",
                    },
                },
                "expected": {},
            }
            observed = {}
            for name in ("rust", "bun"):
                environment_root, workspace = runner.product_roots(case_root, name)
                workspace.mkdir(parents=True)
                observation = runner.execute(
                    runner.Product(name, executable),
                    case,
                    environment_root,
                    workspace,
                )
                self.assertTrue(observation.process_settled)
                self.assertEqual(observation.exit_code, 0)
                observed[name] = json.loads(observation.stdout)
            self.assertNotEqual(observed["rust"]["cwd"], observed["bun"]["cwd"])
            for name in ("rust", "bun"):
                _, workspace = runner.product_roots(case_root, name)
                self.assertEqual(observed[name]["cwd"], str(workspace.resolve()))
                self.assertEqual(
                    observed[name]["forwarded"], str(workspace.resolve() / "nested")
                )
                self.assertEqual((workspace / "state").read_text("utf-8"), "fixture")

    def test_difference_normalization_verifies_and_rewrites_owned_workspace(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            value = {
                "cwd": {
                    "path": str(workspace),
                    "identitySha256": runner.sha256(str(workspace).encode("utf-8")),
                },
                "nested": {"path": str(workspace / "home"), "unrelated": "/elsewhere"},
            }
            normalized = runner.normalized_for_difference(value, workspace)
            self.assertEqual(normalized["cwd"]["path"], "{{WORKSPACE}}")
            self.assertEqual(
                normalized["cwd"]["identitySha256"], "{{WORKSPACE_IDENTITY_SHA256}}"
            )
            self.assertEqual(normalized["nested"]["path"], "{{WORKSPACE}}/home")
            self.assertEqual(normalized["nested"]["unrelated"], "/elsewhere")

    @unittest.skipUnless(
        os.name == "posix", "owned process-group oracle requires POSIX"
    )
    def test_owned_timeout_kills_descendants_and_settles_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "image"
            task = root / "task"
            identities = root / "identities.json"
            image.write_bytes(b"image")
            task.write_bytes(b"task")
            result = runner.run_owned_process(
                [
                    sys.executable,
                    str(runner.FAKE_HARNESS),
                    "run",
                    "--scenario",
                    "descendant",
                    "--image-file",
                    str(image),
                    "--task-file",
                    str(task),
                    "--descendant-pid-file",
                    str(identities),
                ],
                cwd=root,
                environment={"PATH": os.defpath},
                timeout_seconds=2.0,
            )
            self.assertTrue(result.timed_out)
            self.assertTrue(result.settled)
            self.assertEqual(result.exit_code, 124)
            self.assertTrue(identities.is_file())
            published = json.loads(identities.read_text("utf-8"))
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and any(
                runner.pid_exists(published[name])
                for name in ("childPid", "grandchildPid")
            ):
                time.sleep(0.01)
            self.assertFalse(runner.pid_exists(published["childPid"]))
            self.assertFalse(runner.pid_exists(published["grandchildPid"]))

    @unittest.skipUnless(
        os.name == "posix", "owned process-group oracle requires POSIX"
    )
    def test_zero_exit_with_live_owned_descendant_is_not_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = root / "child.json"
            script = (
                "import json,subprocess,sys\n"
                "child=subprocess.Popen([sys.executable,'-c',"
                "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'],"
                "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                "open(sys.argv[1],'w').write(json.dumps({'pid':child.pid}))\n"
            )
            result = runner.run_owned_process(
                [sys.executable, "-c", script, str(identity)],
                cwd=root,
                environment={"PATH": os.defpath},
                timeout_seconds=2.0,
            )
            self.assertEqual(result.exit_code, 124)
            self.assertFalse(result.timed_out)
            self.assertFalse(result.settled)
            child_pid = json.loads(identity.read_text("utf-8"))["pid"]
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and runner.pid_exists(child_pid):
                time.sleep(0.01)
            self.assertFalse(runner.pid_exists(child_pid))

    def test_owned_process_capture_is_bounded_while_pipes_are_fully_drained(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            payload_bytes = runner.MAX_CAPTURE_BYTES * 2 + 17
            script = (
                "import os,sys\n"
                "remaining=int(sys.argv[1])\n"
                "chunk=b'x'*65536\n"
                "while remaining:\n"
                " part=chunk[:remaining]\n"
                " os.write(1,part); os.write(2,part); remaining-=len(part)\n"
            )
            result = runner.run_owned_process(
                [sys.executable, "-c", script, str(payload_bytes)],
                cwd=Path(temporary),
                environment={"PATH": os.defpath},
                timeout_seconds=10.0,
            )
            self.assertEqual(0, result.exit_code)
            self.assertTrue(result.settled)
            self.assertLessEqual(len(result.stdout), runner.MAX_CAPTURE_BYTES)
            self.assertLessEqual(len(result.stderr), runner.MAX_CAPTURE_BYTES)
            self.assertTrue(result.stdout_truncated)
            self.assertTrue(result.stderr_truncated)
            self.assertTrue(result.stdout.endswith(runner.CAPTURE_TRUNCATION_MARKER))
            self.assertTrue(result.stderr.endswith(runner.CAPTURE_TRUNCATION_MARKER))

    @unittest.skipUnless(os.name == "posix", "signal cleanup oracle requires POSIX")
    def test_base_exception_cleans_owned_process_group_before_reraising(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = root / "pid"
            script = (
                "import os,pathlib,signal,sys,time\n"
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                "time.sleep(60)\n"
            )

            def interrupt_after_spawn() -> None:
                deadline = time.monotonic() + 5.0
                while not identity.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                if identity.exists():
                    os.kill(os.getpid(), signal.SIGINT)

            interrupter = threading.Thread(target=interrupt_after_spawn)
            interrupter.start()
            with self.assertRaises(KeyboardInterrupt):
                runner.run_owned_process(
                    [sys.executable, "-c", script, str(identity)],
                    cwd=root,
                    environment={"PATH": os.defpath},
                    timeout_seconds=30.0,
                )
            interrupter.join(timeout=2.0)
            self.assertFalse(interrupter.is_alive())
            child_pid = int(identity.read_text("utf-8"))
            self.assertTrue(
                runner._wait_until(lambda: not runner.pid_exists(child_pid), 2.0)
            )

    def test_deep_subset_reports_nested_drift(self) -> None:
        self.assertEqual(
            [], runner.deep_subset({"a": {"b": 1}, "extra": 2}, {"a": {"b": 1}})
        )
        self.assertEqual(
            ["$.a.b: expected 2, got 1"],
            runner.deep_subset({"a": {"b": 1}}, {"a": {"b": 2}}),
        )

    def test_difference_normalization_keeps_adapter_claims(self) -> None:
        value = {
            "invocationId": "volatile",
            "runner": {"name": "rust", "version": "1", "commit": "x"},
            "timing": {"startedAt": "now", "durationMs": 1},
            "digests": {
                "invocationSha256": "derived",
                "normalizedEventsSha256": "derived",
                "taskSha256": "stable",
            },
            "adapter": {"id": "stable"},
        }
        normalized = runner.normalized_for_difference(value)
        self.assertNotIn("invocationId", normalized)
        self.assertNotIn("name", normalized["runner"])
        self.assertEqual({"taskSha256": "stable"}, normalized["digests"])
        self.assertEqual({"id": "stable"}, normalized["adapter"])

    def test_task_digest_is_shell_neutral(self) -> None:
        task = {
            "schema": "openprose.task-envelope/1",
            "argv": ["prose", "run", "space here", ";$(nope)", "雪"],
            "interactionMode": "non-interactive",
        }
        self.assertEqual(
            runner.sha256(runner.canonical_json(task)),
            runner.sha256(runner.canonical_json(task)),
        )



class SdkSourceCustodyTests(unittest.TestCase):
    def source(self, root):
        ci = str(runner.CLI / 'ci')
        if ci not in sys.path: sys.path.insert(0, ci)
        from test_kernel_rc_evidence import sdk_fixture
        from test_sdk_native_inventory import onedir_fixture
        import sdk_native_inventory as inventory
        source = root / 'production'; source.mkdir()
        sdk, table = sdk_fixture('darwin-x64'); view = onedir_fixture()
        inventory.materialize_macos_payload(source, view['payload'], view['files'], view['directories'], view['symlinks'], view['architecture'])
        for name in ('agents-sdk-build.json', 'AGENTS-SDK-NOTICES.txt'):
            data, mode = table['files'][name]; (source / name).write_bytes(data); (source / name).chmod(mode)
        (source / 'prose').write_bytes(b'synthetic nonexecuted CLI'); (source / 'prose').chmod(0o755)
        product = runner.Product('fixture', source / 'prose', 'bun')
        context = {'directory': str(source), 'platform': 'darwin-x64', 'agentsSdk': sdk}
        return product, context

    def test_source_authentication_is_once_separate_from_cli_only_oracle_substitution(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); product, context = self.source(root)
            product = runner.attach_sdk_sources([product], [['fixture', json.dumps(context)]])[0]
            identity = runner.capture_candidate_identity(product)
            snapshot = runner.snapshot_product(product, identity, root / 'snapshot')
            workspace = root / 'workspace'; workspace.mkdir(); environment = root / 'environment'; environment.mkdir()
            case = json.loads((runner.CASES / 'adapters/sdk-production-04.json').read_text())
            execution, fixture, _ = runner.prepare_sdk_installation(snapshot, case, workspace, environment)
            sibling = Path(fixture['nativePath']).parent
            self.assertFalse((sibling / 'prose-agents-sdk-runtime').exists())
            self.assertEqual(fixture['productionSdkSourceRef'], context['agentsSdk']['receiptSha256'])
            self.assertEqual(fixture['helperSubstitution'], 'provider-free-oracle')
            self.assertIs(fixture['productionSdkExecuted'], False)
            self.assertEqual((sibling / 'prose-agents-sdk').read_bytes(), runner.INSTALLED_ADAPTER_HARNESS.read_bytes())
            report = runner.make_report(phase=7, case_ids=[case['id']], candidates=[(execution, identity)], candidate_passed=1, candidate_failed=0, differential_passed=0, differential_failed=0, failures=[])
            self.assertEqual(len(report['sdkSourceCustody']['evidence']), 1)
            self.assertEqual(runner.validate_report(report), [])
            (sibling / 'prose-agents-sdk').write_bytes(b'poisoned oracle')
            observation = runner.Observation(execution, case, 0, b'', b'', workspace=workspace, sdk_fixture=fixture)
            self.assertIn('SDK fixture helper substitution bytes changed', runner.validate_sdk_effects(observation))

    def test_source_mutation_and_foreign_native_directory_refused(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); product, context = self.source(root)
            changed = dict(context, directory=str(root))
            with self.assertRaises(ValueError): runner.attach_sdk_sources([product], [['fixture', json.dumps(changed)]])
            (Path(context['directory']) / 'prose-agents-sdk-runtime/empty-data').write_bytes(b'changed')
            with self.assertRaises(ValueError): runner.attach_sdk_sources([product], [['fixture', json.dumps(context)]])
            for poison in ({}, dict(context, platform=[]), dict(context, unknown='not-accepted')):
                with self.assertRaises(ValueError): runner.attach_sdk_sources([product], [['fixture', json.dumps(poison)]])

    def test_report_source_claims_and_receipt_references_fail_closed(self):
        import copy
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); product, context = self.source(root)
            product = runner.attach_sdk_sources([product], [['fixture', json.dumps(context)]])[0]
            case = json.loads((runner.CASES / 'adapters/sdk-production-04.json').read_text())
            report = runner.make_report(phase=7, case_ids=[case['id']], candidates=[(product, runner.capture_candidate_identity(product))], candidate_passed=1, candidate_failed=0, differential_passed=0, differential_failed=0, failures=[])
            self.assertEqual(runner.validate_report(report), [])
            for poison in ('claim', 'reference', 'shape', 'policy', 'bool-length', 'digest'):
                changed = copy.deepcopy(report)
                if poison == 'claim': changed['sdkSourceCustody']['productionSdkExecuted'] = True
                elif poison == 'reference': changed['sdkSourceCustody']['candidateReferences']['fixture'] = []
                elif poison == 'shape': next(iter(changed['sdkSourceCustody']['evidence'].values()))['unknown'] = True
                else:
                    sdk = next(iter(changed['sdkSourceCustody']['evidence'].values()))['agentsSdk']
                    if poison == 'policy': sdk['discovery'] = 'PATH'
                    elif poison == 'bool-length': sdk['byteLength'] = True
                    else: sdk['dependencyLockSha256'] = 'not-a-digest'
                with self.subTest(poison=poison): self.assertTrue(runner.validate_report(changed))

if __name__ == "__main__":
    unittest.main(verbosity=2)
