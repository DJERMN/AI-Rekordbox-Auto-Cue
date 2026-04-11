#!/usr/bin/env python3
"""
autocue.py - Automatic Rekordbox memory cues

Usage:
  python autocue.py "Track Name"       - write cues for one track
  python autocue.py --id 12345         - process a track by Rekordbox ID
  python autocue.py --playlist "Name"  - process all tracks in a playlist
  python autocue.py --all              - process all tracks without cues
  python autocue.py --list             - list tracks without cues
  python autocue.py --dry --all        - run analysis without writing changes

Optional configuration via environment variables:
  AUTOCUE_REKORDBOX_DRIVE
  AUTOCUE_PDB
  AUTOCUE_MASTER_DB
  AUTOCUE_LOCAL_MASTER_DB
  AUTOCUE_LOCAL_ANLZ_BASE
"""

import sys, os, json, struct, re, subprocess, warnings, shutil, random
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from pathlib import Path
from datetime import datetime, timezone

warnings.filterwarnings("ignore")

# ── configuration ────────────────────────────────────────────────────────────
PYTHON = sys.executable
NODE = 'node'
HERE = Path(__file__).parent
RB_JS = HERE / 'rb.js'

DEFAULT_DRIVE = os.environ.get('AUTOCUE_REKORDBOX_DRIVE', 'D:\\')
D_DRIVE = Path(DEFAULT_DRIVE)
DEFAULT_RB_ROOT = Path(os.environ.get('APPDATA', str(Path.home()))) / 'Pioneer' / 'rekordbox'
MASTER_DB = Path(os.environ.get('AUTOCUE_MASTER_DB', str(D_DRIVE / 'PIONEER' / 'Master' / 'master.db')))

# Local ANLZ store for tracks not yet exported to the target drive.
LOCAL_ANLZ_BASE = Path(
    os.environ.get(
        'AUTOCUE_LOCAL_ANLZ_BASE',
        str(DEFAULT_RB_ROOT / 'share' / 'PIONEER' / 'USBANLZ'),
    )
)
# Local Rekordbox master.db used as the primary library database.
LOCAL_MASTER_DB = Path(
    os.environ.get(
        'AUTOCUE_LOCAL_MASTER_DB',
        str(DEFAULT_RB_ROOT / 'master.db'),
    )
)


def resolve_anlz_path(anlz_rel: str) -> Path | None:
    """Resolve an ANLZ path by checking the export drive first, then the local share."""
    rel = anlz_rel.lstrip('/')
    # z.B. "PIONEER/USBANLZ/P021/00026C19/ANLZ0000.DAT"
    # Strip "PIONEER/USBANLZ/" prefix to get sub-path
    if rel.startswith('PIONEER/USBANLZ/'):
        sub = rel[len('PIONEER/USBANLZ/'):]
    else:
        sub = rel
    d_path = D_DRIVE / rel
    if d_path.exists():
        return d_path
    local_path = LOCAL_ANLZ_BASE / sub
    if local_path.exists():
        return local_path
    return None


def resolve_audio_path(file_path: str) -> Path | None:
    """Resolve an audio file path, supporting absolute and drive-relative paths."""
    if not file_path:
        return None
    # Absolute path (for example C:/Music/Track.wav)
    p = Path(file_path.replace('/', '\\'))
    if p.is_absolute() and p.exists():
        return p
    # Relative path (for example /Contents/...) -> resolve on the export drive
    rel_path = D_DRIVE / file_path.lstrip('/')
    if rel_path.exists():
        return rel_path
    return None

MAX_CUES  = 8    # maximum number of memory cues to write
WIN_BARS  = 4    # bars before/after for cosine comparison
MIN_GAP   = 16   # minimum gap in PQTZ bars


# ══════════════════════════════════════════════════════════════════════════════
# 1. TRACK LOOKUP via rb.js (export.pdb)
# ══════════════════════════════════════════════════════════════════════════════

def find_tracks_pdb(query: str) -> list[dict]:
    """Search tracks in export.pdb using the rb.js find command."""
    result = subprocess.run(
        [NODE, str(RB_JS), 'find', query],
        capture_output=True, text=True, encoding='utf-8', errors='replace'
    )
    if result.returncode != 0:
        raise RuntimeError(f"rb.js error: {result.stderr.strip()}")
    return json.loads(result.stdout.strip() or '[]')


