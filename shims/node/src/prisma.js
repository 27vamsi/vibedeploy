'use strict';

// Prisma. Readme.md section 14: hook `Module._load` for `@prisma/client` and
// return a PrismaClient extended so every operation runs as a batch
// transaction, `$transaction([set_config..., operation])`.
//
// The batch form matters. Prisma's interactive `$transaction(async tx => ...)`
// hands the callback a different client, and the operation we are wrapping is
// already bound to the outer one, so it would run on another connection and
// the identity would not apply to it. The array form runs both statements on
// one connection, in order, inside one transaction.
//
// Known gap: `$queryRaw` / `$executeRaw` issued by the app are not model
// operations, so they are not wrapped and run without an identity. That fails
// closed - they see zero rows.

const Module = require('node:module');

const { SET_IDENTITY_SQL, identityValues } = require('./identity');

const PATCHED = Symbol.for('vibedeploy.patched');
const CLIENT_MODULES = new Set(['@prisma/client', '.prisma/client']);

function setIdentityStatement(client) {
  return client.$queryRawUnsafe(SET_IDENTITY_SQL, ...identityValues());
}

function wrapPrismaClient(Base) {
  return class VibedeployPrismaClient extends Base {
    constructor(...args) {
      super(...args);

      const base = this;
      const originalTransaction = Base.prototype.$transaction.bind(base);

      // An interactive transaction started by the app gets the identity as its
      // first statement. The array form is left alone: that is what the
      // extension below uses, and re-entering here would recurse.
      base.$transaction = function $transaction(arg, options) {
        if (typeof arg !== 'function') return originalTransaction(arg, options);
        return originalTransaction(async (tx) => {
          await setIdentityStatement(tx);
          return arg(tx);
        }, options);
      };

      // Returning from a constructor is legal and is how the app ends up
      // holding the extended client rather than this one.
      return base.$extends({
        query: {
          $allModels: {
            async $allOperations({ args, query }) {
              const [, result] = await originalTransaction([
                setIdentityStatement(base),
                query(args),
              ]);
              return result;
            },
          },
        },
      });
    }
  };
}

function wrapExports(exported) {
  if (!exported || typeof exported.PrismaClient !== 'function') return exported;
  if (exported.PrismaClient[PATCHED]) return exported;

  const Wrapped = wrapPrismaClient(exported.PrismaClient);
  Wrapped[PATCHED] = true;

  // The module object is frozen in some Prisma builds, so hand back a proxy
  // rather than assigning onto it.
  return new Proxy(exported, {
    get(target, property, receiver) {
      if (property === 'PrismaClient') return Wrapped;
      return Reflect.get(target, property, receiver);
    },
  });
}

function install(paths = [process.cwd()]) {
  try {
    require.resolve('@prisma/client', { paths });
  } catch {
    return false;
  }

  const originalLoad = Module._load;
  if (originalLoad[PATCHED]) return true;

  function load(request, parent, isMain) {
    const exported = originalLoad.call(this, request, parent, isMain);
    return CLIENT_MODULES.has(request) ? wrapExports(exported) : exported;
  }

  load[PATCHED] = true;
  Module._load = load;
  return true;
}

module.exports = { install };
