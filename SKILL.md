---
name: Rekordbox
description: |
  Rekordbox Library Tool — liest die interne Rekordbox 6 Datenbank auf C: (pyrekordbox).
  Playlists anzeigen, Tracks mit BPM/Key/Rating ausgeben, Cue-Points generieren (autocue.py).

  Use when: Nutzer fragt nach Rekordbox-Playlists, Tracks, BPM, Camelot-Key, Ratings, Cue-Points, oder der Library-Datenbank.
  Don't use when: Nutzer fragt nach USB-Export (export.pdb) oder DJCity-Downloads (dafür /DJPlaylist).
license: MIT
metadata:
  author: baris
  version: "1.0.0"
---

# Rekordbox

Greift auf die **interne Rekordbox 6 Datenbank** zu — keine USB-Laufwerk nötig.

## Datenbankpfad

Wird automatisch von pyrekordbox gefunden:
```
C:\Users\baris\AppData\Roaming\Pioneer\rekordbox\master.db
```

## Python-Boilerplate (immer verwenden)

```python
import sys, io, logging
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
logging.disable(logging.WARNING)
from pyrekordbox.db6 import Rekordbox6Database
db = Rekordbox6Database()
```

## Wichtige Feldmappings

| Feld | Zugriff | Hinweis |
|---|---|---|
| Titel | `c.Title` | |
| Artist | `c.Artist.Name` | |
| BPM | `c.BPM / 100` | gespeichert als Integer × 100 |
| Key (Camelot) | `c.Key.ScaleName` | z.B. `2A`, `11B` |
| Rating | `c.Rating` | direkt als 1–5 (nicht 0–255) |
| PlaylistID | `s.PlaylistID` | zum Filtern |
| Reihenfolge | `s.TrackNo` | zum Sortieren |

## Befehle / Anwendungsfälle

### Alle Playlists auflisten

```python
playlists = db.get_playlist()
for pl in playlists:
    print(pl.Name)
```

### Playlist anzeigen (Tracks mit BPM, Key, Rating)

```python
playlists = db.get_playlist()
target = next((pl for pl in playlists if pl.Name == "PLAYLIST_NAME"), None)

songs = db.get_playlist_songs()
pl_songs = [s for s in songs if s.PlaylistID == target.ID]
pl_songs.sort(key=lambda x: x.TrackNo or 0)

print(f"{'#':>2} | {'Rating':6} | {'BPM':>5} | {'Key':>4} | Titel — Artist")
print("-" * 90)
for i, s in enumerate(pl_songs, 1):
    c = s.Content
    if c:
        stars = "★" * c.Rating + "☆" * (5 - c.Rating)
        bpm = round(c.BPM / 100, 1) if c.BPM else "?"
        key = c.Key.ScaleName if c.Key else "?"
        artist = c.Artist.Name if c.Artist else "?"
        print(f"{i:2} | {stars} | {bpm:>5} | {key:>4} | {c.Title} — {artist}")
```

### Cue-Points generieren (autocue.py)

Arbeitsverzeichnis: `C:\Users\baris\AI-Baris`

```bash
# Einzelnen Track (Dry-Run zum Testen)
python autocue.py "Track Name" --dry

# Ganze Playlist
python autocue.py --playlist "PLAYLIST_NAME" --dry

# Ganze Library
python autocue.py --all --dry

# Ohne --dry werden Cues direkt in die DB geschrieben
```

**Wichtig:** Vor echtem Schreiben immer `--dry` zuerst, um zu sehen was geändert wird.

### Tracks ohne Cues finden

```bash
python autocue.py --playlist "PLAYLIST_NAME" --dry
# Output zeigt: "X/14 Tracks brauchen Cues"
```

## Phrase-Analyse (PSSI aus EXT-Datei)

ANLZ-Pfad (lokal): `C:\Users\baris\AppData\Roaming\Pioneer\rekordbox\share\PIONEER\USBANLZ`