# ══════════════════════════════════════════════════════════════════════════════
# 2. READ BEAT GRID from ANLZ (PQTZ section)
# ══════════════════════════════════════════════════════════════════════════════

def read_beat_grid(anlz_path: Path) -> tuple[list[int], float]:
    """
    Read the PQTZ section from ANLZ0000.DAT.
    Returns (bar_times_ms, bpm), where bar_times are timestamps for each beat-1 position.
    """
    data = anlz_path.read_bytes()
    pmai_hdr_len = struct.unpack_from('>I', data, 4)[0]  # read dynamically
    pos = pmai_hdr_len  # child sections start after the PMAI header
    while pos < len(data) - 12:
        tag = data[pos:pos+4]
        hdr_len   = struct.unpack_from('>I', data, pos+4)[0]
        total_len = struct.unpack_from('>I', data, pos+8)[0]
        if tag == b'PQTZ':
            # BeatGrid: len_beats at section+20 (in header extension)
            # Beat entries start at pos+hdr_len (body start)
            len_beats  = struct.unpack_from('>I', data, pos+20)[0]
            entry_start = pos + hdr_len  # body starts here
            bar_times = []
            bpm = 0.0
            for i in range(len_beats):
                off = entry_start + i * 8
                beat_nr  = struct.unpack_from('>H', data, off)[0]
                tempo100 = struct.unpack_from('>H', data, off+2)[0]
                time_ms  = struct.unpack_from('>I', data, off+4)[0]
                if beat_nr == 1:
                    bar_times.append(time_ms)
                    if bpm == 0.0 and tempo100 > 0:
                        bpm = tempo100 / 100.0
            return bar_times, bpm
        if total_len < 12:
            break
        pos += total_len
        raise ValueError(f"No PQTZ section found in {anlz_path}")


# ══════════════════════════════════════════════════════════════════════════════
# 3. CUE DETECTION (mel cosine distance, same logic as analyze_snap.py)
# ══════════════════════════════════════════════════════════════════════════════

def detect_cues(audio_path: Path, bar_times: list[int], bpm: float) -> list[dict]:
    """
    Run analyze_snap.py and return the cue list.
    Format: [{"ms": 1234, "label": "Drop"}, ...]
    """
    payload = json.dumps({
        "path":      audio_path.as_posix(),
        "bar_times": bar_times,
        "bpm":       bpm,
    })
    result = subprocess.run(
        [PYTHON, str(HERE / 'analyze_snap.py'), payload],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"analyze_snap.py error:\n{result.stderr.strip()}")
    # Parse JSON output (last "JSON:..." line)
    for line in reversed(result.stdout.splitlines()):
        if line.startswith('JSON:'):
            return json.loads(line[5:])
    raise ValueError("No JSON output from analyze_snap.py")


# ══════════════════════════════════════════════════════════════════════════════
# 4. WRITE MASTER.DB (pyrekordbox)
# ══════════════════════════════════════════════════════════════════════════════

# Global DB session, opened once and reused for all tracks
_db_session = None
_DjmdContent = None
_DjmdCue = None

def _get_db_session():
    global _db_session, _DjmdContent, _DjmdCue
    if _db_session is None:
        import logging
        logging.disable(logging.WARNING)
        from pyrekordbox.db6 import Rekordbox6Database
        from pyrekordbox.db6.tables import DjmdContent, DjmdCue as _Cue
        _DjmdContent = DjmdContent
        _DjmdCue = _Cue
        _db_session = Rekordbox6Database(str(LOCAL_MASTER_DB)).session
    return _db_session, _DjmdContent, _DjmdCue


