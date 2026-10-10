# SwiftRun

Front-end (static) on **GitHub Pages**. Backend (Flask + SQLite) on a **Python host** such as Render.
GitHub Pages cannot run the backend; it rejects POST requests with 405. That is why the two are deployed separately.

## 1. Push this folder to a GitHub repo
`.env`, the database and secret files are in `.gitignore`. Do not commit them.

## 2. Deploy the backend
**Render (blueprint included):** New > Blueprint > pick the repo. It reads `render.yaml`. When asked, enter:
- `ADMIN_EMAIL` / `ADMIN_PASSWORD`: your admin login
- `ALLOWED_ORIGINS`: `https://YOUR-USERNAME.github.io` (the origin only, no `/repo-name`)

The blueprint uses a persistent disk for the SQLite file. Without a disk (for example Render's free plan), the database is
wiped on each restart or deploy, so orders and accounts would be lost. Check your host's current plans.
Any host that runs Python works: use `backend/` as the root, `pip install -r requirements.txt`, and the start command in `backend/Procfile`.

Check it: open `https://YOUR-BACKEND/api/health`, which should show `{"ok":true,...}`.

## 3. Point the front-end at it
Edit `config.js` and set `BACKEND_URL` to your backend address (no trailing slash). Commit and push.

## 4. Turn on GitHub Pages
Repo > Settings > Pages > Deploy from branch > `main` / root. Open `https://YOUR-USERNAME.github.io/REPO/`.
Log in with `ADMIN_EMAIL` to reach the admin dashboard.

## Run locally instead
```
cd backend && pip install -r requirements.txt
cp .env.example .env     # set ADMIN_EMAIL / ADMIN_PASSWORD
python3 app.py           # open http://127.0.0.1:8000  (config.js needs no change locally)
```

## Notes
- `MPESA_MODE=mock` fakes payments. For real M-Pesa see `backend/README.md`.
- Card payments are not implemented; the server rejects them.
- The whole repo is public on Pages, including `backend/` source code. Secrets live only in your host's environment variables.
- If the site says "Backend URL not set", you haven't edited `config.js` yet.
