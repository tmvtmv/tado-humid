"""Tado session manager.

Performs an interactive-style browser login at https://login.tado.com using
Playwright (mimicking a real Chrome user with the same headers as the example
curl), captures the OAuth2 bearer + refresh tokens from the web app's
localStorage, and then keeps the session alive by hitting the refresh-token
endpoint directly (no browser needed after the first login).

Run:
    python tado_session.py            # logs in, then loops refreshing
    python tado_session.py --once     # logs in, prints token, exits
    python tado_session.py --probe    # logs in, calls zoneStates, exits
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

CREDS_FILE = Path(__file__).parent / "creds.txt"
TOKEN_FILE = Path(__file__).parent / ".tado_token.json"
STATE_FILE = Path(__file__).parent / ".tado_browser_state.json"

LOGIN_HOST = "https://login.tado.com"
APP_URL = "https://app.tado.com/"
TOKEN_URL = f"{LOGIN_HOST}/oauth2/token?ngsw-bypass=true"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36"
)

BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://app.tado.com/",
    "User-Agent": USER_AGENT,
    "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "X-Amzn-Trace-Id": "tado=webapp-3866",
}

REFRESH_LEEWAY_SECONDS = 60
POLL_INTERVAL_SECONDS = 60


@dataclass
class Token:
    access_token: str
    refresh_token: str
    expires_at: float  # epoch seconds
    raw: dict[str, Any]

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> "Token":
        expires_in = int(data.get("expires_in", 599))
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=time.time() + expires_in,
            raw=data,
        )

    def to_disk(self, path: Path) -> None:
        path.write_text(json.dumps({
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "raw": self.raw,
        }))

    @classmethod
    def from_disk(cls, path: Path) -> "Token | None":
        if not path.exists():
            return None
        d = json.loads(path.read_text())
        return cls(
            access_token=d["access_token"],
            refresh_token=d["refresh_token"],
            expires_at=d["expires_at"],
            raw=d.get("raw", {}),
        )

    def seconds_until_expiry(self) -> float:
        return self.expires_at - time.time()

    def _claims(self) -> dict[str, Any]:
        payload_b64 = self.access_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))

    def home_ids(self) -> list[int]:
        """Read the list of home ids embedded in the access token JWT."""
        try:
            return [int(h["id"]) for h in self._claims().get("tado_homes", [])]
        except (ValueError, KeyError, IndexError) as e:
            raise RuntimeError(f"Could not extract home ids from token: {e}")

    def client_id(self) -> str:
        """FusionAuth applicationId from the JWT — used as OAuth client_id on refresh."""
        try:
            return self._claims()["applicationId"]
        except (ValueError, KeyError, IndexError) as e:
            raise RuntimeError(f"Could not extract client_id from token: {e}")


def read_credentials() -> tuple[str, str]:
    raw = CREDS_FILE.read_text().strip()
    user, _, pw = raw.partition(":")
    if not user or not pw:
        raise SystemExit(f"creds.txt must contain <username>:<password>")
    return user, pw


def browser_login(username: str, password: str, headless: bool = True) -> Token:
    """Drive a real Chrome via Playwright, log in, return the OAuth tokens."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            "Playwright not installed. Run:\n"
            "  pip install playwright requests\n"
            "  playwright install chromium"
        )

    with sync_playwright() as p:
        launch_kwargs: dict[str, Any] = {"headless": headless}
        context_kwargs: dict[str, Any] = {
            "user_agent": USER_AGENT,
            "viewport": {"width": 1280, "height": 900},
            "locale": "en-US",
            "extra_http_headers": {
                "sec-ch-ua": BROWSER_HEADERS["sec-ch-ua"],
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"macOS"',
            },
        }
        if STATE_FILE.exists():
            context_kwargs["storage_state"] = str(STATE_FILE)

        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(**context_kwargs)
        page = context.new_page()

        page.goto(APP_URL, wait_until="domcontentloaded")

        # If we land on login.tado.com, fill the form.
        if "login.tado.com" in page.url:
            page.wait_for_selector("#loginId", timeout=20_000)
            page.fill("#loginId", username)
            page.fill("#password", password)
            # Submit: button text varies by locale; try common selectors.
            for sel in ['button[type="submit"]', 'button:has-text("Sign in")',
                        'button:has-text("Inloggen")']:
                if page.locator(sel).count():
                    page.click(sel)
                    break
            else:
                page.keyboard.press("Enter")

        # Wait until the SPA stores the token in localStorage.
        deadline = time.time() + 30
        token_json: str | None = None
        while time.time() < deadline:
            token_json = page.evaluate(
                "() => window.localStorage.getItem('ngStorage-token')"
            )
            if token_json:
                break
            page.wait_for_timeout(500)

        if not token_json:
            html = page.content()[:500]
            browser.close()
            raise SystemExit(f"Login did not produce a token. URL={page.url}\n{html}")

        context.storage_state(path=str(STATE_FILE))
        browser.close()

    data = json.loads(token_json)
    tok = Token.from_response(data)
    tok.to_disk(TOKEN_FILE)
    return tok