def write_cues_masterdb(file_path: str, cues: list[dict]) -> bool:
    """Write memory cues to master.db using a shared DB session."""
    try:
        session, DjmdContent, DjmdCue = _get_db_session()
    except Exception as e:
        print(f"  ⚠ master.db unavailable: {e}")
        return False

    norm_path = file_path.replace('\\', '/')
    filename = norm_path.split('/')[-1]
    # Prefer exact full-path match to avoid writing to wrong duplicate track
    content = session.query(DjmdContent).filter(DjmdContent.FolderPath == norm_path).first()
    if content is None:
        content = session.query(DjmdContent).filter(DjmdContent.FileNameL == filename).first()
    if content is None:
        print(f"  ⚠ Track not found in master.db: {filename}")
        return False

    content_id = content.ID
    now = datetime.now(timezone.utc)

    session.query(DjmdCue).filter(DjmdCue.ContentID == content_id).delete()
    for i, c in enumerate(cues):
        in_msec  = c['ms']
        in_frame = round(in_msec * 150 / 1000)
        import uuid
        cue = DjmdCue(
            ID               = str(random.randint(100_000_000, 2_000_000_000)),
            ContentID        = content_id,
            InMsec           = in_msec,
            InFrame          = in_frame,
            InMpegFrame      = 0,
            InMpegAbs        = 0,
            OutMsec          = -1,
            OutFrame         = 0,
            OutMpegFrame     = 0,
            OutMpegAbs       = 0,
            Kind             = 0,
            Color            = 255,
            ColorTableIndex  = 0,
            ActiveLoop       = 0,
            Comment          = c['label'],
            BeatLoopSize     = 0,
            CueMicrosec      = 0,
            InPointSeekInfo  = None,
            OutPointSeekInfo = None,
            ContentUUID      = content.UUID,
            UUID             = str(uuid.uuid4()),
            rb_local_deleted = 0,
            rb_local_synced  = 0,
            updated_at       = now,
            created_at       = now,
        )
        session.add(cue)

    content.CueUpdated = '1'
    content.updated_at = now
    session.commit()
    return True


# ══════════════════════════════════════════════════════════════════════════════
# 5. WRITE ANLZ (PCOB/PCPT binary)
# ══════════════════════════════════════════════════════════════════════════════

def _build_pcob(cues: list[dict]) -> bytes:
    """Build a PCOB section (memory cues, legacy DAT format) as bytes."""
    entries = b''
    for i, c in enumerate(cues):
        prev = 0xFFFF if i == 0 else i - 1
        entry = struct.pack('>4sIIIIHHHHHHII',
            b'PCPT',
            28,           # hdr_len
            56,           # total_len
            0,            # hot_cue (0 = memory)
            0,            # status
            1, 0,         # word1, word2
            prev,         # prev_idx
            i + 1,        # order (1-indexed)
            0x0100, 0x03E8,
            c['ms'],      # time_ms (big-endian u32)
            0xFFFFFFFF,   # no loop
        )
        entry += b'\x00' * 16
        entries += entry

    total = 24 + len(entries)
    header = struct.pack('>4sIIIII',
        b'PCOB',
        24,       # hdr_len
        total,    # total_len
        0,        # type=0 (memory cues)
        len(cues),
        3,        # extra constant
    )
    return header + entries


def _build_pcp2(cue: dict) -> bytes:
    """Build a PCP2 entry (108 bytes, newer EXT format)."""
    label_utf16 = cue['label'].encode('utf-16-be') + b'\x00\x00'
    str_byte_len = len(label_utf16)
    entry = struct.pack('>4sIIIHHIIIII',
        b'PCP2',
        16,           # hdr_len
        108,          # total_len (always 108, remainder zero-padded)
        0,            # unknown
        0x0100,       # const
        0x03E8,       # const
        cue['ms'],    # time_ms
        0xFFFFFFFF,   # loop_end (no loop)
        0x00010000,   # const
        0,            # zeros
        0,            # zeros
    )
    entry += struct.pack('>I', str_byte_len)
    entry += label_utf16
    entry += b'\x00' * (108 - len(entry))
    return entry


def _build_pco2(cues: list[dict]) -> bytes:
    """Build a PCO2 section (newer EXT format) as bytes."""
    entries = b''.join(_build_pcp2(c) for c in cues)
    n = len(cues)
    total = 20 + len(entries)
    header = struct.pack('>4sIIII',
        b'PCO2',
        20,        # hdr_len
        total,     # total_len
        0,         # field[12]
        n << 16,   # count in high 16 bits
    )
    return header + entries


