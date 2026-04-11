#!/usr/bin/env python3
"""
autocue.py - Automatische Memory-Cues fuer Rekordbox

Verwendung:
  python autocue.py "Trackname"        - Cues fuer einen Track setzen
  python autocue.py --id 12345         - Track per Rekordbox-ID
  python autocue.py --playlist "Name"  - Alle Tracks einer Playlist bearbeiten
  python autocue.py --all              - Alle Tracks ohne Cues bearbeiten
  python autocue.py --list             - Tracks ohne Cues auflisten
  python autocue.py --dry --all        - Analyse ohne Schreibzugriff

Konfiguration optional ueber Umgebungsvariablen:
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

# ── Konfiguration ────────────────────────────────────────────────────────────
PYTHON = sys.executable
NODE = 'node'
HERE = Path(__file__).parent
RB_JS = HERE / 'rb.js'

DEFAULT_DRIVE = os.environ.get('AUTOCUE_REKORDBOX_DRIVE', 'D:\\')
D_DRIVE = Path(DEFAULT_DRIVE)
DEFAULT_RB_ROOT = Path(os.environ.get('APPDATA', str(Path.home()))) / 'Pioneer' / 'rekordbox'
MASTER_DB = Path(os.environ.get('AUTOCUE_MASTER_DB', str(D_DRIVE / 'PIONEER' / 'Master' / 'master.db')))

# Lokaler ANLZ-Store fuer Tracks, die noch nicht auf das Export-Laufwerk geschrieben wurden.
LOCAL_ANLZ_BASE = Path(
    os.environ.get(
        'AUTOCUE_LOCAL_ANLZ_BASE',
        str(DEFAULT_RB_ROOT / 'share' / 'PIONEER' / 'USBANLZ'),
    )
)
# Lokale Rekordbox master.db als fuehrende Library-Datenbank.
LOCAL_MASTER_DB = Path(
    os.environ.get(
        'AUTOCUE_LOCAL_MASTER_DB',
        str(DEFAULT_RB_ROOT / 'master.db'),
    )
)


def resolve_anlz_path(anlz_rel: str) -> Path | None:
    """Löst einen ANLZ-Pfad auf — prüft zuerst D:, dann lokalen Share."""
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
    """Löst einen Audio-Dateipfad auf — unterstützt absolute & D:-relative Pfade."""
    if not file_path:
        return None
    # Absoluter Pfad (z.B. C:/Music/Track.wav)
    p = Path(file_path.replace('/', '\\'))
    if p.is_absolute() and p.exists():
        return p
    # Relativer Pfad (z.B. /Contents/...) → auf D: suchen
    rel_path = D_DRIVE / file_path.lstrip('/')
    if rel_path.exists():
        return rel_path
    return None

MAX_CUES  = 8    # max Memory Cues to write
WIN_BARS  = 4    # Bars vor/nach für Cosinus-Vergleich
MIN_GAP   = 16   # Mindestabstand in PQTZ-Bars


# ══════════════════════════════════════════════════════════════════════════════
# 1. TRACK-SUCHE via rb.js (export.pdb)
# ══════════════════════════════════════════════════════════════════════════════

def find_tracks_pdb(query: str) -> list[dict]:
    """Sucht Tracks in export.pdb via rb.js find-Kommando."""
    result = subprocess.run(
        [NODE, str(RB_JS), 'find', query],
        capture_output=True, text=True, encoding='utf-8', errors='replace'
    )
    if result.returncode != 0:
        raise RuntimeError(f"rb.js Fehler: {result.stderr.strip()}")
    return json.loads(result.stdout.strip() or '[]')


# ══════════════════════════════════════════════════════════════════════════════
# 2. BEAT-GRID lesen aus ANLZ (PQTZ-Section)
# ══════════════════════════════════════════════════════════════════════════════

def read_beat_grid(anlz_path: Path) -> tuple[list[int], float]:
    """
    Liest PQTZ-Section aus ANLZ0000.DAT.
    Gibt (bar_times_ms, bpm) zurück. bar_times = Timestamps aller Beat-1-Positionen.
    """
    data = anlz_path.read_bytes()
    pmai_hdr_len = struct.unpack_from('>I', data, 4)[0]  # dynamisch lesen
    pos = pmai_hdr_len  # Kind-Sections beginnen nach PMAI-Header
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
    raise ValueError(f"Keine PQTZ-Section gefunden in {anlz_path}")


# ══════════════════════════════════════════════════════════════════════════════
# 3. CUE-ERKENNUNG (Mel-Kosinus-Distanz, identisch zu analyze_snap.py)
# ══════════════════════════════════════════════════════════════════════════════

def detect_cues(audio_path: Path, bar_times: list[int], bpm: float) -> list[dict]:
    """
    Ruft analyze_snap.py auf und gibt Cue-Liste zurück.
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
        raise RuntimeError(f"analyze_snap.py Fehler:\n{result.stderr.strip()}")
    # Parse JSON output (last "JSON:..." line)
    for line in reversed(result.stdout.splitlines()):
        if line.startswith('JSON:'):
            return json.loads(line[5:])
    raise ValueError("Kein JSON-Output von analyze_snap.py")


