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

GATED_DESCENDANT = '(touch ready; read release < effect-gate; touch late) & wait'

@contextlib.contextmanager
def descendant_gate(directory):
    os.mkfifo(Path(directory, 'effect-gate'))
    descriptor = os.open(Path(directory, 'effect-gate'), os.O_RDWR | os.O_NONBLOCK)
    try:
        yield descriptor
    finally:
        os.close(descriptor)

async def descendant_ready(directory):
    async def ready():
        while not Path(directory, 'ready').exists():
            await asyncio.sleep(.001)
    await asyncio.wait_for(ready(), 1)

class ShellTest(unittest.IsolatedAsyncioTestCase):
    async def test_descendant_effect_gate_releases_without_cancellation(self):
        with tempfile.TemporaryDirectory() as directory, descendant_gate(directory) as release:
            task = asyncio.create_task(harness.shell(GATED_DESCENDANT, directory, 2, {'PATH': os.environ['PATH']}))
            try:
                await descendant_ready(directory)
                self.assertFalse(Path(directory, 'late').exists())
                os.write(release, b'go\n')
                result = await asyncio.wait_for(task, 1)
                self.assertEqual(result['exit_code'], 0)
                self.assertTrue(Path(directory, 'late').exists())
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

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
        with tempfile.TemporaryDirectory() as directory, descendant_gate(directory) as release:
            task = asyncio.create_task(harness.shell(GATED_DESCENDANT, directory, 2, {'PATH': os.environ['PATH']}))
            try:
                await descendant_ready(directory)
                self.assertFalse(Path(directory, 'late').exists())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                # This owner is outside the killed group. A surviving ready
                # descendant receives the release rather than relying on a timer.
                os.write(release, b'go\n')
            await asyncio.sleep(.5)
            self.assertFalse(Path(directory, 'late').exists())

