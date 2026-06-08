#!/usr/bin/env python3
"""
One-time: mint a Gmail API refresh token for sending mail as your Workspace user.
Run this LOCALLY (it opens a browser); it does not run on Railway.

Prereqs (Google Cloud Console, all free, ~5 min):
  1. Create/select a project → enable the "Gmail API".
  2. OAuth consent screen → User Type = Internal (Workspace only) → save.
  3. Credentials → Create OAuth client ID → type "Desktop app".
     Copy the Client ID and Client secret.

Then run:
    python scripts/gmail_oauth_setup.py --client-id XXXX --client-secret YYYY
  (or set GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET env vars and run with no args)

Sign in as business@viralasia.co and approve. The script prints GMAIL_REFRESH_TOKEN.
Give those three values to set as Railway env vars (web + cron):
    GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

SCOPE = "https://www.googleapis.com/auth/gmail.send"
PORT  = 8765
REDIRECT = f"http://localhost:{PORT}/"

_code_holder = {}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(q)
        _code_holder["code"]  = params.get("code", [None])[0]
        _code_holder["error"] = params.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        msg = "Success. You can close this tab and return to the terminal." \
            if _code_holder.get("code") else "Authorization failed."
        self.wfile.write(f"<html><body><h2>{msg}</h2></body></html>".encode())

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-id",     default=os.environ.get("GMAIL_CLIENT_ID", ""))
    ap.add_argument("--client-secret", default=os.environ.get("GMAIL_CLIENT_SECRET", ""))
    args = ap.parse_args()

    if not args.client_id or not args.client_secret:
        sys.exit("ERROR: provide --client-id and --client-secret (or set GMAIL_CLIENT_ID/SECRET).")

    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id":     args.client_id,
        "redirect_uri":  REDIRECT,
        "response_type": "code",
        "scope":         SCOPE,
        "access_type":   "offline",
        "prompt":        "consent",
    })

    print(f"\nOpening browser for Google consent...\nIf it doesn't open, visit:\n{auth_url}\n")
    server = HTTPServer(("localhost", PORT), _Handler)
    webbrowser.open(auth_url)
    server.handle_request()  # serves the single redirect callback

    if _code_holder.get("error") or not _code_holder.get("code"):
        sys.exit(f"Authorization failed: {_code_holder.get('error') or 'no code returned'}")

    data = urllib.parse.urlencode({
        "code":          _code_holder["code"],
        "client_id":     args.client_id,
        "client_secret": args.client_secret,
        "redirect_uri":  REDIRECT,
        "grant_type":    "authorization_code",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        tok = json.loads(resp.read())

    refresh = tok.get("refresh_token")
    if not refresh:
        sys.exit(f"No refresh_token returned. Response: {tok}")

    print("\n" + "=" * 60)
    print("SUCCESS — set these on Railway (web AND cron services):\n")
    print(f"GMAIL_CLIENT_ID={args.client_id}")
    print(f"GMAIL_CLIENT_SECRET={args.client_secret}")
    print(f"GMAIL_REFRESH_TOKEN={refresh}")
    print("=" * 60)


if __name__ == "__main__":
    main()
