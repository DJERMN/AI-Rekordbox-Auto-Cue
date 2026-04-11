# autocue

Automatic Rekordbox memory-cue generation based on beatgrid and audio analysis.

The project combines:

- `autocue.py` for the main pipeline
- `analyze_snap.py` for structural audio analysis
- `rb.js` as a helper for reading `export.pdb`

## Requirements

- Python 3.11+
- Node.js 20+
- A Rekordbox library with access to `export.pdb`, `master.db`, and the `USBANLZ` files

## Installation

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
npm install
```

## Configuration

Optional environment variables:

- `AUTOCUE_REKORDBOX_DRIVE` - default: `D:\`
- `AUTOCUE_PDB` - path to `export.pdb`
- `AUTOCUE_MASTER_DB` - path to the exported `master.db`
- `AUTOCUE_LOCAL_MASTER_DB` - path to the local Rekordbox `master.db`
- `AUTOCUE_LOCAL_ANLZ_BASE` - path to the local `USBANLZ` directory

`rb.js` reads `AUTOCUE_PDB` and otherwise falls back to `D:\PIONEER\rekordbox\export.pdb`.

## Usage

```powershell
python autocue.py "Track Name"
python autocue.py --id 12345
python autocue.py --playlist "My Playlist"
python autocue.py --all
python autocue.py --list
python autocue.py --dry --playlist "My Playlist"
```

## Note

The script writes to Rekordbox analysis files and to `master.db`. Back up your library before using it in production.