```python
from pathlib import Path
import pyrekordbox.anlz as anlz

LOCAL_ANLZ_BASE = Path(r'C:\Users\baris\AppData\Roaming\Pioneer\rekordbox\share\PIONEER\USBANLZ')

MOOD_LABELS = {
    0: {1:'Intro', 2:'Up', 3:'Down', 5:'Chorus', 6:'Outro', 8:'Bridge', 9:'Chorus', 10:'Outro'},  # Neu (v2 Format)
    1: {1:'Intro', 2:'Up', 3:'Down', 5:'Chorus', 6:'Outro'},                                       # High
    2: {1:'Intro', 2:'Verse 1', 3:'Verse 2', 4:'Verse 3', 5:'Verse 4',                            # Mid
        6:'Verse 5', 7:'Verse 6', 8:'Bridge', 9:'Chorus', 10:'Outro'},
    3: {1:'Intro', 2:'Verse 1', 3:'Verse 1', 4:'Verse 1', 5:'Verse 2',                            # Low
        6:'Verse 2', 7:'Verse 2', 8:'Bridge', 9:'Chorus', 10:'Outro'},
}
CHORUS_KINDS = {0: [5, 9], 1: [5], 2: [9], 3: [9]}  # Welche kind-Werte = Chorus pro Mood

# Beat-Grid aus DAT laden
sub = (c.AnalysisDataPath or '').lstrip('/').replace('PIONEER/USBANLZ/', '')
af_dat = anlz.AnlzFile.parse_file(str(LOCAL_ANLZ_BASE / sub))
beat_times = []
for tag in af_dat.tags:
    if type(tag).__name__ == 'PQTZAnlzTag':
        for e in tag.content.entries:
            beat_times.append(e.time)  # beat_times[i] = ms für Beat i+1

# Phrases laden — mit Fallback auf Raw-Parser für neue ANLZ v2 (mood=0, 24-Byte-Entries)
import struct

def parse_pssi_raw(ext_data, beat_times, mood_labels, chorus_kinds_map):
    pssi_pos    = ext_data.index(b'PSSI')
    header_len  = struct.unpack_from('>I', ext_data, pssi_pos+4)[0]
    total_len   = struct.unpack_from('>I', ext_data, pssi_pos+8)[0]
    num_entries = struct.unpack_from('>H', ext_data, pssi_pos+16)[0]
    mood        = ext_data[pssi_pos+28]
    labels      = mood_labels.get(mood, {})
    ck          = chorus_kinds_map.get(mood, [5])
    body        = ext_data[pssi_pos+header_len : pssi_pos+total_len]
    entry_size  = len(body) // num_entries if num_entries else 24
    phrases, chorus_fills = [], []
    for i in range(num_entries):
        e    = body[i*entry_size : (i+1)*entry_size]
        beat = struct.unpack_from('>H', e, 2)[0]
        kind = struct.unpack_from('>H', e, 4)[0]
        fill = e[9]
        ms   = beat_times[beat-1] if 0 < beat <= len(beat_times) else None
        label = labels.get(kind, f'?{kind}')
        if fill:
            if kind in ck:
                chorus_fills.append((ms, label))
        else:
            phrases.append((ms, label))
    return phrases, chorus_fills

ext_path = LOCAL_ANLZ_BASE / sub.replace('ANLZ0000.DAT', 'ANLZ0000.EXT')
try:
    af_ext = anlz.AnlzFile.parse_file(str(ext_path))
    phrases, chorus_fills = [], []
    for tag in af_ext.tags:
        if type(tag).__name__ == 'PSSIAnlzTag':
            mood   = tag.content.mood
            labels = MOOD_LABELS.get(mood, {})
            ck     = CHORUS_KINDS.get(mood, [5])
            for e in tag.content.entries:
                ms    = beat_times[e.beat - 1] if 0 < e.beat <= len(beat_times) else None
                label = labels.get(e.kind, f'?{e.kind}')
                if e.fill:
                    if e.kind in ck:
                        chorus_fills.append((ms, label))
                else:
                    phrases.append((ms, label))
except Exception:
    # Fallback: Raw-Parser für ANLZ v2 (pyrekordbox kennt Format nicht)
    phrases, chorus_fills = parse_pssi_raw(ext_path.read_bytes(), beat_times, MOOD_LABELS, CHORUS_KINDS)
```

**ANLZ-Versionen:** pyrekordbox kennt nur v1 (`0x01000002`). Tracks die mit neuem Rekordbox analysiert wurden haben v2 (`0x02000002`) mit 24-Byte-Entries statt 12 — Raw-Parser übernimmt automatisch.

## Hot Cues schreiben (Phrase-basierte Logik)

### Slot-Mapping

