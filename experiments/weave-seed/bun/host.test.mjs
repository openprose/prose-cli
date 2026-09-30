import assert from 'node:assert/strict';
import { spyOn } from 'bun:test';
import * as fs from 'node:fs';
import { mkdtempSync, rmSync, readFileSync, writeFileSync, existsSync, mkdirSync, rmdirSync, readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { FileHost, encodeCheckpoint, decodeCheckpoint } from './host.mjs';
import { emptyCheckpoint } from './index.mjs';

const callbacks = (overrides = {}) => ({
  observe: () => ({ identity: 'a', payload: 'a', observedAt: 0, validUntil: 100, gap: false }),
  assess: () => 'work-needed', act: () => {}, clock: () => 10, newId: () => 'attempt-1', ...overrides,
});

// Child modes run in separate Bun processes; the holder is killed while inside act.
if (process.argv[2] === '--hold') {
  const directory = process.argv[3];
  new FileHost(directory).step('v1', callbacks({ act: () => {
    writeFileSync(join(directory, 'entered'), 'pending persisted');
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 60_000);
  } }));
  process.exit(0);
}
if (process.argv[2] === '--contend') {
  try { new FileHost(process.argv[3]).step('v1', callbacks()); process.exit(5); }
  catch (error) { if (!/busy/.test(error.message)) throw error; process.exit(0); }
}

