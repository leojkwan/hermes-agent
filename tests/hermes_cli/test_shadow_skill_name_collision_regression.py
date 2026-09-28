"""Regression tests: the Shadow twin-skill renames must stay collision-free.

Extends the collision-refusal contract exercised by
``tests/tools/test_skills_tool.py::TestSkillViewCollisionDetection`` to each
synthetic pair of canonical and renamed skills. Both fixture roots reach the loader as search directories.

Contract, per pair:
* clean tree — the old name resolves to exactly ONE candidate (the canonical
  copy), the renamed name to exactly ONE (the mainprobe copy), and a multi-pin
  preload loads both without dropping either.
* re-introduced duplicate — a same-named twin with DIFFERENT content in the
  second root makes the single ambiguous pin REFUSE (``Ambiguous skill name``)
  instead of silently passing; the multi-pin partial-success contract surfaces
  the collided name as missing (never silently drops or silently resolves it);
  and an all-missing pin set hits the consumer raise
  (``hermes_cli/oneshot.py::_build_preloaded_skills_prompt`` → ``Unknown
  skill(s): ...`` — the exact surface that failed in the incident).
"""

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.skills_tool import _collect_skill_candidates, skill_view

# (old name, canonical body, renamed name, mainprobe body)
PAIRS = [
    ("shadow", "CANONICAL SHADOW BODY", "shadow-mainprobe", "MAINPROBE SHADOW BODY"),
    ("amplify", "CANONICAL AMPLIFY BODY", "amplify-mainprobe", "MAINPROBE AMPLIFY BODY"),
    (
        "shadow-coach",
        "CANONICAL SHADOW-COACH BODY",
        "shadow-coach-mainprobe",
        "MAINPROBE SHADOW-COACH BODY",
    ),
]
# A skill that was never renamed: proves multi-pin partial success keeps the
# healthy pins while reporting the collided one.
UNTOUCHED = "thermo"


def _make_skill(skills_dir: Path, name: str, body: str) -> Path:
    """Minimal skill dir with frontmatter ``name`` matching the dir (same helper
    shape as tests/tools/test_skills_tool.py)."""
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Collision-regression stub for {name}.\n---\n\n"
        f"# {name}\n\n{body}\n",
        encoding="utf-8",
    )
    return skill_dir


def _canonical_root(tmp_path: Path) -> Path:
    """Stand-in for the canonical skillbox shadow tree (SKILLS_DIR)."""
    root = tmp_path / "canonical-skills"
    for old, body, _renamed, _rbody in PAIRS:
        _make_skill(root, old, body)
    _make_skill(root, UNTOUCHED, "CANONICAL THERMO BODY")
    return root


def _mainprobe_root(tmp_path: Path, *, buggy: bool) -> Path:
    """Stand-in for the mainprobe twin-carrying tree (external dir).

    ``buggy=True`` re-introduces exactly the duplicates task 1 removed: the OLD
    names, with divergent content. Divergent on purpose — byte-identical copies
    inside one root are legitimately rank-resolved by the loader; cross-root
    different-content twins are the shadowing shape the refusal exists for.
    """
    root = tmp_path / "mainprobe-skills"
    for _old, _body, renamed, rbody in PAIRS:
        _make_skill(root, renamed, rbody)
    if buggy:
        for old, _body, _renamed, _rbody in PAIRS:
            _make_skill(root, old, f"TWIN DUPLICATE {old} BODY")
    return root


def _patched_dirs(canonical: Path, mainprobe: Path):
    """Pin all three loader dir seams to just these roots (no project tier, no
    real external dirs — hermes-agent's own top-level skills/ tree must not
    leak into the resolution)."""
    return (
        patch("tools.skills_tool.SKILLS_DIR", canonical),
        patch("agent.skill_utils.get_project_skills_dirs", return_value=[]),
        patch("agent.skill_utils.get_external_skills_dirs", return_value=[mainprobe]),
    )


def _preloaded(identifiers):
    """build_preloaded_skills_prompt with operator-disabled lookups neutered."""
    from agent import skill_commands

    with patch.object(skill_commands, "_disabled_skill_names", return_value=set()):
        return skill_commands.build_preloaded_skills_prompt(list(identifiers))