| Typ | master.db Kind | Beschreibung |
|-----|---------------|--------------|
| Memory Cue | 0 | Grauer Marker, kein Slot-Buchstabe |
| Hot Cue A | 1 | |
| Hot Cue B | 2 | |
| Hot Cue C | 3 | |
| Hot Cue D | 5 | |

### Memory Cues schreiben (optional, zusätzlich zu Hot Cues)

```python
def write_memory_cues(session, content, positions):
    # positions = [(ms, label), ...]
    # Bestehende Memory Cues löschen:
    session.query(DjmdCue).filter(DjmdCue.ContentID == content.ID, DjmdCue.Kind == 0).delete()
    now = datetime.now(timezone.utc)
    for ms, label in positions:
        session.add(DjmdCue(
            ID=str(random.randint(100_000_000, 2_000_000_000)),
            ContentID=content.ID,
            InMsec=ms, InFrame=round(ms * 150 / 1000),
            InMpegFrame=0, InMpegAbs=0,
            OutMsec=-1, OutFrame=0, OutMpegFrame=0, OutMpegAbs=0,
            Kind=0, Color=0, ColorTableIndex=0,
            ActiveLoop=0, Comment=label, BeatLoopSize=0, CueMicrosec=0,
            InPointSeekInfo=None, OutPointSeekInfo=None,
            ContentUUID=content.UUID, UUID=str(uuidmod.uuid4()),
            rb_local_deleted=0, rb_local_synced=0,
            updated_at=now, created_at=now,
        ))
    content.CueUpdated = '1'
    content.updated_at = now
    session.commit()
```

**Wichtig:** Memory Cues immer *zusätzlich* zu Hot Cues schreiben — nie stattdessen.
`Kind > 0` = Hot Cues, `Kind == 0` = Memory Cues. Beide unabhängig voneinander löschen/setzen.

### Auswahl-Logik (bewährt, getestet)

```python
THRESHOLD = 0.45  # C muss ab 45% der Songlänge liegen

def select_hot_cues(phrases, pqtz_entries, beat_times, chorus_fills=None):
    # phrases = non-fill phrases; chorus_fills = Chorus-Fill-Phrasen
    if chorus_fills is None:
        chorus_fills = []
    first_beat1 = next(e.time for e in pqtz_entries if e.beat == 1)
    A = (first_beat1, 'Intro')
    D = next((p for p in reversed(phrases) if 'Outro' in p[1]), phrases[-1])
    cutoff = beat_times[-1] * THRESHOLD  # 45% der Songlänge
    # Alle Choruses (fill + non-fill) chronologisch
    all_choruses = sorted(
        [p for p in phrases if 'Chorus' in p[1]] + chorus_fills,
        key=lambda x: x[0]
    )
    B = all_choruses[0] if all_choruses else None
    C = None
    if B:
        post_B_ch = [p for p in all_choruses if B[0] < p[0] < D[0]]
        post_B_nch = [p for p in phrases if B[0] < p[0] < D[0] and 'Chorus' not in p[1]]
        # 1. Erster Chorus in zweiter Hälfte (ab 45%) — Einstieg in zweiten Drop
        cl = [p for p in post_B_ch if p[0] >= cutoff]
        C = cl[0] if cl else None
        # 2. Erster Down/Bridge in zweiter Hälfte (ab 45%)
        if C is None:
            downs = [p for p in post_B_nch if any(x in p[1] for x in ['Down', 'Bridge']) and p[0] >= cutoff]
            C = downs[0] if downs else None
        # 3. Letzter Chorus gesamt (Fallback)
        if C is None:
            C = post_B_ch[-1] if post_B_ch else None
    return A, B, C, D
```

**Wichtige Regeln:**
- Fill-Phrases (`e.fill == True`) VOR der Auswahl herausfiltern
- A = erster `beat=1` im PQTZ, nicht `beat_times[0]` (kann Auftakt/beat=4 sein)
- C und D sollen in der zweiten Hälfte des Songs liegen (ab 45% der Länge)
- Letzten Down/Bridge nehmen, nicht ersten — maximale Spreizung
- Nur **master.db** schreiben — ANLZ-Dateien NICHT anfassen (zerstört Beat-Grid!)

### In master.db schreiben

