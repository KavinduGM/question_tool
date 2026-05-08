# Voice Clone + Question Tool

A self-hosted web tool for cloning voices from an audio sample and generating
high-quality English text-to-speech from any length of text, using
[Coqui XTTS v2](https://github.com/coqui-ai/TTS) (open-source, free).

Features:
- Clone a voice from a short reference clip (10s to 20min; 30–60s is ideal)
- Persistent voice library with unique voice IDs
- Generate speech from unlimited text (auto chunked + stitched)
- Speed control (0.5x – 2.0x)
- WAV / MP3 output
- Multi-document DOCX queue (Format A flat, Format B with `Animation N` subfolders, with `Voice Name:` filename overrides for Format A)
- **Quiz question generator**: drop a `.docx` of `Question N` blocks (question body, four `a./b./c./d.` answers, `Correct Answer:`, `Explanation:`, three `Option X is incorrect …`) and the tool synthesises four voices per question, merges them with a configurable inter-section pause, and saves one `Question N.<ext>` file per question into a fresh per-doc subfolder.
- Web UI + JSON API (X-API-Key auth)
- Runs locally or on any server

---

## Requirements

- **Python 3.10 or 3.11** (XTTS does not yet support 3.12+)
- **ffmpeg** (used to normalize reference audio)
- **GPU strongly recommended** — NVIDIA RTX 4060 will generate faster than real time. CPU works but is slow (~3–5x slower than real time).

On macOS:
```bash
brew install ffmpeg python@3.11
```

On Ubuntu / Hostinger VPS:
```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv ffmpeg
```

---

## Install & run — Windows (RTX 4060 PC, recommended)

This is the fastest path to "installed software" on your gaming PC.

### One-time setup
1. Install **Python 3.11** from <https://www.python.org/downloads/release/python-3119/> — during install, tick **"Add python.exe to PATH"** and **"py launcher"**.
2. Copy the whole `Voice Clone Tool` folder to the PC (e.g. `C:\Tools\Voice Clone Tool`).
3. Double-click **`setup.bat`**.
   It will:
   - Verify Python
   - Install ffmpeg via winget (or prompt you)
   - Create a local `.venv`
   - Install **PyTorch with CUDA 12.1** (uses your RTX 4060 GPU)
   - Install all other dependencies
   - Download the XTTS v2 model (~1.8 GB, one time)
   - Create a **"Voice Clone Tool" shortcut on your Desktop**

This takes 10–20 minutes depending on your internet speed. Only needed once.

### Daily use
Double-click the **"Voice Clone Tool"** shortcut on your desktop (or `start.bat`).
A terminal window opens, and your browser automatically navigates to
<http://127.0.0.1:8000>. Close the terminal window to stop the server.

### Exposing it to the internet (for your API integrations)
Run the tool, then in a second terminal use a tunnel:
```powershell
# Cloudflare Tunnel (free, recommended)
winget install Cloudflare.cloudflared
cloudflared tunnel --url http://127.0.0.1:8000
```
This prints a public `https://*.trycloudflare.com` URL that proxies to your PC.
Combine with `set API_KEY=your-secret-key` before `start.bat` to lock access.

---

## Install & run — macOS / Linux

```bash
cd "Voice Clone Tool"
./run.sh
```

First launch will:
1. Create a `.venv`
2. Install dependencies (Torch, TTS, FastAPI…)
3. Download the XTTS v2 model on first generation (~1.8 GB, one time)

Then open <http://localhost:8000>.

### Manual run
```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export COQUI_TOS_AGREED=1
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## Hosting

### Recommended: run on the RTX 4060 PC

Your Hostinger KVM2 VPS has **no GPU**, so CPU generation there will be slow.
For best results, run the backend on your RTX 4060 PC and expose it via
Cloudflare Tunnel or Tailscale:

```bash
# on RTX 4060 PC
./run.sh
# then (example) cloudflared tunnel --url http://localhost:8000
```

### Alternative: run on the VPS (CPU only)

It still works — just slower. Install with the CPU wheel:
```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
```
Consider running behind nginx + systemd for production.

---

## API keys & remote access

The tool has **built-in API key management** via the web UI. This lets external
tools running on other machines hit your GPU PC to generate audio.

### Architecture

```
 [Your batch tool on PC #2] ──HTTPS──► [Cloudflare Tunnel] ──► [RTX 4060 PC :8000]
     sends X-API-Key header                                      generates audio
```

### 1. Create an API key

1. Launch the tool on your RTX 4060 PC (double-click the desktop shortcut).
2. Open the UI in your browser — the first time, auth is **disabled** so you can bootstrap.
3. Scroll to **"4. API keys"**, enter a name (e.g. `batch-tool`), click **Create key**.
4. The full key is shown **once** — copy it immediately. It looks like `vct_AbCd...xyz`.
5. As soon as any key exists, auth is automatically enforced on **all** endpoints. The browser will prompt you to enter the key (stored in `localStorage`, so only once per browser).

You can create multiple keys (one per tool/integration) and revoke any of them at any time.

### 2. Expose the server to the internet

Your Hostinger VPS is on the internet, but your RTX 4060 PC probably isn't.
The easiest solution is **Cloudflare Tunnel** — free, stable HTTPS URL, no port forwarding.

On the RTX 4060 PC (after `start.bat` is running):
```powershell
winget install Cloudflare.cloudflared
cloudflared tunnel --url http://127.0.0.1:8000
```
It prints a URL like `https://random-words.trycloudflare.com`. That's your public endpoint.

For a **permanent** URL (recommended for production), create a free Cloudflare
account, add a domain, and run `cloudflared tunnel login` + `cloudflared tunnel create`.
See <https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/>.

Alternative: **Tailscale** (no domain needed, private network).
```powershell
winget install tailscale.tailscale
```
Install on both PCs, then use the RTX 4060's Tailscale IP directly.

### 3. Call the API from your batch tool

All endpoints require the `X-API-Key` header once a key exists.

| Method | Path | Body | Description |
|--------|------|------|-------------|
| GET    | `/api/health` | — | Public — status + device |
| GET    | `/api/voices` | — | List cloned voices |
| POST   | `/api/voices` | multipart: `name`, `description`, `file` | Clone a new voice |
| DELETE | `/api/voices/{voice_id}` | — | Delete a voice |
| POST   | `/api/generate` | form: `voice_id`, `text`, `speed` | Returns WAV audio |
| GET    | `/api/keys` | — | List keys (masked) |
| POST   | `/api/keys` | form: `name` | Create a new key (full value returned once) |
| DELETE | `/api/keys/{key_id}` | — | Revoke a key |

**Example — curl:**
```bash
BASE=https://random-words.trycloudflare.com
KEY=vct_YourFullKeyHere

# list voices
curl -H "X-API-Key: $KEY" $BASE/api/voices

# generate audio from text, save as out.wav
curl -X POST $BASE/api/generate \
  -H "X-API-Key: $KEY" \
  -F "voice_id=abc123def456" \
  -F "text=Hello, this is the cloned voice speaking." \
  -F "speed=1.0" \
  --output out.wav
```

**Example — Python batch script** (split a long script, generate one wav per chunk, zip the result):
```python
import requests, os, zipfile, io

BASE = "https://random-words.trycloudflare.com"
KEY  = "vct_YourFullKeyHere"
VOICE_ID = "abc123def456"
HEADERS = {"X-API-Key": KEY}

# your chunks (e.g. one per paragraph)
chunks = [
    "This is the first paragraph of the script.",
    "Here is the second paragraph, which continues the story.",
    "And the closing remarks of the episode.",
]

buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
    for i, text in enumerate(chunks, 1):
        r = requests.post(
            f"{BASE}/api/generate",
            headers=HEADERS,
            data={"voice_id": VOICE_ID, "text": text, "speed": 1.0},
            timeout=600,
        )
        r.raise_for_status()
        z.writestr(f"chunk_{i:03d}.wav", r.content)

with open("output.zip", "wb") as f:
    f.write(buf.getvalue())
print("Done -> output.zip")
```

### Security notes
- Keys are stored in `api_keys.json` (chmod 600 on posix). Don't commit this file.
- The tool uses constant-time comparison when checking keys.
- Revoking a key takes effect immediately.
- `/api/health` is intentionally public so monitoring tools can ping it.
- CORS is `*` by default — tighten it in `app/main.py` if exposing publicly.

---

## How it works

- **Cloning**: the uploaded audio is converted to 24 kHz mono WAV, loudness-normalized, and trimmed to 60 s. XTTS v2 needs only a short clean sample — longer does not improve quality.
- **Storage**: each voice is stored as `voices/{voice_id}/reference.wav` + `meta.json`.
- **Generation**: text is split on sentence boundaries into ≤240-char chunks (XTTS' sweet spot), each chunk is synthesized with the reference wav as the speaker prompt, and chunks are concatenated with a small pause. The final WAV is returned to the browser for preview + download.
- **Speed**: passed directly to XTTS' `speed` parameter (0.5–2.0).

---

## Project layout

```
Voice Clone Tool/
├── app/
│   ├── main.py          FastAPI app + routes
│   ├── tts_engine.py    XTTS v2 loader, chunker, generator
│   └── voice_store.py   Voice library (disk)
├── web/
│   ├── index.html
│   ├── style.css
│   └── app.js
├── voices/              created on first clone
├── requirements.txt
├── run.sh
└── README.md
```