def _rewrite_anlz(path: Path, cues: list[dict], new_section_tag: bytes, build_fn) -> None:
    """
    Generic ANLZ rewrite: replace sections with count>0 by new ones,
    while keeping empty placeholder sections (count=0).
    """
    data = path.read_bytes()
    pmai_hdr_len = struct.unpack_from('>I', data, 4)[0]
    pmai_hdr = bytearray(data[:pmai_hdr_len])
    pos = pmai_hdr_len
    sections = []
    while pos < len(data) - 12:
        tag = data[pos:pos+4]
        if tag == b'\x00\x00\x00\x00':
            break
        tlen = struct.unpack_from('>I', data, pos+8)[0]
        if tlen < 12:
            break
        if tag == new_section_tag:
            # Keep empty placeholders; skip populated sections
            if new_section_tag == b'PCOB':
                count = struct.unpack_from('>I', data, pos+16)[0]
            else:  # PCO2: count in the high 16 bits of field[16]
                count = struct.unpack_from('>I', data, pos+16)[0] >> 16
            if count > 0:
                pos += tlen
                continue
        sections.append(data[pos:pos+tlen])
        pos += tlen

    sections.append(build_fn(cues))
    body = b''.join(sections)
    struct.pack_into('>I', pmai_hdr, 8, pmai_hdr_len + len(body))
    path.write_bytes(bytes(pmai_hdr) + body)


def write_cues_anlz(anlz_path: Path, cues: list[dict]) -> None:
    """Write memory cues to ANLZ0000.DAT (PCOB) and ANLZ0000.EXT (PCO2)."""
    # DAT: legacy PCOB format (CDJ-compatible)
    _rewrite_anlz(anlz_path, cues, b'PCOB', _build_pcob)

    # EXT: newer PCO2 format (Rekordbox desktop + modern CDJs)
    ext_path = anlz_path.with_suffix('.EXT')
    if ext_path.exists():
        _rewrite_anlz(ext_path, cues, b'PCO2', _build_pco2)


# ══════════════════════════════════════════════════════════════════════════════
# 6. MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def process_track(track: dict, dry_run: bool = False) -> bool:
    """Process one track: analyze it and write cues. Returns True on success."""
    title    = track['title']
    artist   = track['artist']
    anlz_rel = track.get('analyzePath') or track.get('AnalysisDataPath', '')
    file_path = track.get('filePath') or track.get('FolderPath', '')

    if not anlz_rel or not file_path:
        print(f"  ✗ Missing ANLZ or file path for: {artist} – {title}")
        return False

    anlz_path  = resolve_anlz_path(anlz_rel)
    audio_path = resolve_audio_path(file_path)

    if not anlz_path:
        print(f"  ✗ ANLZ not found: {anlz_rel}")
        return False
    if not audio_path:
        print(f"  ✗ Audio file not found: {file_path}")
        return False

    print(f"\n🎵  {artist} – {title}")
    print(f"    ANLZ:  {anlz_path}")
    print(f"    Audio: {audio_path.name}")

    # Read beat grid
    try:
        bar_times, bpm = read_beat_grid(anlz_path)
        print(f"    Beat-Grid: {len(bar_times)} Bars, {bpm:.1f} BPM")
    except Exception as e:
        print(f"  ✗ Beat grid error: {e}")
        return False

    if len(bar_times) < 8:
        print(f"  ✗ Too few bars ({len(bar_times)}) - skipping track")
        return False

    # Detect cues
    try:
        cues = detect_cues(audio_path, bar_times, bpm)
        print(f"    Cues found: {len(cues)}")
        for c in cues:
            ms = c['ms']
            print(f"      {ms//60000}:{(ms%60000)/1000:05.2f}  {c['label']}")
    except Exception as e:
        print(f"  ✗ Analysis error: {e}")
        return False

    if dry_run:
        print("    [dry-run: nothing written]")
        return True

    # ANLZ backup (DAT + EXT)
    for suffix in ['.DAT', '.EXT']:
        orig = anlz_path.with_suffix(suffix)
        if orig.exists():
            bak = anlz_path.with_suffix(suffix + '.bak')
            if not bak.exists():
                shutil.copy2(orig, bak)

    # Write output
    try:
        write_cues_anlz(anlz_path, cues)
        print(f"    ✅ ANLZ written ({len(cues)} cues)")
    except Exception as e:
        print(f"  ✗ ANLZ write error: {e}")
        return False

    try:
        ok = write_cues_masterdb(file_path, cues)
        if ok:
            print(f"    ✅ master.db written")
    except Exception as e:
        print(f"  ⚠ master.db error: {e}")

    return True


