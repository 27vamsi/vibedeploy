'use strict';

// Readme.md section 14: patch `http.Server.prototype.emit` so each 'request'
// event is handled inside `als.run(identity, ...)`. Everything built on Node's
// http server - Express, Next.js standalone, bare http - goes through here, so
// we never have to know which framework the app uses.

const http = require('node:http');

const { HEADER_NAME, loadKey, run, verify } = require('./identity');

const PATCHED = Symbol.for('vibedeploy.patched');

let cachedKey;

function key() {
  // Read lazily: --require runs before the process has its secrets in some
  // launchers, and we only need the key on the first request.
  if (cachedKey === undefined) cachedKey = loadKey();
  return cachedKey;
}

function install() {
  const original = http.Server.prototype.emit;
  if (original[PATCHED]) return true;

  function emit(event, ...args) {
    if (event !== 'request') return original.call(this, event, ...args);
    // Duplicated headers arrive joined by ", " and will not verify. Fail closed.
    const identity = verify(args[0]?.headers?.[HEADER_NAME], key());
    return run(identity, () => original.call(this, event, ...args));
  }

  emit[PATCHED] = true;
  http.Server.prototype.emit = emit;
  return true;
}

module.exports = { install };
