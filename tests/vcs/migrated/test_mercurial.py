"""Contains functional tests of the Mercurial class."""

import configparser
import os
from pathlib import Path

from cpip.vcs.mercurial import Mercurial
from cpip.vcs.support import hide_url
from cpip_test_support import need_mercurial


@need_mercurial
def test_mercurial_switch_updates_config_file_when_found(tmp_path: Path) -> None:
    hg = Mercurial()
    options = hg.make_rev_options()
    hg_dir = os.path.join(tmp_path, ".hg")
    os.mkdir(hg_dir)

    config = configparser.RawConfigParser()
    config.add_section("paths")
    config.set("paths", "default", "old_url")

    hgrc_path = os.path.join(hg_dir, "hgrc")
    with open(hgrc_path, "w") as f:
        config.write(f)
    hg.switch(os.fspath(tmp_path), hide_url("new_url"), options)

    config.read(hgrc_path)

    default_path = config.get("paths", "default")
    assert default_path == "new_url"