# ══════════════════════════════════════════════════════════════════════════════
# 4. MASTER.DB schreiben (pyrekordbox)
# ══════════════════════════════════════════════════════════════════════════════

# Globale DB-Session (einmal geöffnet, für alle Tracks wiederverwendet)
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
    """Schreibt Memory Cues in master.db (DB-Session wird einmalig geöffnet)."""
    try:
        session, DjmdContent, DjmdCue = _get_db_session()
    except Exception as e:
        print(f"  ⚠ master.db nicht erreichbar: {e}")
        return False

    norm_path = file_path.replace('\\', '/')
    filename = norm_path.split('/')[-1]
    # Prefer exact full-path match to avoid writing to wrong duplicate track
    content = session.query(DjmdContent).filter(DjmdContent.FolderPath == norm_path).first()
    if content is None:
        content = session.query(DjmdContent).filter(DjmdContent.FileNameL == filename).first()
    if content is None:
        print(f"  ⚠ Track nicht in master.db gefunden: {filename}")
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
# 5. ANLZ schreiben (PCOB/PCPT Binary)
# ══════════════════════════════════════════════════════════════════════════════

def _build_pcob(cues: list[dict]) -> bytes:
    """Baut PCOB-Section (Memory Cues, älteres DAT-Format) als Bytes."""
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
    """Baut PCP2-Eintrag (108 Bytes, neues EXT-Format)."""
    label_utf16 = cue['label'].encode('utf-16-be') + b'\x00\x00'
    str_byte_len = len(label_utf16)
    entry = struct.pack('>4sIIIHHIIIII',
        b'PCP2',
        16,           # hdr_len
        108,          # total_len (immer 108, Rest zero-padded)
        0,            # unknown
        0x0100,       # const
        0x03E8,       # const
        cue['ms'],    # time_ms
        0xFFFFFFFF,   # loop_end (kein Loop)
        0x00010000,   # const
        0,            # zeros
        0,            # zeros
    )
    entry += struct.pack('>I', str_byte_len)
    entry += label_utf16
    entry += b'\x00' * (108 - len(entry))
    return entry


