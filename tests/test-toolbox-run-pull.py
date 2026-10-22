#!/usr/bin/env python3
"""toolbox-run.sh must revalidate remote-registry TOOLBOX_IMAGE references
against the registry on every run (#645): a locally-present mutable tag
(e.g. :latest) is never revalidated by the engine's run, so a fresh release
never reaches a host that already pulled it. The wrapper pulls remote refs
best-effort: a failed pull must warn on stderr and continue with the local
cache, never aborting a lifecycle operation. Local builds (bare name[:tag])
have no registry source and must never be pulled."""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "scripts/toolbox-run.sh"

ENGINE_STUB = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["ENGINE_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1] == "pull" and os.environ.get("FAKE_PULL_FAIL") == "1":
    sys.stderr.write("fake pull failure\\n")
    sys.exit(1)
sys.exit(0)
"""

DIGEST = "sha256:" + "a" * 64


def run_once(image, pull_fail=False):
    """Run the wrapper against a fake docker in an isolated fake repo;
    returns (invocations, returncode, stderr). invocations is the ordered
    list of argv (engine name excluded) the engine was called with."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # toolbox-run.sh always cds to its own repo root ("$(dirname
        # "$0")/.."); copy it into an isolated fake repo so the run (and the
        # .kube/ dir it creates) never touches the real checkout.
        fake_repo = tmp / "repo"
        (fake_repo / "scripts").mkdir(parents=True)
        wrapper = fake_repo / "scripts/toolbox-run.sh"
        shutil.copy(WRAPPER, wrapper)
        os.chmod(wrapper, 0o755)
        bin_dir = tmp / "bin"; bin_dir.mkdir()
        stub = bin_dir / "docker"
        stub.write_text(ENGINE_STUB)
        os.chmod(stub, 0o755)
        log = tmp / "engine.log"
        env = {k: v for k, v in os.environ.items()
               if k not in ("TOOLBOX_IMAGE", "CONTAINER_ENGINE", "KROPS_PROFILE",
                            "FAKE_PULL_FAIL", "ENGINE_LOG")}
        env.update(
            HOME=str(tmp / "home"),
            PATH=f"{bin_dir}:{env['PATH']}",
            CONTAINER_ENGINE="docker",
            ENGINE_LOG=str(log),
        )
        if image is not None:
            env["TOOLBOX_IMAGE"] = image
        if pull_fail:
            env["FAKE_PULL_FAIL"] = "1"
        result = subprocess.run(
            ["bash", str(wrapper), "bootstrap", "local-host"],
            env=env, cwd=fake_repo, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, check=False,
        )
        invocations = (
            [json.loads(line) for line in log.read_text().splitlines()]
            if log.exists() else []
        )
        return invocations, result.returncode, result.stderr


def pulls(invocations):
    return [i for i, argv in enumerate(invocations) if argv and argv[0] == "pull"]


def runs(invocations):
    return [i for i, argv in enumerate(invocations) if argv and argv[0] == "run"]


def test_default_remote_ref_is_pulled_before_run() -> None:
    invocations, rc, _ = run_once(None)
    if rc != 0:
        raise AssertionError(f"wrapper exited {rc}")
    pull_idxs = pulls(invocations)
    run_idxs = runs(invocations)
    if not pull_idxs:
        raise AssertionError(f"default remote image must be pulled: {invocations}")
    if not run_idxs:
        raise AssertionError(f"run invocation missing: {invocations}")
    if pull_idxs[0] >= run_idxs[0]:
        raise AssertionError(f"pull must precede run: {invocations}")
    image = invocations[pull_idxs[0]][1]
    if image != "ghcr.io/polarsquad/krops-toolbox:latest":
        raise AssertionError(f"wrong image pulled: {image}")


def test_remote_ref_with_tag_is_pulled() -> None:
    invocations, rc, _ = run_once("ghcr.io/polarsquad/krops-toolbox:0.3.1")
    if rc != 0:
        raise AssertionError(f"wrapper exited {rc}")
    pull_idxs = pulls(invocations)
    if not pull_idxs:
        raise AssertionError(f"remote ref with tag must be pulled: {invocations}")
    image = invocations[pull_idxs[0]][1]
    if image != "ghcr.io/polarsquad/krops-toolbox:0.3.1":
        raise AssertionError(f"wrong image pulled: {image}")


def test_digest_pinned_remote_ref_is_pulled() -> None:
    invocations, rc, _ = run_once(f"ghcr.io/polarsquad/krops-toolbox@{DIGEST}")
    if rc != 0:
        raise AssertionError(f"wrapper exited {rc}")
    pull_idxs = pulls(invocations)
    if not pull_idxs:
        raise AssertionError(f"digest-pinned remote ref must be pulled: {invocations}")
    image = invocations[pull_idxs[0]][1]
    if image != f"ghcr.io/polarsquad/krops-toolbox@{DIGEST}":
        raise AssertionError(f"wrong image pulled: {image}")


def test_local_builds_are_never_pulled() -> None:
    for image in ("krops-toolbox:dev", "toolbox:test"):
        invocations, rc, _ = run_once(image)
        if rc != 0:
            raise AssertionError(f"wrapper exited {rc} for {image}")
        if pulls(invocations):
            raise AssertionError(f"local build {image} must never be pulled: {invocations}")
        if not runs(invocations):
            raise AssertionError(f"run invocation missing for {image}: {invocations}")


def test_pull_failure_warns_and_continues() -> None:
    invocations, rc, stderr = run_once("ghcr.io/polarsquad/krops-toolbox:latest", pull_fail=True)
    if rc != 0:
        raise AssertionError(f"wrapper must not abort on a failed pull: rc={rc}, stderr={stderr!r}")
    if not pulls(invocations):
        raise AssertionError(f"pull attempt missing: {invocations}")
    if not runs(invocations):
        raise AssertionError(f"run must still happen after a failed pull: {invocations}")
    if "WARNING" not in stderr or "krops-toolbox:latest" not in stderr:
        raise AssertionError(f"failed pull must warn on stderr naming the image: {stderr!r}")


def test_localhost_registry_is_remote() -> None:
    invocations, rc, _ = run_once("localhost:5000/krops-toolbox:dev")
    if rc != 0:
        raise AssertionError(f"wrapper exited {rc}")
    if not pulls(invocations):
        raise AssertionError(f"localhost:5000/... must be treated as remote: {invocations}")


def main() -> int:
    tests = [
        test_default_remote_ref_is_pulled_before_run,
        test_remote_ref_with_tag_is_pulled,
        test_digest_pinned_remote_ref_is_pulled,
        test_local_builds_are_never_pulled,
        test_pull_failure_warns_and_continues,
        test_localhost_registry_is_remote,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"ok - {test.__name__}")
        except AssertionError as exc:
            print(f"FAILED: {test.__name__}: {exc}", file=sys.stderr)
            failed += 1
    if failed:
        return 1
    print("toolbox-run.sh image refresh OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
