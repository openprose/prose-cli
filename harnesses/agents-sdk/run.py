#!/usr/bin/env python3
"""Generic local shell agent. No program-language interpretation is implemented here."""
import argparse
import asyncio
import json
import math
import re
import os
from pathlib import Path
import signal
import sys
import time
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit

from openai import AsyncOpenAI
from agents.models.openai_provider import OpenAIProvider

from agents import Agent, Runner, RunConfig, RunHooks, ModelSettings, ModelRetrySettings, function_tool, WebSearchTool
from dotenv import dotenv_values


class UsageObservation(RunHooks):
    """Allowlisted per-response observations, never normalized context snapshots."""
    fields = ('input_tokens', 'output_tokens', 'total_tokens',
              'input_tokens_details.cached_tokens',
              'input_tokens_details.cache_write_tokens',
              'output_tokens_details.reasoning_tokens')

    def __init__(self, max_requests=None, max_tools=None, max_total_tokens=None):
        self.max_requests = max_requests
        self.max_tools = max_tools
        self.max_total_tokens = max_total_tokens
        self.tool_calls = 0
        self.started = 0
        self.completed = 0
        self.duplicates = 0
        self.seen = set()
        self.responses = []  # Keep object identities alive when provider IDs are absent.
        self.totals = {}
        self.coverage = {}

    async def on_llm_start(self, context, agent, system_prompt, input_items):
        if self.max_requests is not None and self.started >= self.max_requests:
            raise BudgetExceeded()
        if self.max_total_tokens is not None and self.totals.get("total_tokens", 0) >= self.max_total_tokens:
            raise BudgetExceeded()
        self.started += 1

    async def on_llm_end(self, context, agent, response):
        response_id = getattr(response, 'response_id', None)
        request_id = getattr(response, 'request_id', None)
        response_id = response_id if isinstance(response_id, str) and response_id else None
        request_id = request_id if isinstance(request_id, str) and request_id else None
        identity = (('response', response_id) if response_id else
                    ('request', request_id) if request_id else ('object', id(response)))
        if identity in self.seen:
            self.duplicates += 1
            return
        self.seen.add(identity)
        if not response_id and not request_id:
            self.responses.append(response)
        self.completed += 1
        raw = getattr(response, 'raw_usage', None)
        for field in self.fields:
            value = raw
            for part in field.split('.'):
                value = value.get(part) if isinstance(value, dict) else None
            if type(value) is int and value >= 0:
                self.totals[field] = self.totals.get(field, 0) + value
                self.coverage[field] = self.coverage.get(field, 0) + 1

    async def on_tool_start(self, context, agent, tool):
        if self.max_tools is not None and self.tool_calls >= self.max_tools:
            raise BudgetExceeded()
        self.tool_calls += 1

    def summary(self):
        return {
            'source': 'sdk_completed_response_raw_usage',
            'aggregationScope': 'unique_completed_responses_in_parent_and_children',
            'startedCallCount': self.started,
            'completedResponseCount': self.completed,
            'duplicateResponseCallbackCount': self.duplicates,
            'outstandingCallCount': max(0, self.started - self.completed),
            'outstandingProviderRequestCount': None,
            'observedTokenTotals': dict(self.totals),
            'fieldResponseCounts': dict(self.coverage),
            'totalRunUsageKnown': False,
        }


class BudgetExceeded(Exception):
    """An aggregate invocation budget was exhausted."""