class TestCleanTreeAfterRenames:
    @pytest.mark.parametrize("old,obody,renamed,rbody", PAIRS)
    def test_each_name_resolves_to_exactly_one_skill(self, tmp_path, old, obody, renamed, rbody):
        canonical = _canonical_root(tmp_path)
        mainprobe = _mainprobe_root(tmp_path, buggy=False)
        p1, p2, p3 = _patched_dirs(canonical, mainprobe)
        with p1, p2, p3:
            assert len(_collect_skill_candidates(old, None, [canonical, mainprobe])) == 1
            assert len(_collect_skill_candidates(renamed, None, [canonical, mainprobe])) == 1

            loaded_old = json.loads(skill_view(old))
            loaded_new = json.loads(skill_view(renamed))

        assert loaded_old["success"] is True
        assert obody in loaded_old["content"]
        assert old in loaded_old["path"]
        assert loaded_new["success"] is True
        assert rbody in loaded_new["content"]
        assert renamed in loaded_new["path"]

    @pytest.mark.parametrize("old,obody,renamed,rbody", PAIRS)
    def test_multi_pin_loads_both_sides_of_a_pair(self, tmp_path, old, obody, renamed, rbody):
        canonical = _canonical_root(tmp_path)
        mainprobe = _mainprobe_root(tmp_path, buggy=False)
        p1, p2, p3 = _patched_dirs(canonical, mainprobe)
        with p1, p2, p3:
            prompt, loaded, missing = _preloaded([old, renamed, UNTOUCHED])

        assert missing == []
        assert loaded == [old, renamed, UNTOUCHED]
        assert obody in prompt
        assert rbody in prompt
        assert "CANONICAL THERMO BODY" in prompt


class TestReintroducedDuplicateFailsFast:
    @pytest.mark.parametrize("old,obody,renamed,rbody", PAIRS)
    def test_single_ambiguous_pin_refuses_instead_of_silently_passing(
        self, tmp_path, old, obody, renamed, rbody
    ):
        canonical = _canonical_root(tmp_path)
        mainprobe = _mainprobe_root(tmp_path, buggy=True)
        p1, p2, p3 = _patched_dirs(canonical, mainprobe)
        with p1, p2, p3:
            candidates = _collect_skill_candidates(old, None, [canonical, mainprobe])
            assert len(candidates) == 2, "fixture must reproduce the duplicate pair"

            result = json.loads(skill_view(old))

        assert result["success"] is False
        assert f"Ambiguous skill name '{old}'" in result["error"]
        assert len(result["matches"]) == 2
        assert any(str(canonical) in m for m in result["matches"])
        assert any(str(mainprobe) in m for m in result["matches"])

        # The RENAMED sibling of the pair is unaffected by a twin of the old name.
        with p1, p2, p3:
            sibling = json.loads(skill_view(renamed))
        assert sibling["success"] is True
        assert rbody in sibling["content"]

    @pytest.mark.parametrize("old,obody,renamed,rbody", PAIRS)
    def test_multi_pin_reports_collision_without_silently_dropping_the_rest(
        self, tmp_path, old, obody, renamed, rbody, caplog
    ):
        """cli.py's contract: a typo'd pin must not crash a kanban worker, so a
        partial set warns and continues — but the collided name must land in
        the MISSING report, never silently resolve to one side or vanish."""
        canonical = _canonical_root(tmp_path)
        mainprobe = _mainprobe_root(tmp_path, buggy=True)
        p1, p2, p3 = _patched_dirs(canonical, mainprobe)
        with p1, p2, p3, caplog.at_level(logging.WARNING):
            prompt, loaded, missing = _preloaded([old, UNTOUCHED])

        assert missing == [old]
        assert loaded == [UNTOUCHED]
        assert "CANONICAL THERMO BODY" in prompt
        assert old not in loaded
        assert any(old in rec.getMessage() for rec in caplog.records)

    @pytest.mark.parametrize("old,obody,renamed,rbody", PAIRS)
    def test_pin_where_every_name_is_missing_raises_unknown_skills(
        self, tmp_path, old, obody, renamed, rbody
    ):
        """The incident's hard-fail: when EVERY pinned skill is missing, the
        oneshot preload path raises ``Unknown skill(s): ...`` (oneshot.py raises
        only when loaded is empty). A re-introduced duplicate must land the
        collided name in that raise, not pretend it loaded."""
        from hermes_cli.oneshot import _build_preloaded_skills_prompt

        canonical = _canonical_root(tmp_path)
        mainprobe = _mainprobe_root(tmp_path, buggy=True)
        p1, p2, p3 = _patched_dirs(canonical, mainprobe)
        with p1, p2, p3:
            with pytest.raises(ValueError, match="Unknown skill"):
                _build_preloaded_skills_prompt([old])
