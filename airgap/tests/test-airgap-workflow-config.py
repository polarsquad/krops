#!/usr/bin/env python3
"""Guard the air-gapped workflow's triggers, guards, timeouts, and DNS gate (docs/airgap.md finding 11)."""

import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (REPO_ROOT / ".github/workflows/air-gapped.yml").read_text()
OFFLINE_RUN = (REPO_ROOT / "airgap/scripts/offline-run.sh").read_text()
ZARF = (REPO_ROOT / "airgap/zarf.yaml").read_text()
DEPLOY_STEP = "Deploy with external egress blocked"
EVIDENCE_STEP = "Store isolated deployment evidence"
DNS_FUNCTIONS = ("valid_ipv4", "valid_ipv6", "parse_ext_servers", "write_egress_gate")
# The step runs under GitHub's default `bash -e` and sets `-uo pipefail` itself,
# so the extracted helpers run under the same `-euo pipefail`.
HELPER_SHELL = "set -euo pipefail\n"
DOCKER_USER_SPEC = (
    '-i "$bridge_interface" ! -o "$bridge_interface" -m conntrack --ctstate NEW '
    '-m comment --comment "$firewall_comment" -j DROP'
)
DOCKER_USER_INSERT = f"sudo iptables -I DOCKER-USER 1 {DOCKER_USER_SPEC}"
DOCKER_USER_CHECK = f"if ! sudo iptables -C DOCKER-USER {DOCKER_USER_SPEC}; then"
GATE_CALL = (
    'if ! write_egress_gate "$capture" /tmp/airgap-public-egress-all.txt '
    '/tmp/airgap-public-egress.txt "$dns_upstreams"; then'
)
FINAL_CHECK = "if [ -s /tmp/airgap-public-egress.txt ]; then"
GATE_FILE = "/tmp/airgap-public-egress.txt"
CAPTURE_FILTER = (
    "'(ip and not dst net 10.0.0.0/8 and not dst net 172.16.0.0/12 and not dst net 192.168.0.0/16"
    " and not dst net 127.0.0.0/8 and not dst net 169.254.0.0/16 and not multicast)"
    " or (ip6 and not dst net fc00::/7 and not dst net fe80::/10 and not dst host ::1 and not multicast)'"
)
OFFLINE_CALL = "if airgap/scripts/offline-run.sh airgap/zarf-package-krops-airgap-*.tar.zst; then"
CAPTURE_ALIVE_CHECK = 'if ! sudo kill -0 "$tcpdump_pid" 2>/dev/null; then'
CAPTURE_STOPPED = "::error::Traffic capture stopped before the deploy finished"
# Frames for the gate test: (src, dst, protocol, dport[, sport]). Packet i has source port SPORT_BASE + i
# unless a fifth element overrides it.
SPORT_BASE = 40000
GATE_FRAMES = (
    ("172.18.0.2", "8.8.8.8", 17, 53),        # 0 upstream DNS, UDP
    ("172.18.0.2", "8.8.8.8", 6, 53),         # 1 upstream DNS, TCP
    ("172.18.0.2", "1.1.1.1", 17, 53),        # 2 second upstream DNS, UDP
    ("172.18.0.2", "93.184.216.34", 6, 443),  # 3 public HTTPS
    ("172.18.0.2", "8.8.8.8", 17, 443),       # 4 upstream host, not port 53
    ("172.18.0.2", "1.1.1.1", 6, 443),        # 5 second upstream host, not port 53
    ("8.8.8.8", "9.9.9.9", 17, 53),           # 6 upstream as source, another host's port 53
    ("172.18.0.2", "8.8.8.8", 17, 443, 53),   # 7 upstream host, UDP from source port 53
    ("172.18.0.2", "8.8.8.8", 132, 53),       # 8 upstream DNS, SCTP (not UDP or TCP)
)
GATE_CASES = (
    ("one upstream", "8.8.8.8", {0, 1}),
    ("two upstreams", "8.8.8.8 1.1.1.1", {0, 1, 2}),
    ("no upstreams: unfiltered", "", set()),
)
# Stands in for tcpdump: counts reads, fails or truncates read SHIM_AT when SHIM_MODE says so.
TCPDUMP_SHIM = """#!/bin/sh
n=$(cat "$SHIM_COUNT" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$SHIM_COUNT"
if [ "$n" -eq "$SHIM_AT" ] && [ "$SHIM_MODE" = fail ]; then
  exit 1
fi
if [ "$n" -eq "$SHIM_AT" ] && [ "$SHIM_MODE" = short ]; then
  "$REAL_TCPDUMP" "$@" | head -n 1
  exit 0
fi
exec "$REAL_TCPDUMP" "$@"
"""