def _build_pco2(cues: list[dict]) -> bytes:
    """Baut PCO2-Section (neues EXT-Format) als Bytes."""
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
    Generisches ANLZ-Rewrite: ersetzt Sections mit count>0 durch neue,
    leere Platzhalter-Sections (count=0) bleiben erhalten.
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
            # Leere Platzhalter behalten; Sections mit Einträgen überspringen
            if new_section_tag == b'PCOB':
                count = struct.unpack_from('>I', data, pos+16)[0]
            else:  # PCO2: count in high 16 bits von field[16]
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
    """Schreibt Memory Cues in ANLZ0000.DAT (PCOB) und ANLZ0000.EXT (PCO2)."""
    # DAT: älteres PCOB-Format (CDJ-kompatibel)
    _rewrite_anlz(anlz_path, cues, b'PCOB', _build_pcob)

    # EXT: neues PCO2-Format (Rekordbox Desktop + moderne CDJs)
    ext_path = anlz_path.with_suffix('.EXT')
    if ext_path.exists():
        _rewrite_anlz(ext_path, cues, b'PCO2', _build_pco2)


# ══════════════════════════════════════════════════════════════════════════════
# 6. HAUPT-PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def process_track(track: dict, dry_run: bool = False) -> bool:
    """Verarbeitet einen Track: Analyse → Cues schreiben. Gibt True bei Erfolg."""
    title    = track['title']
    artist   = track['artist']
    anlz_rel = track.get('analyzePath') or track.get('AnalysisDataPath', '')
    file_path = track.get('filePath') or track.get('FolderPath', '')

    if not anlz_rel or not file_path:
        print(f"  ✗ Kein ANLZ- oder Dateipfad für: {artist} – {title}")
        return False

    anlz_path  = resolve_anlz_path(anlz_rel)
    audio_path = resolve_audio_path(file_path)

    if not anlz_path:
        print(f"  ✗ ANLZ nicht gefunden: {anlz_rel}")
        return False
    if not audio_path:
        print(f"  ✗ Audiodatei nicht gefunden: {file_path}")
        return False

    print(f"\n🎵  {artist} – {title}")
    print(f"    ANLZ:  {anlz_path}")
    print(f"    Audio: {audio_path.name}")

    # Beat-Grid lesen
    try:
        bar_times, bpm = read_beat_grid(anlz_path)
        print(f"    Beat-Grid: {len(bar_times)} Bars, {bpm:.1f} BPM")
    except Exception as e:
        print(f"  ✗ Beat-Grid Fehler: {e}")
        return False

    if len(bar_times) < 8:
        print(f"  ✗ Zu wenige Bars ({len(bar_times)}) — Track überspringen")
        return False

    # Cue-Erkennung
    try:
        cues = detect_cues(audio_path, bar_times, bpm)
        print(f"    Cues gefunden: {len(cues)}")
        for c in cues:
            ms = c['ms']
            print(f"      {ms//60000}:{(ms%60000)/1000:05.2f}  {c['label']}")
    except Exception as e:
        print(f"  ✗ Analyse Fehler: {e}")
        return False

    if dry_run:
        print("    [dry-run: nichts geschrieben]")
        return True

    # ANLZ Backup (DAT + EXT)
    for suffix in ['.DAT', '.EXT']:
        orig = anlz_path.with_suffix(suffix)
        if orig.exists():
            bak = anlz_path.with_suffix(suffix + '.bak')
            if not bak.exists():
                shutil.copy2(orig, bak)

    # Schreiben
    try:
        write_cues_anlz(anlz_path, cues)
        print(f"    ✅ ANLZ geschrieben ({len(cues)} Cues)")
    except Exception as e:
        print(f"  ✗ ANLZ Schreibfehler: {e}")
        return False

    try:
        ok = write_cues_masterdb(file_path, cues)
        if ok:
            print(f"    ✅ master.db geschrieben")
    except Exception as e:
        print(f"  ⚠ master.db Fehler: {e}")

    return True


def has_cues_anlz(anlz_path: Path) -> bool:
    """Prüft ob bereits Cues in den ANLZ-Dateien vorhanden sind (DAT + EXT)."""
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
    """Prüft ob ein Track bereits Cues in master.db hat (primäre Quelle für Rekordbox Desktop)."""
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
        print(f"Kein Track gefunden für: \"{query}\"")
        sys.exit(1)
    if len(tracks) > 1:
        print(f"{len(tracks)} Treffer:")
        for t in tracks:
            print(f"  [{t['id']:5}] {t['artist']} – {t['title']}")
        print("\nBitte genauer suchen oder --id <ID> verwenden.")
        sys.exit(1)
    process_track(tracks[0], dry_run=dry_run)


