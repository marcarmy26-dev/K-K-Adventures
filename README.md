# Keralee & Kayden Starter

## What is included
- `frontend/index.html` — single-file frontend with route-style pages, live-page pop-outs, comments, camera preview, uploads, and a browser-only internal lock for prototype use.
- `backend/app/main.py` — FastAPI starter with comments endpoints, journal endpoints, profanity blocking, and a websocket route for live comments.
- `netlify.toml` and `frontend/_redirects` — Netlify config to publish the frontend and proxy `/api/*` to Render.
- `render.yaml` — Render starter manifest.
- `SECURITY_PLAN.md` — production security checklist.

## Deploy
1. Deploy `frontend/` to Netlify.
2. Deploy `backend/` to Render.
3. Replace `YOUR-RENDER-BACKEND.onrender.com` in `netlify.toml` and `_redirects`.
4. Set backend environment variables from `.env.example`.

## Routes in the frontend
- `#/home`
- `#/channels`
- `#/live`
- `#/videos`
- `#/shorts`
- `#/photos`
- `#/journal`
- `#/studio/keralee`
- `#/studio/kayden`
- `#/studio/together`

## Prototype caveat
The HTML file stores uploads/comments locally for demo use. Real multi-user comments, moderation, and media storage must be enforced by the backend.
