# Voice Clone + Question Tool — API Reference

This document covers the HTTP API exposed by this server. Use it from any other
program (your video generator, a CLI, etc.) to drive voice cloning and TTS.

---

## 1. What you need from this server

To call the API from another tool you need **three** values:

| Value | Where to get it | Example |
|---|---|---|
| **Base URL** | The address where this server is reachable. Locally `http://localhost:8000`. If exposed via Cloudflare Tunnel, that public `https://…trycloudflare.com` URL. | `https://my-voice.trycloudflare.com` |
| **API key** | Section 6 of the web UI → "Create key". The full key is shown **once**. Starts with `vct_…`. | `vct_abc123…xyz` |
| **Voice ID** | Section 2 of the web UI (listed under each voice as `voice_id`), or via `GET /api/voices`. 12-char hex. | `e6f31a8c4d92` |

Send the API key on every request as the `X-API-Key` HTTP header.

> **Bootstrap mode**: if no API keys have been created yet, auth is disabled and
> every endpoint is open. Once you create the first key, the header becomes
> required for everything except `/api/health` and `/api/auth/status`.

---

## 2. Generate audio — the one endpoint your video tool will call most

```
POST /api/generate
```

**Headers**

| Name | Value |
|---|---|
| `X-API-Key` | your key |

**Body** — `multipart/form-data` or `application/x-www-form-urlencoded`

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `voice_id` | string | yes | — | The cloned voice to speak with. |
| `text` | string | yes | — | English. Use `<break />` (1.5 s default) or `<break time="2s" />` to insert pauses. Long text is chunked automatically. |
| `speed` | float | no | `1.0` | Clamped to 0.5 – 2.0. |
| `format` | `"wav"` or `"mp3"` | no | `"wav"` | `mp3` is 192 kbps. |

**Response** — the raw audio bytes. `Content-Type` is `audio/wav` or `audio/mpeg`.

### curl

```bash
curl -X POST "https://my-voice.trycloudflare.com/api/generate" \
  -H "X-API-Key: vct_abc123…xyz" \
  -F voice_id=e6f31a8c4d92 \
  -F text="Hello from my video tool." \
  -F speed=1.0 \
  -F format=mp3 \
  --output narration.mp3
```

### Python (`requests`)

```python
import requests

BASE = "https://my-voice.trycloudflare.com"
API_KEY = "vct_abc123…xyz"
VOICE_ID = "e6f31a8c4d92"

def synth(text: str, out_path: str, speed: float = 1.0, fmt: str = "mp3") -> None:
    r = requests.post(
        f"{BASE}/api/generate",
        headers={"X-API-Key": API_KEY},
        data={"voice_id": VOICE_ID, "text": text, "speed": speed, "format": fmt},
        timeout=600,  # long texts can take minutes on CPU
    )
    r.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(r.content)

synth("Welcome back to the channel.", "intro.mp3")
```

### Node.js (built-in `fetch`, Node 18+)

```js
import { writeFile } from "node:fs/promises";

const BASE = "https://my-voice.trycloudflare.com";
const API_KEY = "vct_abc123…xyz";
const VOICE_ID = "e6f31a8c4d92";

async function synth(text, outPath, { speed = 1.0, format = "mp3" } = {}) {
  const fd = new FormData();
  fd.set("voice_id", VOICE_ID);
  fd.set("text", text);
  fd.set("speed", String(speed));
  fd.set("format", format);

  const res = await fetch(`${BASE}/api/generate`, {
    method: "POST",
    headers: { "X-API-Key": API_KEY },
    body: fd,
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  await writeFile(outPath, Buffer.from(await res.arrayBuffer()));
}

await synth("Welcome back to the channel.", "intro.mp3");
```

---

## 3. Other endpoints you might want

### List your voices

```
GET /api/voices
```

```json
{ "voices": [
  { "voice_id": "e6f31a8c4d92",
    "name": "Narrator A",
    "description": "deep calm male voice",
    "created_at": "2026-05-08T12:34:56Z",
    "reference_seconds": 287.41,
    "segment_count": 10 }
] }
```

Use this to discover voice IDs programmatically rather than copy-pasting from
the UI.

### Health check (no auth)

```
GET /api/health
→ { "status": "ok", "device": "cuda", "voices": 3, "auth_required": true }
```

Good for "is the server ready?" probes from your video tool.

### Batch / quiz endpoints

These are used by the web UI but are also callable directly:

- `POST /api/generate-batch` — multiple texts separated by `<<<QUESTION>>>`, returns a ZIP.
- `POST /api/batch/parse-docx` — parse a Format A / Format B DOCX into entries.
- `POST /api/questions/parse-docx` — parse a quiz DOCX into question dicts.
- `POST /api/questions/generate-one` — synthesise + merge one question's four voices.

Schemas are in the FastAPI auto-docs at `<BASE>/docs`.

---

## 4. Errors

| Status | Meaning |
|---|---|
| `401 Unauthorized` | Missing or wrong `X-API-Key`. |
| `400` | Bad input (`voice_id` blank, `text` empty, unsupported `format`, non-English characters in `text`, …). The JSON body has `{ "detail": "…" }`. |
| `404` | Unknown `voice_id` or `key_id`. |
| `500` | Generation or encoding failed. The detail message includes the underlying error. |
| `503` | Server is still loading the XTTS model — retry after 30–60 s. |

---

## 5. Tips for the video generator integration

- **Pre-warm**: call `GET /api/health` once at startup and wait until `status == "ok"`
  before sending real generation requests. The first request after a fresh boot
  loads the model and takes longer.
- **Caching**: identical `(voice_id, text, speed, format)` produces identical
  audio every call — safe to cache by a hash of those inputs.
- **Pauses**: insert `<break time="1s" />` between scene narration chunks to
  give your video editor predictable beats.
- **Timeouts**: a 5-minute generation is normal on CPU. Set client timeout to
  10+ minutes (or stream over a single long-lived connection).
- **Long texts** are chunked server-side automatically, so you don't need to
  pre-split. But you *can* split per video-scene if you want each scene as its
  own file.
- **Don't ship the API key in the video tool's source** — read it from an env
  var or a local config file.
