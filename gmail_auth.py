#!/usr/bin/env python3
# -*- coding:utf-8 -*-
"""
One-time Gmail authorization helper.

main.py only *reads* an existing token.json (and refreshes it) - it has no way
to create one. Run this once, interactively, on a machine with a web browser
(your desktop, NOT the headless Pi) to produce token.json, then copy that file
into the project directory on the Pi.

Prerequisites (see the README "Gmail" section):
  1. A Google Cloud project with the Gmail API enabled.
  2. An OAuth 2.0 Client ID of type "Desktop app", downloaded as JSON and saved
     next to this script as `credentials.json`.
  3. Your Google account added as a Test user on the OAuth consent screen.

Usage:
    pip install google-auth-oauthlib google-api-python-client
    python gmail_auth.py

It opens a browser, asks you to grant read-only Gmail access, then writes
token.json. That token refreshes itself forever after, so this is a one-time
step unless you revoke access or change the scope.
"""
import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

BASE_DIR = os.path.dirname(os.path.realpath(__file__))
CREDENTIALS_PATH = os.path.join(BASE_DIR, 'credentials.json')
TOKEN_PATH = os.path.join(BASE_DIR, 'token.json')

# Must match GMAIL_SCOPES in main.py exactly, or main.py will reject the token.
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']


def main():
    if not os.path.exists(CREDENTIALS_PATH):
        print(f"ERROR: {CREDENTIALS_PATH} not found.")
        print("Download your OAuth 'Desktop app' client from the Google Cloud")
        print("Console, rename it to credentials.json, and place it here.")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
    # Spins up a temporary localhost server and opens the browser to it. If this
    # machine has no browser, it prints a URL to open elsewhere instead.
    creds = flow.run_local_server(port=0)

    with open(TOKEN_PATH, 'w') as f:
        f.write(creds.to_json())
    os.chmod(TOKEN_PATH, 0o600)
    print(f"\nWrote {TOKEN_PATH}")

    # Prove the token works before you bother copying it to the Pi.
    try:
        service = build('gmail', 'v1', credentials=creds, cache_discovery=False)
        inbox = service.users().labels().get(userId='me', id='INBOX').execute()
        print(f"Verified: INBOX has {inbox.get('messagesUnread', 0)} unread "
              f"of {inbox.get('messagesTotal', 0)} messages.")
        print("\nNow copy token.json to the Pi:")
        print("    scp token.json raspberrypi.local:~/Pi_dashboard/")
    except Exception as e:
        print(f"Token written, but a test call failed: {e}")


if __name__ == '__main__':
    main()
