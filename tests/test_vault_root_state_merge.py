"""council-map's settings files at the vault root are the app's, not the
user's data: the startup copy into data_in/ (council_core.vault_setup) must
skip them and the vault search must not find them (conversation_logger).

Both lists came from qt-migration; the files came from council-map, so
neither branch alone could have listed them."""
from __future__ import annotations

import json

from council_core import vault_setup
import conversation_logger

COUNCIL_MAP_ROOT_FILES = ("node_routing.json", "quant_duel.json")


def test_settings_files_are_not_copied_in_as_data(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    for name in COUNCIL_MAP_ROOT_FILES:
        (vault / name).write_text(json.dumps({"settings": True}))
    (vault / "sales.csv").write_text("a,b\n1,2\n")       # real user data
    copied = {p.name for p in vault_setup.copy_loose_data_files(vault)}
    assert copied == {"sales.csv"}


def test_settings_files_are_kept_out_of_vault_search(tmp_path):
    for name in COUNCIL_MAP_ROOT_FILES:
        assert conversation_logger.is_protected_path(tmp_path / name, tmp_path)
