#!/usr/bin/env python3
"""Boot-scope assertions for the AWS virtualized e2e arm (issue #355).

Run at the end of scripts/aws-e2e-virtualized.sh, after every Deployment is
up. Three checks:

1. The five Deployments (WireMock, CAPA, the three ACK controllers) are
   Available while intercepted.
2. Interception proof: at least MIN_GET_CALLER_IDENTITY STS
   GetCallerIdentity POSTs arrived at WireMock (one per ACK controller).
   Without this check, a silently unpatched controller talking to real
   AWS would look identical. CAPA is expected to make zero AWS calls at
   boot with no CRs (the spike observed EC2 calls only under an
   AWSCluster reconcile), so only the ACK controllers are asserted.
3. Zero unmatched requests with an empty allow-list: every call matched a
   stub. If a boot-time call shows up as unmatched, extend
   virtualized-e2e/aws/wiremock/stubs-configmap.yaml - do NOT open the
   allow-list.

No fixture CRs are in scope (Phase 2/3), so there are no fixture-Ready
assertions here.

Requires KROPS_E2E_CA_CERT to point at the run's throwaway CA: the admin
access over kubectl port-forward is https://localhost:<port> (the server
certificate carries a localhost SAN for exactly this), and TLS
verification still uses the throwaway CA.
"""

import json
import os
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))

import assertions  # noqa: E402

DEPLOYMENTS = [
    ("wiremock-system", "wiremock"),
    ("capa-system", "capa-controller-manager"),
    ("ack-system", "ack-s3-controller-s3-chart"),
    ("ack-system", "ack-rds-controller-rds-chart"),
    ("ack-system", "ack-iam-controller-iam-chart"),
]
MIN_GET_CALLER_IDENTITY = 3
ADMIN_PORT = 18443


def start_admin_port_forward() -> subprocess.Popen:
    """port-forward the WireMock Service and wait until the admin port opens."""
    proc = subprocess.Popen(
        [
            "kubectl",
            "port-forward",
            "-n",
            "wiremock-system",
            "service/wiremock",
            f"{ADMIN_PORT}:443",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit(
                "kubectl port-forward exited before the admin port opened"
            )
        try:
            with socket.create_connection(("127.0.0.1", ADMIN_PORT), timeout=1):
                return proc
        except OSError:
            time.sleep(0.5)
    proc.kill()
    proc.wait()
    raise SystemExit(
        f"port-forward to wiremock did not open within 60s (port {ADMIN_PORT})"
    )


def admin_get(path: str, ca_cert: Path):
    """GET a WireMock admin endpoint over the port-forward, CA-verified."""
    context = ssl.create_default_context(cafile=str(ca_cert))
    url = f"https://localhost:{ADMIN_PORT}{path}"
    try:
        with urllib.request.urlopen(url, context=context, timeout=30) as response:
            return json.load(response)
    except Exception as exc:
        raise SystemExit(f"WireMock admin endpoint {url} failed: {exc}") from exc


def count_get_caller_identity(journal: dict) -> int:
    """Count STS GetCallerIdentity POSTs in the request journal."""
    return sum(
        1
        for entry in journal.get("requests", [])
        if entry.get("method") == "POST"
        and entry.get("url") == "/"
        and "Action=GetCallerIdentity" in (entry.get("body") or "")
    )


def main() -> int:
    ca_env = os.environ.get("KROPS_E2E_CA_CERT")
    if not ca_env:
        raise SystemExit("KROPS_E2E_CA_CERT (path to the throwaway CA) must be set")
    ca_cert = Path(ca_env)
    if not ca_cert.is_file():
        raise SystemExit(f"CA certificate not found: {ca_cert}")

    failures = []
    for namespace, name in DEPLOYMENTS:
        try:
            # The e2e script already waited on these; this re-check is the
            # safety net for running the script standalone.
            assertions.check_deployment_available(namespace, name, timeout="120s")
            print(f"deployment/{name} in {namespace}: Available")
        except subprocess.CalledProcessError as exc:
            failures.append(
                f"deployment/{name} in {namespace} is not Available "
                f"(kubectl wait exit {exc.returncode})"
            )
    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1

    forward = start_admin_port_forward()
    try:
        journal = admin_get("/__admin/requests", ca_cert)
        arrivals = count_get_caller_identity(journal)
        if arrivals < MIN_GET_CALLER_IDENTITY:
            print(
                f"only {arrivals} STS GetCallerIdentity arrival(s) at WireMock, "
                f"expected >= {MIN_GET_CALLER_IDENTITY}: interception is not proven",
                file=sys.stderr,
            )
            return 1
        print(
            f"STS GetCallerIdentity arrivals: {arrivals} "
            f"(>= {MIN_GET_CALLER_IDENTITY})"
        )

        remaining = assertions.filter_allowed(
            assertions.fetch_unmatched_requests(
                f"https://localhost:{ADMIN_PORT}", str(ca_cert)
            ),
            [],
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
    finally:
        forward.kill()
        forward.wait()


if __name__ == "__main__":
    raise SystemExit(main())
