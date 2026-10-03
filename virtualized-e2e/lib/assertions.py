#!/usr/bin/env python3
"""Shared assertion helpers for the Phase 5 virtualized e2e scripts (#355).

Two checks, identical across clouds:

1. Deployment availability: `kubectl wait --for=condition=Available` for a
   controller (or WireMock) Deployment.
2. WireMock unmatched requests: GET /__admin/requests/unmatched must return
   an empty list (minus allow-listed entries), proving every controller call
   matched a stub instead of falling through to WireMock's 404 near-miss
   page.

Usable as a library (import assertions) or as a CLI:

    python3 assertions.py deployment-available <namespace> <name> [--timeout 300s]
    python3 assertions.py unmatched [--admin-url https://localhost:8443] \
        [--cacert ca.crt] [--allow 'POST:/$'] [--allow 'GET:/health.*']

The admin URL is plain https://localhost:<port-forward>: the WireMock server
certificate carries a localhost SAN for exactly this access path (see
lib/wiremock/deployment.yaml.tmpl), so TLS verification against the
throwaway CA still applies.
"""

import argparse
import json
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.request


def check_deployment_available(namespace: str, name: str, timeout: str = "300s") -> None:
    """Wait until a Deployment reports Available; raise on timeout."""
    subprocess.run(
        [
            "kubectl",
            "wait",
            "--for=condition=Available",
            f"deployment/{name}",
            "--namespace",
            namespace,
            f"--timeout={timeout}",
        ],
        check=True,
    )


def fetch_unmatched_requests(
    admin_url: str = "https://localhost:8443",
    cacert: str | None = None,
) -> list[dict]:
    """Return the unmatched-request journal entries from WireMock.

    cacert is the throwaway CA certificate file; when omitted, verification
    is left to the system trust store (useful only with a real CA).

    Raises SystemExit with a readable message when the admin endpoint is unreachable or returns an HTTP error.
    """
    context = ssl.create_default_context(cafile=cacert)
    url = f"{admin_url}/__admin/requests/unmatched"
    try:
        with urllib.request.urlopen(url, context=context, timeout=30) as response:
            journal = json.load(response)
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            f"WireMock admin endpoint {url} returned HTTP {exc.code}: {exc.reason}"
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise SystemExit(
            f"WireMock admin endpoint unreachable at {url}: {reason}"
            " (is the kubectl port-forward running?)"
        ) from exc
    return journal.get("requests", [])


def filter_allowed(
    requests: list[dict], allow: list[tuple[str, str]]
) -> list[dict]:
    """Drop entries whose method and URL match an allow-listed pattern."""

    def allowed(entry: dict) -> bool:
        method = entry.get("method", "")
        url = entry.get("url", "")
        return any(
            method == allowed_method and re.search(allowed_url, url)
            for allowed_method, allowed_url in allow
        )

    return [entry for entry in requests if not allowed(entry)]


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)

    available = subparsers.add_parser(
        "deployment-available", help="wait for a Deployment to become Available"
    )
    available.add_argument("namespace")
    available.add_argument("name")
    available.add_argument("--timeout", default="300s")

    unmatched = subparsers.add_parser(
        "unmatched", help="fail if WireMock logged unmatched requests"
    )
    unmatched.add_argument("--admin-url", default="https://localhost:8443")
    unmatched.add_argument("--cacert", default=None)
    unmatched.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="METHOD:URL-REGEX",
        help="allow-listed unmatched request (repeatable)",
    )
    args = parser.parse_args()

    if args.command == "deployment-available":
        check_deployment_available(args.namespace, args.name, args.timeout)
        print(f"deployment/{args.name} in {args.namespace}: Available")
        return 0

    for entry in args.allow:
        if ":" not in entry:
            parser.error(f"--allow value {entry!r} must be METHOD:URL-REGEX")
    allow = [tuple(entry.split(":", 1)) for entry in args.allow]
    remaining = filter_allowed(
        fetch_unmatched_requests(args.admin_url, args.cacert), allow
    )
    for entry in remaining:
        print(
            f"unmatched: {entry.get('method', '?')} {entry.get('url', '?')}",
            file=sys.stderr,
        )
    if remaining:
        print(f"{len(remaining)} unmatched request(s)", file=sys.stderr)
        return 1
    print("no unmatched requests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
