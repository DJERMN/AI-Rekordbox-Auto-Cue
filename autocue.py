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
# 3. PHRASE DETECTION from PSSI (Rekordbox phrase analysis)
# ══════════════════════════════════════════════════════════════════════════════

# Phrase label tables: _PHRASE_LABELS[mood][kind] → display label
# mood=1 High, mood=2 Mid, mood=3 Low
# Reference: https://pyrekordbox.readthedocs.io/en/stable/formats/anlz.html
_PHRASE_LABELS: dict[int, dict[int, str]] = {
    1: {  # High — Intro / Up / Down / Chorus / Outro
        1: "Intro",  2: "Up",     3: "Down",
        5: "Chorus", 6: "Outro",
        # fall-backs for any undocumented kind values
        4: "Down",   7: "Outro",  8: "Bridge", 9: "Chorus", 10: "Outro",
    },
    2: {  # Mid — Intro / Verse 1-6 / Bridge / Chorus / Outro
        1: "Intro",   2: "Verse 1", 3: "Verse 2", 4: "Verse 3",
        5: "Verse 4", 6: "Verse 5", 7: "Verse 6",
        8: "Bridge",  9: "Chorus",  10: "Outro",
    },
    3: {  # Low — Intro / Verse 1-2 / Bridge / Chorus / Outro
        1: "Intro",
        2: "Verse 1", 3: "Verse 1", 4: "Verse 1",
        5: "Verse 2", 6: "Verse 2", 7: "Verse 2",
        8: "Bridge",  9: "Chorus",  10: "Outro",
    },
}

# Priority order for cue selection when > MAX_CUES phrases are present
_PHRASE_PRIORITY = ["Intro", "Chorus", "Up", "Bridge", "Down",
                    "Outro", "Verse 1", "Verse 2", "Verse 3",
                    "Verse 4", "Verse 5", "Verse 6"]


def _build_beat_ms_lookup(anlz_path: Path) -> dict[int, int]:
    """Build a {beat_index (1-based): time_ms} dict from the PQTZ section in a DAT file."""
    data = anlz_path.read_bytes()
    pmai_hdr_len = struct.unpack_from('>I', data, 4)[0]
    pos = pmai_hdr_len
    while pos < len(data) - 12:
        tag       = data[pos:pos+4]
        hdr_len   = struct.unpack_from('>I', data, pos+4)[0]
        total_len = struct.unpack_from('>I', data, pos+8)[0]
        if tag == b'PQTZ':
            len_beats   = struct.unpack_from('>I', data, pos+20)[0]
            entry_start = pos + hdr_len
            return {
                i + 1: struct.unpack_from('>I', data, entry_start + i * 8 + 4)[0]
                for i in range(len_beats)
            }
        if total_len < 12:
            break
        pos += total_len
    return {}


def read_phrases_anlz(anlz_path: Path) -> list[dict] | None:
    """
    Read PSSI phrase data from the EXT file paired with anlz_path (DAT).
    Returns [{"ms": ..., "label": ...}] or None if no phrase data available.
    Caps results at MAX_CUES, preferring musically important phrase types.
    """
    ext_path = anlz_path.with_suffix('.EXT')
    if not ext_path.exists():
        return None
    try:
        import pyrekordbox.anlz as anlz_mod
        ext = anlz_mod.AnlzFile.parse_file(str(ext_path))
        if 'PSSI' not in ext.tag_types:
            return None

        content   = ext.get_tag('PSSI').content
        mood      = content.mood
        entries   = list(content.entries)
        if not entries:
            return None

        beat_ms   = _build_beat_ms_lookup(anlz_path)
        if not beat_ms:
            return None

        label_map = _PHRASE_LABELS.get(mood, _PHRASE_LABELS[2])
        cues: list[dict] = []
        for e in entries:
            ms = beat_ms.get(e.beat)
            if ms is None:
                continue
            label = label_map.get(e.kind, f"Phrase {e.kind}")
            cues.append({"ms": ms, "label": label})

        if not cues:
            return None

        # Trim to MAX_CUES: always keep Intro + Outro, then fill by priority
        if len(cues) > MAX_CUES:
            cues = _trim_phrases(cues)

        return cues

    except Exception as exc:
        print(f"    ⚠ Could not read PSSI: {exc}")
        return None