const root = mkdtempSync(join(tmpdir(), 'weave-bun-host-'));
let checks = 0;
function check(name, fn) { fn(); checks++; console.log(`PASS ${name}`); }
const directory = name => join(root, name);
try {
  check('shared schema fixture and encoding roundtrip', () => {
    const fixture = readFileSync(new URL('../fixtures/checkpoint-v1.json', import.meta.url));
    const checkpoint = decodeCheckpoint(fixture);
    assert.equal(checkpoint.pending, 'attempt-1');
    assert.deepEqual(decodeCheckpoint(encodeCheckpoint(checkpoint)), checkpoint);
  });
  check('large escaped strings roundtrip and escaped duplicate keys fail closed', () => {
    const checkpoint = { ...emptyCheckpoint(), binding: '\\"'.repeat(65_536) };
    const encoded = encodeCheckpoint(checkpoint);
    assert.deepEqual(decodeCheckpoint(encoded), checkpoint);
    const duplicate = encoded.replace('"binding":', '"\\u0062inding":"duplicate","binding":');
    assert.throws(() => decodeCheckpoint(duplicate), /duplicate checkpoint key/);
    const quotedNumbers = { ...emptyCheckpoint(), evidence: '"schema":1.0, "attempts":0e0' };
    assert.deepEqual(decodeCheckpoint(encodeCheckpoint(quotedNumbers)), quotedNumbers);
  });
  check('missing checkpoint, durable save, reopened reuse, and escaped session rejection', () => {
    const host = new FileHost(directory('save'));
    let escaped;
    host.withLock(session => { assert.deepEqual(session.load(), emptyCheckpoint()); escaped = session; });
    assert.throws(() => escaped.load(), /no longer locked/);
    assert.throws(() => escaped.save(emptyCheckpoint()), /no longer locked/);
    const first = host.step('v1', callbacks({ assess: () => 'satisfied', act: () => assert.fail('unexpected effect') }));
    assert.equal(first.status, 'satisfied');
    const replay = new FileHost(host.directory).step('v1', callbacks({ assess: () => assert.fail('unexpected reassessment') }));
    assert.equal(replay.status, 'reused');
    assert.equal(JSON.parse(readFileSync(host.checkpointPath)).schema, 1);
    assert.deepEqual(readdirSync(host.directory), ['checkpoint.json']);
  });
  check('malformed JSON, schema, values, extras, settlement, and UTF-8 fail closed', () => {
    const host = new FileHost(directory('invalid'));
    host.withLock(() => {});
    const valid = JSON.parse(encodeCheckpoint(emptyCheckpoint()));
    const invalid = ['{', 'null', '[]', JSON.stringify({ ...valid, schema: 2 }), JSON.stringify({ ...valid, extra: 1 }),
      JSON.stringify({ ...valid, attempts: -1 }), JSON.stringify({ ...valid, attempts: 1.5 }),
      JSON.stringify({ ...valid, validUntil: Number.MAX_SAFE_INTEGER + 1 }), JSON.stringify({ ...valid, pending: ' ' }),
      JSON.stringify({ ...valid, disposition: 'fulfilled' }), JSON.stringify({ ...valid, settlement: {} }),
      JSON.stringify({ ...valid, settlement: { binding: 'v1', attempt: 'a', outcome: 'unknown', receipt: 'r' } }),
      JSON.stringify({ ...valid, settlement: { binding: 'v1', attempt: 'a', outcome: 'completed', receipt: 'r', extra: 'x' } }),
      Buffer.from([0xff]), '\ufeff' + JSON.stringify(valid),
      JSON.stringify(valid).replace('"schema":1', '"schema":1.0'),
      JSON.stringify(valid).replace('"attempts":0', '"attempts":0e0'),
      JSON.stringify(valid).replace('"attempts":0', '"attempts":-0'),
      JSON.stringify(valid).replace('"pending":null', '"pending":"unsettled","pending":null'),
      JSON.stringify(valid).replace('"schema":1', '"schema":999,"schema":1')];
    for (const surrogate of ['\ud800', '\udfff']) {
      for (const field of ['binding', 'evidence', 'pending']) invalid.push(JSON.stringify({ ...valid, [field]: surrogate }));
      invalid.push(JSON.stringify({ ...valid, settlement: { binding: 'v1', attempt: 'a', outcome: 'completed', receipt: surrogate } }));
      assert.throws(() => encodeCheckpoint({ ...emptyCheckpoint(), binding: surrogate }), /invalid checkpoint/);
    }
    for (const blank of ['\u0085', '\ufeff', '\u0085 \t\ufeff']) {
      invalid.push(JSON.stringify({ ...valid, pending: blank }));
      for (const field of ['binding', 'attempt', 'receipt']) {
        invalid.push(JSON.stringify({ ...valid, settlement: { binding: 'v1', attempt: 'a', outcome: 'completed', receipt: 'r', [field]: blank } }));
      }
      assert.throws(() => encodeCheckpoint({ ...emptyCheckpoint(), pending: blank }), /invalid checkpoint/);
    }
    const unicode = { ...emptyCheckpoint(), binding: 'brief-🧶', evidence: 'café/東京', validUntil: Number.MAX_SAFE_INTEGER };
    assert.deepEqual(decodeCheckpoint(encodeCheckpoint(unicode)), unicode);
    const missing = { ...valid }; delete missing.evidence; invalid.push(JSON.stringify(missing));
    for (const bytes of invalid) {
      writeFileSync(host.checkpointPath, bytes);
      const original = readFileSync(host.checkpointPath);
      assert.throws(() => host.step('v1', callbacks({ observe: () => assert.fail('corrupt input observed') })));
      assert.deepEqual(readFileSync(host.checkpointPath), original);
      assert.equal(existsSync(host.lockPath), false);
    }
  });
  check('encoding failure preserves prior valid bytes and removes no checkpoint', () => {
    const host = new FileHost(directory('encode'));
    host.withLock(session => {
      session.save(emptyCheckpoint());
      const original = readFileSync(host.checkpointPath);
      assert.throws(() => session.save({ ...emptyCheckpoint(), extra: true }), /invalid checkpoint/);
      assert.deepEqual(readFileSync(host.checkpointPath), original);
    });
  });
  check('pending-save rename failure prevents effect and leaves no temporary file', () => {
    const host = new FileHost(directory('save-failure'));
    let actions = 0;
    assert.throws(() => host.step('v1', callbacks({
      newId: () => {
        // The assessment was saved; block only the subsequent pending save.
        rmSync(host.checkpointPath);
        mkdirSync(host.checkpointPath);
        return 'attempt-1';
      },
      act: () => { actions++; },
    })));
    assert.equal(actions, 0);
    assert.deepEqual(readdirSync(host.directory).sort(), ['checkpoint.json', 'lock']);
    assert.throws(() => new FileHost(host.directory).withLock(() => {}), /busy/);
  });
  check('temporary-file sync failure preserves the preceding checkpoint and blocks action', () => {
    const host = new FileHost(directory('temporary-sync-failure'));
    const originalSync = fs.fsyncSync;
    let syncs = 0; let actions = 0; let previous;
    const spy = spyOn(fs, 'fsyncSync').mockImplementation(descriptor => {
      if (++syncs === 3) throw Error('injected temporary sync failure');
      return originalSync(descriptor);
    });
    try {
      assert.throws(() => host.step('v1', callbacks({
        newId: () => { previous = readFileSync(host.checkpointPath); return 'attempt-1'; },
        act: () => { actions++; },
      })), /injected temporary sync/);
    } finally { spy.mockRestore(); }
    assert.equal(actions, 0);
    assert.deepEqual(readFileSync(host.checkpointPath), previous);
    assert.deepEqual(readdirSync(host.directory), ['checkpoint.json']);
  });
  check('parent sync failure retains a poisoned lock and prevents the pending effect', () => {
    const host = new FileHost(directory('parent-sync-failure'));
    const originalSync = fs.fsyncSync;
    let syncs = 0; let actions = 0;
    const spy = spyOn(fs, 'fsyncSync').mockImplementation(descriptor => {
      if (++syncs === 4) throw Error('injected parent sync failure');
      return originalSync(descriptor);
    });
    try { assert.throws(() => host.step('v1', callbacks({ act: () => { actions++; } })), /injected parent sync/); }
    finally { spy.mockRestore(); }
    assert.equal(actions, 0);
    assert.equal(decodeCheckpoint(readFileSync(host.checkpointPath)).pending, 'attempt-1');
    assert.equal(existsSync(host.lockPath), true);
    assert.throws(() => new FileHost(host.directory).withLock(() => {}), /busy/);
  });
  check('uncertain save after action keeps the lock even when pending is visibly cleared', () => {
    const host = new FileHost(directory('after-action-sync-failure'));
    const originalSync = fs.fsyncSync;
    let syncs = 0; let actions = 0;
    const spy = spyOn(fs, 'fsyncSync').mockImplementation(descriptor => {
      if (++syncs === 6) throw Error('injected post-action parent sync failure');
      return originalSync(descriptor);
    });
    try { assert.throws(() => host.step('v1', callbacks({ act: () => { actions++; } })), /post-action parent sync/); }
    finally { spy.mockRestore(); }
    assert.equal(actions, 1);
    assert.equal(decodeCheckpoint(readFileSync(host.checkpointPath)).pending, null);
    assert.equal(existsSync(host.lockPath), true);
    assert.throws(() => new FileHost(host.directory).step('v1', callbacks()), /busy/);
  });
  check('a caught uncertain save cannot reset poison through another save', () => {
    const host = new FileHost(directory('caught-parent-failure'));
    const originalSync = fs.fsyncSync;
    let syncs = 0;
    const spy = spyOn(fs, 'fsyncSync').mockImplementation(descriptor => {
      if (++syncs === 2) throw Error('injected parent sync failure');
      return originalSync(descriptor);
    });
    try {
      host.withLock(session => {
        assert.throws(() => session.save(emptyCheckpoint()), /injected parent sync/);
        assert.throws(() => session.save(emptyCheckpoint()), /durability is uncertain/);
        assert.throws(() => session.load(), /durability is uncertain/);
      });
    } finally { spy.mockRestore(); }
    assert.equal(existsSync(host.lockPath), true);
  });
  check('actor failure preserves pending across reopen; explicit settlement forces reassessment', () => {
    const host = new FileHost(directory('settle'));
    const failed = host.step('v1', callbacks({ act: () => { throw Error('uncertain effect'); } }));
    assert.equal(failed.status, 'action-outcome-unknown');
    const reopened = new FileHost(host.directory);
    assert.equal(reopened.step('v1', callbacks({ observe: () => assert.fail('pending observed') })).status, 'recovery-needed');
    const before = readFileSync(host.checkpointPath);
    assert.throws(() => reopened.settle('wrong', 'attempt-1', 'completed', 'receipt'));
    assert.deepEqual(readFileSync(host.checkpointPath), before);
    const settled = reopened.settle('v1', 'attempt-1', 'completed', 'trusted-receipt');
    assert.equal(settled.attempts, 1); assert.equal(settled.pending, null); assert.equal(settled.validUntil, 0);
    assert.throws(() => reopened.settle('v1', 'attempt-1', 'completed', 'trusted-receipt'));
    let assessments = 0;
    assert.equal(reopened.step('v1', callbacks({ assess: () => { assessments++; return 'satisfied'; }, act: () => assert.fail('unexpected effect') })).status, 'satisfied');
    assert.equal(assessments, 1);
  });
  check('not-applied settlement does not replenish exhausted attempts', () => {
    const host = new FileHost(directory('not-applied'));
    host.step('v1', callbacks({ act: () => { throw Error('uncertain effect'); } }));
    host.settle('v1', 'attempt-1', 'not-applied', 'trusted-inspection');
    assert.equal(host.step('v1', callbacks({ act: () => assert.fail('budget bypass') })).status, 'attempt-limit');
  });
  check('existing lock is conservative; rejected asynchronous callback does not start', () => {
    const host = new FileHost(directory('locks'));
    mkdirSync(host.directory); mkdirSync(host.lockPath);
    assert.throws(() => host.withLock(() => {}), /busy/);
    assert.equal(existsSync(host.lockPath), true);
    rmdirSync(host.lockPath);
    let invoked = false;
    assert.throws(() => host.withLock(async () => { invoked = true; }), /synchronous/);
    assert.equal(invoked, false);
    assert.throws(() => host.withLock(() => ({ then() {} })), /synchronous/);
    assert.equal(existsSync(host.lockPath), false);
  });

  const processDir = directory('processes');
  const holder = Bun.spawn([process.execPath, import.meta.path, '--hold', processDir], { stdin: 'ignore', stdout: 'pipe', stderr: 'pipe', env: { PATH: '/usr/bin:/bin' } });
  try {
    const end = Date.now() + 10_000;
    while (!existsSync(join(processDir, 'entered')) && Date.now() < end) await new Promise(resolve => setTimeout(resolve, 10));
    assert.ok(existsSync(join(processDir, 'entered')), 'child did not reach pending action');
    const stored = decodeCheckpoint(readFileSync(join(processDir, 'checkpoint.json')));
    assert.equal(stored.pending, 'attempt-1');
    const contender = Bun.spawnSync([process.execPath, import.meta.path, '--contend', processDir], { stdin: 'ignore', stdout: 'pipe', stderr: 'pipe', env: { PATH: '/usr/bin:/bin' } });
    assert.equal(contender.exitCode, 0, contender.stderr.toString());
    assert.equal(existsSync(join(processDir, 'lock')), true);
    holder.kill('SIGKILL'); await holder.exited;
    const reopened = new FileHost(processDir);
    assert.throws(() => reopened.step('v1', callbacks()), /busy/);
    assert.equal(existsSync(reopened.lockPath), true);
    // The test acts as the trusted operator after proving the child has exited.
    rmdirSync(reopened.lockPath);
    assert.equal(reopened.step('v1', callbacks({ observe: () => assert.fail('pending replay') })).status, 'recovery-needed');
    assert.equal(decodeCheckpoint(readFileSync(reopened.checkpointPath)).attempts, 1);
    checks++; console.log('PASS competing processes, abrupt termination, conservative lock, and pending restart');
  } finally { if (holder.exitCode === null) { holder.kill('SIGKILL'); await holder.exited; } }
  console.log(`PASS ${checks} local host checks on ${process.platform}/${process.arch}`);
} finally { rmSync(root, { recursive: true, force: true }); }
