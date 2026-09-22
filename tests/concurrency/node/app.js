'use strict';

// The Node half of the M2 proof. Readme.md section 8, M2.
//
// `/notes` runs `SELECT id FROM notes` with no WHERE, the same as the Python
// fixture app. Nothing in this file touches identity: the shim is loaded with
// NODE_OPTIONS=--require and has already patched http.Server, Prisma and pg by
// the time this runs.
//
// VD_APP_MODE picks the database library: `pg` or `prisma`.

const http = require('node:http');

const MODE = process.env.VD_APP_MODE ?? 'pg';
const PORT = Number(process.env.PORT);

let readNoteIds;

if (MODE === 'prisma') {
  const { PrismaClient } = require('@prisma/client');
  const prisma = new PrismaClient();
  readNoteIds = async () => {
    const rows = await prisma.note.findMany({ select: { id: true } });
    return rows.map((row) => row.id);
  };
} else {
  const { Pool } = require('pg');
  const pool = new Pool({ connectionString: process.env.DATABASE_URL, max: 10 });
  readNoteIds = async () => {
    const result = await pool.query('SELECT id FROM notes');
    return result.rows.map((row) => row.id);
  };
}

const server = http.createServer((req, res) => {
  if (!req.url.startsWith('/notes')) {
    res.writeHead(404).end();
    return;
  }
  readNoteIds().then(
    (ids) => {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ ids }));
    },
    (error) => {
      res.writeHead(500, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ error: String(error && error.message) }));
    }
  );
});

server.listen(PORT, '127.0.0.1');