def has_cues_anlz(anlz_path: Path) -> bool:
    """Check whether cues already exist in the ANLZ files (DAT + EXT)."""
    def _check(path: Path, tag: bytes, count_fn) -> bool:
        try:
            data = path.read_bytes()
            hdr_len = struct.unpack_from('>I', data, 4)[0]
            pos = hdr_len
            while pos < len(data) - 12:
                t = data[pos:pos+4]
                tlen = struct.unpack_from('>I', data, pos+8)[0]
                if t == tag and count_fn(data, pos) > 0:
                    return True
                if tlen < 12:
                    break
                pos += tlen
        except Exception:
            pass
        return False

    # DAT: PCOB, count at pos+16
    if _check(anlz_path, b'PCOB', lambda d, p: struct.unpack_from('>I', d, p+16)[0]):
        return True
    # EXT: PCO2, count in high 16 bits of field at pos+16
    ext = anlz_path.with_suffix('.EXT')
    if ext.exists() and _check(ext, b'PCO2', lambda d, p: struct.unpack_from('>I', d, p+16)[0] >> 16):
        return True
    return False


def has_cues_masterdb(file_path: str) -> bool:
    """Check whether a track already has cues in master.db, the primary desktop source."""
    try:
        session, DjmdContent, DjmdCue = _get_db_session()
        filename = file_path.replace('\\', '/').split('/')[-1]
        content = session.query(DjmdContent).filter(DjmdContent.FileNameL == filename).first()
        if content is None:
            return False
        return session.query(DjmdCue).filter(DjmdCue.ContentID == content.ID).count() > 0
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
# 7. CLI ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def cmd_single(query: str, dry_run: bool = False):
    tracks = find_tracks_pdb(query)
    if not tracks:
        print(f"No track found for: \"{query}\"")
        sys.exit(1)
    if len(tracks) > 1:
        print(f"{len(tracks)} matches:")
        for t in tracks:
            print(f"  [{t['id']:5}] {t['artist']} – {t['title']}")
        print("\nPlease refine the query or use --id <ID>.")
        sys.exit(1)
    process_track(tracks[0], dry_run=dry_run)


def cmd_by_id(track_id: int, dry_run: bool = False):
    tracks = find_tracks_pdb(str(track_id))
    match = next((t for t in tracks if t['id'] == track_id), None)
    if not match:
        print(f"Track ID {track_id} not found.")
        sys.exit(1)
    process_track(match, dry_run=dry_run)


def _load_all_local_tracks() -> list[dict]:
    """Load all tracks from the local Rekordbox master.db, including UUID-based ANLZ tracks."""
    try:
        import logging
        logging.disable(logging.WARNING)
        from pyrekordbox import Rekordbox6Database
        db = Rekordbox6Database(str(LOCAL_MASTER_DB))
        tracks = []
        for c in db.get_content():
            adp = c.AnalysisDataPath or ''
            if not adp:
                continue
            tracks.append({
                'id':          c.ID,
                'title':       c.Title or '',
                'artist':      c.ArtistName or '',
                'analyzePath': adp,
                'filePath':    c.FolderPath or '',
            })
        return tracks
    except Exception as e:
        print(f"  ⚠ Local master.db could not be read: {e}")
        return []