class ShellOwnershipTest(unittest.IsolatedAsyncioTestCase):
    class Process:
        pid = 900001  # Every signal call in this class is mocked.
        returncode = None
        def __init__(self):
            self.reading = asyncio.Event()
            self.reaping = asyncio.Event()
            self.reap_release = None
            self.waits = 0
            self.stdout = SimpleNamespace(read=self.read)
            self.stderr = SimpleNamespace(read=self.read)
        async def read(self, size):
            self.reading.set()
            await asyncio.Future()
        async def wait(self):
            self.waits += 1
            self.reaping.set()
            if self.reap_release is not None:
                await self.reap_release.wait()
            self.returncode = -9
            return -9

    async def test_cancel_pending_acquisition_retains_handle_and_reaps(self):
        for cancel_before_start in (False, True):
            with self.subTest(cancel_before_start=cancel_before_start):
                created, release = asyncio.Event(), asyncio.Event()
                process = self.Process()
                async def spawn(*args, **kwargs):
                    created.set()
                    await release.wait()
                    return process
                with patch.object(harness.asyncio, 'create_subprocess_exec', spawn), patch.object(harness.os, 'killpg') as kill:
                    task = asyncio.create_task(harness.shell('private-command', '.', 1, {}))
                    if cancel_before_start:
                        # Start shell so its retained acquisition exists, but
                        # cancel before the acquisition coroutine is scheduled.
                        await asyncio.sleep(0)
                    else:
                        await asyncio.wait_for(created.wait(), 1)
                    task.cancel()
                    await asyncio.wait_for(created.wait(), 1)
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 1)
                    kill.assert_called_once_with(process.pid, harness.signal.SIGKILL)
                    self.assertEqual(process.waits, 1)

    async def test_repeated_cancel_during_reap_does_not_abandon_cleanup(self):
        process = self.Process()
        process.reap_release = asyncio.Event()
        with patch.object(harness.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)), patch.object(harness.os, 'killpg') as kill:
            task = asyncio.create_task(harness.shell('ignored', '.', 1, {}))
            await asyncio.wait_for(process.reading.wait(), 1)
            task.cancel()
            await asyncio.wait_for(process.reaping.wait(), 1)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            process.reap_release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            kill.assert_called_once()
            self.assertEqual(process.waits, 1)

    async def test_repeated_cancel_during_reader_settlement_does_not_skip_reap(self):
        process = self.Process()
        settling, release = asyncio.Event(), asyncio.Event()
        async def read(size):
            process.reading.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                settling.set()
                await release.wait()
                raise
        process.stdout = SimpleNamespace(read=read)
        process.stderr = SimpleNamespace(read=read)
        with patch.object(harness.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)), patch.object(harness.os, 'killpg') as kill:
            task = asyncio.create_task(harness.shell('ignored', '.', 1, {}))
            await asyncio.wait_for(process.reading.wait(), 1)
            task.cancel()
            await asyncio.wait_for(settling.wait(), 1)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            kill.assert_called_once()
            self.assertEqual(process.waits, 1)

    async def test_cancel_before_shell_start_creates_no_process(self):
        with patch.object(harness.asyncio, 'create_subprocess_exec', AsyncMock()) as spawn, patch.object(harness.os, 'killpg') as kill:
            task = asyncio.create_task(harness.shell('ignored', '.', 1, {}))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            spawn.assert_not_called()
            kill.assert_not_called()

    async def test_acquisition_failure_and_late_failure_preserve_primary(self):
        error = FileNotFoundError(2, 'private-path')
        with patch.object(harness.asyncio, 'create_subprocess_exec', AsyncMock(side_effect=error)), patch.object(harness.os, 'killpg') as kill:
            with self.assertRaises(FileNotFoundError) as caught:
                await harness.shell('ignored', '.', 1, {})
            self.assertIs(caught.exception, error)
            kill.assert_not_called()
            self.assertEqual(error._shell_cleanup_failures, [{'phase': 'acquire', 'errorType': 'FileNotFoundError', 'errno': 2}])
        created, release = asyncio.Event(), asyncio.Event()
        async def late(*args, **kwargs):
            created.set()
            await release.wait()
            raise error
        with patch.object(harness.asyncio, 'create_subprocess_exec', late), patch.object(harness.os, 'killpg') as kill:
            task = asyncio.create_task(harness.shell('ignored', '.', 1, {}))
            await asyncio.wait_for(created.wait(), 1)
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with self.assertRaises(asyncio.CancelledError) as caught:
                await asyncio.wait_for(task, 1)
            self.assertEqual(harness._shell_cleanup_details(caught.exception)['shellCleanupFailures'][0]['phase'], 'acquire')
            kill.assert_not_called()

    async def test_kill_failure_preserves_cancel_and_still_settles_and_reaps(self):
        for error in (ProcessLookupError(3, 'private'), PermissionError(1, 'private'), OSError(5, 'private')):
            process = self.Process()
            with self.subTest(error=type(error).__name__), patch.object(harness.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)), patch.object(harness.os, 'killpg', side_effect=error):
                task = asyncio.create_task(harness.shell('ignored', '.', 1, {}))
                await asyncio.wait_for(process.reading.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError) as caught:
                    await asyncio.wait_for(task, 1)
                self.assertEqual(process.waits, 1)
                details = harness._shell_cleanup_details(caught.exception)
                if isinstance(error, ProcessLookupError):
                    self.assertEqual(details, {})
                else:
                    self.assertEqual(details['shellCleanupFailures'], [{'phase': 'kill', 'errorType': type(error).__name__, 'errno': error.errno}])
                    self.assertNotIn('private', json.dumps(details))

    async def test_reader_and_reap_failures_are_bounded_safe_and_primary_survives(self):
        process = self.Process()
        secret = type('SecretCommandAndToken', (Exception,), {})('private-output')
        process.stdout = SimpleNamespace(read=AsyncMock(side_effect=secret))
        process.stderr = SimpleNamespace(read=AsyncMock(side_effect=secret))
        process.wait = AsyncMock(side_effect=OSError(5, 'private-path'))
        with patch.object(harness.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)), patch.object(harness.os, 'killpg') as kill:
            with self.assertRaises(type(secret)) as caught:
                await asyncio.wait_for(harness.shell('private-command', '.', 1, {}), 1)
            self.assertIs(caught.exception, secret)
            kill.assert_called_once()
            process.wait.assert_awaited_once()
            self.assertEqual(secret._shell_cleanup_failures, [
                {'phase': 'readers', 'errorType': 'ExecutionError'},
                {'phase': 'readers', 'errorType': 'ExecutionError'},
                {'phase': 'reap', 'errorType': 'OSError', 'errno': 5}])
            self.assertNotIn('private', json.dumps(harness._shell_cleanup_details(secret)))
            self.assertNotIn('SecretCommandAndToken', json.dumps(harness._shell_cleanup_details(secret)))

    async def test_timeout_preserves_primary_and_cleans_owned_process(self):
        process = self.Process()
        with patch.object(harness.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)), patch.object(harness.os, 'killpg', side_effect=PermissionError(1, 'private')):
            with self.assertRaises(asyncio.TimeoutError) as caught:
                await harness.shell('ignored', '.', .001, {})
            self.assertEqual(process.waits, 1)
            self.assertEqual(caught.exception._shell_cleanup_failures, [{'phase': 'kill', 'errorType': 'PermissionError', 'errno': 1}])

    async def test_empty_and_invalid_output_preserves_result_and_draining(self):
        process = self.Process()
        process.stdout = SimpleNamespace(read=AsyncMock(side_effect=[b'\xffabcd', b'ef', b'']))
        process.stderr = SimpleNamespace(read=AsyncMock(return_value=b''))
        with patch.object(harness.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)), patch.object(harness.os, 'killpg') as kill:
            result = await harness.shell('ignored', '.', 1, {}, output_limit=3)
            self.assertEqual(result['stdout'], '\ufffdab')
            self.assertTrue(result['stdout_truncated'])
            self.assertEqual(result['stderr'], '')
            self.assertFalse(result['stderr_truncated'])
            self.assertEqual(process.stdout.read.await_count, 3)
            kill.assert_not_called()

    def test_cleanup_metadata_is_closed_bounded_and_context_safe(self):
        class Hostile(Exception):
            @property
            def _shell_cleanup_failures(self):
                raise AssertionError('private property must not execute')
            @property
            def __context__(self):
                raise AssertionError('private property must not execute')
            @property
            def __cause__(self):
                raise AssertionError('private property must not execute')
        self.assertEqual(harness._shell_cleanup_details(Hostile()), {})
        for row in ({'phase': 'private-command', 'errorType': 'OSError'},
                    {'phase': 'kill', 'errorType': 'SecretToken'},
                    {'phase': 'kill', 'errorType': 'OSError', 'errno': True},
                    {'phase': 'kill', 'errorType': 'OSError', 'message': 'private'},
                    {'phase': 'kill', 'errorType': 'OSError', 'errno': 1.0}):
            error = RuntimeError('private-message')
            error._shell_cleanup_failures = [row]
            self.assertEqual(harness._shell_cleanup_details(error), {})
        error._shell_cleanup_failures = [{'phase': 'readers', 'errorType': 'ExecutionError'}] * 3
        self.assertEqual(harness._shell_cleanup_details(error), {})
        error.__context__ = error
        self.assertEqual(harness._shell_cleanup_details(error), {})
        error._shell_cleanup_failures = [{'phase': 'kill', 'errorType': 'PermissionError', 'errno': 1}]
        outer = asyncio.CancelledError()
        outer.__context__ = error
        self.assertEqual(harness._shell_cleanup_details(outer), {'shellCleanupFailures': error._shell_cleanup_failures})
        for _ in range(8):
            wrapper = asyncio.CancelledError()
            wrapper.__context__ = outer
            outer = wrapper
        self.assertEqual(harness._shell_cleanup_details(outer), {})

    def test_hostile_chain_truth_and_metadata_key_methods_are_not_executed(self):
        class HostileCause(Exception):
            def __bool__(self):
                raise AssertionError('exception truth must not execute')
        cause = HostileCause()
        cause._shell_cleanup_failures = [{'phase': 'kill', 'errorType': 'PermissionError', 'errno': 1}]
        outer = asyncio.CancelledError()
        outer.__cause__ = cause
        self.assertEqual(harness._shell_cleanup_details(outer), {'shellCleanupFailures': cause._shell_cleanup_failures})
        class HostileKey:
            armed = False
            calls = 0
            def __hash__(self):
                if self.armed:
                    self.calls += 1
                    raise AssertionError('metadata key hash must not execute')
                return hash('phase')
            def __eq__(self, other):
                if self.armed:
                    self.calls += 1
                    raise AssertionError('metadata key equality must not execute')
                return False
        key = HostileKey()
        row = {key: 'private', 'errorType': 'OSError'}
        key.armed = True
        error = RuntimeError()
        error._shell_cleanup_failures = [row]
        self.assertEqual(harness._shell_cleanup_details(error), {})
        self.assertEqual(key.calls, 0)

    async def test_wait_for_cancel_wrapper_preserves_safe_cleanup_context(self):
        primary = asyncio.CancelledError('private-command')
        primary._shell_cleanup_failures = [{'phase': 'kill', 'errorType': 'PermissionError', 'errno': 1}]
        async def cancelled():
            raise primary
        with self.assertRaises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(cancelled(), 1)
        self.assertEqual(harness._shell_cleanup_details(caught.exception), {'shellCleanupFailures': primary._shell_cleanup_failures})
        self.assertNotIn('private', json.dumps(harness._shell_cleanup_details(caught.exception)))