def job_block(name: str) -> str:
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \w[\w-]*:\n|\Z)", WORKFLOW, re.S | re.M)
    return match.group(1) if match else ""


def step_text(name: str) -> str:
    match = re.search(rf"^      - name: {re.escape(name)}\n(.*?)(?=^      - name:|^  \w|\Z)", WORKFLOW, re.S | re.M)
    return match.group(1) if match else ""


def step_run(name: str) -> str:
    match = re.search(r"^        run: \|\n(.*?)(?=^        \S|\Z)", step_text(name), re.S | re.M)
    return textwrap.dedent(match.group(1)) if match else ""


def shell_function(run: str, name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", run, re.S | re.M)
    return match.group(0) if match else ""


def flat(script: str) -> str:
    """Joins backslash-continued lines so each command can be matched as one line."""
    return re.sub(r"\s*\\\n\s*", " ", script)


def block_at(text: str, at: int) -> str:
    """The if-block starting at offset `at`, up to the fi at the same indentation."""
    indent = text[text.rfind("\n", 0, at) + 1:at]
    end = text.find(f"\n{indent}fi\n", at)
    return text[at:end] if end != -1 else ""


def block(text: str, anchor: str) -> str:
    at = text.find(anchor)
    return block_at(text, at) if at != -1 else ""


def ordering_failures(text: str, anchors: tuple) -> list[str]:
    found = []
    for label, needle in anchors:
        at = text.find(needle)
        if at == -1:
            return [f"the deploy step must contain {label}: {needle}"]
        found.append((at, label))
    return [
        f"{later} must run after {earlier}"
        for (before, earlier), (after, later) in zip(found, found[1:])
        if after <= before
    ]


def check_dns_guards() -> list[str]:
    failures = []
    step = step_text(DEPLOY_STEP)
    run = step_run(DEPLOY_STEP)
    text = flat(re.sub(r"(?m)^[ \t]*#.*\n", "", run))
    failures.extend(ordering_failures(text, (
        ("the DNS baseline probe", 'baseline_out=$(timeout 60 docker run --rm --network kind "$probe_image" nslookup "$baseline_name" 2>&1)'),
        ("the forwarding-path record", 'cat /etc/resolv.conf > "$container_resolv"'),
        ("the forwarding-path parse", 'parse_ext_servers "$container_resolv"'),
        ("the DOCKER-USER insert", DOCKER_USER_INSERT),
        ("the capture start", 'sudo tcpdump -i "$bridge_interface" -w "$capture"'),
        ("the host DNS block", "sudo iptables -I OUTPUT 1 -p udp --dport 53 ! -d 127.0.0.0/8"),
        ("the blocked-name probe", 'blocked_out=$(timeout 60 docker run --rm --network kind "$probe_image" nslookup "$blocked_name" 2>&1)'),
        ("the offline-run.sh call", OFFLINE_CALL),
        ("the capture liveness check", CAPTURE_STOPPED),
        ("stop_capture", "\nstop_capture\n"),
        ("the egress gate call", GATE_CALL),
        ("the final public-egress check", FINAL_CHECK),
    )))
    if "exit 1" not in block(text, GATE_CALL):
        failures.append("the egress gate call must exit 1 when the gate cannot be written or split")
    if "exit 1" not in block(text, FINAL_CHECK):
        failures.append("the final public-egress check must exit 1 when the gate file is non-empty")
    if "exit 1" not in block(text, DOCKER_USER_CHECK):
        failures.append("the DOCKER-USER insert must exit 1 when the block is not installed")
    liveness = [match.start() for match in re.finditer(re.escape(CAPTURE_ALIVE_CHECK), text)]
    if len(liveness) != 2:
        failures.append("the capture liveness check must run at start and again before stop_capture")
    else:
        if not text.find(OFFLINE_CALL) < liveness[1] < text.find("\nstop_capture\n"):
            failures.append("the capture liveness check must run after offline-run.sh and before stop_capture")
        if "exit 1" not in block_at(text, liveness[1]):
            failures.append("the capture liveness check must exit 1 when tcpdump has exited")
    checks = [match.start() for match in re.finditer(re.escape(DOCKER_USER_CHECK), text)]
    offline_at = text.find(OFFLINE_CALL)
    if len(checks) != 2:
        failures.append("the DOCKER-USER block must be verified after the insert and again after the deploy")
    else:
        if checks[1] < offline_at:
            failures.append("the post-deploy DOCKER-USER verification must run after offline-run.sh")
        if "exit 1" not in block_at(text, checks[1]):
            failures.append("the post-deploy DOCKER-USER verification must exit 1 when the block is gone")
    outside = run
    for name in DNS_FUNCTIONS:
        outside = outside.replace(shell_function(run, name), "")
    if re.search(r"dns_upstreams\+?=", outside):
        failures.append("dns_upstreams must be set only by parse_ext_servers, not at the call sites")
    if f'-w "$capture" {CAPTURE_FILTER}' not in text:
        failures.append(f"the capture must record with the full public-destination BPF filter {CAPTURE_FILTER}")
    gate_refs = (
        GATE_CALL.count(GATE_FILE)
        + block(text, FINAL_CHECK).count(GATE_FILE)
        + step_text(EVIDENCE_STEP).count(GATE_FILE)
    )
    if WORKFLOW.count(GATE_FILE) != gate_refs:
        failures.append(f"{GATE_FILE} must be referenced only by the gate call, the final check and the evidence list")
    if "no excludable IPv4 container-namespace DNS upstreams, the gate is unfiltered" not in run:
        failures.append("the unfiltered gate message must say there are no excludable IPv4 upstreams")
    for needle, why in (
        (r"PROBE: forwarding path [^\n]*\| tee -a \"\$dns_probe\"", "the forwarding-path PROBE line"),
        (r"ExtServers: [^\n]*\| tee -a \"\$dns_probe\"", "the ExtServers line"),
    ):
        if not re.search(needle, text):
            failures.append(f"the deploy step must write {why} to the step log with tee -a \"$dns_probe\"")
    for needle, why in (
        ("ExtServers", "the container resolv.conf ExtServers line"),
        ("/etc/docker/daemon.json", "the daemon.json diagnostic"),
        ("/run/systemd/resolve/resolv.conf", "the systemd-resolved diagnostic"),
    ):
        if needle not in step:
            failures.append(f"the deploy step must record {why} ({needle})")
    for stale in ("hole this test closes", "hole exists", "never crosses the kind bridge"):
        if stale in WORKFLOW:
            failures.append(f"the bridge-only wording {stale!r} must be gone from the workflow")
    if "/tmp/airgap-public-egress-all.txt" not in step_text(EVIDENCE_STEP):
        failures.append("the evidence step must store /tmp/airgap-public-egress-all.txt")
    return failures


def run_bash(script: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script, "bash", *args], check=False, capture_output=True, text=True, env=env)