async def shell(command, cwd, timeout, env, output_limit=30000):
    process = await asyncio.create_subprocess_exec(
        '/bin/bash', '-c', command, cwd=cwd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    async def capture(stream):
        kept = bytearray()
        total = 0
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                return bytes(kept), total > output_limit
            total += len(chunk)
            kept.extend(chunk[:max(0, output_limit - len(kept))])
    readers = [asyncio.create_task(capture(stream)) for stream in (process.stdout, process.stderr)]
    async def complete():
        captured = await asyncio.gather(*readers)
        await process.wait()
        return captured
    try:
        stdout, stderr = await asyncio.wait_for(complete(), timeout)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        await process.wait()
        raise
    return {'exit_code': process.returncode,
            'stdout': stdout[0].decode(errors='replace'),
            'stderr': stderr[0].decode(errors='replace'),
            'stdout_truncated': stdout[1], 'stderr_truncated': stderr[1]}


async def retrieve_public(url, timeout, output_limit=60000):
    """HTTPS GET with DNS-pinned public addresses, bounded body, no redirects."""
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
            parsed.password or parsed.port not in (None, 443) or len(url) > 4096 or
            any(ord(c) < 33 for c in url)):
        raise ValueError('unsupported public URL')
    host = parsed.hostname.encode('idna').decode('ascii')
    async def exchange():
        addresses = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError('non-public address')
        # Pin the vetted IP; TLS still validates the original hostname.
        reader, writer = await asyncio.open_connection(addresses[0][4][0], 443,
            ssl=ssl.create_default_context(), server_hostname=host, limit=16384)
        try:
            target = parsed.path or '/'
            if parsed.query:
                target += '?' + parsed.query
            authority = '[' + host + ']' if ':' in host else host
            writer.write(('GET ' + target + ' HTTP/1.1\r\nHost: ' + authority +
                '\r\nUser-Agent: prose-agents-sdk/0.1.0\r\nAccept: text/html,text/plain,application/json\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n').encode('ascii'))
            await writer.drain()
            headers = await reader.readuntil(b'\r\n\r\n')
            lines = headers.decode('iso-8859-1').split('\r\n')
            if not re.fullmatch(r'HTTP/1\.[01] [0-9]{3}(?: [^\r\n]*)?', lines[0]):
                raise ValueError('invalid HTTP status')
            status = int(lines[0].split(' ')[1])
            fields = {}
            for line in lines[1:-2]:
                if ':' not in line or line.startswith((' ', '\t')):
                    raise ValueError('invalid HTTP header')
                name, value = line.split(':', 1)
                if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                    raise ValueError('invalid HTTP header name')
                name = name.lower()
                if name in fields and name in ('content-length', 'transfer-encoding', 'content-encoding'):
                    raise ValueError('duplicate HTTP framing header')
                fields[name] = value.strip().lower()
            if status != 200:
                return {'status': status, 'error': 'HTTP response is not 200; redirects are not followed'}
            if fields.get('content-encoding', 'identity') != 'identity':
                return {'status': status, 'error': 'compressed response is unsupported'}
            transfer = fields.get('transfer-encoding')
            length = fields.get('content-length')
            if transfer is not None and (transfer != 'chunked' or length is not None):
                raise ValueError('unsupported or conflicting HTTP framing')
            if length is not None and not re.fullmatch(r'[0-9]+', length):
                raise ValueError('invalid HTTP content length')
            body = bytearray()
            if transfer == 'chunked':
                while len(body) <= output_limit:
                    size_line = await reader.readline()
                    size_text = size_line.split(b';', 1)[0].strip()
                    if not size_line.endswith(b'\r\n') or not re.fullmatch(rb'[0-9A-Fa-f]+', size_text):
                        raise ValueError('invalid HTTP chunk size')
                    size = int(size_text, 16)
                    if size == 0:
                        trailer_bytes = 0
                        while True:
                            trailer = await reader.readline()
                            trailer_bytes += len(trailer)
                            if not trailer.endswith(b'\r\n') or trailer_bytes > 16384:
                                raise ValueError('invalid or oversized HTTP chunk trailer')
                            if trailer == b'\r\n':
                                break
                            if b':' not in trailer or trailer.startswith((b' ', b'\t')):
                                raise ValueError('invalid HTTP chunk trailer')
                        break
                    take = min(size, output_limit + 1 - len(body))
                    body.extend(await reader.readexactly(take))
                    if take < size:
                        break
                    if await reader.readexactly(2) != b'\r\n':
                        raise ValueError('invalid HTTP chunk')
            elif length is not None:
                body.extend(await reader.readexactly(min(int(length), output_limit + 1)))
            else:
                while len(body) <= output_limit:
                    chunk = await reader.read(min(8192, output_limit + 1 - len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
            return {'status': status, 'content': bytes(body[:output_limit]).decode('utf-8', errors='replace'),
                    'truncated': len(body) > output_limit}
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 0.25)
            except (asyncio.TimeoutError, OSError):
                pass
    return await asyncio.wait_for(exchange(), timeout)


class GuardedResponses:
    """Guard the actual SDK request after instruction/history/tool conversion."""
    def __init__(self, resource, max_input_bytes, observed_models, observed_tiers=None):
        self.resource = resource
        self.max_input_bytes = max_input_bytes
        self.observed_models = observed_models
        self.observed_tiers = observed_tiers if observed_tiers is not None else set()

    async def create(self, **kwargs):
        payload = {field: kwargs[field] for field in ('instructions', 'input', 'tools', 'text')
                   if field in kwargs and isinstance(kwargs[field], (str, list, dict))}
        if len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) > self.max_input_bytes:
            raise BudgetExceeded()
        response = await self.resource.create(**kwargs)
        model = getattr(response, 'model', None)
        if isinstance(model, str) and 0 < len(model) <= 128 and all(c.isalnum() or c in '._-/' for c in model):
            self.observed_models.add(model)
        tier = getattr(response, 'service_tier', None)
        if isinstance(tier, str) and tier in ('auto', 'default', 'flex', 'scale', 'priority', 'fast', 'ultrafast'):
            self.observed_tiers.add(tier)
        return response