def _trim_phrases(cues: list[dict]) -> list[dict]:
    """
    Select up to MAX_CUES entries from a longer phrase list.
    Strategy: always keep Intro (first) and Outro (last labelled Outro),
    then fill remaining slots with the first occurrence of each label type,
    ordered by _PHRASE_PRIORITY, and finally by position.
    """
    if not cues:
        return cues

    kept: list[dict] = [cues[0]]          # always keep Intro
    outro = next((c for c in reversed(cues) if c['label'] == 'Outro'), None)
    if outro and outro is not cues[0]:
        kept.append(outro)

    remaining_slots = MAX_CUES - len(kept)
    candidates = [c for c in cues if c not in kept]

    # First pass: one representative per priority label
    seen_labels: set[str] = {c['label'] for c in kept}
    priority_picks: list[dict] = []
    for lbl in _PHRASE_PRIORITY:
        if remaining_slots <= 0:
            break
        if lbl in seen_labels:
            continue
        match = next((c for c in candidates if c['label'] == lbl), None)
        if match:
            priority_picks.append(match)
            seen_labels.add(lbl)
            remaining_slots -= 1

    # Second pass: fill with candidates — prefer new labels first,
    # then duplicate labels sorted by phrase priority (e.g. 2nd Chorus > 2nd Verse)
    used = [c for c in candidates if c not in priority_picks]
    extra_new  = [c for c in used if c['label'] not in seen_labels][:remaining_slots]
    remaining_slots -= len(extra_new)
    label_rank = {lbl: i for i, lbl in enumerate(_PHRASE_PRIORITY)}
    extra_dup  = sorted(
        (c for c in used if c not in extra_new),
        key=lambda c: label_rank.get(c['label'], 99)
    )[:remaining_slots]
    extra = extra_new + extra_dup

    all_selected = kept + priority_picks + extra
    all_selected.sort(key=lambda c: c['ms'])
    return all_selected[:MAX_CUES]


# ══════════════════════════════════════════════════════════════════════════════
# 4. CUE DETECTION fallback (mel cosine distance, same logic as analyze_snap.py)
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
# 5b. HOT CUE WRITING (A/B/C/D)
# ══════════════════════════════════════════════════════════════════════════════

# Slot assignment: A=Intro, B=build (last phrase before chorus), C=Chorus, D=Outro
_HOT_CUE_LABELS_A = {'Intro'}
_HOT_CUE_LABELS_B = {'Up', 'Down', 'Verse 1', 'Verse 2', 'Verse 3',
                      'Verse 4', 'Verse 5', 'Verse 6', 'Verse'}
_HOT_CUE_LABELS_C = {'Chorus', 'Up'}
_HOT_CUE_LABELS_D = {'Outro'}

# ANLZ hot_cue slot → master.db Kind mapping
# Rekordbox uses Kind=5 for D (Kind=4 is reserved, not a standard hot cue)
_SLOT_TO_KIND = {1: 1, 2: 2, 3: 3, 4: 5}

# master.db Kind → ColorTableIndex
_HOT_CUE_COLOR = {1: 36, 2: 22, 3: 22, 5: 22}  # A=green, B/C/D=orange


def select_hot_cues(cues: list[dict]) -> list[dict]:
    """
    Select up to 4 hot cue points from phrase cues.
    Returns cues with 'hot_cue' key (1=A, 2=B, 3=C, 4=D).

    Logic:
      A = first Intro phrase
      B = last build-type phrase BEFORE the first Chorus (pre-drop)
      C = first Chorus/Drop phrase
      D = first Outro phrase
    """
    first_chorus_ms = next(
        (c['ms'] for c in cues if c['label'] in _HOT_CUE_LABELS_C), None
    )

    slots: dict[int, dict] = {}

    # A: Intro
    intro = next((c for c in cues if c['label'] in _HOT_CUE_LABELS_A), None)
    if intro:
        slots[1] = {**intro, 'hot_cue': 1}

    # B: last build phrase before first Chorus
    build = next(
        (c for c in reversed(cues)
         if c['label'] in _HOT_CUE_LABELS_B
         and (first_chorus_ms is None or c['ms'] < first_chorus_ms)),
        None,
    )
    if build:
        slots[2] = {**build, 'hot_cue': 2}

    # C: first Chorus
    chorus = next((c for c in cues if c['label'] in _HOT_CUE_LABELS_C), None)
    if chorus:
        slots[3] = {**chorus, 'hot_cue': 3}

    # D: Outro
    outro = next((c for c in cues if c['label'] in _HOT_CUE_LABELS_D), None)
    if outro:
        slots[4] = {**outro, 'hot_cue': 4}

    return [v for _, v in sorted(slots.items())]