def check_dns_snippets() -> list[str]:
    failures = []
    run = step_run(DEPLOY_STEP)
    missing = [name for name in DNS_FUNCTIONS if not shell_function(run, name)]
    if missing:
        return [f"the deploy step must define the DNS helper functions: {', '.join(missing)}"]
    functions = "".join(shell_function(run, name) for name in DNS_FUNCTIONS)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        run_file = tmp_dir / "run.sh"
        run_file.write_text(run)
        syntax = subprocess.run(["bash", "-n", str(run_file)], check=False, capture_output=True, text=True)
        if syntax.returncode != 0:
            failures.append(f"bash -n rejects the deploy step run block: {syntax.stderr.strip()}")
        if shutil.which("shellcheck"):
            # SC2024: sudo covers only tcpdump; the redirect writes a user-owned file (the pattern main already used).
            lint = subprocess.run(
                ["shellcheck", "-s", "bash", "--severity=warning", "-e", "SC2024", str(run_file)],
                check=False, capture_output=True, text=True,
            )
            if lint.returncode != 0:
                failures.append(f"shellcheck reports warnings in the deploy step run block:\n{lint.stdout}")
        elif os.environ.get("CI"):
            failures.append("shellcheck must be installed in CI for the deploy step run block lint")
        else:
            print("shellcheck not found: skipping the run block lint")

        parser_script = HELPER_SHELL + functions + (
            'parse_ext_servers "$1"\n'
            "printf '%s\\n%s\\n%s\\n' \"$dns_path\" \"$dns_upstreams\" \"$dns_notes\"\n"
        )
        parser_cases = (
            ("all host()", "# ExtServers: [host(10.1.2.3) host(10.4.5.6)]\n",
             "host namespace", "", ""),
            ("all plain", "# ExtServers: [1.1.1.1 8.8.8.8]\n",
             "container namespace", "1.1.1.1 8.8.8.8", ""),
            ("mixed", "# ExtServers: [host(127.0.0.53) 1.1.1.1]\n",
             "mixed", "1.1.1.1", ""),
            ("missing line", "nameserver 127.0.0.11\noptions ndots:0\n",
             "unknown (no ExtServers line)", "", ""),
            ("malformed entries", "# ExtServers: [1.1.1.999 foo host(bar) 8.8.8.8 010.1.1.1 1.1.1.1;x]\n",
             "container namespace", "8.8.8.8", "malformed ExtServers entries ignored: 5"),
            ("IPv6 container upstream", "# ExtServers: [2001:db8::53]\n",
             "container namespace", "", "1 IPv6 container-namespace upstream(s) not excluded from the gate"),
            ("only malformed", "# ExtServers: [foo]\n",
             "unknown (no usable ExtServers entries)", "", "malformed ExtServers entries ignored: 1"),
        )
        for label, content, want_path, want_upstreams, want_notes in parser_cases:
            resolv = tmp_dir / "resolv.conf"
            resolv.write_text(content)
            result = run_bash(parser_script, str(resolv))
            got = result.stdout.splitlines()
            if result.returncode != 0 or got[:3] != [want_path, want_upstreams, want_notes]:
                failures.append(
                    f"parse_ext_servers ({label}): want path={want_path!r} upstreams={want_upstreams!r} "
                    f"notes={want_notes!r}, got rc={result.returncode} {got!r} {result.stderr.strip()}"
                )

        failures.extend(check_gate(functions, tmp_dir))
    return failures