class GuardedClient:
    """Retain request guards when the SDK uses public client.with_options()."""
    def __init__(self, client, max_input_bytes, observed_models, observed_tiers):
        self.client = client
        self.max_input_bytes = max_input_bytes
        self.observed_models = observed_models
        self.observed_tiers = observed_tiers
        self.responses = GuardedResponses(client.responses, max_input_bytes, observed_models, observed_tiers)

    def __getattr__(self, name):
        return getattr(self.client, name)

    def with_options(self, **kwargs):
        clone = getattr(self.client, 'with_options', None)
        client = clone(**kwargs) if callable(clone) else self.client
        return GuardedClient(client, self.max_input_bytes, self.observed_models, self.observed_tiers)


async def run(args):
    # This filters environment inheritance, not filesystem/network permissions.
    tool_env = {k: v for k, v in os.environ.items()
                if not any(s in k.upper() for s in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD'))}
    for name in ('LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH'):
        original = os.environ.get(name + '_ORIG')
        tool_env.pop(name, None)
        tool_env.pop(name + '_ORIG', None)
        if getattr(sys, "frozen", False) and original:
            tool_env[name] = original
    start = time.monotonic()
    observed_models = set()
    observed_tiers = set()
    def emit(kind, **data):
        data.setdefault('usageObservation', observation.summary())
        data['modelIdentity'] = {'requested': args.model, 'observed': sorted(observed_models),
                                 'serviceTier': {'requested': 'default', 'observed': sorted(observed_tiers)}}
        print(json.dumps({'type': kind, 'event': kind, 'elapsed_seconds': round(time.monotonic()-start, 3), **data}), flush=True)
    max_input_bytes = getattr(args, 'max_input_bytes', 256000)
    max_tools = getattr(args, 'max_tools', 80)
    max_total_tokens = getattr(args, 'max_total_tokens', 500000)
    limits = {'maxTurns': args.max_turns, 'timeoutSeconds': args.timeout,
              'toolTimeoutSeconds': args.tool_timeout, 'maxOutputTokens': args.max_output_tokens,
              'maxAggregateRequests': args.max_turns, 'maxAggregateHostedWebCalls': args.max_turns, 'maxAggregateFunctionTools': max_tools,
              'maxObservedTotalTokens': max_total_tokens, 'maxRequestInputBytes': max_input_bytes, 'maxChildren': 8, 'maxChildDepth': 1}
    observation = UsageObservation(args.max_turns, max_tools, max_total_tokens)
    def fail(kind, message):
        emit('error', error_type=kind, message=message, limits=limits,
             usageObservation=observation.summary())
        return 1
    try:
        key = dotenv_values(args.env_file).get('OPENAI_API_KEY') if args.env_file else os.environ.get('OPENAI_API_KEY')
        if not isinstance(key, str) or not key.strip():
            return fail('SetupError', 'Set OPENAI_API_KEY to an OpenAI API key, or configure the OpenAI API-key credential profile. No model request was sent.')
        cwd = str(Path(args.cwd).resolve(strict=True))
        if not Path(cwd).is_dir():
            return fail('SetupError', 'The working directory must exist.')
        instructions = 'You are a helpful coding agent. Use available tools to complete the user request.'
        if args.instructions:
            with Path(args.instructions).open('rb') as source:
                raw = source.read(max_input_bytes + 1)
            if len(raw) > max_input_bytes:
                return fail('SetupError', 'The instruction file exceeds the configured request input-byte budget.')
            instructions += '\n\n' + raw.decode('utf-8')
        instructions += '\nYour working directory is: ' + cwd
    except (OSError, ValueError):
        return fail('SetupError', 'Cannot read the working directory, instructions, or credential file. Check the supplied paths and permissions.')
    deadline = start + args.timeout
    children = 0
    child_usage = []
    child_lock = asyncio.Lock()
    def remaining():
        return max(.001, deadline - time.monotonic())
    @function_tool(failure_error_function=None)
    async def execute_shell(command: str) -> str:
        """Execute bash in the working directory with host OS permissions; read/edit files. This is not a sandbox."""
        if len(command.encode()) > 30000:
            return json.dumps({'error': 'shell command exceeds 30000 bytes'})
        emit('tool_call', name='execute_shell', command=command)
        try:
            result = await shell(command, cwd, min(args.tool_timeout, remaining()), tool_env)
        except asyncio.TimeoutError:
            result = {'error': 'shell timeout; process group terminated'}
        except OSError:
            result = {'error': 'shell could not start; check working directory and bash availability'}
        emit('tool_result', name='execute_shell', result=result)
        return json.dumps(result)
    @function_tool(failure_error_function=None)
    async def retrieve_url(url: str) -> str:
        """Retrieve text from a public HTTPS URL. Private addresses, credentials, non-443 ports and redirects are rejected."""
        emit('tool_call', name='retrieve_url', url=url[:4096])
        try:
            result = await retrieve_public(url, min(args.tool_timeout, remaining()))
        except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            result = {'error': 'public retrieval failed; check the HTTPS URL, server availability and response format'}
        emit('tool_result', name='retrieve_url', result=result)
        return json.dumps(result)
    client = AsyncOpenAI(api_key=key.strip(), max_retries=0, timeout=args.timeout)
    client = GuardedClient(client, max_input_bytes, observed_models, observed_tiers)
    config = RunConfig(tracing_disabled=True, model_provider=OpenAIProvider(openai_client=client))
    def make_agent(depth):
        @function_tool(failure_error_function=None)
        async def delegate(task: str) -> str:
            """Run an independent fresh agent context. Supply all needed task inputs explicitly; no parent conversation is inherited. Files remain shared."""
            nonlocal children
            if depth >= 1 or children >= 8:
                raise BudgetExceeded()
            if len(task.encode()) > 60000:
                return json.dumps({'error': 'child input exceeds 60000 bytes'})
            # A single lock covers the complete child: sibling provider calls never overlap.
            async with child_lock:
                if children >= 8:
                    raise BudgetExceeded()
                children += 1
                child_id = children
                emit('tool_call', name='delegate', child_id=child_id, depth=depth + 1)
                child = make_agent(depth + 1)
                result = await Runner.run(child, task, max_turns=args.max_turns,
                    run_config=config, hooks=observation)
                child_usage.append(result.context_wrapper.usage)
                output = str(result.final_output)
                if len(output.encode()) > 60000:
                    raise BudgetExceeded()
                emit('tool_result', name='delegate', child_id=child_id, result=output)
                return output
        return Agent(name='Local coding agent', instructions=instructions, model=args.model,
            tools=[execute_shell, retrieve_url, WebSearchTool(search_context_size='low')] + ([delegate] if depth == 0 else []),
            model_settings=ModelSettings(max_tokens=args.max_output_tokens, timeout=args.timeout,
                preserve_raw_usage=True, parallel_tool_calls=False,
                retry=ModelRetrySettings(max_retries=0),
                extra_args={'max_tool_calls': 1, 'service_tier': 'default'}))
    emit('start', model=args.model, cwd=cwd, limits=limits,
         permissions={'shell': 'host_os_permissions', 'filesystemSandbox': False,
                      'networkSandbox': False, 'freshChildConversation': True})
    try:
        result = await asyncio.wait_for(Runner.run(make_agent(0), args.prompt,
            max_turns=args.max_turns, run_config=config, hooks=observation), remaining())
        normalized = [result.context_wrapper.usage] + child_usage
        usage = {field: sum(getattr(value, field) for value in normalized) for field in ('requests', 'input_tokens', 'output_tokens', 'total_tokens')}
        emit('final', output=result.final_output, usage={
            'requests': usage['requests'], 'input_tokens': usage['input_tokens'],
            'output_tokens': usage['output_tokens'], 'total_tokens': usage['total_tokens']},
            usageObservation=observation.summary())
        return 0
    except asyncio.CancelledError:
        return fail('CancelledError', 'Execution cancelled; active tool process groups terminated.')
    except Exception as error:
        name = type(error).__name__
        if name in ('AuthenticationError', 'PermissionDeniedError'):
            return fail('SetupError', 'OpenAI rejected the credential or account permissions. Check the API key and model access; no fallback was selected.')
        if name == 'NotFoundError':
            return fail('SetupError', 'The configured OpenAI model is unavailable to this account. Check the model selection and account access; no fallback was selected.')
        safe = name if name in ('MaxTurnsExceeded', 'TimeoutError', 'BudgetExceeded') else 'ExecutionError'
        return fail(safe, 'Execution stopped. Check configured limits, tool availability and OpenAI account access.')
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version='prose-agents-sdk 0.1.0')
    parser.add_argument('--model', default='gpt-6.1-sol')
    parser.add_argument('--cwd', required=True)
    parser.add_argument('--instructions', help='Opaque text appended to generic instructions')
    parser.add_argument('--prompt', required=True)
    parser.add_argument('--env-file', help='Optional dotenv containing OPENAI_API_KEY; shell never receives it')
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--tool-timeout', type=float, default=30)
    parser.add_argument('--max-turns', type=int, default=20)
    parser.add_argument('--max-output-tokens', type=int, default=12000)
    parser.add_argument('--max-input-bytes', type=int, default=256000)
    parser.add_argument('--max-tools', type=int, default=80)
    parser.add_argument('--max-total-tokens', type=int, default=500000)
    args = parser.parse_args()
    if not 0 < args.max_turns <= 9007199254740991:
        parser.error('--max-turns must be a positive safe integer')
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 9007199254740.991:
        parser.error('--timeout must be positive and finite within the supported range')
    for label, value in (('--tool-timeout', args.tool_timeout),):
        if not math.isfinite(value) or not 0 < value <= 9007199254740.991:
            parser.error(label + ' must be positive and finite within the supported range')
    for label, value in (('--max-input-bytes', args.max_input_bytes), ('--max-output-tokens', args.max_output_tokens), ('--max-tools', args.max_tools), ('--max-total-tokens', args.max_total_tokens)):
        if not 0 < value <= 9007199254740991:
            parser.error(label + ' must be a positive safe integer')
    async def supervised():
        task = asyncio.create_task(run(args))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, task.cancel)
        return await task
    raise SystemExit(asyncio.run(supervised()))

if __name__ == '__main__':
    main()