def cmd_by_id(track_id: int, dry_run: bool = False):
    tracks = find_tracks_pdb(str(track_id))
    match = next((t for t in tracks if t['id'] == track_id), None)
    if not match:
        print(f"Track ID {track_id} nicht gefunden.")
        sys.exit(1)
    process_track(match, dry_run=dry_run)


def _load_all_local_tracks() -> list[dict]:
    """Lädt alle Tracks aus dem lokalen Rekordbox master.db (inkl. neue UUID-ANLZ-Tracks)."""
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
        print(f"  ⚠ Lokales master.db nicht lesbar: {e}")
        return []


def cmd_all(dry_run: bool = False, list_only: bool = False):
    """Verarbeitet alle Tracks ohne Cues (D:-export.pdb + lokales master.db)."""
    # Tracks aus export.pdb (D: Drive, numerische Pfade)
    result = subprocess.run(
        [NODE, str(RB_JS), 'find', ''],
        capture_output=True, text=True, encoding='utf-8', errors='replace'
    )
    pdb_tracks = json.loads(result.stdout.strip() or '[]')

    # Tracks aus lokalem master.db (UUID-Pfade, noch nicht exportiert)
    local_tracks = _load_all_local_tracks()

    # Deduplizieren: lokale Tracks, die in PDB nicht vorhanden sind (per Dateiname)
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
            # Track ist in master.db → master.db ist maßgeblich für Rekordbox Desktop
            try:
                session, DjmdContent, DjmdCue = _get_db_session()
                filename = fp.replace('\\', '/').split('/')[-1]
                content = session.query(DjmdContent).filter(DjmdContent.FileNameL == filename).first()
                if content is not None:
                    # Track in master.db gefunden → nur cue-Count in master.db zählt
                    cue_count = session.query(DjmdCue).filter(DjmdCue.ContentID == content.ID).count()
                    if cue_count > 0:
                        continue
                    without_cues.append(t)
                    continue
            except Exception:
                pass
        # Fallback: Track nicht in master.db → ANLZ prüfen
        anlz_path = resolve_anlz_path(ap)
        if anlz_path and has_cues_anlz(anlz_path):
            continue
        without_cues.append(t)

    total = len(all_tracks)
    print(f"\n{len(without_cues)} Tracks ohne Cues (von {total} gesamt, davon {len(extra_local)} lokal)")

    if list_only:
        for t in without_cues:
            tid = t.get('id', '?')
            print(f"  [{str(tid):>10}] {t['artist']} – {t['title']}")
        return

    for i, t in enumerate(without_cues, 1):
        print(f"\n[{i}/{len(without_cues)}]", end='')
        process_track(t, dry_run=dry_run)


def cmd_playlist(playlist_name: str, dry_run: bool = False):
    """Verarbeitet alle Tracks einer Playlist (nach master.db Cue-Status)."""
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
        print(f"Playlist nicht gefunden: {playlist_name!r}")
        available = [p.Name for p in playlists if p.Name]
        print(f"Verfügbare Playlists: {available}")
        sys.exit(1)

    songs = list(target.Songs) if target.Songs else []
    print(f"Playlist: {target.Name!r} — {len(songs)} Tracks")

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

    print(f"{len(need_cues)}/{len(songs)} Tracks brauchen Cues\n")
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

    # Positionsargument = Suchbegriff
    query = ' '.join(a for a in args if not a.startswith('--'))
    if not query:
        print(__doc__)
        sys.exit(0)

    cmd_single(query, dry_run=dry_run)


if __name__ == '__main__':
    main()