```python
import random, uuid as uuidmod
from datetime import datetime, timezone
from pyrekordbox.db6.tables import DjmdCue

SLOT_KIND = {1:1, 2:2, 3:3, 4:5}

def write_hotcues_masterdb(session, content, cues):
    # cues = [{'slot': 1-4, 'ms': int, 'label': str}, ...]
    session.query(DjmdCue).filter(DjmdCue.ContentID == content.ID, DjmdCue.Kind > 0).delete()
    now = datetime.now(timezone.utc)
    for c in cues:
        in_msec = c['ms']
        session.add(DjmdCue(
            ID=str(random.randint(100_000_000, 2_000_000_000)),
            ContentID=content.ID,
            InMsec=in_msec, InFrame=round(in_msec * 150 / 1000),
            InMpegFrame=0, InMpegAbs=0,
            OutMsec=-1, OutFrame=0, OutMpegFrame=0, OutMpegAbs=0,
            Kind=SLOT_KIND[c['slot']], Color=255, ColorTableIndex=0,
            ActiveLoop=0, Comment=c['label'], BeatLoopSize=0, CueMicrosec=0,
            InPointSeekInfo=None, OutPointSeekInfo=None,
            ContentUUID=content.UUID, UUID=str(uuidmod.uuid4()),
            rb_local_deleted=0, rb_local_synced=0,
            updated_at=now, created_at=now,
        ))
    content.CueUpdated = '1'
    content.updated_at = now
    session.commit()
```

### Getestete Ergebnisse (Playlist: NightStart - Light Start)

| Track | Mood | A | B | C | D |
|-------|------|---|---|---|---|
| Rude Boy | High | Intro 0:00 | Chorus 0:29 | Chorus 3:10 (letzter) | Outro 3:37 |
| I Don't Care | High | Intro 0:00 | Chorus 0:18 | Down 2:49 (einziger) | Outro 3:45 |
| One Dance | Mid | Intro 0:00 | Chorus 0:09 | Bridge 1:34 | Outro 2:11 |
| Tequila | High | Intro 0:00 | Chorus 0:37 | Chorus 1:15 (letzter) | Outro 1:25 |
| RITMO | High | Intro 0:00 | Chorus 0:33 | Down 2:09 (letzter) | Outro 3:17 |

## Fehlerbehandlung

- **UnicodeEncodeError**: Immer den UTF-8 Boilerplate (`sys.stdout = io.TextIOWrapper(...)`) verwenden
- **AttributeError 'Tonality'**: Key über `c.Key.ScaleName` abrufen, nicht `c.Tonality`
- **`get_playlist_songs()` mit Argument**: Kein Argument übergeben — danach per `PlaylistID` filtern
- **Rating = 0 obwohl Sterne gesetzt**: Rating ist direkt 1–5, keine Division durch 51 nötig
- **`.3EX`-Datei**: MessagePack-Format mit AI-Embeddings (64-dim Vektoren) — KEIN Phrase-Daten, pyrekordbox wirft Fehler, ignorieren
- **PQTZ Beat-Indizierung**: Alle Entries sequenziell in `beat_times[]` sammeln; `beat_times[beat_nr - 1]` für Zeitstempel
- **ANLZ nie direkt schreiben**: Zerstört Beat-Grid (PQTZ). Nur master.db für Hot Cues verwenden.
- **EXT Parse-Fehler `u1 parsing expected 16777218 but parsed 33554434`**: ANLZ v2-Format (mood=0, 24-Byte-Entries). `parse_pssi_raw()` als Fallback verwenden — liest PSSI direkt aus Rohdaten ohne pyrekordbox-Versionscheck.
- **mood=0**: Neuer Mood-Typ in ANLZ v2. Kind-Mapping: 1=Intro, 5=Chorus, 6=Outro, 8=Bridge, 9=Chorus, 10=Outro. Chorus-Kinds: [5, 9].

## Beispiele

**Beispiel 1: Playlist anzeigen**
Nutzer: "zeig mir die Playlist NightStart - Light Start"
→ Boilerplate laden → Playlist per Name suchen → Songs filtern + sortieren → Tabelle ausgeben

**Beispiel 2: Cue-Points prüfen**
Nutzer: "welche Tracks in NightStart brauchen noch Cues?"
→ `python autocue.py --playlist "NightStart - Light Start" --dry`

**Beispiel 3: Alle Playlists**
Nutzer: "welche Playlists habe ich?"
→ `db.get_playlist()` → Namen ausgeben
