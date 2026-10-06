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
from agents.tool_context import ToolContext

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

    async def test_large_output_is_bounded_while_pipe_drains(self):
        with tempfile.TemporaryDirectory() as directory:
            result = await harness.shell("head -c 2000000 /dev/zero", directory, 2, {'PATH': os.environ['PATH']}, output_limit=4096)
            self.assertEqual(len(result['stdout']), 4096)
            self.assertTrue(result['stdout_truncated'])
            self.assertEqual(result['exit_code'], 0)

    async def test_cancellation_stops_delayed_descendant_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            task = asyncio.create_task(harness.shell('(sleep 0.4; touch late) & wait', directory, 2, {'PATH': os.environ['PATH']}))
            await asyncio.sleep(.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(.5)
            self.assertFalse(Path(directory, 'late').exists())

class BudgetTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'OPENAI_API_KEY': 'fixture-no-provider-access'})
        self.env.start()
        self.addCleanup(self.env.stop)

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
            self.assertEqual(records[0]['limits']['maxAggregateRequests'],40)
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
                self.assertEqual([tool.name for tool in agent.tools], ['execute_shell', 'retrieve_url', 'web_search', 'delegate'])
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

    async def test_missing_key_fails_before_runner(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True), patch.object(harness.Runner, 'run', AsyncMock()) as runner:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(await harness.run(self.args(directory)), 1)
            runner.assert_not_called()
            self.assertIn('OPENAI_API_KEY', output.getvalue())
            self.assertEqual(json.loads(output.getvalue())['error_type'], 'SetupError')

    async def test_children_are_fresh_and_aggregate_usage(self):
        calls = []
        result = SimpleNamespace(final_output='done', context_wrapper=SimpleNamespace(usage=SimpleNamespace(requests=1,input_tokens=3,output_tokens=2,total_tokens=5)))
        async def runner(agent, prompt, **kwargs):
            calls.append((agent, prompt, kwargs))
            hooks = kwargs['hooks']
            await hooks.on_llm_start(None, agent, None, [])
            await hooks.on_llm_end(None, agent, SimpleNamespace(response_id=str(len(calls)), raw_usage={'input_tokens':3, 'output_tokens':2, 'total_tokens':5}))
            if len(calls) == 1:
                delegate = next(tool for tool in agent.tools if tool.name == 'delegate')
                await delegate.on_invoke_tool(ToolContext(context=None, tool_name='delegate', tool_call_id='fixture', tool_arguments='{}'), '{"task":"explicit child input"}')
            return result
        with tempfile.TemporaryDirectory() as directory, patch.object(harness.Runner, 'run', runner):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(await harness.run(self.args(directory)), 0)
            self.assertEqual(calls[1][1], 'explicit child input')
            self.assertIsNot(calls[0][0], calls[1][0])
            self.assertEqual(calls[0][0].instructions, calls[1][0].instructions)
            self.assertNotIn('delegate', [tool.name for tool in calls[1][0].tools])
            self.assertIs(calls[0][2]['hooks'], calls[1][2]['hooks'])
            final = json.loads(output.getvalue().splitlines()[-1])
            self.assertEqual(final['usage']['requests'], 2)
            self.assertEqual(final['usage']['total_tokens'], 10)

    async def test_real_sdk_loop_delegates_without_parent_history_or_retries(self):
        from openai.types.responses import Response
        def response(index, output):
            return Response.model_validate(dict(id=str(index), object='response', created_at=1,
                model='fixture', output=output, parallel_tool_calls=False, tool_choice='auto', tools=[],
                status='completed', usage=dict(input_tokens=3, output_tokens=2, total_tokens=5,
                    input_tokens_details=dict(cached_tokens=0,cache_write_tokens=0), output_tokens_details=dict(reasoning_tokens=0))))
        def text(value):
            return dict(type='message', id=value, role='assistant', status='completed', content=[dict(type='output_text',text=value,annotations=[])])
        create = AsyncMock(side_effect=[response(1,[dict(type='function_call',id='call',call_id='child',name='delegate',arguments='{"task":"isolated input"}',status='completed')]), response(2,[text('child result')]), response(3,[text('parent result')])])
        client = SimpleNamespace(responses=SimpleNamespace(create=create), base_url='https://api.openai.com/v1/', close=AsyncMock())
        with tempfile.TemporaryDirectory() as directory, patch.object(harness, 'AsyncOpenAI', return_value=client) as factory:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(await harness.run(self.args(directory)), 0)
            self.assertEqual(factory.call_args.kwargs['max_retries'], 0)
            requests = [call.kwargs for call in create.call_args_list]
            child_input = json.dumps(requests[1]['input'])
            self.assertIn('isolated input', child_input)
            self.assertNotIn('opaque request', child_input)
            self.assertNotIn('parent result', child_input)
            self.assertTrue(all(request['max_tool_calls'] == 1 for request in requests))
            final = json.loads(output.getvalue().splitlines()[-1])
            self.assertEqual(final['usage']['requests'], 3)
            self.assertEqual(final['usage']['total_tokens'], 15)
            self.assertEqual(final['usageObservation']['completedResponseCount'], 3)

    async def test_real_sdk_conversation_lock_error_is_not_retried(self):
        import httpx2 as httpx
        from openai import BadRequestError
        error = BadRequestError('private-provider-message', response=httpx.Response(400,
            request=httpx.Request('POST', 'https://example.invalid/v1/responses')),
            body={'code': 'conversation_locked', 'message': 'private-provider-message'})
        create = AsyncMock(side_effect=error)
        client = SimpleNamespace(responses=SimpleNamespace(create=create), base_url='https://api.openai.com/v1/', close=AsyncMock())
        with tempfile.TemporaryDirectory() as directory, patch.object(harness, 'AsyncOpenAI', return_value=client):
            output = io.StringIO()
            args = self.args(directory)
            args.max_turns = 1
            with contextlib.redirect_stdout(output):
                self.assertEqual(await harness.run(args), 1)
            self.assertEqual(create.await_count, 1)
            self.assertEqual(json.loads(output.getvalue().splitlines()[-1])['usageObservation']['startedCallCount'], 1)
            self.assertNotIn('private-provider-message', output.getvalue())
            client.close.assert_awaited_once()

    async def test_real_sdk_active_child_shell_cancel_preserves_usage_and_kills_effect(self):
        from openai.types.responses import Response
        def response(number,name,arguments):
            return Response.model_validate(dict(id=str(number),object='response',created_at=1,
                model='fixture',output=[dict(type='function_call',id='call'+str(number),call_id='call'+str(number),name=name,arguments=json.dumps(arguments),status='completed')],parallel_tool_calls=False,tool_choice='auto',tools=[],status='completed',
                usage=dict(input_tokens=3,output_tokens=2,total_tokens=5,input_tokens_details=dict(cached_tokens=0,cache_write_tokens=0),output_tokens_details=dict(reasoning_tokens=0))))
        create=AsyncMock(side_effect=[response(1,'delegate',{'task':'explicit child only'}),response(2,'execute_shell',{'command':'(sleep 0.4; touch late) & wait'})])
        client=SimpleNamespace(responses=SimpleNamespace(create=create),base_url='https://api.openai.com/v1/',close=AsyncMock())
        started=asyncio.Event()
        actual_shell=harness.shell
        async def tracked_shell(*args,**kwargs):
            # Set after process creation has had a chance to settle; cancellation remains in actual shell.
            task=asyncio.create_task(actual_shell(*args,**kwargs))
            await asyncio.sleep(.03)
            started.set()
            return await task
        with tempfile.TemporaryDirectory() as directory, patch.object(harness,'AsyncOpenAI',return_value=client), patch.object(harness,'shell',tracked_shell):
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                task=asyncio.create_task(harness.run(self.args(directory)))
                await asyncio.wait_for(started.wait(),1)
                task.cancel()
                self.assertEqual(await asyncio.wait_for(task,1),1)
            await asyncio.sleep(.5)
            self.assertFalse(Path(directory,'late').exists())
            records=[json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(records[-1]['error_type'],'CancelledError')
            self.assertEqual(records[-1]['usageObservation']['completedResponseCount'],2)
            self.assertEqual(records[-1]['usageObservation']['observedTokenTotals']['total_tokens'],10)
            self.assertFalse(any(record['type']=='final' for record in records))
            self.assertEqual(create.await_count,2)
            client.close.assert_awaited_once()

    async def test_actual_request_input_guard_includes_tools_and_history(self):
        resource = SimpleNamespace(create=AsyncMock())
        observed = set()
        guard = harness.GuardedResponses(resource, 80, observed)
        for field in ('instructions', 'input', 'tools'):
            with self.assertRaises(harness.BudgetExceeded):
                await guard.create(**{field: 'a' * 100})
        resource.create.assert_not_called()
        resource.create.return_value = SimpleNamespace(model='gpt-fixture')
        await guard.create(instructions='opaque', input=[], tools=[])
        self.assertEqual(observed, {'gpt-fixture'})

    async def test_aggregate_request_and_tool_budgets(self):
        observation = harness.UsageObservation(max_requests=1, max_tools=1)
        await observation.on_llm_start(None, None, None, [])
        with self.assertRaises(harness.BudgetExceeded):
            await observation.on_llm_start(None, None, None, [])
        await observation.on_tool_start(None, None, None)
        with self.assertRaises(harness.BudgetExceeded):
            await observation.on_tool_start(None, None, None)
        self.assertEqual(observation.started, 1)
        self.assertEqual(observation.tool_calls, 1)

    async def test_observed_token_limit_blocks_next_request(self):
        observation = harness.UsageObservation(max_total_tokens=5)
        await observation.on_llm_end(None, None, SimpleNamespace(response_id='one', raw_usage={'total_tokens': 5}))
        with self.assertRaises(harness.BudgetExceeded):
            await observation.on_llm_start(None, None, None, [])

    async def test_public_retrieval_rejects_private_and_unsafe_urls(self):
        for url in ('http://example.com', 'https://user:pass@example.com', 'https://example.com:444/', 'https://example.com/\r\nInjected'):
            with self.assertRaises(ValueError):
                await harness.retrieve_public(url, .1)
        with patch.object(asyncio.get_running_loop(), 'getaddrinfo', AsyncMock(return_value=[(None,None,None,None,('127.0.0.1',443))])), patch.object(asyncio, 'open_connection', AsyncMock()) as connect:
            with self.assertRaises(ValueError):
                await harness.retrieve_public('https://example.com', .1)
            connect.assert_not_called()

    async def test_cancelled_parent_has_safe_terminal_error(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(harness.Runner, 'run', AsyncMock(side_effect=asyncio.CancelledError())):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(await harness.run(self.args(directory)), 1)
            self.assertEqual(json.loads(output.getvalue().splitlines()[-1])['error_type'], 'CancelledError')

    async def test_auth_and_model_failures_do_not_leak_bodies(self):
        for name in ('AuthenticationError', 'PermissionDeniedError', 'NotFoundError'):
            with tempfile.TemporaryDirectory() as directory, patch.object(harness.Runner, 'run', AsyncMock(side_effect=type(name, (Exception,), {})('credential-value'))):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(await harness.run(self.args(directory)), 1)
                self.assertNotIn('credential-value', output.getvalue())
                self.assertEqual(json.loads(output.getvalue().splitlines()[-1])['error_type'], 'SetupError')


class RetrievalTest(unittest.IsolatedAsyncioTestCase):
    def connection(self, payload=None):
        reader = asyncio.StreamReader(limit=16384)
        if payload is not None:
            reader.feed_data(payload)
            reader.feed_eof()
        writer = SimpleNamespace(write=unittest.mock.Mock(), drain=AsyncMock(),
            close=unittest.mock.Mock(), wait_closed=AsyncMock())
        return reader, writer

    async def request(self, payload, output_limit=60000):
        reader, writer = self.connection(payload)
        lookup = AsyncMock(return_value=[(None,None,None,None,('93.184.216.34',443))])
        with patch.object(asyncio.get_running_loop(), 'getaddrinfo', lookup), patch.object(asyncio, 'open_connection', AsyncMock(return_value=(reader, writer))) as connect:
            result = await harness.retrieve_public('https://example.com/path?q=fixture', .2, output_limit)
        self.assertEqual(connect.call_args.args, ('93.184.216.34',443))
        self.assertEqual(connect.call_args.kwargs['server_hostname'], 'example.com')
        self.assertTrue(connect.call_args.kwargs['ssl'].check_hostname)
        self.assertEqual(connect.call_args.kwargs['ssl'].verify_mode, __import__('ssl').CERT_REQUIRED)
        self.assertIn(b'GET /path?q=fixture HTTP/1.1\r\nHost: example.com\r\n', writer.write.call_args.args[0])
        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()
        return result

    async def test_successful_content_length_chunked_and_eof_framing(self):
        for payload in (b'HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\nfixture',
                        b'HTTP/1.0 200 OK\r\n\r\nfixture',
                        b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nfix\r\n4;ext=1\r\nture\r\n0\r\nX-Test: yes\r\n\r\n'):
            with self.subTest(payload=payload):
                self.assertEqual(await self.request(payload), {'status':200,'content':'fixture','truncated':False})

    async def test_truncation_is_explicit_for_each_framing(self):
        for payload in (b'HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\nfixture',
                        b'HTTP/1.0 200 OK\r\n\r\nfixture',
                        b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n7\r\nfixture\r\n0\r\n\r\n'):
            with self.subTest(payload=payload):
                self.assertEqual(await self.request(payload,3), {'status':200,'content':'fix','truncated':True})

    async def test_incomplete_and_malformed_framing_cannot_claim_complete_content(self):
        for payload in (b'HTTP/1.1 200 OK\r\nContent-Length: 8\r\n\r\nshort',
                        b'HTTP/1.1 200 OK\r\nContent-Length: -1\r\n\r\n',
                        b'HTTP/1.1 200 OK\r\nContent-Length: 1\r\nContent-Length: 1\r\n\r\nx',
                        b'HTTP/1.1 200 OK\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\nx',
                        b'HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip\r\n\r\nx',
                        b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n7\r\nshort',
                        b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1\r\nxNO',
                        b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nz\r\n',
                        b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n',
                        b'garbage\r\n\r\n', b'HTTP/1.1 200 OK\r\nmalformed\r\n\r\n'):
            with self.subTest(payload=payload):
                with self.assertRaises((ValueError, asyncio.IncompleteReadError)):
                    await self.request(payload)

    async def test_redirects_and_compressed_content_do_not_return_source_content(self):
        for payload in (b'HTTP/1.1 302 Found\r\nLocation: https://other.example\r\n\r\n',
                        b'HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n\r\n'):
            result = await self.request(payload)
            self.assertIn('error', result)
            self.assertNotIn('content', result)

    async def test_mixed_private_dns_rejected_without_connection(self):
        addresses = [(None,None,None,None,('93.184.216.34',443)),(None,None,None,None,('192.168.1.1',443))]
        with patch.object(asyncio.get_running_loop(),'getaddrinfo',AsyncMock(return_value=addresses)), patch.object(asyncio,'open_connection',AsyncMock()) as connect:
            with self.assertRaises(ValueError):
                await harness.retrieve_public('https://example.com',.2)
            connect.assert_not_called()

    async def test_timeout_and_cancellation_close_established_connection(self):
        for cancel in (False, True):
            reader, writer = self.connection()
            lookup = AsyncMock(return_value=[(None,None,None,None,('93.184.216.34',443))])
            with self.subTest(cancel=cancel), patch.object(asyncio.get_running_loop(),'getaddrinfo',lookup), patch.object(asyncio,'open_connection',AsyncMock(return_value=(reader,writer))):
                task = asyncio.create_task(harness.retrieve_public('https://example.com',.05 if not cancel else 2))
                if cancel:
                    await asyncio.sleep(.01)
                    task.cancel()
                with self.assertRaises(asyncio.CancelledError if cancel else asyncio.TimeoutError):
                    await task
                writer.close.assert_called_once()
                writer.wait_closed.assert_awaited_once()

    async def test_malformed_response_is_a_safe_tool_error_with_real_sdk_loop(self):
        from openai.types.responses import Response
        def response(number,output):
            return Response.model_validate(dict(id=str(number),object='response',created_at=1,
                model='fixture',output=output,parallel_tool_calls=False,tool_choice='auto',tools=[],status='completed',
                usage=dict(input_tokens=3,output_tokens=2,total_tokens=5,input_tokens_details=dict(cached_tokens=0,cache_write_tokens=0),output_tokens_details=dict(reasoning_tokens=0))))
        create = AsyncMock(side_effect=[response(1,[dict(type='function_call',id='call',call_id='retrieve',name='retrieve_url',arguments='{"url":"https://example.com"}',status='completed')]),response(2,[dict(type='message',id='final',role='assistant',status='completed',content=[dict(type='output_text',text='reported safe retrieval error',annotations=[])])])])
        client = SimpleNamespace(responses=SimpleNamespace(create=create),base_url='https://api.openai.com/v1/',close=AsyncMock())
        reader, writer = self.connection(b'bad-status\r\n\r\n')
        args = argparse.Namespace(cwd=None,model='fixture',instructions=None,env_file=None,prompt='opaque request',max_turns=3,timeout=2,tool_timeout=.2,max_output_tokens=100)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{'OPENAI_API_KEY':'fixture-no-provider-access'}), patch.object(harness,'AsyncOpenAI',return_value=client), patch.object(asyncio.get_running_loop(),'getaddrinfo',AsyncMock(return_value=[(None,None,None,None,('93.184.216.34',443))])), patch.object(asyncio,'open_connection',AsyncMock(return_value=(reader,writer))):
            args.cwd=directory
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(await harness.run(args),0)
            records=[json.loads(line) for line in output.getvalue().splitlines()]
            tool_result=next(record for record in records if record['type']=='tool_result')
            self.assertIn('error',tool_result['result'])
            self.assertNotIn('bad-status',output.getvalue())


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
        self.assertEqual(stats['aggregationScope'], 'unique_completed_responses_in_parent_and_children')

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