def ipv4_frame(src: str, dst: str, proto: int, dport: int, sport: int) -> bytes:
    if proto == 17:
        l4 = struct.pack("!HHHH", sport, dport, 8, 0)
    elif proto == 132:
        l4 = struct.pack("!HHII", sport, dport, 0, 0)
    else:
        l4 = struct.pack("!HHIIBBHHH", sport, dport, 0, 0, 5 << 4, 0x02, 65535, 0, 0)
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(l4), 0, 0, 64, proto, 0,
                     socket.inet_aton(src), socket.inet_aton(dst))
    return bytes(12) + b"\x08\x00" + ip + l4


def write_pcap(path: Path, frames: list[bytes]) -> None:
    with open(path, "wb") as out:
        out.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        for index, data in enumerate(frames):
            out.write(struct.pack("<IIII", index, 0, len(data), len(data)))
            out.write(data)


def write_exec(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def packet_sig(index: int) -> str:
    src, dst, _, dport, *sport = GATE_FRAMES[index]
    return f"{src}.{sport[0] if sport else SPORT_BASE + index} > {dst}.{dport}"


def run_gate(gate_script: str, env: dict, case_dir: Path, capture: Path, upstreams: str, shim: tuple | None = None):
    case_dir.mkdir()
    all_out, gate_out, count = case_dir / "all.txt", case_dir / "gate.txt", case_dir / "reads"
    run_env = dict(env, SHIM_COUNT=str(count), SHIM_MODE="none", SHIM_AT="0")
    if shim:
        run_env.update(SHIM_MODE=shim[0], SHIM_AT=str(shim[1]))
    result = run_bash(gate_script, str(capture), str(all_out), str(gate_out), upstreams, env=run_env)
    reads = int(count.read_text().strip()) if count.exists() else 0
    all_lines = all_out.read_text().splitlines() if all_out.exists() else []
    gate_lines = gate_out.read_text().splitlines() if gate_out.exists() else []
    return result, all_lines, gate_lines, reads, gate_out


def check_gate(functions: str, tmp_dir: Path) -> list[str]:
    tcpdump = shutil.which("tcpdump")
    if not tcpdump:
        if os.environ.get("CI"):
            return ["tcpdump must be installed in CI for the egress gate test"]
        print("tcpdump not found: skipping the egress gate test")
        return []
    failures = []
    capture = tmp_dir / "capture.pcap"
    write_pcap(capture, [ipv4_frame(src, dst, proto, dport, sport[0] if sport else SPORT_BASE + index)
                         for index, (src, dst, proto, dport, *sport) in enumerate(GATE_FRAMES)])
    shim_dir = tmp_dir / "bin"
    shim_dir.mkdir()
    write_exec(shim_dir / "sudo", '#!/bin/sh\nexec "$@"\n')
    write_exec(shim_dir / "tcpdump", TCPDUMP_SHIM)
    env = dict(os.environ, PATH=f"{shim_dir}{os.pathsep}{os.environ['PATH']}", REAL_TCPDUMP=tcpdump)
    # Called as the step calls it: inside `if !`, where errexit is off, so each `|| return 1` must hold alone.
    gate_script = HELPER_SHELL + functions + 'if ! write_egress_gate "$1" "$2" "$3" "$4"; then\n  exit 1\nfi\n'

    for index, (label, upstreams, excluded) in enumerate(GATE_CASES):
        result, all_lines, gate_lines, reads, _ = run_gate(
            gate_script, env, tmp_dir / f"case-{index}", capture, upstreams)
        if result.returncode != 0:
            failures.append(f"write_egress_gate ({label}) failed: {result.stdout} {result.stderr.strip()}")
            continue
        want_reads = 3 if upstreams else 1
        if reads != want_reads:
            failures.append(f"write_egress_gate ({label}): want {want_reads} tcpdump reads, got {reads}")
        for packet in range(len(GATE_FRAMES)):
            sig = packet_sig(packet)
            if sum(sig in line for line in all_lines) != 1:
                failures.append(f"write_egress_gate ({label}): the unfiltered read must keep {sig}")
            want = 0 if packet in excluded else 1
            got = sum(sig in line for line in gate_lines)
            if got != want:
                failures.append(f"write_egress_gate ({label}): {sig} in the gate: want {want}, got {got}")
        if upstreams and f"excluded {len(excluded)} " not in result.stdout:
            failures.append(f"write_egress_gate ({label}): want the excluded count {len(excluded)} in the step log, got {result.stdout.strip()!r}")
        if not upstreams and "unfiltered" not in result.stdout:
            failures.append(f"write_egress_gate ({label}): the step log must say the gate is unfiltered, got {result.stdout.strip()!r}")

    # A failed or truncated read must stop the gate: no later read, and a split mismatch only where it is the cause.
    two_upstreams = GATE_CASES[1][1]
    for mode, at, want_reads, want_split in (
        ("fail", 1, 1, False),
        ("fail", 2, 2, False),
        ("fail", 3, 3, False),
        ("short", 2, 3, True),
        ("short", 3, 3, True),
    ):
        label = f"tcpdump {mode} on read {at}"
        result, _, _, reads, _ = run_gate(
            gate_script, env, tmp_dir / f"shim-{mode}-{at}", capture, two_upstreams, shim=(mode, at))
        if result.returncode == 0:
            failures.append(f"write_egress_gate must fail when {label}")
        if reads != want_reads:
            failures.append(f"write_egress_gate ({label}): want {want_reads} tcpdump reads, got {reads}")
        if ("does not split" in result.stdout) != want_split:
            failures.append(f"write_egress_gate ({label}): split check message expected={want_split}, got {result.stdout.strip()!r}")

    result, _, gate_lines, _, gate_out = run_gate(
        gate_script, env, tmp_dir / "missing", tmp_dir / "missing.pcap", "8.8.8.8")
    if result.returncode == 0:
        failures.append("write_egress_gate must fail when tcpdump cannot read the capture")
    if gate_out.exists() and gate_out.stat().st_size == 0:
        failures.append("a failed capture read must not leave an empty gate file that passes")
    return failures


def main() -> int:
    failures = []

    on_block = re.search(r"^on:\n(.*?)(?=^\w)", WORKFLOW, re.S | re.M).group(1)
    triggers = set(re.findall(r"^  (\w+):", on_block, re.M))
    if triggers != {"schedule", "workflow_dispatch"}:
        failures.append(f"triggers must be schedule and workflow_dispatch only, got {sorted(triggers)}")

    build = job_block("build")
    if re.search(r"^    if:", build, re.M):
        failures.append("the build job must not carry an if: guard (it must run on forks and any branch)")
    if "timeout-minutes: 15" not in build:
        failures.append("the build job timeout must be 15 minutes")
    if "timeout-minutes: 30" not in job_block("isolated-deploy"):
        failures.append("the deploy job timeout must be 30 minutes")

    deploy = job_block("isolated-deploy")
    offline_run = deploy.find("airgap/scripts/offline-run.sh")
    for needle in (
        "sudo sysctl -w fs.inotify.max_user_watches=524288",
        "sudo sysctl -w fs.inotify.max_user_instances=512",
    ):
        at = deploy.find(needle)
        if at == -1:
            failures.append(f"isolated-deploy must run `{needle}` (see docs/airgap.md: inotify limits)")
        elif offline_run == -1 or at > offline_run:
            failures.append(f"`{needle}` must run before offline-run.sh (see docs/airgap.md: inotify limits)")

    update_rollback = job_block("update-rollback")
    if not update_rollback:
        failures.append("the update-rollback job is missing (#369)")
    else:
        if "timeout-minutes: 15" not in update_rollback:
            failures.append("the update-rollback job timeout must be 15 minutes")
        if "unshare --net" not in update_rollback:
            failures.append("the update-rollback run step must run under unshare --net")
        if "--zarf-package" not in update_rollback:
            failures.append("the update-rollback run step must pass --zarf-package")
        if re.search(r"^    if:", update_rollback, re.M):
            failures.append("the update-rollback job must not carry an if: guard")

    if "/tmp/airgap-workload-debug.txt" not in WORKFLOW:
        failures.append("the workflow must upload /tmp/airgap-workload-debug.txt")

    for needle, why in (
        ('--registry-mode=nodeport --components="" --timeout 1m --confirm', "zarf init timeout"),
        ('package deploy "$PACKAGE" --timeout 5m --confirm', "zarf deploy timeout"),
        ("seq 1 80", "20m workload cluster wait"),
    ):
        if needle not in OFFLINE_RUN:
            failures.append(f"offline-run.sh: missing {why}: {needle}")
    if "fluxinstance/flux --timeout=1m" not in ZARF:
        failures.append("zarf.yaml: the FluxInstance wait must time out after 1m")

    failures.extend(check_dns_guards())
    failures.extend(check_dns_snippets())

    if failures:
        print("air-gapped workflow config check FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("air-gapped workflow config OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
