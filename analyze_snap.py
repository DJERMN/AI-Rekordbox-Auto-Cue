import librosa, numpy as np, json, sys, warnings
warnings.filterwarnings("ignore")

# Beat-Grid aus JSON (wird von Node.js übergeben)
data       = json.loads(sys.argv[1])
audio_path = data["path"]
bar_times  = data["bar_times"]   # ms jedes PQTZ-Bar-Anfangs (beat_nr==1)
bpm        = data["bpm"]         # z.B. 174.0 (PQTZ-Tempo)

print(f"Lade: {audio_path.split('/')[-1]}", flush=True)
y, sr    = librosa.load(audio_path, mono=True)
duration = librosa.get_duration(y=y, sr=sr)
hop      = 512

# ─── 1. Mel-Spektrum berechnen (128 Bins, dB-skaliert) ──────────────────────
mel    = librosa.feature.melspectrogram(y=y, sr=sr, hop_length=hop, n_mels=128)
mel_db = librosa.power_to_db(mel + 1e-8)  # (128, frames)

bar_times_s = [b / 1000.0 for b in bar_times]
frame_times = librosa.frames_to_time(np.arange(mel_db.shape[1]), sr=sr, hop_length=hop)

# ─── 2. Pro Bar: Durchschnitts-Spektrum berechnen ────────────────────────────
bar_vecs = []
for i, bt in enumerate(bar_times_s):
    t_end  = bar_times_s[i + 1] if i + 1 < len(bar_times_s) else bt + 1.5
    idx_s  = int(np.searchsorted(frame_times, bt))
    idx_e  = int(np.searchsorted(frame_times, t_end))
    if idx_e > idx_s:
        bar_vecs.append(mel_db[:, idx_s:idx_e].mean(axis=1))
    else:
        bar_vecs.append(bar_vecs[-1] if bar_vecs else np.zeros(128))

bar_vecs = np.array(bar_vecs)  # (N_bars, 128)

# ─── 3. Boundary-Score: Cosinus-Distanz Vorher vs. Nachher ──────────────────
# Vergleicht einen Block von `win_bars` Bars VOR vs. NACH jedem Takt.
# Hoher Score = das Klangbild wechselt stark = strukturelle Grenze.
win_bars = 4   # 4 PQTZ-Bars vor/nach = ~2 echte Bars
n        = len(bar_vecs)
boundary_score = np.zeros(n)

for i in range(win_bars, n - win_bars):
    before = bar_vecs[max(0, i - win_bars):i].mean(axis=0)
    after  = bar_vecs[i:min(n, i + win_bars)].mean(axis=0)
    norm_b = np.linalg.norm(before)
    norm_a = np.linalg.norm(after)
    if norm_b > 0 and norm_a > 0:
        boundary_score[i] = 1.0 - np.dot(before, after) / (norm_b * norm_a)

# ─── 4. Besten Grenzen auswählen mit Mindest-Abstand ────────────────────────
# Mindestabstand: 16 PQTZ-Bars (= ~8 echte Bars bei halbierten BPM)
min_gap = 16
selected_idx = [0]  # Bar 1 immer als Intro

sorted_idx = np.argsort(-boundary_score)  # absteigend nach Score
for bi in sorted_idx:
    if bi == 0:
        continue
    if all(abs(bi - si) >= min_gap for si in selected_idx):
        selected_idx.append(bi)
    if len(selected_idx) >= 7:
        break

selected_idx.sort()

# ─── 5. Energie-Level für Labels ────────────────────────────────────────────
rms    = librosa.feature.rms(y=y, hop_length=hop)[0]
e_vals = []
for i in selected_idx:
    t     = bar_times_s[i]
    f_idx = min(int(np.searchsorted(frame_times, t)), len(rms) - 1)
    e_vals.append(rms[f_idx])

e_max = max(e_vals) if e_vals else 1.0

def label(pos_ratio, e_norm, is_first, is_last):
    if is_first:      return "Intro"
    if pos_ratio > 0.85: return "Outro"
    if e_norm > 0.75: return "Drop"
    if e_norm > 0.40: return "Build"
    return "Verse"

print(f"\n=== {len(selected_idx)} Cues (Cosinus-Boundary-Snap) ===")
result = []
for rank, i in enumerate(selected_idx):
    ms    = bar_times[i]
    score = boundary_score[i]
    e     = e_vals[rank] / e_max
    pos   = (ms / 1000) / duration
    lbl   = label(pos, e, rank == 0, rank == len(selected_idx) - 1)
    m, s  = ms // 60000, (ms % 60000) / 1000
    print(f"  {m}:{s:04.1f}  Bar {i+1:3d}  {lbl:<8}  score:{score:.4f}")
    result.append({"ms": int(ms), "label": lbl})

print("\nJSON:" + json.dumps(result))