def has_hot_cues_anlz(anlz_path: Path) -> bool:
    """Return True if any type=1 hot cue section in DAT or EXT has cues."""
    def _check(path: Path, tag: bytes, is_pco2: bool) -> bool:
        try:
            data = path.read_bytes()
            hdr_len = struct.unpack_from('>I', data, 4)[0]
            pos = hdr_len
            while pos < len(data) - 12:
                t = data[pos:pos+4]
                if t == b'\x00\x00\x00\x00':
                    break
                tlen = struct.unpack_from('>I', data, pos+8)[0]
                if tlen < 12:
                    break
                if t == tag:
                    sec_type = struct.unpack_from('>I', data, pos+12)[0]
                    if sec_type == 1:
                        if is_pco2:
                            count = struct.unpack_from('>I', data, pos+16)[0] >> 16
                        else:
                            count = struct.unpack_from('>I', data, pos+16)[0]
                        if count > 0:
                            return True
                pos += tlen
        except Exception:
            pass
        return False

    if _check(anlz_path, b'PCOB', False):
        return True
    ext = anlz_path.with_suffix('.EXT')
    if ext.exists() and _check(ext, b'PCO2', True):
        return True
    return False


def has_hot_cues_masterdb(file_path: str) -> bool:
    """Return True if the track already has hot cues (Kind > 0) in master.db."""
    try:
        session, DjmdContent, DjmdCue = _get_db_session()
        norm = file_path.replace('\\', '/')
        content = (
            session.query(DjmdContent).filter(DjmdContent.FolderPath == norm).first()
            or session.query(DjmdContent).filter(
                DjmdContent.FileNameL == norm.split('/')[-1]
            ).first()
        )
        if content is None:
            return False
        return (
            session.query(DjmdCue)
            .filter(DjmdCue.ContentID == content.ID, DjmdCue.Kind > 0)
            .count() > 0
        )
    except Exception:
        return False


def _build_pcob_hot(hot_cues: list[dict]) -> bytes:
    """Build a PCOB type=1 section (hot cues, legacy DAT format)."""
    entries = b''
    for i, c in enumerate(hot_cues):
        prev = 0xFFFF if i == 0 else i - 1
        entry = struct.pack('>4sIIIIHHHHHHII',
            b'PCPT',
            28,               # hdr_len
            56,               # total_len
            c['hot_cue'],     # hot_cue slot (1=A, 2=B, 3=C, 4=D)
            0,                # status
            1, 0,             # word1, word2
            prev,             # prev_idx
            i + 1,            # order (1-indexed)
            0x0100, 0x03E8,
            c['ms'],          # time_ms
            0xFFFFFFFF,       # no loop
        )
        entry += b'\x00' * 16
        entries += entry

    total = 24 + len(entries)
    header = struct.pack('>4sIIIII',
        b'PCOB',
        24,                # hdr_len
        total,             # total_len
        1,                 # type=1 (hot cues)
        len(hot_cues),
        3,                 # extra constant
    )
    return header + entries


def _build_pcp2_hot(cue: dict) -> bytes:
    """Build a PCP2 hot cue entry (88 bytes, observed size for hot cues)."""
    # Include label if it fits within 88-byte budget
    # struct = 44 bytes; str_byte_len field = 4 bytes; label_utf16 + padding = 40 bytes max
    label = cue.get('label', '')
    label_utf16 = label.encode('utf-16-be') + b'\x00\x00'
    if len(label_utf16) > 40:           # shouldn't happen with our short labels
        label_utf16 = b'\x00\x00'
    str_byte_len = len(label_utf16)
    entry = struct.pack('>4sIIIHHIIIII',
        b'PCP2',
        16,               # hdr_len
        88,               # total_len (88 for hot cues, observed)
        cue['hot_cue'],   # slot (1=A, 2=B, 3=C, 4=D)
        0x0100,           # const
        0x03E8,           # const
        cue['ms'],        # time_ms
        0xFFFFFFFF,       # loop_end (no loop)
        0x00010000,       # const
        0,
        0,
    )
    entry += struct.pack('>I', str_byte_len)
    entry += label_utf16
    entry += b'\x00' * (88 - len(entry))
    return entry


