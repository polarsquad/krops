#!/usr/bin/env python3
"""The toolbox image must resolve bare aws/clusterawsadm shims (#635).

aws-cli and clusterawsadm are pinned only in mise.aws.toml, which mise loads
only inside the `aws` env layer. The checkout mounted at /workspace at run
time carries no env layer, so without a global binding a bare `aws` shim
fails with "No version is set for shim: aws" mid-bootstrap. The gate: the
Dockerfile bakes the exact pins into the image's global mise config with
`mise -E aws use -g` (before `mise reshim`), and a separate smoke RUN
executes both tools bare so a shim-resolution regression fails the build.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "bootstrap-rs/Dockerfile"


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
    blocks = list(run_blocks(dockerfile))

    binding_runs = [(start, block) for start, block in blocks
                    if "mise -E aws use -g" in block]
    assert len(binding_runs) == 1, (
        f"expected exactly one RUN that bakes the global aws bindings "
        f"(`mise -E aws use -g`), found {len(binding_runs)}")
    binding_start, binding_block = binding_runs[0]
    for tool in ("aws-cli", "clusterawsadm"):
        pin = f'"{tool}@$(mise -E aws current {tool})"'
        assert pin in binding_block, (
            f"the binding RUN must pin {tool} from the aws env layer's "
            f"resolved version ({pin!r}); a bare or hardcoded version "
            f"would bake a stale or floating pin: {binding_block!r}")
    reshim = binding_block.find("mise reshim")
    assert reshim != -1, (
        f"the binding RUN must regenerate the shims with `mise reshim`: "
        f"{binding_block!r}")
    assert binding_block.find("mise -E aws use -g") < reshim, (
        "the global bindings must be written before `mise reshim` "
        f"so the shims resolve them: {binding_block!r}")

    smoke_runs = [(start, block) for start, block in blocks
                  if start != binding_start
                  and re.search(r"(^|\s)aws --version(\s|$)", block)
                  and "clusterawsadm version" in block]
    assert len(smoke_runs) == 1, (
        "expected exactly one smoke RUN executing both tools bare, "
        f"found {len(smoke_runs)}")
    smoke_start, smoke_block = smoke_runs[0]
    assert smoke_start > binding_start, (
        "the smoke RUN must come after the RUN that bakes the global "
        "bindings, so it resolves through them")
    for not_bare in ("MISE_ENV", "mise exec", "mise x ", "mise run",
                     "mise use"):
        assert not_bare not in smoke_block, (
            f"the smoke RUN must invoke the tools bare (no {not_bare!r}): "
            f"{smoke_block!r}")

    print("toolbox bare aws/clusterawsadm shims (global bindings, smoke "
          "RUN) OK")


if __name__ == "__main__":
    main()