class OwnedOperationsTest(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_joins_effects_despite_repeated_cancellation(self):
        operations = harness.OwnedOperations(80)
        entered, cleaning, release = (asyncio.Event() for _ in range(3))
        async def effect():
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cleaning.set()
                await release.wait()
                raise
        invocation = asyncio.create_task(operations.perform(effect))
        await asyncio.wait_for(entered.wait(), 1)
        primary = RuntimeError('private-primary')
        closing = asyncio.create_task(operations.shutdown(primary))
        await asyncio.wait_for(cleaning.wait(), 1)
        closing.cancel()
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        release.set()
        await asyncio.wait_for(closing, 1)
        await asyncio.gather(invocation, return_exceptions=True)
        self.assertTrue(all(task.done() for task in operations.tasks))
        await operations.shutdown(primary)

    async def test_first_cancel_during_normal_shutdown_is_preserved(self):
        operations = harness.OwnedOperations(1)
        entered, cleaning, release = (asyncio.Event() for _ in range(3))
        async def effect():
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cleaning.set()
                await release.wait()
                raise
        invocation = asyncio.create_task(operations.perform(effect))
        await asyncio.wait_for(entered.wait(), 1)
        closing = asyncio.create_task(operations.shutdown())
        await asyncio.wait_for(cleaning.wait(), 1)
        closing.cancel()
        closing.cancel()
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(closing, 1)
        await asyncio.gather(invocation, return_exceptions=True)
        self.assertTrue(all(task.done() for task in operations.tasks))

    async def test_registered_factory_cannot_start_after_shutdown_closes(self):
        operations = harness.OwnedOperations(1)
        started = asyncio.Event()
        async def effect():
            started.set()
        factory = AsyncMock(side_effect=effect)
        invocation = asyncio.create_task(operations.perform(factory))
        await asyncio.sleep(0)
        self.assertEqual(len(operations.tasks), 1)
        factory.assert_not_called()
        await operations.shutdown(RuntimeError('private-primary'))
        outcomes = await asyncio.gather(invocation, return_exceptions=True)
        self.assertIsInstance(outcomes[0], asyncio.CancelledError)
        factory.assert_not_called()
        self.assertFalse(started.is_set())
        self.assertTrue(all(task.done() for task in operations.tasks))

    async def test_closed_factory_and_provider_clones_never_start(self):
        operations = harness.OwnedOperations(1)
        await operations.shutdown()
        factory = AsyncMock()
        with self.assertRaises(asyncio.CancelledError):
            await operations.perform(factory)
        factory.assert_not_called()
        resource = SimpleNamespace(create=AsyncMock())
        client = SimpleNamespace(responses=resource)
        guarded = harness.GuardedClient(client, 1000, set(), set(), operations.require_open)
        with self.assertRaises(asyncio.CancelledError):
            await guarded.with_options().responses.create(input='fixture')
        resource.create.assert_not_called()

    async def test_single_shell_validation_and_distinct_operation_failures_retained(self):
        operations = harness.OwnedOperations(80)
        rows = [{'phase': 'acquire', 'errorType': 'ExecutionError'},
                {'phase': 'kill', 'errorType': 'PermissionError', 'errno': 1},
                {'phase': 'readers', 'errorType': 'ExecutionError'},
                {'phase': 'readers', 'errorType': 'ExecutionError'},
                {'phase': 'reap', 'errorType': 'OSError', 'errno': 5}]
        async def failure():
            error = OSError(5, 'private-message')
            error._shell_cleanup_failures = rows
            raise error
        for _ in range(80):
            with self.assertRaises(OSError):
                await operations.perform(failure, is_shell=True)
        actual = operations.cleanup_details()['shellCleanupFailures']
        self.assertEqual(actual, rows * 80)
        self.assertEqual(len(actual), 400)
        self.assertLess(len(json.dumps(operations.cleanup_details()).encode()), 1024 * 1024)
        self.assertNotIn('private', json.dumps(operations.cleanup_details()))
        factory = AsyncMock()
        with self.assertRaises(harness.BudgetExceeded):
            await operations.perform(factory, is_shell=True)
        factory.assert_not_called()
        await operations.shutdown()

    async def test_hostile_shell_metadata_not_adopted(self):
        operations = harness.OwnedOperations(1)
        async def failure():
            error = RuntimeError('private-message')
            error._shell_cleanup_failures = [{'phase': 'kill', 'errorType': 'PrivateToken', 'message': 'private'}]
            raise error
        with self.assertRaises(RuntimeError):
            await operations.perform(failure, is_shell=True)
        self.assertEqual(operations.cleanup_details(), {})
        await operations.shutdown()

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
            self.assertEqual(json.loads(output.getvalue().splitlines()[-1])['error_type'], 'SetupError')

    async def test_local_setup_errors_start_before_failure_without_runner_or_client(self):
        with tempfile.TemporaryDirectory() as directory:
            oversized = Path(directory) / 'oversized-instructions'
            oversized.write_bytes(b'x' * 257)
            for instruction in (str(oversized), str(Path(directory) / 'absent-instructions')):
                args = self.args(directory)
                args.instructions = instruction
                args.max_input_bytes = 256
                output = io.StringIO()
                with patch.object(harness.Runner, 'run', AsyncMock()) as runner, patch.object(harness, 'AsyncOpenAI') as client, contextlib.redirect_stdout(output):
                    self.assertEqual(await harness.run(args), 1)
                runner.assert_not_called()
                client.assert_not_called()
                records = [json.loads(line) for line in output.getvalue().splitlines()]
                self.assertEqual([record['type'] for record in records], ['start', 'error'])
                self.assertEqual(records[0]['cwd'], directory)
                self.assertEqual(records[0]['limits'], records[1]['limits'])
                self.assertEqual(records[1]['error_type'], 'SetupError')
                self.assertEqual(records[1]['setup_reason'], 'local-input')
                self.assertEqual(records[1]['usageObservation']['startedCallCount'], 0)

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
            self.assertTrue(all(request['service_tier'] == 'default' for request in requests))
            self.assertEqual(json.loads(output.getvalue().splitlines()[-1])['modelIdentity']['serviceTier'], {'requested': 'default', 'observed': []})
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
        create=AsyncMock(side_effect=[response(1,'delegate',{'task':'explicit child only'}),response(2,'execute_shell',{'command':GATED_DESCENDANT})])
        client=SimpleNamespace(responses=SimpleNamespace(create=create),base_url='https://api.openai.com/v1/',close=AsyncMock())
        with tempfile.TemporaryDirectory() as directory, descendant_gate(directory) as release, patch.object(harness,'AsyncOpenAI',return_value=client):
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                task=asyncio.create_task(harness.run(self.args(directory)))
                try:
                    await descendant_ready(directory)
                    self.assertFalse(Path(directory,'late').exists())
                    task.cancel()
                    self.assertEqual(await asyncio.wait_for(task,1),1)
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    os.write(release, b'go\n')
            await asyncio.sleep(.5)
            self.assertFalse(Path(directory,'late').exists())
            records=[json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(records[-1]['error_type'],'CancelledError')
            self.assertEqual(records[-1]['usageObservation']['completedResponseCount'],2)
            self.assertEqual(records[-1]['usageObservation']['observedTokenTotals']['total_tokens'],10)
            # These records precede cancellation: outer cleanup may close stdout
            # before the terminal error is received, so retain completed usage now.
            shell_call = next(record for record in records if record['type'] == 'tool_call' and record['name'] == 'execute_shell')
            self.assertEqual(shell_call['usageObservation']['completedResponseCount'], 2)
            self.assertEqual(shell_call['usageObservation']['observedTokenTotals']['total_tokens'], 10)
            self.assertFalse(shell_call['usageObservation']['totalRunUsageKnown'])
            self.assertFalse(any(record['type']=='final' for record in records))
            self.assertEqual(create.await_count,2)
            client.close.assert_awaited_once()

    async def test_real_sdk_parent_waits_for_nested_cleanup_before_terminal_and_close(self):
        from openai.types.responses import Response
        def response(number, name, arguments):
            return Response.model_validate(dict(id=str(number), object='response', created_at=1,
                model='fixture', output=[dict(type='function_call', id='call'+str(number),
                call_id='call'+str(number), name=name, arguments=json.dumps(arguments), status='completed')],
                parallel_tool_calls=False, tool_choice='auto', tools=[], status='completed',
                usage=dict(input_tokens=3, output_tokens=2, total_tokens=5,
                input_tokens_details=dict(cached_tokens=0, cache_write_tokens=0),
                output_tokens_details=dict(reasoning_tokens=0))))
        entered, cleaning, release, settled = (asyncio.Event() for _ in range(4))
        async def fake_shell(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError as error:
                cleaning.set()
                await release.wait()
                error._shell_cleanup_failures = [{'phase': 'kill', 'errorType': 'PermissionError', 'errno': 1}]
                settled.set()
                raise
        create = AsyncMock(side_effect=[response(1, 'delegate', {'task': 'explicit child only'}),
                                        response(2, 'execute_shell', {'command': 'fake-only'})])
        client = SimpleNamespace(responses=SimpleNamespace(create=create), base_url='https://api.openai.com/v1/', close=AsyncMock())
        with tempfile.TemporaryDirectory() as directory, patch.object(harness, 'AsyncOpenAI', return_value=client), patch.object(harness, 'shell', fake_shell):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                task = asyncio.create_task(harness.run(self.args(directory)))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    task.cancel()
                    await asyncio.wait_for(cleaning.wait(), 1)
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    self.assertFalse(settled.is_set())
                    self.assertFalse(any(json.loads(line)['type'] in ('error', 'final') for line in output.getvalue().splitlines()))
                    client.close.assert_not_awaited()
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    release.set()
                    self.assertEqual(await asyncio.wait_for(task, 1), 1)
                finally:
                    release.set()
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
            final = json.loads(output.getvalue().splitlines()[-1])
            self.assertEqual(final['error_type'], 'CancelledError')
            self.assertEqual(final['shellCleanupFailures'], [{'phase': 'kill', 'errorType': 'PermissionError', 'errno': 1}])
            self.assertTrue(settled.is_set())
            self.assertEqual(create.await_count, 2)
            client.close.assert_awaited_once()

    async def test_native_normal_error_timeout_and_first_shutdown_cancel_join_work(self):
        for mode in ('normal', 'error', 'timeout', 'first-normal-cancel'):
            with self.subTest(mode=mode):
                entered, cleaning, release, settled = (asyncio.Event() for _ in range(4))
                tool_invocations = []
                captured = {}
                async def fake_shell(*args, **kwargs):
                    entered.set()
                    try:
                        await asyncio.Future()
                    except asyncio.CancelledError:
                        cleaning.set()
                        await release.wait()
                        settled.set()
                        raise
                async def runner(agent, *args, **kwargs):
                    captured['agent'] = agent
                    tool = next(tool for tool in agent.tools if tool.name == 'execute_shell')
                    invocation = asyncio.create_task(tool.on_invoke_tool(ToolContext(context=None, tool_name='execute_shell', tool_call_id='fixture', tool_arguments='{}'), '{"command":"fake"}'))
                    tool_invocations.append(invocation)
                    await entered.wait()
                    if mode == 'error':
                        raise RuntimeError('private-primary')
                    if mode == 'timeout':
                        raise asyncio.TimeoutError('private-primary')
                    return SimpleNamespace(final_output='done', context_wrapper=SimpleNamespace(usage=SimpleNamespace(requests=0,input_tokens=0,output_tokens=0,total_tokens=0)))
                resource = SimpleNamespace(create=AsyncMock())
                client = SimpleNamespace(responses=resource, close=AsyncMock())
                providers = []
                def provider(**kwargs):
                    value = SimpleNamespace(client=kwargs['openai_client'])
                    providers.append(value)
                    return value
                with tempfile.TemporaryDirectory() as directory, patch.object(harness.Runner, 'run', runner), patch.object(harness, 'shell', fake_shell) as shell_mock, patch.object(harness, 'AsyncOpenAI', return_value=client), patch.object(harness, 'OpenAIProvider', provider), patch.object(harness, 'retrieve_public', AsyncMock()) as retrieve:
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        task = asyncio.create_task(harness.run(self.args(directory)))
                        try:
                            await asyncio.wait_for(cleaning.wait(), 1)
                            self.assertFalse(task.done())
                            client.close.assert_not_awaited()
                            self.assertFalse(any(json.loads(line)['type'] in ('error', 'final') for line in output.getvalue().splitlines()))
                            for tool_name, arguments in [('execute_shell', {'command': 'late'}), ('retrieve_url', {'url': 'https://example.invalid'}), ('delegate', {'task': 'late'})]:
                                tool = next(tool for tool in captured['agent'].tools if tool.name == tool_name)
                                with self.assertRaises(asyncio.CancelledError):
                                    await tool.on_invoke_tool(ToolContext(context=None, tool_name=tool_name, tool_call_id='late', tool_arguments='{}'), json.dumps(arguments))
                            with self.assertRaises(asyncio.CancelledError):
                                await providers[0].client.with_options().responses.create(input='late')
                            resource.create.assert_not_called()
                            retrieve.assert_not_called()
                            if mode in ('error', 'first-normal-cancel'):
                                task.cancel()
                                await asyncio.sleep(0)
                                self.assertFalse(task.done())
                            release.set()
                            self.assertEqual(await asyncio.wait_for(task, 1), 1)
                        finally:
                            release.set()
                            if not task.done():
                                task.cancel()
                                await asyncio.gather(task, return_exceptions=True)
                            await asyncio.gather(*tool_invocations, return_exceptions=True)
                    final = json.loads(output.getvalue().splitlines()[-1])
                    self.assertEqual(final['error_type'], {'timeout': 'TimeoutError', 'first-normal-cancel': 'CancelledError'}.get(mode, 'ExecutionError'))
                    self.assertTrue(settled.is_set())
                    self.assertFalse(any(json.loads(line)['type'] == 'final' for line in output.getvalue().splitlines()))
                    self.assertNotIn('private', output.getvalue())
                    client.close.assert_awaited_once()

    async def test_delegate_waiting_for_child_lock_cannot_start_after_closing(self):
        entered = asyncio.Event()
        invocations = []
        child_starts = []
        async def runner(agent, *args, **kwargs):
            if any(tool.name == 'delegate' for tool in agent.tools):
                delegate = next(tool for tool in agent.tools if tool.name == 'delegate')
                for number in range(2):
                    invocations.append(asyncio.create_task(delegate.on_invoke_tool(ToolContext(context=None, tool_name='delegate', tool_call_id=str(number), tool_arguments='{}'), '{"task":"fixture"}')))
                await entered.wait()
                raise RuntimeError('private-primary')
            child_starts.append(agent)
            entered.set()
            await asyncio.Future()
        with tempfile.TemporaryDirectory() as directory, patch.object(harness.Runner, 'run', runner):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(await asyncio.wait_for(harness.run(self.args(directory)), 1), 1)
            outcomes = await asyncio.wait_for(asyncio.gather(*invocations, return_exceptions=True), 1)
            self.assertEqual(len(child_starts), 1)
            self.assertTrue(all(isinstance(value, asyncio.CancelledError) for value in outcomes))
            self.assertEqual(json.loads(output.getvalue().splitlines()[-1])['error_type'], 'ExecutionError')

    async def test_active_retrieval_settles_before_native_cancellation_returns(self):
        entered, cleaning, release, settled = (asyncio.Event() for _ in range(4))
        invocations = []
        async def retrieval(*args):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cleaning.set()
                await release.wait()
                settled.set()
                raise
        async def runner(agent, *args, **kwargs):
            tool = next(tool for tool in agent.tools if tool.name == 'retrieve_url')
            invocations.append(asyncio.create_task(tool.on_invoke_tool(ToolContext(context=None, tool_name='retrieve_url', tool_call_id='fixture', tool_arguments='{}'), '{"url":"https://example.invalid"}')))
            await asyncio.Future()
        with tempfile.TemporaryDirectory() as directory, patch.object(harness.Runner, 'run', runner), patch.object(harness, 'retrieve_public', retrieval):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                task = asyncio.create_task(harness.run(self.args(directory)))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    task.cancel()
                    await asyncio.wait_for(cleaning.wait(), 1)
                    self.assertFalse(task.done())
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    release.set()
                    self.assertEqual(await asyncio.wait_for(task, 1), 1)
                finally:
                    release.set()
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    await asyncio.gather(*invocations, return_exceptions=True)
            self.assertTrue(settled.is_set())
            final = json.loads(output.getvalue().splitlines()[-1])
            self.assertEqual(final['error_type'], 'CancelledError')
            self.assertNotIn('shellCleanupFailures', final)

    async def test_native_aggregate_preserves_identical_distinct_shell_failures_once(self):
        rows = [{'phase': 'reap', 'errorType': 'OSError', 'errno': 5}]
        async def failure(*args, **kwargs):
            error = OSError(5, 'private-path')
            error._shell_cleanup_failures = rows
            raise error
        async def runner(agent, *args, **kwargs):
            tool = next(tool for tool in agent.tools if tool.name == 'execute_shell')
            await asyncio.gather(*(tool.on_invoke_tool(ToolContext(context=None, tool_name='execute_shell', tool_call_id=str(number), tool_arguments='{}'), '{"command":"fixture"}') for number in range(2)))
            return SimpleNamespace(final_output='done', context_wrapper=SimpleNamespace(usage=SimpleNamespace(requests=0,input_tokens=0,output_tokens=0,total_tokens=0)))
        with tempfile.TemporaryDirectory() as directory, patch.object(harness.Runner, 'run', runner), patch.object(harness, 'shell', failure):
            output = io.StringIO()
            args = self.args(directory)
            args.max_tools = 2
            with contextlib.redirect_stdout(output):
                self.assertEqual(await harness.run(args), 1)
            records = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(records[-1]['error_type'], 'ExecutionError')
            self.assertEqual(records[-1]['shellCleanupFailures'], rows * 2)
            results = [row for row in records if row['type'] == 'tool_result']
            self.assertEqual(len(results), 2)
            self.assertTrue(all(row['result']['shellCleanupFailures'] == rows for row in results))
            self.assertNotIn('private', output.getvalue())

    async def test_service_tier_observations_are_allowlisted_and_never_inferred(self):
        observed_models, observed_tiers = set(), set()
        resource = SimpleNamespace(create=AsyncMock())
        guard = harness.GuardedResponses(resource, 1024, observed_models, observed_tiers)
        for invalid in (None, '', 'private-provider-value', {}, [], True):
            resource.create.return_value = SimpleNamespace(model='fixture', service_tier=invalid)
            await guard.create(input=[])
        self.assertEqual(observed_tiers, set())
        for tier in ('auto', 'default', 'flex', 'scale', 'priority', 'fast', 'ultrafast'):
            resource.create.return_value = SimpleNamespace(model='fixture', service_tier=tier)
            await guard.create(input=[])
        self.assertEqual(observed_tiers, {'auto', 'default', 'flex', 'scale', 'priority', 'fast', 'ultrafast'})

    async def test_real_openai_http_transport_pins_standard_tier_and_observes_response(self):
        import httpx2 as httpx
        transmitted = []
        def respond(request):
            transmitted.append(json.loads(request.content))
            return httpx.Response(200, json=dict(id='response', object='response', created_at=1,
                model='fixture-observed', service_tier='default', parallel_tool_calls=False,
                tool_choice='auto', tools=[], status='completed',
                output=[dict(type='message', id='message', role='assistant', status='completed',
                    content=[dict(type='output_text', text='done', annotations=[])])],
                usage=dict(input_tokens=3,output_tokens=2,total_tokens=5,
                    input_tokens_details=dict(cached_tokens=0,cache_write_tokens=0),
                    output_tokens_details=dict(reasoning_tokens=0))))
        client = harness.AsyncOpenAI(api_key='fixture-key', max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        with tempfile.TemporaryDirectory() as directory, patch.object(harness, 'AsyncOpenAI', return_value=client):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(await harness.run(self.args(directory)), 0)
        self.assertEqual(len(transmitted), 1)
        self.assertEqual(transmitted[0]['service_tier'], 'default')
        self.assertEqual(transmitted[0]['max_tool_calls'], 1)
        self.assertEqual(transmitted[0]['max_output_tokens'], 12000)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(records[0]['modelIdentity']['serviceTier'], {'requested': 'default', 'observed': []})
        self.assertEqual(records[-1]['modelIdentity']['serviceTier'], {'requested': 'default', 'observed': ['default']})
        self.assertEqual(records[-1]['modelIdentity']['observed'], ['fixture-observed'])

    async def test_real_openai_sdk_client_clone_cannot_bypass_input_guard(self):
        import httpx2 as httpx
        transmitted = []
        def respond(request):
            transmitted.append(request)
            raise AssertionError('oversized request reached transport')
        client = harness.AsyncOpenAI(api_key='fixture-key', max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        with tempfile.TemporaryDirectory() as directory, patch.object(harness, 'AsyncOpenAI', return_value=client):
            args = self.args(directory)
            args.max_input_bytes = 80
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(await harness.run(args), 1)
        self.assertEqual(transmitted, [])
        final = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(final['error_type'], 'BudgetExceeded')
        self.assertEqual(final['modelIdentity']['observed'], [])
        self.assertEqual(final['modelIdentity']['serviceTier']['observed'], [])

    async def test_real_openai_http_409_is_not_retried_or_assumed_standard(self):
        import httpx2 as httpx
        transmitted = []
        def respond(request):
            transmitted.append(json.loads(request.content))
            return httpx.Response(409, json={'error': {'message': 'private-provider-body',
                'code': 'conversation_locked', 'type': 'invalid_request_error'}})
        client = harness.AsyncOpenAI(api_key='fixture-key', max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        with tempfile.TemporaryDirectory() as directory, patch.object(harness, 'AsyncOpenAI', return_value=client):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(await harness.run(self.args(directory)), 1)
        self.assertEqual(len(transmitted), 1)
        self.assertEqual(transmitted[0]['service_tier'], 'default')
        final = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(final['modelIdentity']['serviceTier'], {'requested': 'default', 'observed': []})
        self.assertNotIn('private-provider-body', output.getvalue())

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

    async def test_shell_cleanup_failures_visible_in_native_errors_without_secrets(self):
        for error in (asyncio.CancelledError('private-token'), RuntimeError('private-command')):
            error._shell_cleanup_failures = [harness._shell_cleanup_failure('kill', PermissionError(1, 'private-path'))]
            with tempfile.TemporaryDirectory() as directory, patch.object(harness.Runner, 'run', AsyncMock(side_effect=error)):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(await harness.run(self.args(directory)), 1)
                record = json.loads(output.getvalue().splitlines()[-1])
                self.assertEqual(record['shellCleanupFailures'], [{'phase': 'kill', 'errorType': 'PermissionError', 'errno': 1}])
                self.assertEqual(record['error_type'], 'CancelledError' if isinstance(error, asyncio.CancelledError) else 'ExecutionError')
                if isinstance(error, asyncio.CancelledError):
                    self.assertEqual(record['message'], 'Execution cancelled.')
                self.assertNotIn('private', output.getvalue())
                self.assertNotIn('groups terminated', output.getvalue())

    async def test_shell_cleanup_failures_visible_in_existing_tool_results(self):
        for error in (asyncio.TimeoutError('private-command'), OSError(5, 'private-path')):
            error._shell_cleanup_failures = [harness._shell_cleanup_failure('reap', RuntimeError('private-token'))]
            async def invoke(agent, *args, **kwargs):
                tool = next(tool for tool in agent.tools if tool.name == 'execute_shell')
                raw = await tool.on_invoke_tool(ToolContext(context=None, tool_name='execute_shell', tool_call_id='fixture', tool_arguments='{}'), '{"command":"fixture"}')
                self.assertEqual(json.loads(raw)['shellCleanupFailures'], [{'phase': 'reap', 'errorType': 'RuntimeError'}])
                return SimpleNamespace(final_output='done', context_wrapper=SimpleNamespace(usage=SimpleNamespace(requests=0,input_tokens=0,output_tokens=0,total_tokens=0)))
            with tempfile.TemporaryDirectory() as directory, patch.object(harness.Runner, 'run', invoke), patch.object(harness, 'shell', AsyncMock(side_effect=error)):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(await harness.run(self.args(directory)), 1)
                records = [json.loads(line) for line in output.getvalue().splitlines()]
                result = next(row['result'] for row in records if row['type'] == 'tool_result')
                self.assertEqual(result['shellCleanupFailures'], [{'phase': 'reap', 'errorType': 'RuntimeError'}])
                if isinstance(error, asyncio.TimeoutError):
                    self.assertEqual(result['error'], 'shell command timed out')
                self.assertEqual(records[-1]['error_type'], 'ExecutionError')
                self.assertEqual(records[-1]['shellCleanupFailures'], result['shellCleanupFailures'])
                self.assertNotIn('private', output.getvalue())
                self.assertNotIn('process group terminated', output.getvalue())

    async def test_auth_and_model_failures_do_not_leak_bodies(self):
        for name in ('AuthenticationError', 'PermissionDeniedError', 'NotFoundError'):
            with tempfile.TemporaryDirectory() as directory, patch.object(harness.Runner, 'run', AsyncMock(side_effect=type(name, (Exception,), {})('credential-value'))):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(await harness.run(self.args(directory)), 1)
                self.assertNotIn('credential-value', output.getvalue())
                self.assertEqual(json.loads(output.getvalue().splitlines()[-1])['error_type'], 'SetupError')
                self.assertEqual(json.loads(output.getvalue().splitlines()[-1])['setup_reason'], 'model-unavailable' if name == 'NotFoundError' else 'credential-or-permission')


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
