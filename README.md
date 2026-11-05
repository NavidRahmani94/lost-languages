# Lost Languages

*Every language has a music before it has a meaning.*

An interactive audiovisual installation for **Creative Programming and Computing** (Politecnico di Milano).
A visitor speaks a sentence in their mother tongue. The system ignores *what* they say and listens only to
*how* they say it — the melody, rhythm and colour of the voice — and turns it into a piece of music and a
line of generative calligraphy. Every voice is kept in an archive; together they form a collective,
many-language score.

```
 microphone ─► Python (librosa prosody analysis) ──OSC :57120──► SuperCollider  (melody, breath, drone, granular voice)
                                                 └─OSC :12000──► Processing     (generative calligraphy)
```

## The app (recommended)

**Double-click the `Lost Languages` shortcut on the Desktop** (or `Lost Languages.bat` in this folder) or run `python app/lost_languages_app.py`.
One window holds everything: record button, writing direction, the calligraphy page, and a
*speech → music* view that shows the pitch contour, syllables and the resulting notes with a
moving playhead. SuperCollider is started invisibly in the background (dedicated server on port
57190); if it is not installed, a built-in Python synth is used instead. The status dot in the
bottom-left corner says which engine is playing.

## Folder structure

```
lost_languages/
├── app/lost_languages_app.py         the desktop app (UI + hidden engines)
├── python/lost_languages.py           analysis + OSC sender (record / file / archive)
├── supercollider/lost_languages.scd   sound engine (started hidden by the app)
├── assets/icon.ico                    app icon
├── archive/                           every recorded voice (.wav + .json), created automatically
└── requirements.txt
```

## Setup

1. **Python 3.9+**
   ```
   pip install -r requirements.txt
   ```
   (`sounddevice` needs PortAudio — included on macOS/Windows wheels; on Linux `sudo apt install libportaudio2`.)
2. **SuperCollider** 3.12+ — nothing to install.
3. **Processing 4** — install the **oscP5** library: *Sketch → Import Library → Manage Libraries → "oscP5"*.

## Run (in this order)

1. **SuperCollider**: open `supercollider/lost_languages.scd`, put the cursor inside the big `( … )` block,
   press **Cmd/Ctrl + Enter**. Wait for `LOST LANGUAGES sound engine ready`.
2. **Processing**: open `processing/LostLanguages/LostLanguages.pde`, press **Run**.
   (Press **D** to test the visuals alone with a fake voice.)
3. **Python**:
   ```
   cd python
   python lost_languages.py record --seconds 8 --lang fa --dir rtl
   ```

### Commands

| Command | What it does |
|---|---|
| `python lost_languages.py record --seconds 8 --lang it` | Record from the mic, analyse, perform, save to archive |
| `python lost_languages.py file voice.wav --lang zh --dir vert` | Use an existing recording |
| `python lost_languages.py archive` | Replay every archived voice — the collective piece |
| `... --no-osc --plot` | Analysis only + saves a pitch/energy/brightness plot |

### Options

| Option | Default | Meaning |
|---|---|---|
| `--lang` | `unknown` | Label written next to the line (use Latin letters: `fa`, `persian`, `zh`…) |
| `--dir` | `ltr` | Calligraphy direction: `ltr`, `rtl` (Persian, Arabic, Hebrew), `vert` (Chinese, Japanese) |
| `--stretch` | `1.4` | Slows the performance down; >1 sounds more musical |
| `--exaggerate` | `1.8` | Widens speech intervals into melody intervals |
| `--verbose` | off | Print every note as it is sent |

### Processing keys
`D` demo · `1/2/3` LTR / RTL / vertical · `C` clear · `F` fade (palimpsest) · `S` save PNG

## How speech becomes music and ink

| Speech feature (librosa) | Sound (SuperCollider) | Ink (Processing) |
|---|---|---|
| Pitch contour (`pyin`) | Melody, exaggerated and quantised to the voice's own mode | Height of the line |
| Syllables (energy peaks) | Note onsets → rhythm | Ink drops on stressed syllables |
| Energy (RMS) | Note loudness | Brush thickness |
| Voiced / unvoiced | Tone vs. filtered noise (consonants, breath) | Brush stroke vs. dry-brush spray |
| Spectral centroid | FM brightness, filter | Ink hue (indigo → crimson) |
| Pauses | Silence over the drone | Pen lifted, blank space |
| The recording itself | Granular layer: *who* is speaking, never *what* | — |
| Speaker's centre pitch | Drone root | — |

**The voice's own mode:** the analysis measures which pitch classes the speaker actually uses and picks
the closest musical mode (pentatonic, Dorian, Phrygian, Hijaz, Hirajoshi…). The terminal tells each
visitor which mode their voice "sings" in.

## Tips
- Use a headset or lavalier mic; laptop mics in a noisy room confuse the pitch tracker.
- 5–10 seconds of natural speech works best; a lullaby, a proverb or a memory is more musical than a list of words.
- If the line looks flat, raise `--exaggerate`; if the music is too fast, raise `--stretch`.

## Roadmap
- [ ] Real-time version (stream analysis while the visitor is still speaking)
- [ ] Endangered-language archive layer (open datasets, e.g. Mozilla Common Voice)
- [ ] Exhibition mode: push-to-talk button, full-screen, automatic archive replay when idle
