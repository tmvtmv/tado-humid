# tado-humid

Polls a Tado home's `zoneStates` endpoint (temperature + humidity per zone) once
per minute. Logs in like a real browser at `https://login.tado.com`, captures
the OAuth2 bearer + refresh tokens from the web app, and keeps the session
alive by hitting Tado's refresh-token endpoint directly.

## Installation

Requires Python 3.10+. Works on macOS and Linux.

### macOS

```sh
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/playwright install chromium
```

Chromium is downloaded into `~/Library/Caches/ms-playwright` (~100 MB).

### Linux

```sh
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/playwright install --with-deps chromium
```

Chromium is downloaded into `~/.cache/ms-playwright` (~100 MB). The
`--with-deps` flag uses `apt`/`dnf` to install the system libraries Chromium
needs (fonts, NSS, libxkbcommon, etc.) — it requires `sudo` and only supports
Debian/Ubuntu and Fedora. On other distros, drop `--with-deps` and install the
equivalent packages manually (see `playwright install-deps --dry-run`).

The headless default means no X server / Wayland session is needed, so this
also works on a bare server or inside a container.

## Configuration

Create `creds.txt` next to `tado_session.py` with one line:

```
your-email@example.com:your-password
```

The home id is read automatically from the `tado_homes` claim in the access
token JWT.

## Usage

```sh
# Poll zoneStates every 60s (default)
./venv/bin/python -u tado_session.py

# Poll every 30s for home 12345
./venv/bin/python -u tado_session.py --interval 30

# One-shot: log in, print fresh tokens, exit
./venv/bin/python tado_session.py --once

# One-shot: log in, print zoneStates payload, exit
./venv/bin/python tado_session.py --probe

# Show the browser window during the initial login (debugging)
./venv/bin/python tado_session.py --show
```

State files written next to the script:

- `.tado_token.json` — current access + refresh tokens
- `.tado_browser_state.json` — Playwright cookies, speeds up re-login

Both are safe to delete; the next run will recreate them.

## How it works

1. First run: Playwright drives headless Chromium to `app.tado.com`, fills the
   login form on `login.tado.com`, and reads the OAuth2 token JSON from the
   SPA's `localStorage["ngStorage-token"]`.
2. Subsequent runs: the saved refresh token is exchanged for a fresh access
   token via `POST https://login.tado.com/oauth2/token` — no browser needed.
   The OAuth `client_id` sent on refresh is read from the access token's
   `applicationId` claim, so there's nothing hardcoded to update if Tado
   rotates it.
3. If the refresh fails (e.g. token revoked or older than 30 days), the script
   falls back to a fresh browser login automatically.

API calls use the same headers as a real Chrome session (User-Agent,
sec-ch-ua, Referer, X-Amzn-Trace-Id) to mimic the web app.
