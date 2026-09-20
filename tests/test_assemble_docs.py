from assemble_docs import assemble, rewrite_doc_links, rewrite_readme_links

GH = "https://github.com/polarsquad/krops"


def raw(name: str) -> str:
    return f"{GH}/raw/main/docs/{name}.svg"


def test_readme_docs_links_become_site_relative():
    assert rewrite_readme_links("[a](docs/architecture.md)") == "[a](architecture.md)"


def test_readme_docs_links_keep_anchors():
    assert (
        rewrite_readme_links("[a](docs/operations.md#pivot-recovery)")
        == "[a](operations.md#pivot-recovery)"
    )


def test_readme_diagram_images_become_raw_links():
    for name in ("aws-infra", "azure-infra", "local-talos-infra"):
        assert rewrite_readme_links(f"![l](docs/{name}.svg)") == (
            f"[![l]({name}.svg)]({raw(name)})"
        )


def test_readme_root_file_links_point_at_github():
    assert rewrite_readme_links("[l](LICENSE)") == f"[l]({GH}/blob/main/LICENSE)"
    assert rewrite_readme_links("[r](renovate.json5)") == f"[r]({GH}/blob/main/renovate.json5)"


def test_readme_external_and_anchor_links_untouched():
    text = "[f](https://fluxcd.io/) [m](mailto:x@y.z) [s](#quickstart)"
    assert rewrite_readme_links(text) == text


def test_doc_parent_file_links_point_at_github():
    assert rewrite_doc_links("[b](../bootstrap.toml)") == f"[b]({GH}/blob/main/bootstrap.toml)"
    assert rewrite_doc_links("[a](../AGENTS.md)") == f"[a]({GH}/blob/main/AGENTS.md)"


def test_doc_parent_directory_links_point_at_github_tree():
    assert rewrite_doc_links("[c](../bootstrap-rs/)") == f"[c]({GH}/tree/main/bootstrap-rs)"


def test_doc_sibling_links_untouched():
    text = "[o](./operations.md#pivot-recovery) [s](secrets.md) [x](#write-back)"
    assert rewrite_doc_links(text) == text


def test_doc_diagram_images_become_raw_links():
    assert rewrite_doc_links("![d](air-gap-infra.svg)") == (
        f"[![d](air-gap-infra.svg)]({raw('air-gap-infra')})"
    )


def test_doc_plain_svg_images_untouched():
    assert rewrite_doc_links("![logo](krops-logo.svg)") == "![logo](krops-logo.svg)"


def test_assemble_builds_docs_dir(tmp_path):
    krops = tmp_path / "krops"
    (krops / "docs").mkdir(parents=True)
    (krops / "README.md").write_text(
        "# krops\n[arch](docs/architecture.md) [l](LICENSE)\n![arch-img](docs/aws-infra.svg)\n"
    )
    (krops / "docs" / "architecture.md").write_text("# Architecture\n[b](../bootstrap.toml)\n")
    (krops / "docs" / "aws-infra.svg").write_text("<svg/>")
    (krops / "docs" / "scratch.png").write_bytes(b"\x89PNG")
    src = tmp_path / "src"
    (src / "assets" / "css").mkdir(parents=True)
    (src / "assets" / "css" / "krops.css").write_text(":root{}")
    out = tmp_path / "build" / "docs"
    (out / "stale").mkdir(parents=True)

    assemble(krops=krops, src=src, out=out)

    assert (
        (out / "index.md").read_text()
        == f"# krops\n[arch](architecture.md) [l]({GH}/blob/main/LICENSE)\n"
        f"[![arch-img](aws-infra.svg)]({raw('aws-infra')})\n"
    )
    assert (out / "architecture.md").read_text() == f"# Architecture\n[b]({GH}/blob/main/bootstrap.toml)\n"
    assert (out / "aws-infra.svg").read_text() == "<svg/>"
    assert (out / "assets" / "css" / "krops.css").exists()
    assert not (out / "scratch.png").exists()
    assert not (out / "stale").exists()


def test_assemble_includes_proposals_folder(tmp_path):
    krops = tmp_path / "krops"
    (krops / "docs" / "proposals").mkdir(parents=True)
    (krops / "README.md").write_text("# krops\n")
    text = f"# P\n[abs]({GH}/blob/main/bootstrap.toml) [sib](q.md) [s](#x)\n"
    (krops / "docs" / "proposals" / "p.md").write_text(text)
    (krops / "docs" / "proposals" / "notes.txt").write_text("skip")
    src = tmp_path / "src"
    src.mkdir()
    out = tmp_path / "build" / "docs"

    assemble(krops=krops, src=src, out=out)

    assert (out / "proposals" / "p.md").read_text() == text
    assert not (out / "proposals" / "notes.txt").exists()
