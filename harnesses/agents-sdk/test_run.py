import asyncio
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
import argparse
import contextlib
import io
import json
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location('harness', Path(__file__).with_name('run.py'))
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)

class ShellTest(unittest.IsolatedAsyncioTestCase):
    async def test_read_write_and_exit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'in.txt').write_text('a\nb\n')
            result = await harness.shell('cat in.txt > out.txt; cat out.txt; exit 7', directory, 2, {'PATH': os.environ['PATH']})
            self.assertEqual(result['exit_code'], 7)
            self.assertEqual(result['stdout'], 'a\nb\n')
            self.assertEqual(Path(directory, 'out.txt').read_text(), 'a\nb\n')

    async def test_timeout_stops_delayed_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(asyncio.TimeoutError):
                await harness.shell('(sleep 0.5; touch late) & wait', directory, 0.03, {'PATH': os.environ['PATH']})
            await asyncio.sleep(0.6)
            self.assertFalse(Path(directory, 'late').exists())

class BudgetTest(unittest.IsolatedAsyncioTestCase):
    def args(self, directory):
        return argparse.Namespace(cwd=directory,model='fixture',instructions=None,env_file=None,
            prompt='opaque request',max_turns=40,timeout=300,tool_timeout=30,max_output_tokens=12000)

    async def test_forwarding_and_success_limits(self):
        result=SimpleNamespace(final_output='done',context_wrapper=SimpleNamespace(usage=SimpleNamespace(requests=1,input_tokens=1,output_tokens=1,total_tokens=2)))
        with tempfile.TemporaryDirectory() as directory, patch.object(harness.Runner,'run',AsyncMock(return_value=result)) as run:
            output=io.StringIO()
            with contextlib.redirect_stdout(output): code=await harness.run(self.args(directory))
            self.assertEqual(code,0)
            self.assertEqual(run.call_args.kwargs['max_turns'],40)
            records=[json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(records[0]['limits'],dict(maxTurns=40,timeoutSeconds=300,toolTimeoutSeconds=30,maxOutputTokens=12000))
            self.assertEqual(records[-1]['type'],'final')
            self.assertEqual(records[-1]['usage'], dict(requests=1,input_tokens=1,output_tokens=1,total_tokens=2))
            self.assertEqual(records[-1]['usageObservation']['observedTokenTotals'], {})

    def test_invalid_cli_budgets_fail_before_run(self):
        for flag,value in [('--max-turns','0'),('--max-turns','-1'),('--max-turns','9007199254740992'),('--timeout','nan'),('--timeout','inf'),('--timeout','0')]:
            with patch('sys.argv',['run.py','--model','fixture','--cwd','/tmp','--prompt','opaque',flag,value]), patch.object(harness,'run') as run, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught: harness.main()
                self.assertEqual(caught.exception.code,2)
                run.assert_not_called()

    async def test_safe_error_limits_no_final(self):
        for name in ['MaxTurnsExceeded','TimeoutError','SecretExceptionName']:
            error=type(name,(Exception,),{})('must not appear')
            with tempfile.TemporaryDirectory() as directory, patch.object(harness.Runner,'run',AsyncMock(side_effect=error)):
                output=io.StringIO()
                with contextlib.redirect_stdout(output): code=await harness.run(self.args(directory))
                self.assertEqual(code,1)
                self.assertNotIn('must not appear',output.getvalue())
                records=[json.loads(line) for line in output.getvalue().splitlines()]
                self.assertEqual(records[-1]['error_type'],name if name in ('MaxTurnsExceeded','TimeoutError') else 'ExecutionError')
                self.assertFalse(any(r['type']=='final' for r in records))
                self.assertEqual(records[-1]['limits']['maxTurns'],40)

    async def test_completed_calls_survive_error_exhaustion_and_actual_timeout(self):
        for failure in ('ExecutionError', 'MaxTurnsExceeded', 'timeout'):
            async def runner(agent, prompt, **kwargs):
                hooks = kwargs['hooks']
                self.assertTrue(agent.model_settings.preserve_raw_usage)
                self.assertEqual(agent.instructions, 'You are a helpful coding agent. Use available tools to complete the user request.\nYour working directory is: ' + str(Path(directory).resolve()))
                self.assertEqual([tool.name for tool in agent.tools], ['execute_shell'])
                self.assertEqual(agent.tools[0].params_json_schema['properties'], {'command': {'title': 'Command', 'type': 'string'}})
                for index in range(2):
                    await hooks.on_llm_start(None, agent, 'secret prompt', [])
                    await hooks.on_llm_end(None, agent, SimpleNamespace(response_id=str(index), raw_usage={
                        'input_tokens': 10, 'output_tokens': 3, 'total_tokens': 13,
                        'input_tokens_details': {'cached_tokens': 0},
                        'output_tokens_details': {'reasoning_tokens': 2}, 'secret': 'never log'}))
                await hooks.on_llm_start(None, agent, None, [])
                if failure == 'timeout':
                    await asyncio.sleep(10)
                raise type(failure, (Exception,), {})('secret credential')
            with tempfile.TemporaryDirectory() as directory, patch.object(harness.Runner, 'run', runner):
                args = self.args(directory)
                if failure == 'timeout': args.timeout = .02
                output = io.StringIO()
                with contextlib.redirect_stdout(output): code = await harness.run(args)
                self.assertEqual(code, 1)
                records = [json.loads(line) for line in output.getvalue().splitlines()]
                self.assertEqual([r['type'] for r in records], ['start', 'error'])
                stats = records[-1]['usageObservation']
                self.assertEqual(stats['completedResponseCount'], 2)
                self.assertEqual(stats['outstandingCallCount'], 1)
                self.assertIsNone(stats['outstandingProviderRequestCount'])
                self.assertEqual(stats['observedTokenTotals']['input_tokens'], 20)
                self.assertEqual(stats['fieldResponseCounts']['input_tokens_details.cached_tokens'], 2)
                self.assertFalse(stats['totalRunUsageKnown'])
                self.assertNotIn('secret', output.getvalue())

    async def test_unobserved_error_has_no_invented_zero_tokens(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(harness.Runner, 'run', AsyncMock(side_effect=RuntimeError('secret'))):
            output = io.StringIO()
            with contextlib.redirect_stdout(output): await harness.run(self.args(directory))
            stats = json.loads(output.getvalue().splitlines()[-1])['usageObservation']
            self.assertEqual(stats['observedTokenTotals'], {})
            self.assertEqual(stats['fieldResponseCounts'], {})
            self.assertFalse(stats['totalRunUsageKnown'])

class UsageTest(unittest.IsolatedAsyncioTestCase):
    async def test_per_response_not_context_duplicates_and_missing_fields(self):
        observation = harness.UsageObservation()
        context = SimpleNamespace(usage=SimpleNamespace(input_tokens=999999))
        for raw, identity in [({'input_tokens': 10, 'output_tokens': 0}, 'one'),
                              ({'input_tokens': 10, 'output_tokens': 0}, 'one'),
                              ({'input_tokens': 4, 'total_tokens': None}, 'two'),
                              (None, 'three'),
                              ({'input_tokens': True, 'output_tokens': -1, 'total_tokens': 1.5}, 'four')]:
            await observation.on_llm_end(context, None, SimpleNamespace(response_id=identity, raw_usage=raw))
        stats = observation.summary()
        self.assertEqual(stats['completedResponseCount'], 4)
        self.assertEqual(stats['duplicateResponseCallbackCount'], 1)
        self.assertEqual(stats['observedTokenTotals'], {'input_tokens': 14, 'output_tokens': 0})
        self.assertEqual(stats['fieldResponseCounts'], {'input_tokens': 2, 'output_tokens': 1})
        self.assertEqual(stats['aggregationScope'], 'unique_completed_responses_in_this_runner_run')

    async def test_response_without_provider_identity_deduplicated_by_object(self):
        observation = harness.UsageObservation()
        response = SimpleNamespace(raw_usage={'input_tokens': 3})
        await observation.on_llm_end(None, None, response)
        await observation.on_llm_end(None, None, response)
        self.assertEqual(observation.summary()['observedTokenTotals'], {'input_tokens': 3})

    async def test_malformed_ids_fall_back_without_collisions_or_leaks(self):
        observation = harness.UsageObservation()
        for invalid in ({'secret': 'credential'}, ['secret'], True, False, ''):
            for _ in range(2):
                response = SimpleNamespace(response_id=invalid, request_id=invalid,
                    raw_usage={'input_tokens': 3})
                await observation.on_llm_end(None, None, response)
                await observation.on_llm_end(None, None, response)
        stats = observation.summary()
        self.assertEqual(stats['completedResponseCount'], 10)
        self.assertEqual(stats['duplicateResponseCallbackCount'], 10)
        self.assertEqual(stats['observedTokenTotals'], {'input_tokens': 30})
        self.assertNotIn('secret', json.dumps(stats))
        self.assertNotIn('credential', json.dumps(stats))

    async def test_valid_request_id_used_when_response_id_malformed(self):
        observation = harness.UsageObservation()
        for _ in range(2):
            await observation.on_llm_end(None, None, SimpleNamespace(response_id=[],
                request_id='private-provider-id', raw_usage={'input_tokens': 3}))
        stats = observation.summary()
        self.assertEqual(stats['completedResponseCount'], 1)
        self.assertEqual(stats['duplicateResponseCallbackCount'], 1)
        self.assertNotIn('private-provider-id', json.dumps(stats))

    async def test_raw_preservation_does_not_change_provider_request(self):
        from agents.models.openai_responses import OpenAIResponsesModel
        from agents.models.interface import ModelTracing
        from openai.types.responses import Response
        response = Response.model_validate(dict(id='fixture', object='response', created_at=1,
            model='fixture', output=[], parallel_tool_calls=True, tool_choice='auto', tools=[],
            status='completed', usage=dict(input_tokens=4, output_tokens=2, total_tokens=6,
                input_tokens_details=dict(cached_tokens=0, cache_write_tokens=0), output_tokens_details=dict(reasoning_tokens=1))))
        create = AsyncMock(return_value=response)
        client = SimpleNamespace(responses=SimpleNamespace(create=create), base_url='https://api.openai.com/v1/')
        model = OpenAIResponsesModel('fixture', client)
        @harness.function_tool
        async def execute_shell(command: str) -> str:
            """Execute a bash command in the working directory. Read and edit files using ordinary shell tools."""
            return ''
        calls = []
        for enabled in (False, True):
            result = await model.get_response(system_instructions='opaque unchanged', input='opaque prompt',
                model_settings=harness.ModelSettings(max_tokens=12000, timeout=300, preserve_raw_usage=enabled),
                tools=[execute_shell], output_schema=None, handoffs=[], tracing=ModelTracing.DISABLED)
            calls.append(create.call_args)
            self.assertEqual(result.raw_usage is not None, enabled)
        self.assertEqual(calls[0], calls[1])

if __name__ == '__main__':
    unittest.main()
