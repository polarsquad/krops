#!/usr/bin/env python3
"""The toolbox image must trust the checkout mount as a git safe.directory (#485).

The container runs as root while the bind-mounted checkout at /workspace is
owned by the host user; on native rootful Linux Docker git then refuses the
repository ("detected dubious ownership"). The gate: the image marks exactly
that mount a safe.directory at build time in system scope (never a wildcard),
the oci-push task no longer hides git's own error, and the wrapper's mount
contract still points at the trusted path.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "bootstrap-rs/Dockerfile"
MISE_LOCAL_HOST = REPO_ROOT / "mise.local-host.toml"
WRAPPER = REPO_ROOT / "scripts/toolbox-run.sh"


def run_blocks(text):
    """Yield (first_line_index, block_text) for every RUN directive,
    joining backslash-continued lines."""
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        if lines[index].lstrip().startswith("RUN "):
            start = index
            block = [lines[index]]
            while block[-1].rstrip().endswith("\\") and index + 1 < len(lines):
                index += 1
                block.append(lines[index])
            yield start, "\n".join(block)
        index += 1


def main():
    dockerfile = DOCKERFILE.read_text()
    config_blocks = [(start, block) for start, block in run_blocks(dockerfile)
                     if "safe.directory" in block]
    assert len(config_blocks) == 1, (
        f"expected exactly one safe.directory RUN in the Dockerfile, "
        f"found {len(config_blocks)}")
    config_start, config_block = config_blocks[0]
    match = re.search(r"git config --system --add safe\.directory (\S+)",
                      config_block)
    assert match, f"the RUN must be system scope: {config_block!r}"
    value = match.group(1)
    assert value == "/workspace", (
        f"the trusted path must be the checkout mount exactly, got {value!r}")
    assert "*" not in config_block, (
        f"safe.directory must not fall back to the wildcard opt-out: "
        f"{config_block!r}")
    git_blocks = [(start, block) for start, block in run_blocks(dockerfile)
                  if "apt-get install" in block
                  and re.search(r"(^|\s)git\s", block)]
    assert len(git_blocks) == 1, (
        f"expected the apt RUN that installs git, found {len(git_blocks)}")
    assert git_blocks[0][0] < config_start, (
        "the safe.directory RUN must come after the RUN that installs git")

    section = MISE_LOCAL_HOST.read_text().split("[tasks.oci-push]", 1)[1]
    section = section.split("[tasks.kubeconfigs]", 1)[0]
    parse_lines = [line for line in section.splitlines()
                   if "git rev-parse HEAD" in line]
    assert len(parse_lines) == 1, (
        f"expected one git rev-parse HEAD line in oci-push, "
        f"found {len(parse_lines)}")
    assert "2>/dev/null" not in parse_lines[0], (
        "git's own error must surface when the fallback fails: "
        f"{parse_lines[0]!r}")

    mount = re.search(r'-v "\$REPO_ROOT:([^"]+)"', WRAPPER.read_text())
    assert mount, "the wrapper must still mount the checkout at a fixed path"
    assert mount.group(1) == value, (
        f"the wrapper mounts the checkout at {mount.group(1)!r} but the "
        f"image trusts {value!r}; the trust entry must track the mount")

    print("toolbox git safe.directory (image, oci-push, mount contract) OK")


if __name__ == "__main__":
    main()
