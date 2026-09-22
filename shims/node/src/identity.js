'use strict';

// The X-VD-Identity contract. Readme.md section 7.1.
//
// Byte-for-byte the same as shims/python/vibedeploy_shim/identity.py: the
// sidecar mints one header and either shim must accept exactly the same set.
// Everything fails closed - a missing, malformed, unsigned, expired or
// unknown-version header yields null, and null becomes an empty app.user_id,
// which vd_user_id() turns into NULL, which matches no row.

const crypto = require('node:crypto');
const { AsyncLocalStorage } = require('node:async_hooks');

const HEADER_NAME = 'x-vd-identity';
const KEY_ENV = 'VD_IDENTITY_KEY';
const VERSION = 1;
const TTL_SECONDS = 60;
const ROLES = new Set(['member', 'admin']);

// One identity per request, carried across every await the request makes.
const storage = new AsyncLocalStorage();

function current() {
  return storage.getStore() ?? null;
}

function run(identity, callback) {
  return storage.run(identity ?? null, callback);
}

function loadKey(env) {
  const raw = (env ?? process.env)[KEY_ENV];
  if (!raw) return null;
  const key = Buffer.from(raw, 'base64url');
  return key.length ? key : null;
}

function sign(payload, key) {
  // Sorted keys so a payload signed here verifies in Python and vice versa.
  const raw = Buffer.from(
    JSON.stringify(payload, Object.keys(payload).sort()),
    'utf8'
  );
  const signature = crypto.createHmac('sha256', key).update(raw).digest();
  return `${raw.toString('base64url')}.${signature.toString('base64url')}`;
}

function makeHeader({ app, sub, role, key, now }) {
  const issued = Math.floor(now ?? Date.now() / 1000);
  return sign(
    { v: VERSION, app, sub, role, iat: issued, exp: issued + TTL_SECONDS },
    key
  );
}

function verify(value, key, options = {}) {
  if (!key || !value || typeof value !== 'string') return null;

  const parts = value.split('.');
  if (parts.length !== 2) return null;

  const payloadBytes = Buffer.from(parts[0], 'base64url');
  const signature = Buffer.from(parts[1], 'base64url');
  const expected = crypto.createHmac('sha256', key).update(payloadBytes).digest();
  // timingSafeEqual throws on a length mismatch, so check that first.
  if (signature.length !== expected.length) return null;
  if (!crypto.timingSafeEqual(signature, expected)) return null;

  let payload;
  try {
    payload = JSON.parse(payloadBytes.toString('utf8'));
  } catch {
    return null;
  }
  if (payload === null || typeof payload !== 'object' || Array.isArray(payload)) {
    return null;
  }

  // Reject versions we do not understand rather than guessing at the shape.
  if (payload.v !== VERSION) return null;

  if (typeof payload.exp !== 'number' || !Number.isFinite(payload.exp)) return null;
  const now = options.now ?? Date.now() / 1000;
  if (now >= payload.exp) return null;

  if (typeof payload.app !== 'string' || !payload.app) return null;
  if (typeof payload.sub !== 'string' || !payload.sub) return null;
  if (!ROLES.has(payload.role)) return null;
  if (options.app != null && payload.app !== options.app) return null;

  return { app: payload.app, sub: payload.sub, role: payload.role };
}

// What every database integration writes at the start of a transaction.
const SET_IDENTITY_SQL =
  "SELECT set_config('app.user_id', $1, true), set_config('app.role', $2, true)";

function identityValues() {
  const who = current();
  return [who ? who.sub : '', who ? who.role : ''];
}

module.exports = {
  HEADER_NAME,
  KEY_ENV,
  ROLES,
  SET_IDENTITY_SQL,
  TTL_SECONDS,
  VERSION,
  current,
  identityValues,
  loadKey,
  makeHeader,
  run,
  sign,
  verify,
};
