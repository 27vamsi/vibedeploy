'use strict';

// node-postgres. Readme.md section 14.
//
// `pool.query()` is the common case and has no transaction of its own, so we
// give it one: check out a client, BEGIN, set the identity, run the query,
// COMMIT. If the app drives a client itself and sends its own BEGIN, we append
// the set_config right after it.
//
// Identity always travels as a bind parameter, never as SQL text.

const { SET_IDENTITY_SQL, identityValues } = require('./identity');

const PATCHED = Symbol.for('vibedeploy.patched');
const BEGIN = /^\s*(BEGIN|START\s+TRANSACTION)\b/i;

function resolvePg(paths) {
  try {
    return require(require.resolve('pg', { paths }));
  } catch {
    return null;
  }
}

function statementText(first) {
  if (typeof first === 'string') return first;
  if (first && typeof first.text === 'string') return first.text;
  return null;
}

function install(paths = [process.cwd()]) {
  const pg = resolvePg(paths);
  if (!pg) return false;
  if (pg.Pool.prototype.query[PATCHED]) return true;

  const clientQuery = pg.Client.prototype.query;

  function setIdentity(client) {
    return clientQuery.call(client, {
      text: SET_IDENTITY_SQL,
      values: identityValues(),
    });
  }

  function poolQuery(...args) {
    const callback =
      typeof args[args.length - 1] === 'function' ? args.pop() : null;

    const pool = this;
    const promise = (async () => {
      const client = await pool.connect();
      try {
        await clientQuery.call(client, 'BEGIN');
        await setIdentity(client);
        const result = await clientQuery.apply(client, args);
        await clientQuery.call(client, 'COMMIT');
        return result;
      } catch (error) {
        try {
          await clientQuery.call(client, 'ROLLBACK');
        } catch {
          // The connection is already unusable; releasing it is what matters.
        }
        throw error;
      } finally {
        client.release();
      }
    })();

    if (!callback) return promise;
    promise.then(
      (result) => callback(null, result),
      (error) => callback(error)
    );
    return undefined;
  }
  poolQuery[PATCHED] = true;
  pg.Pool.prototype.query = poolQuery;

  function patchedClientQuery(...args) {
    const isBegin = BEGIN.test(statementText(args[0]) ?? '');
    const hasCallback = typeof args[args.length - 1] === 'function';
    if (!isBegin || hasCallback) return clientQuery.apply(this, args);

    const client = this;
    return (async () => {
      const result = await clientQuery.apply(client, args);
      await setIdentity(client);
      return result;
    })();
  }
  patchedClientQuery[PATCHED] = true;
  pg.Client.prototype.query = patchedClientQuery;

  return true;
}

module.exports = { install };