def refresh(tok: Token) -> Token:
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": tok.client_id(),
            "grant_type": "refresh_token",
            "refresh_token": tok.refresh_token,
        },
        headers={**BROWSER_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Refresh failed {resp.status_code}: {resp.text}")
    new = Token.from_response(resp.json())
    new.to_disk(TOKEN_FILE)
    return new


def load_or_login(headless: bool = True) -> Token:
    """Return a valid token, refreshing or logging in as needed."""
    tok = Token.from_disk(TOKEN_FILE)
    if tok:
        try:
            if tok.seconds_until_expiry() > REFRESH_LEEWAY_SECONDS:
                return tok
            return refresh(tok)
        except Exception as e:
            print(f"[tado] refresh failed ({e}); falling back to browser login",
                  file=sys.stderr)
    user, pw = read_credentials()
    return browser_login(user, pw, headless=headless)


def api_get(tok: Token, path: str) -> requests.Response:
    """Call the Tado API using the same headers as example-curl.sh."""
    url = path if path.startswith("http") else f"https://my.tado.com{path}"
    return requests.get(
        url,
        headers={**BROWSER_HEADERS, "Authorization": f"Bearer {tok.access_token}"},
        timeout=20,
    )


def _summarize_zones(payload: dict[str, Any]) -> str:
    parts = []
    for zid, z in (payload.get("zoneStates") or {}).items():
        sdp = z.get("sensorDataPoints", {})
        temp = sdp.get("insideTemperature", {}).get("celsius")
        hum = sdp.get("humidity", {}).get("percentage")
        parts.append(f"z{zid}={temp}\u00b0C/{hum}%")
    return " ".join(parts) if parts else "(no zones)"


def poll_loop(headless: bool = True,
              interval: float = POLL_INTERVAL_SECONDS,
              home_id: int | None = None) -> None:
    """Refresh the token as needed and call zoneStates once per `interval` seconds."""
    tok = load_or_login(headless=headless)
    print(f"[tado] logged in. Token expires in {tok.seconds_until_expiry():.0f}s")
    if home_id is None:
        ids = tok.home_ids()
        if not ids:
            raise SystemExit("No home ids found in token; pass --home-id explicitly.")
        home_id = ids[0]
        print(f"[tado] using home id {home_id} (from token; all: {ids})")
    path = f"/api/v2/homes/{home_id}/zoneStates?ngsw-bypass=true"

    while True:
        if tok.seconds_until_expiry() <= REFRESH_LEEWAY_SECONDS:
            try:
                tok = refresh(tok)
                print(f"[tado] refreshed. New expiry in {tok.seconds_until_expiry():.0f}s")
            except Exception as e:
                print(f"[tado] refresh error: {e}; re-logging in", file=sys.stderr)
                user, pw = read_credentials()
                tok = browser_login(user, pw, headless=headless)

        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            r = api_get(tok, path)
            if r.status_code == 401:
                # Token rejected mid-flight; try refresh, falling back to a
                # fresh browser login if the refresh token itself is dead.
                try:
                    tok = refresh(tok)
                except Exception as e:
                    print(f"[{ts}] refresh after 401 failed: {e}; re-logging in",
                          file=sys.stderr)
                    user, pw = read_credentials()
                    tok = browser_login(user, pw, headless=headless)
                r = api_get(tok, path)
            if r.status_code == 200:
                print(f"[{ts}] {_summarize_zones(r.json())}")
            else:
                print(f"[{ts}] HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
        except Exception as e:
            print(f"[{ts}] poll error: {e}", file=sys.stderr)

        time.sleep(interval)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="login then print token and exit")
    ap.add_argument("--probe", action="store_true", help="login then call zoneStates and exit")
    ap.add_argument("--show", action="store_true", help="show browser window during login")
    ap.add_argument("--interval", type=float, default=POLL_INTERVAL_SECONDS,
                    help=f"poll interval in seconds (default {POLL_INTERVAL_SECONDS})")
    ap.add_argument("--home-id", type=int, default=None,
                    help="Tado home id (default: derived from access token)")
    args = ap.parse_args()

    # Always start clean: stale tokens / browser state cause refresh loops on
    # an invalidated refresh_token, so force a fresh browser login each run.
    TOKEN_FILE.unlink(missing_ok=True)
    STATE_FILE.unlink(missing_ok=True)

    headless = not args.show

    if args.probe:
        tok = load_or_login(headless=headless)
        home_id = args.home_id or tok.home_ids()[0]
        r = api_get(tok, f"/api/v2/homes/{home_id}/zoneStates?ngsw-bypass=true")
        print(r.status_code)
        print(r.text[:1000])
        return

    if args.once:
        tok = load_or_login(headless=headless)
        print(f"access_token: {tok.access_token}")
        print(f"refresh_token: {tok.refresh_token}")
        print(f"expires_in: {tok.seconds_until_expiry():.0f}s")
        return

    poll_loop(headless=headless, interval=args.interval, home_id=args.home_id)


if __name__ == "__main__":
    main()
