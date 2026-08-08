#!/usr/bin/env python3
"""Mint a DataHub personal access token and write it into .env.

Requires DataHub to run with METADATA_SERVICE_AUTH_ENABLED=true — without it the
metadata service accepts unauthenticated calls and tokens are pointless.

    python scripts/make_datahub_token.py                    # localhost defaults
    python scripts/make_datahub_token.py --frontend http://datahub.internal:9002

Authenticates against the DataHub frontend (which holds the system credentials),
asks it for a PERSONAL token via GraphQL, and stores the result as
DATAHUB_GMS_TOKEN in .env. The token value is printed masked.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import sys
import urllib.error
import urllib.request

DURATIONS = ("ONE_HOUR", "ONE_DAY", "ONE_WEEK", "ONE_MONTH", "THREE_MONTHS", "SIX_MONTHS", "ONE_YEAR")

CREATE_TOKEN = """
mutation createAccessToken($input: CreateAccessTokenInput!) {
  createAccessToken(input: $input) { accessToken metadata { id name expiresAt } }
}
"""


def _opener():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def login(opener, frontend: str, user: str, password: str) -> None:
    body = json.dumps({"username": user, "password": password}).encode()
    req = urllib.request.Request(
        f"{frontend}/logIn", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        opener.open(req, timeout=30).read()
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            f"login failed ({exc.code}). Check the username and password — the quickstart "
            f"default is datahub / datahub."
        ) from exc


def mint(opener, frontend: str, actor: str, name: str, duration: str) -> dict:
    payload = json.dumps({
        "query": CREATE_TOKEN,
        "variables": {"input": {
            "type": "PERSONAL", "actorUrn": actor, "duration": duration, "name": name,
        }},
    }).encode()
    req = urllib.request.Request(
        f"{frontend}/api/v2/graphql", data=payload, headers={"Content-Type": "application/json"}
    )
    data = json.load(opener.open(req, timeout=60))
    if data.get("errors"):
        raise SystemExit(f"token creation failed: {json.dumps(data['errors'])[:400]}")
    return data["data"]["createAccessToken"]


def write_env(path: str, token: str) -> None:
    """Set DATAHUB_GMS_TOKEN in .env, replacing any existing (or commented) line."""
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    pattern = re.compile(r"^\s*#?\s*DATAHUB_GMS_TOKEN\s*=")
    replaced = False
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = f"DATAHUB_GMS_TOKEN={token}"
            replaced = True
            break
    if not replaced:
        lines.append(f"DATAHUB_GMS_TOKEN={token}")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend", default=os.getenv("DATAHUB_FRONTEND_URL", "http://localhost:9002"))
    parser.add_argument("--user", default=os.getenv("DATAHUB_USER", "datahub"))
    parser.add_argument("--password", default=os.getenv("DATAHUB_PASSWORD", "datahub"))
    parser.add_argument("--name", default="contextci-ci")
    parser.add_argument("--duration", default="ONE_MONTH", choices=DURATIONS)
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()

    frontend = args.frontend.rstrip("/")
    actor = f"urn:li:corpuser:{args.user}"

    opener = _opener()
    login(opener, frontend, args.user, args.password)
    result = mint(opener, frontend, actor, args.name, args.duration)

    token = result["accessToken"]
    write_env(args.env_file, token)

    meta = result["metadata"]
    print(f"minted '{meta['name']}' for {actor}, expires {meta['expiresAt']}")
    print(f"token: {token[:12]}…{token[-6:]} ({len(token)} chars)")
    print(f"written to {args.env_file} as DATAHUB_GMS_TOKEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
