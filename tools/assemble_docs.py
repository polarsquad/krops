#!/usr/bin/env python3
"""Assemble the MkDocs docs_dir from the krops repository.

Content is never edited here; only repository-relative links are rewritten so
that the rendered site resolves them (sibling pages) or sends the reader to
GitHub (files that are not part of the documentation).
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

GITHUB = "https://github.com/polarsquad/krops"

# Markdown link or image: group 1 = "[text](", group 2 = target, group 3 = ")".
LINK_RE = re.compile(r"(!?\[[^\]]*\]\()([^)\s]+)(\))")


def _is_external(target: str) -> bool:
    return target.startswith(("http://", "https://", "mailto:", "#"))


def github_url(repo_path: str) -> str:
    """URL of a file (blob) or directory (tree, trailing slash) in the krops repo."""
    kind = "tree" if repo_path.endswith("/") else "blob"
    return f"{GITHUB}/{kind}/main/{repo_path.strip('/')}"


def rewrite_readme_links(text: str) -> str:
    """README.md becomes index.md at the docs root, so docs/x -> x; other repo files -> GitHub."""

    def sub(match: re.Match[str]) -> str:
        pre, target, post = match.groups()
        if _is_external(target):
            return match.group(0)
        if target.startswith("docs/"):
            return f"{pre}{target[len('docs/'):]}{post}"
        return f"{pre}{github_url(target)}{post}"

    return LINK_RE.sub(sub, text)


def rewrite_doc_links(text: str) -> str:
    """docs/*.md stay at the docs root; only ../ links leave the documentation -> GitHub."""

    def sub(match: re.Match[str]) -> str:
        pre, target, post = match.groups()
        if _is_external(target) or not target.startswith("../"):
            return match.group(0)
        return f"{pre}{github_url(target[len('../'):])}{post}"

    return LINK_RE.sub(sub, text)


ROOT = Path(__file__).resolve().parent.parent
KROPS = ROOT
SRC = ROOT / "website"
OUT = ROOT / "build" / "docs"


def assemble(krops: Path = KROPS, src: Path = SRC, out: Path = OUT) -> Path:
    """Recreate `out`: README -> index.md, docs/*.md and docs/*.svg, then the src/ overlay."""
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / "index.md").write_text(rewrite_readme_links((krops / "README.md").read_text()))
    for path in sorted((krops / "docs").iterdir()):
        if path.suffix == ".md":
            (out / path.name).write_text(rewrite_doc_links(path.read_text()))
        elif path.suffix == ".svg":
            shutil.copy2(path, out / path.name)
    shutil.copytree(src, out, dirs_exist_ok=True)
    return out


if __name__ == "__main__":
    result = assemble()
    count = sum(1 for p in result.rglob("*") if p.is_file())
    print(f"assembled {count} files into {result}")