def cmd_all(dry_run: bool = False, list_only: bool = False):
    """Process all tracks without cues from export.pdb and the local master.db."""
    # Tracks from export.pdb (export drive, numeric paths)
    result = subprocess.run(
        [NODE, str(RB_JS), 'find', ''],
        capture_output=True, text=True, encoding='utf-8', errors='replace'
    )
    pdb_tracks = json.loads(result.stdout.strip() or '[]')

    # Tracks from local master.db (UUID paths, not yet exported)
    local_tracks = _load_all_local_tracks()

    # Deduplicate local tracks that are not present in the PDB by filename
    pdb_filenames = {t.get('filePath', '').replace('\\', '/').split('/')[-1].lower()
                     for t in pdb_tracks}
    extra_local = [
        t for t in local_tracks
        if t['filePath'].replace('\\', '/').split('/')[-1].lower() not in pdb_filenames
    ]

    all_tracks = pdb_tracks + extra_local

    without_cues = []
    for t in all_tracks:
        ap = t.get('analyzePath', '')
        if not ap:
            continue
        fp = t.get('filePath', '')
        if fp:
            # If the track is in master.db, that is the authoritative source for Rekordbox desktop
            try:
                session, DjmdContent, DjmdCue = _get_db_session()
                filename = fp.replace('\\', '/').split('/')[-1]
                content = session.query(DjmdContent).filter(DjmdContent.FileNameL == filename).first()
                if content is not None:
                    # Track found in master.db -> only the master.db cue count matters
                    cue_count = session.query(DjmdCue).filter(DjmdCue.ContentID == content.ID).count()
                    if cue_count > 0:
                        continue
                    without_cues.append(t)
                    continue
            except Exception:
                pass
        # Fallback: if the track is not in master.db, inspect the ANLZ files
        anlz_path = resolve_anlz_path(ap)
        if anlz_path and has_cues_anlz(anlz_path):
            continue
        without_cues.append(t)

    total = len(all_tracks)
    print(f"\n{len(without_cues)} tracks without cues (out of {total} total, {len(extra_local)} local-only)")

    if list_only:
        for t in without_cues:
            tid = t.get('id', '?')
            print(f"  [{str(tid):>10}] {t['artist']} – {t['title']}")
        return

    for i, t in enumerate(without_cues, 1):
        print(f"\n[{i}/{len(without_cues)}]", end='')
        process_track(t, dry_run=dry_run)


def cmd_playlist(playlist_name: str, dry_run: bool = False):
    """Process all tracks in a playlist based on master.db cue state."""
    import logging
    logging.disable(logging.WARNING)
    from pyrekordbox import Rekordbox6Database

    db = Rekordbox6Database(str(LOCAL_MASTER_DB))
    playlists = list(db.get_playlist())
    target = next((p for p in playlists if p.Name and p.Name.lower() == playlist_name.lower()), None)
    if not target:
        # Fuzzy fallback
        target = next((p for p in playlists if p.Name and playlist_name.lower() in p.Name.lower()), None)
    if not target:
        print(f"Playlist not found: {playlist_name!r}")
        available = [p.Name for p in playlists if p.Name]
        print(f"Available playlists: {available}")
        sys.exit(1)

    songs = list(target.Songs) if target.Songs else []
    print(f"Playlist: {target.Name!r} — {len(songs)} tracks")

    session, DjmdContent, DjmdCue = _get_db_session()
    need_cues = []
    for s in songs:
        c = s.Content
        cue_count = session.query(DjmdCue).filter(DjmdCue.ContentID == c.ID).count()
        if cue_count == 0:
            fp = c.FolderPath or ''
            adp = c.AnalysisDataPath or ''
            need_cues.append({
                'id':          c.ID,
                'title':       c.Title or '',
                'artist':      c.ArtistName or '',
                'analyzePath': adp,
                'filePath':    fp,
            })

    print(f"{len(need_cues)}/{len(songs)} tracks need cues\n")
    for i, t in enumerate(need_cues, 1):
        print(f"[{i}/{len(need_cues)}]", end='')
        process_track(t, dry_run=dry_run)


def main():
    args = sys.argv[1:]
    dry_run   = '--dry' in args or '--dry-run' in args
    list_only = '--list' in args

    if list_only:
        cmd_all(dry_run=True, list_only=True)
        return

    if '--all' in args:
        cmd_all(dry_run=dry_run)
        return

    if '--playlist' in args:
        idx = args.index('--playlist')
        cmd_playlist(args[idx + 1], dry_run=dry_run)
        return

    if '--id' in args:
        idx = args.index('--id')
        cmd_by_id(int(args[idx + 1]), dry_run=dry_run)
        return

    # Positional argument = search query
    query = ' '.join(a for a in args if not a.startswith('--'))
    if not query:
        print(__doc__)
        sys.exit(0)

    cmd_single(query, dry_run=dry_run)


if __name__ == '__main__':
    main()