def _build_pco2_hot(hot_cues: list[dict]) -> bytes:
    """Build a PCO2 type=1 section (hot cues, newer EXT format)."""
    entries = b''.join(_build_pcp2_hot(c) for c in hot_cues)
    n = len(hot_cues)
    total = 20 + len(entries)
    header = struct.pack('>4sIIII',
        b'PCO2',
        20,        # hdr_len
        total,     # total_len
        1,         # type=1 (hot cues)
        n << 16,   # count in high 16 bits
    )
    return header + entries


def _rewrite_anlz_hotcues(path: Path, tag: bytes, new_section: bytes) -> None:
    """
    Replace the type=1 hot cue section in a DAT/EXT file without touching
    the type=0 memory cue sections.
    """
    data = path.read_bytes()
    pmai_hdr_len = struct.unpack_from('>I', data, 4)[0]
    pmai_hdr = bytearray(data[:pmai_hdr_len])
    pos = pmai_hdr_len
    sections: list[bytes] = []
    replaced = False

    while pos < len(data) - 12:
        t = data[pos:pos+4]
        if t == b'\x00\x00\x00\x00':
            break
        tlen = struct.unpack_from('>I', data, pos+8)[0]
        if tlen < 12:
            break
        if t == tag:
            sec_type = struct.unpack_from('>I', data, pos+12)[0]
            if sec_type == 1 and not replaced:
                sections.append(new_section)
                replaced = True
                pos += tlen
                continue
        sections.append(data[pos:pos+tlen])
        pos += tlen

    if not replaced:
        sections.append(new_section)

    body = b''.join(sections)
    struct.pack_into('>I', pmai_hdr, 8, pmai_hdr_len + len(body))
    path.write_bytes(bytes(pmai_hdr) + body)


def write_hot_cues_anlz(anlz_path: Path, hot_cues: list[dict]) -> None:
    """Write hot cues to ANLZ0000.DAT (PCOB type=1) and .EXT (PCO2 type=1)."""
    _rewrite_anlz_hotcues(anlz_path, b'PCOB', _build_pcob_hot(hot_cues))

    ext_path = anlz_path.with_suffix('.EXT')
    if ext_path.exists():
        _rewrite_anlz_hotcues(ext_path, b'PCO2', _build_pco2_hot(hot_cues))


