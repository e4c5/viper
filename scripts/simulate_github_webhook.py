#!/usr/bin/env python3
"""
Simulate a GitHub App Pull Request Webhook being retriggered.
This script extracts the PR owner, repo, and number from a given GitHub URL
and POSTs a mock webhook payload to a local application.
"""

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import socket
import sys
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent a validated local target from redirecting to an unvalidated URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_local_target(url: str) -> str:
    """Validate that a webhook target is HTTP(S) on a loopback address."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Webhook target must use http or https")
    if parsed.username or parsed.password:
        raise ValueError("Webhook target must not contain credentials")
    if not parsed.hostname:
        raise ValueError("Webhook target must include a hostname")

    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except socket.gaierror as exc:
        raise ValueError(f"Webhook target hostname cannot be resolved: {parsed.hostname}") from exc
    if not addresses or not all(ipaddress.ip_address(address).is_loopback for address in addresses):
        raise ValueError("Webhook target must resolve only to loopback addresses")
    return url


def parse_github_pr_url(url: str) -> tuple[str, str, int]:
    """Parse the owner, repo, and PR number from a GitHub PR URL."""
    try:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.strip("/")
        parts = path.split("/")
        if len(parts) >= 4 and parts[2] in ("pull", "pulls"):
            return parts[0], parts[1], int(parts[3])
    except Exception:
        pass
    raise ValueError(
        "Invalid GitHub PR URL. Expected format: https://github.com/owner/repo/pull/123"
    )


def create_signature(payload_bytes: bytes, secret: str) -> str:
    """Generate the GitHub X-Hub-Signature-256 header value."""
    hmac_gen = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256)
    return "sha256=" + hmac_gen.hexdigest()


def main():
    parser = argparse.ArgumentParser(description="Simulate a GitHub Pull Request Webhook")
    parser.add_argument(
        "url", help="GitHub Pull Request URL (e.g., https://github.com/owner/repo/pull/123)"
    )
    parser.add_argument(
        "--target",
        default="http://localhost:8080/webhooks/github",
        help="Target webhook URL (default: http://localhost:8080/webhooks/github)",
    )
    parser.add_argument(
        "--action",
        default="opened",
        help="PR event action (e.g. synchronize, opened) (default: opened)",
    )
    parser.add_argument(
        "--head-sha",
        default="1234567890abcdef1234567890abcdef12345678",
        help="Head SHA to send in the payload",
    )
    parser.add_argument(
        "--installation-id",
        type=int,
        default=12345,
        help="GitHub App Installation ID to use in the payload",
    )
    parser.add_argument(
        "--secret", help="Webhook secret (defaults to VIPER_GITHUB_APP_WEBHOOK_SECRET env var)"
    )

    args = parser.parse_args()

    try:
        owner, repo, pr_number = parse_github_pr_url(args.url)
        target = validate_local_target(args.target)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    secret = args.secret or os.environ.get("VIPER_GITHUB_APP_WEBHOOK_SECRET", "")

    # Construct a minimal viable payload based on viper_github_app.events parsing
    payload = {
        "action": args.action,
        "pull_request": {"number": pr_number, "head": {"sha": args.head_sha}},
        "repository": {"name": repo, "owner": {"login": owner}},
        "installation": {"id": args.installation_id},
    }

    payload_bytes = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": "fake-delivery-guid",
        "User-Agent": "GitHub-Hookshot/717be6c",
    }

    if secret:
        headers["X-Hub-Signature-256"] = create_signature(payload_bytes, secret)
    else:
        print("Warning: No webhook secret provided, signature header will be missing.")

    print(f"Simulating webhook for PR: {owner}/{repo} #{pr_number}")
    print(f"Target URL: {target}")
    print(f"Action: {args.action}")

    req = urllib.request.Request(target, data=payload_bytes, headers=headers, method="POST")
    try:
        opener = urllib.request.build_opener(_NoRedirectHandler())
        with opener.open(req) as response:
            print(f"\nSuccess! Status: {response.status}")
            print(f"Response: {response.read().decode('utf-8')}")
    except HTTPError as e:
        print(f"\nHTTP Error: {e.code} - {e.reason}")
        try:
            print(f"Response: {e.read().decode('utf-8')}")
        except (UnicodeDecodeError, OSError):
            pass
        sys.exit(1)
    except URLError as e:
        print(f"\nURL Error: {e.reason} - Is the app running on {target}?")
        sys.exit(1)


if __name__ == "__main__":
    main()
