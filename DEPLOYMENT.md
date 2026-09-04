# Deploying Munshi publicly

Two separate services, same shape as CostGuard's own deployment:

- **Backend** (`server/`) -> Render, as a Docker web service (`render.yaml`
  blueprint at the repo root).
- **Frontend** (`client/`) -> Vercel, as a static site (zero-config Vite
  build, `client/vercel.json`).

Both steps below need to happen from a GitHub repo -- this project isn't
one yet locally (no `.git`). Push it to GitHub first, then connect Render
and Vercel to it.

## 1. Backend on Render

1. In the Render dashboard: **New +** -> **Blueprint**, point it at this
   GitHub repo. Render reads `render.yaml` and provisions one Docker web
   service (`munshi-backend`) building `server/Dockerfile`.
2. Render will prompt for these env vars (marked `sync: false` in
   `render.yaml` -- never read from or written to the repo):

   | Env var | Value |
   |---|---|
   | `DEEPSEEK_API_KEY` | a `cgmk_...` CostGuard monitoring key -- **use a dedicated, lower-budget key for this public demo**, not your main one |
   | `DEEPSEEK_BASE_URL` | your CostGuard gateway URL, e.g. `https://costguard-backend-e1mg.onrender.com/v1` |
   | `DEEPSEEK_GATEWAY_PROVIDER_ID` | e.g. `deepseek-primary` |
   | `ANTHROPIC_API_KEY` | only needed if `LLM_PROVIDER=anthropic` |
   | `ANTHROPIC_BASE_URL` | only needed if `LLM_PROVIDER=anthropic` |
   | `SARVAM_API_KEY` | needed for the voice pipeline (STT/TTS) |
   | `ADMIN_KEY` | a fresh, strong value -- **do not reuse** the local dev key. One was generated for you in this session; ask if you need it again. |

   `DATA_DIR`, `LLM_PROVIDER`, and `JUDGE_AUTO_RUN` are already set as plain
   values in `render.yaml` and don't need manual entry.
3. Deploy. Render builds the image from `server/Dockerfile` (context
   `server/`) and runs the committed `server/data/baseline_seed42/` demo
   dataset -- no data upload step needed.
4. Note the resulting URL, e.g. `https://munshi-backend.onrender.com`.

**Free-tier cold start**: Render's free plan spins the service down after
~15 minutes idle. The first request after that takes 30-60s to wake it back
up. Worth a heads-up to whoever's about to try it live, so a slow first
response doesn't read as broken.

## 2. Frontend on Vercel

1. **New Project** -> import the same GitHub repo -> set the **Root
   Directory** to `client/`.
2. Vercel auto-detects Vite (`client/vercel.json` makes the build command
   and output dir explicit anyway). Before the first build, add one env var:

   | Env var | Value |
   |---|---|
   | `VITE_SERVER_URL` | the Render backend URL from step 1.4, e.g. `https://munshi-backend.onrender.com` |

   This is a **build-time** var (baked into the JS bundle by Vite, not read
   at runtime) -- if you change it later, redeploy to pick it up.
3. Deploy. No code changes were needed on the client side beyond the
   `client/vite.config.js` fix below.

## What was actually fixed for this to work

- `client/vite.config.js` (new): Vite's default production build only
  bundles `index.html`. This project also has `admin.html` (the eval-trace
  dashboard) as a second static entry point, which a plain `vite build` was
  silently dropping -- confirmed by running the build before and after.
  Fixed by listing both as `rollupOptions.input`.
- `.gitignore`: `server/data/` was fully ignored, so a fresh clone would
  have no data for the deployed engine to read. Changed to ignore
  everything under `server/data/` **except** `baseline_seed42/` (the
  dataset used throughout this session's eval runs) -- it's 100% synthetic,
  generator-produced data, safe to commit.

## Known limitations to disclose upfront

- **WebRTC voice may not connect for everyone.** There's no TURN server, so
  a recruiter behind a restrictive NAT/firewall may not get a voice
  connection. The text-input box on the same page (`sendTypedText()` in
  `client/src/voiceClient.js`) exercises the identical LLM + tool-calling +
  calculation-engine + trace pipeline and is far more likely to just work --
  lead with that as the reliable path, voice as a bonus.
- **This repo has no git history yet.** `git init`, the first commit
  (including the ~24MB of demo CSVs), and pushing to a GitHub remote are
  all still needed before either platform above can deploy from it.