def write_hot_cues_masterdb(file_path: str, hot_cues: list[dict]) -> bool:
    """Add hot cue rows (Kind=1..4) to master.db without touching memory cues."""
    try:
        session, DjmdContent, DjmdCue = _get_db_session()
    except Exception as e:
        print(f"  ⚠ master.db unavailable: {e}")
        return False

    import uuid
    norm = file_path.replace('\\', '/')
    filename = norm.split('/')[-1]
    content = (
        session.query(DjmdContent).filter(DjmdContent.FolderPath == norm).first()
        or session.query(DjmdContent).filter(DjmdContent.FileNameL == filename).first()
    )
    if content is None:
        print(f"  ⚠ Track not found in master.db: {filename}")
        return False

    content_id = content.ID
    now = datetime.now(timezone.utc)

    # Remove any existing hot cues before re-writing
    session.query(DjmdCue).filter(
        DjmdCue.ContentID == content_id, DjmdCue.Kind > 0
    ).delete()

    for c in hot_cues:
        slot = c['hot_cue']                      # ANLZ slot 1/2/3/4
        kind = _SLOT_TO_KIND.get(slot, slot)     # master.db Kind (D=5, not 4)
        in_msec = c['ms']
        in_frame = round(in_msec * 150 / 1000)
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
            Kind             = kind,
            Color            = 255,
            ColorTableIndex  = _HOT_CUE_COLOR.get(kind, 22),
            ActiveLoop       = 0,
            Comment          = c.get('label', ''),
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
# 6. MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def process_track(track: dict, dry_run: bool = False, force: bool = False) -> bool:
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
    # audio_path check is deferred: PSSI path does not need the audio file

    print(f"\n🎵  {artist} – {title}")
    print(f"    ANLZ:  {anlz_path}")

    # ── 1. Try Rekordbox phrase analysis (PSSI) — no audio needed ────────────
    cues = read_phrases_anlz(anlz_path)
    if cues:
        print(f"    ✦ Phrases (PSSI): {len(cues)} cue(s)")
        for c in cues:
            ms = c['ms']
            print(f"      {ms//60000}:{(ms%60000)/1000:05.2f}  {c['label']}")
    else:
        # ── 2. Fallback: mel cosine analysis — requires audio file ────────────
        if not audio_path:
            print(f"  ✗ Audio file not found and no PSSI data: {file_path}")
            return False
        print(f"    Audio: {audio_path.name}")

        try:
            bar_times, bpm = read_beat_grid(anlz_path)
            print(f"    Beat-Grid: {len(bar_times)} bars, {bpm:.1f} BPM")
        except Exception as e:
            print(f"  ✗ Beat grid error: {e}")
            return False

        if len(bar_times) < 8:
            print(f"  ✗ Too few bars ({len(bar_times)}) — skipping")
            return False

        try:
            cues = detect_cues(audio_path, bar_times, bpm)
            print(f"    ✦ Mel analysis: {len(cues)} cue(s)")
            for c in cues:
                ms = c['ms']
                print(f"      {ms//60000}:{(ms%60000)/1000:05.2f}  {c['label']}")
        except Exception as e:
            print(f"  ✗ Analysis error: {e}")
            return False

    # ── Hot cue selection (A/B/C/D) ──────────────────────────────────────────
    hot_cues = select_hot_cues(cues)
    if hot_cues:
        print(f"    ✦ Hot Cues ({len(hot_cues)}):")
        slot_name = {1: 'A', 2: 'B', 3: 'C', 4: 'D'}
        for h in hot_cues:
            ms = h['ms']
            print(f"      [{slot_name[h['hot_cue']]}] {ms//60000}:{(ms%60000)/1000:05.2f}  {h['label']}")

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

    # Write memory cues
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

    # Write hot cues — only if none already exist
    if hot_cues:
        anlz_has_hc  = has_hot_cues_anlz(anlz_path)
        masterdb_has_hc = has_hot_cues_masterdb(file_path)
        if not force and (anlz_has_hc or masterdb_has_hc):
            print(f"    ⏭ Hot cues already set — skipping")
        else:
            try:
                write_hot_cues_anlz(anlz_path, hot_cues)
                print(f"    ✅ Hot cues written to ANLZ ({len(hot_cues)} slots)")
            except Exception as e:
                print(f"  ✗ Hot cue ANLZ write error: {e}")
            try:
                ok = write_hot_cues_masterdb(file_path, hot_cues)
                if ok:
                    print(f"    ✅ Hot cues written to master.db")
            except Exception as e:
                print(f"  ⚠ Hot cue master.db error: {e}")

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


def cmd_playlist(playlist_name: str, dry_run: bool = False, force: bool = False):
    """Process all tracks in a playlist.

    - force=False: skips tracks that already have hot cues; adds hot cues to
      tracks with memory cues but no hot cues; fully processes tracks without
      any cues.
    - force=True: re-analyses and overwrites memory cues AND hot cues for every
      track.
    """
    import logging
    logging.disable(logging.WARNING)
    from pyrekordbox import Rekordbox6Database

    db = Rekordbox6Database(str(LOCAL_MASTER_DB))
    playlists = list(db.get_playlist())
    target = next((p for p in playlists if p.Name and p.Name.lower() == playlist_name.lower()), None)
    if not target:
        target = next((p for p in playlists if p.Name and playlist_name.lower() in p.Name.lower()), None)
    if not target:
        print(f"Playlist not found: {playlist_name!r}")
        available = [p.Name for p in playlists if p.Name]
        print(f"Available playlists: {available}")
        sys.exit(1)

    songs = list(target.Songs) if target.Songs else []
    print(f"Playlist: {target.Name!r} — {len(songs)} tracks\n")

    session, DjmdContent, DjmdCue = _get_db_session()
    for i, s in enumerate(songs, 1):
        c = s.Content
        fp  = c.FolderPath or ''
        adp = c.AnalysisDataPath or ''
        track = {
            'id':          c.ID,
            'title':       c.Title or '',
            'artist':      c.ArtistName or '',
            'analyzePath': adp,
            'filePath':    fp,
        }

        cue_count = session.query(DjmdCue).filter(DjmdCue.ContentID == c.ID).count()
        if force or cue_count == 0:
            # Full process (re-analyse + overwrite memory cues + hot cues)
            print(f"[{i}/{len(songs)}]", end='')
            process_track(track, dry_run=dry_run, force=force)
        else:
            # Has memory cues → add hot cues only if missing
            anlz_path = resolve_anlz_path(adp) if adp else None
            if anlz_path and (has_hot_cues_anlz(anlz_path) or has_hot_cues_masterdb(fp)):
                continue  # already complete, silent skip
            # Try to add hot cues from PSSI
            if not anlz_path:
                continue
            cues = read_phrases_anlz(anlz_path)
            if not cues:
                continue
            hot_cues = select_hot_cues(cues)
            if not hot_cues:
                continue
            slot_name = {1: 'A', 2: 'B', 3: 'C', 4: 'D'}
            hc_str = ', '.join(f"[{slot_name[h['hot_cue']]}]{h['label']}" for h in hot_cues)
            print(f"[{i}/{len(songs)}] 🎵  {track['artist']} – {track['title']}")
            print(f"    + Hot Cues: {hc_str}")
            if not dry_run:
                try:
                    write_hot_cues_anlz(anlz_path, hot_cues)
                    write_hot_cues_masterdb(fp, hot_cues)
                    print(f"    ✅ hot cues written")
                except Exception as e:
                    print(f"    ✗ {e}")


