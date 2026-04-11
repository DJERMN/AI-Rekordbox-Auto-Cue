"""Restore all .bak files back to .DAT and .EXT for playlist tracks."""
import sys, shutil, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from autocue import _get_db_session, resolve_anlz_path

playlist_name = "DJ_Set_CC_Bass_Beat"

try:
    session, DjmdContent, DjmdCue = _get_db_session()
    from pyrekordbox.db6 import DjmdPlaylist, DjmdSongPlaylist
except Exception as e:
    print(f"DB error: {e}")
    sys.exit(1)

playlist = session.query(DjmdPlaylist).filter(DjmdPlaylist.Name == playlist_name).first()
if not playlist:
    print(f"Playlist not found: {playlist_name}")
    sys.exit(1)

songs = session.query(DjmdSongPlaylist).filter(DjmdSongPlaylist.PlaylistID == playlist.ID).all()
print(f"Restoring backups for playlist '{playlist_name}' — {len(songs)} tracks\n")

restored = 0
for sp in songs:
    c = session.query(DjmdContent).filter(DjmdContent.ID == sp.ContentID).first()
    if not c or not c.AnalysisDataPath:
        continue
    anlz = resolve_anlz_path(c.AnalysisDataPath)
    if not anlz:
        continue
    for suffix in ['.DAT', '.EXT']:
        orig = anlz.with_suffix(suffix)
        bak  = anlz.with_suffix(suffix + '.bak')
        if bak.exists():
            shutil.copy2(bak, orig)
            print(f"  ✅ restored {orig.name}  ← {bak.name}")
            restored += 1
        else:
            print(f"  ⚠ no backup for {orig}")

print(f"\nRestored {restored} files.")
