#!/usr/bin/env python3
"""The toolbox image must run mise with auto-install disabled (#601).

The image carries the full pinned tool layer and deliberately omits the
dev-only toolchains (go, zarf). With mise auto-install on, any lifecycle
`mise run` that touches a dev-only pin downloads it at runtime, an
unexpected outbound dependency mid-bootstrap. The gate: the single ENV
block sets MISE_AUTO_INSTALL=0 alongside the other mise settings, no
non-comment directive re-enables it, and the block precedes the tool
install RUN so the whole runtime stage runs with it.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "bootstrap-rs/Dockerfile"


def directive_blocks(text):
    """Yield (kind, first_line_index, block_text) for every ENV and RUN
    directive, joining backslash-continued lines."""
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        stripped = lines[index].lstrip()
        if stripped.startswith("ENV ") or stripped.startswith("RUN "):
            kind = stripped.split()[0]
            start = index
            block = [lines[index]]
            while block[-1].rstrip().endswith("\\") and index + 1 < len(lines):
                index += 1
                block.append(lines[index])
            yield kind, start, "\n".join(block)
        index += 1


def main():
    dockerfile = DOCKERFILE.read_text()
    directives = list(directive_blocks(dockerfile))

    envs = [(start, block) for kind, start, block in directives
            if kind == "ENV"]
    assert len(envs) == 1, (
        f"expected exactly one ENV directive in the Dockerfile, "
        f"found {len(envs)}")
    env_start, env_block = envs[0]
    env_lines = [line.strip() for line in
                 env_block.replace(" \\\n", "\n").splitlines()]
    env_lines[0] = env_lines[0][len("ENV "):].strip()
    assert "MISE_AUTO_INSTALL=0" in env_lines, (
        f"the ENV block must set MISE_AUTO_INSTALL=0: {env_block!r}")
    for setting in ("MISE_DATA_DIR=", "MISE_YES="):
        assert any(line.startswith(setting) for line in env_lines), (
            f"MISE_AUTO_INSTALL belongs in the mise environment block that "
            f"sets {setting}: {env_block!r}")

    reenabled = [
        line.strip() for line in dockerfile.splitlines()
        if not line.lstrip().startswith("#")
        and re.search(r"MISE_AUTO_INSTALL\s*=\s*[1Tt]", line)
    ]
    assert not reenabled, (
        "no directive may re-enable mise auto-install: "
        f"{reenabled!r}")

    install_runs = [
        start for kind, start, block in directives
        if kind == "RUN" and "mise install" in block
    ]
    assert install_runs, "expected the mise install RUN in the Dockerfile"
    assert env_start < min(install_runs), (
        "the ENV block must precede the mise install RUN so the whole "
        "runtime stage runs with auto-install off")

    print("toolbox mise auto-install off (image ENV block) OK")


if __name__ == "__main__":
    main()
