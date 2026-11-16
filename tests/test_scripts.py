import ast
import json

from kentucky_stac import scripts

ENTRIES = scripts.entries_for(
    [("a.tif", "s3://bkt/a.tif"), ("b.tif", "https://acct.blob.core.windows.net/box/b.tif"), ("a.tif", "dup")]
)


def test_entries_for():
    assert ENTRIES == [
        ("a.tif", "https://bkt.s3.amazonaws.com/a.tif"),
        ("b.tif", "https://acct.blob.core.windows.net/box/b.tif"),
    ]


def test_python_and_notebook_are_valid_python():
    ast.parse(scripts.render("python", ENTRIES))
    nb = json.loads(scripts.render("notebook", ENTRIES))
    assert nb["nbformat"] == 4
    for cell in nb["cells"]:
        if cell["cell_type"] == "code":
            ast.parse("".join(cell["source"]))


def test_shell_lists_every_tile():
    sh = scripts.render("shell", ENTRIES)
    assert sh.startswith("#!/usr/bin/env bash")
    assert "a.tif|https://bkt.s3.amazonaws.com/a.tif" in sh


def test_r_lists_every_tile_and_is_balanced():
    r = scripts.render("r", ENTRIES)
    assert r.startswith("#!/usr/bin/env Rscript")
    assert 'c("a.tif", "https://bkt.s3.amazonaws.com/a.tif"),' in r
    assert '  c("b.tif", "https://acct.blob.core.windows.net/box/b.tif")\n)' in r
    assert "@ROWS@" not in r and "@HEADER@" not in r
    for open_, close in ("()", "{}"):
        assert r.count(open_) == r.count(close)
