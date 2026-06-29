# Automation-Documentation

Turn a Confluence page into a polished **slide deck (PDF)** or a **narrated MP4 tutorial video** — automatically — and upload the result to SharePoint.

You paste a Confluence URL into a web UI, pick a few options, and the system fetches the page, uses an LLM to plan the content, drives a real browser to capture screenshots / record a screen session, builds the deliverable (Gamma deck or Remotion video), and saves it to SharePoint — all while streaming live progress back to the browser.

---

## Architecture at a glance

The project is made of **three cooperating apps**:

| App | Stack | Port | Role | Entry point |
|-----|-------|------|------|-------------|
| `backend/` | Python · FastAPI | 8000 | The brains — crawling, screenshots, LLM, Gamma, recording, SharePoint | `backend/main.py` |
| `frontend/` | Node · Express | 3000 | The web UI — paste a URL, watch progress, manage the library | `frontend/server.js` |
| `video/` | Node · Remotion (React/TS) | — | The video renderer the backend drives to produce the MP4 | `video/src/Root.tsx` |

### User flow

```
Browser (UI)
   → frontend/server.js   (Express, :3000)
   → backend/main.py      (FastAPI, :8000)
   → pipelines            (presentation or video)
   → Gamma / Remotion
   → SharePoint
```

---

## The two pipelines

Both live in `backend/`, run asynchronously, and stream live progress events to the UI.

### A) Presentation pipeline — `backend/pipeline.py` → `POST /api/generate`

1. **`confluence.py`** — fetch the Confluence page as markdown
2. **`llm.py`** — format the content and plan which screenshots to take
3. **`screenshots.py`** — capture the screens with Playwright
4. **`sharepoint.py`** — upload screenshots; **`imgbb.py`** hosts public image URLs
5. **`gamma.py`** — build the slide deck and export a PDF
6. **`sharepoint.py`** — upload the final PDF

### B) Video pipeline — `backend/video_pipeline.py` → `POST /api/generate-video`

1. **`confluence.py`** — fetch the Confluence page as markdown
2. **`llm.py`** — generate a 5–12 step video script with narration
3. **`tts.py`** — synthesize the voiceover (ElevenLabs, with Azure Speech fallback)
4. **`recorder.py`** — drive a real Playwright browser session following the script
5. **`video.py` + `video/`** — render the MP4 with subtitles via Remotion
6. **`sharepoint.py`** — upload the MP4

---

## Project layout

```
backend/
  main.py             FastAPI app + all HTTP endpoints
  config.py           reads .env; central settings & credentials
  pipeline.py         presentation orchestrator (yields progress events)
  video_pipeline.py   video orchestrator (yields progress events)
  services/           one file per integration:
                        llm, gamma, confluence, sharepoint, screenshots,
                        recorder, video, tts, imgbb, route_crawler
  prompts/            the LLM prompt templates
  routes_map.json     crawled map of the product's pages/buttons
  requirements.txt    Python dependencies
frontend/
  server.js           Express server + tutorial library / Excel logic
  public/             static web UI (generate + library pages)
video/
  src/                Remotion React components for the MP4
  remotion.config.ts  Remotion configuration
tutorials.json        saved tutorial runs shown in the library
JeenVideos/           rendered example videos
```

---

## Getting started

### Prerequisites

- **Python 3.11+**
- **Node.js 18+** and **npm**

### 1. Install dependencies

```bash
# Backend (Python) — installs deps + Playwright's Chromium browser
pip install -r backend/requirements.txt
python3 -m playwright install chromium

# Frontend (Express web UI)
cd frontend && npm install && cd ..

# Video renderer (Remotion)
cd video && npm install && cd ..
```

### 2. Configure your environment

Create a `.env` file in the repo root. The required keys (read in `backend/config.py`) are:

```bash
# Azure OpenAI (LLM)
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=...

# Confluence
CONFLUENCE_EMAIL=...
CONFLUENCE_API_TOKEN=...

# Gamma (slide-deck generation)
GAMMA_API_KEY=...

# SharePoint (upload destination)
SP_TENANT_ID=...
SP_CLIENT_ID=...
```

Optional keys (also in `backend/config.py`) let you tune behavior — e.g.
`AZURE_OPENAI_DEPLOYMENT`, `CONFLUENCE_BASE_URL`, `SP_SITE_URL`, the various
`SP_*` folder paths, `IMGBB_API_KEY` / `PUBLIC_BASE_URL` for public screenshot
URLs, `ELEVENLABS_API_KEY` / `AZURE_SPEECH_*` for voiceover, and the Jeen login
credentials used by the browser automation.

### 3. Run it

```bash
# Terminal 1 — backend (FastAPI, http://localhost:8000)
python3 -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

# Terminal 2 — frontend (Express UI, http://localhost:3000)
cd frontend && npm start
```

Then open **http://localhost:3000** in your browser.

To preview or iterate on the video template directly:

```bash
cd video && npm start      # opens Remotion Studio
cd video && npm run build  # renders straight to video/output/video.mp4
```

---

## Key API endpoints

| Method & path | Purpose |
|---------------|---------|
| `GET  /health` | Liveness check |
| `POST /api/generate` | Run the presentation pipeline (SSE progress stream) |
| `POST /api/generate/sync` | Same, but returns the final JSON result |
| `POST /api/generate-video/stream` | Run the video pipeline (SSE progress stream) |
| `POST /api/generate-video/sync` | Same, but returns the final JSON result |
| `POST /api/discover-routes` | Crawl the product and merge new routes into `routes_map.json` |
| `POST /api/export-pdfs-to-sharepoint` | Copy PDFs into a SharePoint export folder |
| `POST /api/sync-export-folder` | Date-based export with carry-forward |
| `POST /api/upload-excel-to-sharepoint` | Upload the summary Excel file |
| `GET  /screenshots/{session_id}/{filename}` | Proxy a SharePoint screenshot as a real image URL |

---

## Notes

- `.env` and `node_modules` are git-ignored.
- Generated TTS audio accumulates under `video/public/audio/video-run-*` and can be deleted safely.
- `routes_map.json` is built by the crawler (`backend/services/route_crawler.py`) and grows as new product pages/buttons are discovered.