def cmd_playlist_hotcues(playlist_name: str, dry_run: bool = False):
    """Add hot cues (A/B/C/D) to playlist tracks that have PSSI but no hot cues yet."""
    import logging
    logging.disable(logging.WARNING)
    from pyrekordbox import Rekordbox6Database

    db = Rekordbox6Database(str(LOCAL_MASTER_DB))
    playlists = list(db.get_playlist())
    target = next((p for p in playlists if p.Name and p.Name.lower() == playlist_name.lower()), None)
    if not target:
        target = next((p for p in playlists if p.Name and playlist_name.lower() in p.Name.lower()), None)
    if not target:
        print(f"Playlist not found: {playlist_name!r}")
        sys.exit(1)

    songs = list(target.Songs) if target.Songs else []
    print(f"Playlist: {target.Name!r} — {len(songs)} tracks\n")

    ok = skip = err = 0
    for i, s in enumerate(songs, 1):
        c = s.Content
        title  = c.Title or ''
        artist = c.ArtistName or ''
        fp     = c.FolderPath or ''
        adp    = c.AnalysisDataPath or ''

        if not adp:
            print(f"[{i}/{len(songs)}] ⚠ No ANLZ path — {artist} – {title}")
            err += 1
            continue

        anlz_path = resolve_anlz_path(adp)
        if not anlz_path:
            print(f"[{i}/{len(songs)}] ⚠ ANLZ not found — {artist} – {title}")
            err += 1
            continue

        # Skip if hot cues already set
        if has_hot_cues_anlz(anlz_path) or has_hot_cues_masterdb(fp):
            print(f"[{i}/{len(songs)}] ⏭ Already has hot cues — {artist} – {title}")
            skip += 1
            continue

        # Derive hot cues from PSSI
        cues = read_phrases_anlz(anlz_path)
        if not cues:
            print(f"[{i}/{len(songs)}] ⚠ No PSSI phrases — {artist} – {title}")
            err += 1
            continue

        hot_cues = select_hot_cues(cues)
        if not hot_cues:
            print(f"[{i}/{len(songs)}] ⚠ No hot cues selectable — {artist} – {title}")
            err += 1
            continue

        slot_name = {1: 'A', 2: 'B', 3: 'C', 4: 'D'}
        hc_str = ', '.join(f"[{slot_name[h['hot_cue']]}]{h['label']}" for h in hot_cues)
        print(f"[{i}/{len(songs)}] 🎵  {artist} – {title}")
        print(f"    Hot Cues: {hc_str}")

        if dry_run:
            print("    [dry-run]")
            ok += 1
            continue

        try:
            write_hot_cues_anlz(anlz_path, hot_cues)
            write_hot_cues_masterdb(fp, hot_cues)
            print(f"    ✅ written")
            ok += 1
        except Exception as e:
            print(f"    ✗ {e}")
            err += 1

    print(f"\n✅ {ok} written  ⏭ {skip} skipped  ✗ {err} errors")


def main():
    args = sys.argv[1:]
    dry_run   = '--dry' in args or '--dry-run' in args
    force     = '--force' in args
    list_only = '--list' in args

    if list_only:
        cmd_all(dry_run=True, list_only=True)
        return

    if '--all' in args:
        cmd_all(dry_run=dry_run)
        return

    if '--playlist' in args:
        idx = args.index('--playlist')
        cmd_playlist(args[idx + 1], dry_run=dry_run, force=force)
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
