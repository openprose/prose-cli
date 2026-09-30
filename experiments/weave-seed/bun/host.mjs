/** Synchronous local checkpoint host. Requires a trusted host-specific directory. */
import { mkdirSync, readFileSync, openSync, writeFileSync, fsyncSync, closeSync, renameSync, unlinkSync, rmdirSync, lstatSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { emptyCheckpoint, reconcile, settlePending } from './index.mjs';

const checkpointKeys = ['schema', 'binding', 'evidence', 'disposition', 'validUntil', 'pending', 'attempts', 'settlement'];
const coreKeys = checkpointKeys.filter(key => key !== 'schema');
const settlementKeys = ['binding', 'attempt', 'outcome', 'receipt'];
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value) && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);
const exactKeys = (value, keys) => plain(value) && Object.keys(value).sort().join(',') === [...keys].sort().join(',');
const scalarText = value => typeof value === 'string' && !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(value);
const nonempty = value => scalarText(value) && /[^\s\u0085]/u.test(value);
const integer = value => Number.isSafeInteger(value) && value >= 0;
const synchronous = value => {
  if (value !== null && (typeof value === 'object' || typeof value === 'function') && typeof value.then === 'function') throw new Error('synchronous operation required');
  return value;
};

function validate(value, disk) {
  if (!exactKeys(value, disk ? checkpointKeys : coreKeys) || (disk && value.schema !== 1)
      || !scalarText(value.binding) || !scalarText(value.evidence)
      || !['unknown', 'satisfied', 'work-needed'].includes(value.disposition)
      || !integer(value.validUntil) || !integer(value.attempts)
      || !(value.pending === null || nonempty(value.pending))) throw new Error('invalid checkpoint schema or values');
  if (value.settlement !== null) {
    const record = value.settlement;
    if (!exactKeys(record, settlementKeys) || !Object.values(record).every(nonempty)
        || !['completed', 'not-applied'].includes(record.outcome)) throw new Error('invalid checkpoint settlement');
  }
}

export function encodeCheckpoint(checkpoint) {
  const snapshot = structuredClone(checkpoint);
  validate(snapshot, false);
  return JSON.stringify({ schema: 1, ...snapshot }) + '\n';
}

// JSON.parse validates grammar before this scan. Each character is visited
// once, including escaped quotes/backslashes; no search can restart inside a
// string and trigger regular-expression backtracking.
function* checkpointTokens(text) {
  let offset = 0;
  while (offset < text.length) {
    const start = offset;
    const character = text[offset++];
    if (character === '"') {
      while (offset < text.length) {
        const next = text[offset++];
        if (next === '\\') offset++;
        else if (next === '"') break;
      }
      yield text.slice(start, offset);
    } else if ('{}[],:'.includes(character)) {
      yield character;
    } else if (character === '-' || /[0-9]/.test(character)) {
      while (offset < text.length && '0123456789.eE+-'.includes(text[offset])) offset++;
      yield text.slice(start, offset);
    }
  }
}

// JSON.parse alone accepts duplicate keys and noncanonical numeric spellings.
function validateTokens(text) {
  const frames = [];
  for (const token of checkpointTokens(text)) {
    const top = frames.at(-1);
    if (token === '{') frames.push({ object: true, keys: new Set(), expectingKey: true });
    else if (token === '[') frames.push({ object: false });
    else if (token === '}' || token === ']') frames.pop();
    else if (token === ',' && top?.object) top.expectingKey = true;
    else if (token.startsWith('"') && top?.object && top.expectingKey) {
      const key = JSON.parse(token);
      if (top.keys.has(key)) throw new Error('duplicate checkpoint key');
      top.keys.add(key); top.expectingKey = false;
    } else if (/^-?\d/.test(token) && !/^(0|[1-9]\d*)$/.test(token)) throw new Error('noncanonical checkpoint integer');
  }
}

export function decodeCheckpoint(bytes) {
  const text = typeof bytes === 'string' ? bytes : new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes);
  const stored = JSON.parse(text);
  validateTokens(text);
  validate(stored, true);
  const { schema: _schema, ...checkpoint } = stored;
  return checkpoint;
}

export class FileHost {
  constructor(directory) {
    this.directory = resolve(directory);
    this.checkpointPath = join(this.directory, 'checkpoint.json');
    this.lockPath = join(this.directory, 'lock');
  }

  withLock(operation) {
    if (typeof operation !== 'function' || operation.constructor?.name === 'AsyncFunction') throw new Error('synchronous operation required');
    mkdirSync(this.directory, { recursive: true, mode: 0o700 });
    try { mkdirSync(this.lockPath, { mode: 0o700 }); }
    catch (error) {
      if (error.code === 'EEXIST') throw new Error('checkpoint host busy; existing lock requires owner completion or trusted reconciliation');
      throw error;
    }
    const owner = lstatSync(this.lockPath);
    let active = true;
    let poisoned = false;
    const requireLock = () => {
      if (!active) throw new Error('checkpoint session is no longer locked');
      if (poisoned) throw new Error('checkpoint durability is uncertain; trusted reconciliation required');
    };
    const session = Object.freeze({
      load: () => {
        requireLock();
        let bytes;
        try { bytes = readFileSync(this.checkpointPath); }
        catch (error) { if (error.code === 'ENOENT') return emptyCheckpoint(); throw error; }
        return decodeCheckpoint(bytes);
      },
      save: checkpoint => {
        requireLock();
        // Complete validation and encoding before touching the previous checkpoint.
        const encoded = encodeCheckpoint(checkpoint);
        const temporary = join(this.directory, `checkpoint-${randomUUID()}.tmp`);
        let descriptor;
        let created = false;
        try {
          descriptor = openSync(temporary, 'wx', 0o600);
          created = true;
          writeFileSync(descriptor, encoded, { encoding: 'utf8' });
          fsyncSync(descriptor);
          closeSync(descriptor);
          descriptor = undefined;
          // A failed replacement/directory sync may leave new state visible but not durable.
          poisoned = true;
          renameSync(temporary, this.checkpointPath);
          created = false;
          const parent = openSync(this.directory, 'r');
          try { fsyncSync(parent); } finally { closeSync(parent); }
          poisoned = false;
        } finally {
          if (descriptor !== undefined) closeSync(descriptor);
          if (created) unlinkSync(temporary);
        }
      },
    });
    try { return synchronous(operation(session)); }
    finally {
      active = false;
      const current = lstatSync(this.lockPath);
      if (current.dev !== owner.dev || current.ino !== owner.ino) throw new Error('checkpoint lock ownership changed; refusing removal');
      if (!poisoned) rmdirSync(this.lockPath);
    }
  }

  step(binding, callbacks, maxAttempts = 1) {
    return this.withLock(session => reconcile(binding, session.load(), {
      observe: () => callbacks.observe(), assess: evidence => callbacks.assess(evidence),
      act: (evidence, attempt) => callbacks.act(evidence, attempt),
      clock: () => callbacks.clock(), newId: () => callbacks.newId(),
      save: checkpoint => session.save(checkpoint),
    }, maxAttempts));
  }

  settle(binding, attempt, outcome, receipt) {
    return this.withLock(session => {
      const checkpoint = settlePending(session.load(), binding, attempt, outcome, receipt);
      session.save(checkpoint);
      return checkpoint;
    });
  }
}
