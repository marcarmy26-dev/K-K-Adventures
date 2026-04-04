# Security Plan

## 1) Access control
- Require real accounts for comments, uploads, and moderation.
- Use server-side roles: `viewer`, `internal_moderator`, `admin`.
- Never rely on frontend passcodes for real access control.

## 2) Child safety
- Keep channels private or invite-only.
- Log moderation actions.
- Add comment rate limits, mute/ban controls, and reporting.
- Consider pre-moderation or delayed publishing for comments.

## 3) Profanity filtering
- Filter on the frontend for user feedback.
- Enforce again on the FastAPI backend before saving.
- Keep a versioned blocklist and moderation override process.

## 4) Media and storage
- Store metadata in PostgreSQL.
- Store photos/videos in object storage, not Postgres blobs.
- Use signed upload URLs.

## 5) Deployment
- Frontend on Netlify.
- Backend and Postgres on Render.
- Tighten CORS to your Netlify domain only.
- Move secrets into Netlify/Render environment variables.
- Add CSP, secure cookies if using sessions, and HTTPS-only settings.

## 6) Realtime comments
- Use WebSockets or a managed realtime service.
- Authenticate websocket connections.
- Server-side moderation must apply before broadcast.
