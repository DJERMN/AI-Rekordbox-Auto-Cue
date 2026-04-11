/**
 * Pioneer rekordbox export.pdb - playlist viewer & finder
 * Requires: npm install rekordbox-parser  (already installed)
 *
 * Usage:
 *   node rb.js list                        - list all playlists/folders
 *   node rb.js show "<Name>"               - show tracks in a playlist
 *   node rb.js rate <TrackID> <0-5>        - set a track rating
 *   node rb.js search "<Query>"            - search tracks
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { parsePdb, tableRows } = require('rekordbox-parser');

const PDB = process.env.AUTOCUE_PDB || process.env.PDB || 'D:\\PIONEER\\rekordbox\\export.pdb';
const STARS = ['☆☆☆☆☆','★☆☆☆☆','★★☆☆☆','★★★☆☆','★★★★☆','★★★★★'];

// ── helpers ──────────────────────────────────────────────────────────────────

function str(s) {
  try { return s && s.body ? s.body.text : ''; } catch { return ''; }
}

function loadPdb() {
  const buf = fs.readFileSync(PDB);
  const pdb = parsePdb(buf);
  const tables = pdb.tables;

  const tracks   = new Map([...tableRows(tables[0])].map(t => [t.id, t]));
  const artists  = new Map([...tableRows(tables[2])].map(a => [a.id, a]));
  const albums   = new Map([...tableRows(tables[3])].map(a => [a.id, a]));
  const plNodes  = [...tableRows(tables[7])];          // PlaylistTree
  const entries  = [...tableRows(tables[8])];          // PlaylistEntries

  // group entries by playlist id
  const entryMap = new Map();
  for (const e of entries) {
    if (!entryMap.has(e.playlistId)) entryMap.set(e.playlistId, []);
    entryMap.get(e.playlistId).push(e);
  }

  return { buf, pdb, tracks, artists, albums, plNodes, entryMap };
}

function trackLine(t, artists) {
  const artist = artists.get(t.artistId);
  const aname  = artist ? str(artist.name) : '?';
  const title  = str(t.title);
  const bpm    = (t.tempo / 100).toFixed(1);
  const dur    = `${Math.floor(t.duration/60)}:${String(t.duration%60).padStart(2,'0')}`;
  const rating = STARS[t.rating] || STARS[0];
  return `  [${String(t.id).padStart(5)}] ${rating}  ${bpm.padStart(6)} BPM  ${dur}  ${aname} – ${title}`;
}

// ── tree helpers ─────────────────────────────────────────────────────────────

function buildTree(plNodes) {
  const byParent = new Map();
  for (const n of plNodes) {
    if (!byParent.has(n.parentId)) byParent.set(n.parentId, []);
    byParent.get(n.parentId).push(n);
  }
  return byParent;
}

function printTree(byParent, parentId = 0, indent = '') {
  const children = (byParent.get(parentId) || []).sort((a,b) => a.sortOrder - b.sortOrder);
  for (const n of children) {
    const icon = n.rawIsFolder ? '📁' : '🎵';
    console.log(`${indent}${icon} [${n.id}] ${str(n.name)}`);
    if (n.rawIsFolder) printTree(byParent, n.id, indent + '   ');
  }
}

function findPlaylist(plNodes, query) {
  const q = query.toLowerCase();
  return plNodes.filter(n => !n.rawIsFolder && str(n.name).toLowerCase().includes(q));
}

// ── commands ──────────────────────────────────────────────────────────────────

function cmdList() {
  const { plNodes } = loadPdb();
  const tree = buildTree(plNodes);
  console.log('\n── Rekordbox Playlists ──────────────────────────────');
  printTree(tree);
}

function cmdShow(query) {
  const { tracks, artists, albums, plNodes, entryMap } = loadPdb();
  const matches = findPlaylist(plNodes, query);
  if (!matches.length) {
    console.error(`No playlist found for: "${query}"`);
    process.exit(1);
  }
  for (const pl of matches) {
    const name = str(pl.name);
    const plEntries = (entryMap.get(pl.id) || []).sort((a,b) => a.entryIndex - b.entryIndex);
    console.log(`\n── ${name} (${plEntries.length} tracks) ──────────────────`);
    for (const e of plEntries) {
      const t = tracks.get(e.trackId);
      if (t) console.log(trackLine(t, artists));
    }
  }
}

function cmdFind(query) {
  const { tracks, artists } = loadPdb();
  const q = query.toLowerCase();
  const results = [];
  for (const t of tracks.values()) {
    const title  = str(t.title).toLowerCase();
    const artist = str((artists.get(t.artistId) || {}).name || '').toLowerCase();
    if (title.includes(q) || artist.includes(q) || String(t.id) === q.trim()) {
      results.push({
        id:          t.id,
        title:       str(t.title),
        artist:      str((artists.get(t.artistId) || {}).name || ''),
        bpm:         t.tempo / 100,
        duration:    t.duration,
        filePath:    str(t.filePath),
        analyzePath: str(t.analyzePath),
      });
    }
  }
  console.log(JSON.stringify(results));
}

function cmdSearch(query) {
  const { tracks, artists } = loadPdb();
  const q = query.toLowerCase();
  let found = 0;
  console.log(`\n── Search: "${query}" ──────────────────────────────────`);
  for (const t of tracks.values()) {
    const title  = str(t.title).toLowerCase();
    const artist = str((artists.get(t.artistId)||{}).name||'').toLowerCase();
    if (title.includes(q) || artist.includes(q)) {
      console.log(trackLine(t, artists));
      found++;
    }
  }
  console.log(`\n${found} matches.`);
}

function cmdRate(trackId, rating) {
  const id = parseInt(trackId);
  const r  = parseInt(rating);
  if (isNaN(id) || isNaN(r) || r < 0 || r > 5) {
    console.error('Usage: node rb.js rate <TrackID> <0-5>');
    process.exit(1);
  }

  const { buf, tracks, artists } = loadPdb();
  const t = tracks.get(id);
  if (!t) { console.error(`Track ID ${id} not found.`); process.exit(1); }

  // Locate the rating byte: scan for track row by ID (u32LE at row+72, subtype 0x24 at row+0)
  // The kaitai IO byteOffset gives the absolute position of the row in the file
  const rowPos = t._io._byteOffset;
  const ratingPos = rowPos + 89;

  // Verify we found the right row
  const storedId = buf.readUInt32LE(rowPos + 72);
  if (storedId !== id) {
    // Fallback: scan the buffer
    let found = -1;
    for (let i = 0; i + 90 < buf.length; i += 2) {
      if (buf[i] === 0x24 && buf[i+1] === 0x00 && buf.readUInt32LE(i+72) === id) {
        found = i; break;
      }
    }
    if (found < 0) { console.error('Could not locate track position.'); process.exit(1); }
    buf[found + 89] = r;
    fs.writeFileSync(PDB, buf);
  } else {
    buf[ratingPos] = r;
    fs.writeFileSync(PDB, buf);
  }

  const title  = str(t.title);
  const artist = str((artists.get(t.artistId)||{}).name||'');
  console.log(`✅ Rating updated: ${artist} – ${title}`);
  console.log(`   ${STARS[t.rating]} → ${STARS[r]}`);
}

// ── main ──────────────────────────────────────────────────────────────────────

const [,, cmd, ...args] = process.argv;
switch (cmd) {
  case 'list':   cmdList(); break;
  case 'show':   cmdShow(args.join(' ')); break;
  case 'search': cmdSearch(args.join(' ')); break;
  case 'rate':   cmdRate(args[0], args[1]); break;
  case 'find':   cmdFind(args.join(' ')); break;
  default:
    console.log(`
Usage:
  node rb.js list                  - list all playlists
  node rb.js show "<Name>"         - show tracks in a playlist
  node rb.js rate <ID> <0-5>       - set a rating
  node rb.js search "<Query>"      - search tracks
    `);
}
