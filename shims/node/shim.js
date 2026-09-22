'use strict';

// Delivery. Readme.md section 14: loaded with NODE_OPTIONS=--require /vd/shim.js,
// so this runs before the app's first line and before it requires Prisma or pg.
//
// One startup line is printed. The worker reads it out of the app's logs: if it
// is missing, or says a part is not active, the app is marked Unprotected.

const identity = require('./src/identity');
const httpIntegration = require('./src/http');
const pgIntegration = require('./src/pg');
const prismaIntegration = require('./src/prisma');

const PREFIX = 'vibedeploy-shim';

function statusLine({ framework, db, key }) {
  const parts = [
    PREFIX,
    framework && db && key ? 'active' : 'inactive',
    'lang=node',
    `db=${db ?? 'none'}`,
    `framework=${framework ?? 'none'}`,
  ];
  if (!key) parts.push('reason=no-identity-key');
  else if (!db) parts.push('reason=unsupported-database-library');
  else if (!framework) parts.push('reason=unsupported-framework');
  return parts.join(' ');
}

let status = null;

function install(paths = [process.cwd()]) {
  if (status) return status;

  const framework = httpIntegration.install() ? 'http' : null;
  // Prisma first: an app using both should have its ORM covered.
  const db = prismaIntegration.install(paths)
    ? 'prisma'
    : pgIntegration.install(paths)
      ? 'pg'
      : null;
  const key = identity.loadKey() !== null;

  status = statusLine({ framework, db, key });
  process.stdout.write(`${status}\n`);
  return status;
}

module.exports = { install, statusLine };

install();
