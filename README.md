# SwiftRun backend

Flask + SQLite. One process serves both the API (`/api/...`) and your front-end files.

## Run it
```bash
cd backend
pip install -r requirements.txt
cp .env.example .env        # set ADMIN_EMAIL / ADMIN_PASSWORD
python3 app.py              # http://127.0.0.1:8000
python3 tests.py            # optional: 8 tests, temp database
```
Production: `gunicorn -w 2 -b 0.0.0.0:8000 app:app` behind HTTPS (Caddy/nginx), with `TRUST_PROXY=1`.
Back up `swiftrun.db`; keep `.env`, `.secret_key` and `.mpesa_callback_secret` private.

## Connect the front-end
`index.html` already loads `backend.js` (keep it last). If you use your own `index.html`, run `python3 install.py index.html` from the project folder.
The page finds the backend by itself: the server it was loaded from, or `http://127.0.0.1:8000` if you opened it from another local
server such as VS Code Live Server. The simplest way is to open **http://127.0.0.1:8000** while `app.py` is running.
Opening `index.html` by double-click (a `file://` address) does not work.
Hosting the page on a different domain than the API? Set `window.SWIFTRUN_API='https://api.example.com'` before `backend.js`
and add the page's origin to `ALLOWED_ORIGINS` in `.env`. Note that a static host such as GitHub Pages cannot run the backend itself.

## API
| Method | Path | Who |
|---|---|---|
| POST | /api/auth/register, /api/auth/login | anyone |
| GET | /api/auth/me | logged in |
| GET | /api/services | anyone |
| POST | /api/quote | anyone (server-side pricing) |
| POST | /api/orders | anyone (guest or logged in) |
| GET | /api/orders | logged in (own orders) |
| GET | /api/orders/track?q= | anyone, exact order ID / phone / email |
| POST | /api/orders/:id/cancel | owner, admin, or guest with matching phone |
| POST | /api/orders/:id/pay/mpesa, GET /api/orders/:id/payment | anyone with the order ID |
| POST | /api/mpesa/callback/:secret | Safaricom |
| GET/PATCH | /api/admin/orders[/:id] (action: confirm, assign, dispatch, complete, cancel) | admin |
| GET/POST/PATCH/DELETE | /api/admin/riders[/:id] | admin |
| GET | /api/admin/payments | admin |

## M-Pesa
`MPESA_MODE=mock` (default) simulates a successful payment after a few seconds, so the whole flow works with no credentials.
For real payments set `MPESA_MODE=sandbox` (or `production`) plus the Daraja key, secret, shortcode, passkey and a public
HTTPS `MPESA_CALLBACK_BASE`. The server confirms an order only when Safaricom's callback arrives with the matching amount.
The real Daraja calls are written to the Daraja docs but have not been run against Safaricom's sandbox; test there first.

## Known limits
- **Card payments are not implemented.** The old UI marked card orders as paid instantly, which was fake. The server rejects them; wire Pesapal/Flutterwave/Stripe if you need cards.
- **Distance pricing**: no maps API, so the fee is a flat base per errand type (`BASE_FEE` in app.py), plus `PER_KM_FEE` if the client sends `distanceKm`.
- **Track by phone/email is unauthenticated** (as in the original UI): anyone who knows a phone number can see that customer's orders. For production add an SMS/OTP check.
- Rider management is in the admin dashboard (Riders tab: add, edit, set active/offline, delete). Riders with order history can only be set offline, not deleted.
- The Admin nav link is visible to everyone, but the page now requires an admin login.
- Keep `services.json` in sync with `categories.js` if you add services.
- Cancelling a paid order flags "refund due"; refunds are manual.
