from __future__ import annotations

from skyn3t.studio.visual_design_contract import (
    CORE_DESIGN_TOKENS,
    VISUAL_DESIGN_CONTRACT_RELATIVE_PATH,
    derive_visual_design_contract,
    read_visual_design_contract,
    write_visual_design_contract,
)


def test_contract_is_versioned_deterministic_and_records_real_asset_policy():
    first = derive_visual_design_contract("A golf tutorial library using supplied course photos")
    second = derive_visual_design_contract("A golf tutorial library using supplied course photos")

    assert first == second
    assert first["schema_version"] == 1
    assert first["contract_id"]
    assert all(first["tokens"][token] for token in CORE_DESIGN_TOKENS)
    assert first["imagery"]["policy"] == "prefer-user-supplied-or-licensed-real-assets"
    assert first["imagery"]["generated_imagery"] == "only-when-the-brief-explicitly-requests-it"


def test_contract_persists_only_when_it_is_a_valid_skyn3t_managed_artifact(tmp_path):
    written = write_visual_design_contract(tmp_path, "A calm bakery site")

    assert written is not None
    assert (tmp_path / VISUAL_DESIGN_CONTRACT_RELATIVE_PATH).is_file()
    assert read_visual_design_contract(tmp_path) == written

    path = tmp_path / VISUAL_DESIGN_CONTRACT_RELATIVE_PATH
    path.write_text('{"managed_by": "someone-else"}\n', encoding="utf-8")
    assert write_visual_design_contract(tmp_path, "A different bakery site") is None
    assert read_visual_design_contract(tmp_path) is None


def test_contract_records_a_seeded_best_of_n_winners_design(tmp_path):
    """A best-of-N winner generated under design seed N ships that seed's
    fonts/tokens/theme/shape/archetype; the contract must record those (and
    stay schema-valid), while seed 0 is the unchanged brief-derived contract."""
    from skyn3t.studio.design_seeds import design_seed_for

    brief = "a habit tracker for busy parents"
    assert derive_visual_design_contract(brief, design_seed_index=0) == (
        derive_visual_design_contract(brief)
    )
    for index in (1, 2, 3):
        seed = design_seed_for(brief, index)
        contract = derive_visual_design_contract(brief, design_seed_index=index)
        assert contract["design_seed_index"] == index
        assert contract["typography"] == {
            "heading": seed["font_heading"], "body": seed["font_body"],
        }
        assert contract["theme"] == seed["theme"]
        assert contract["shape_language"] == seed["style"]
        assert contract["layout_archetype"] == seed["archetype"]
        assert {k: v for k, v in contract["tokens"].items() if k in seed["tokens"]} == seed["tokens"]
        assert seed["font_heading"] in contract["tokens"]["--font-heading"]
        assert all(contract["tokens"][token] for token in CORE_DESIGN_TOKENS)
        assert contract["contract_id"] != derive_visual_design_contract(brief)["contract_id"]

    written = write_visual_design_contract(tmp_path, brief, design_seed_index=2)
    assert written is not None
    assert read_visual_design_contract(tmp_path) == written
