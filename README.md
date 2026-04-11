# autocue

Automatisches Setzen von Rekordbox-Memory-Cues anhand von Beatgrid- und Audio-Analyse.

Das Projekt kombiniert:

- `autocue.py` fuer die Hauptpipeline
- `analyze_snap.py` fuer die strukturelle Audio-Analyse
- `rb.js` als Helper zum Lesen von `export.pdb`

## Voraussetzungen

- Python 3.11+
- Node.js 20+
- Rekordbox-Library mit Zugriff auf `export.pdb`, `master.db` und die `USBANLZ`-Dateien

## Installation

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
npm install
```

## Konfiguration

Optional ueber Umgebungsvariablen:

- `AUTOCUE_REKORDBOX_DRIVE` - Standard: `D:\`
- `AUTOCUE_PDB` - Pfad zu `export.pdb`
- `AUTOCUE_MASTER_DB` - Pfad zur exportierten `master.db`
- `AUTOCUE_LOCAL_MASTER_DB` - Pfad zur lokalen Rekordbox-`master.db`
- `AUTOCUE_LOCAL_ANLZ_BASE` - Pfad zum lokalen `USBANLZ`-Ordner

`rb.js` liest `AUTOCUE_PDB` und faellt sonst auf `D:\PIONEER\rekordbox\export.pdb` zurueck.

## Verwendung

```powershell
python autocue.py "Trackname"
python autocue.py --id 12345
python autocue.py --playlist "Meine Playlist"
python autocue.py --all
python autocue.py --list
python autocue.py --dry --playlist "Meine Playlist"
```

## Hinweis

Das Script schreibt in Rekordbox-Analyse-Dateien und in `master.db`. Vor produktivem Einsatz sollte eine Sicherung der Library vorhanden sein.
