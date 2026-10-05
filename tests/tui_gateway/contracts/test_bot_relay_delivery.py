"""The selected Bot Chat delivery pair is optional but must be complete and well typed."""

import pytest
from pydantic import ValidationError

from tui_gateway.contracts.groups_bot_relay import BotRelayDeliverParams


def test_legacy_and_selected_delivery_contract_shapes():
    assert BotRelayDeliverParams.model_validate({"profile": "ops", "message": "hi"})
    valid = {
        "profile": "ops", "message": "hi", "session_id": "persistent-chat",
        "delivery_id": "a" * 64,
    }
    assert BotRelayDeliverParams.model_validate(valid)


@pytest.mark.parametrize("patch", [
    {"session_id": "persistent-chat"},
    {"delivery_id": "a" * 64},
    {"delivery_id": None, "session_id": "persistent-chat"},
    {"delivery_id": "a" * 64, "session_id": None},
    {"delivery_id": "", "session_id": "persistent-chat"},
    {"delivery_id": "A" * 64, "session_id": "persistent-chat"},
    {"delivery_id": "a" * 31, "session_id": "persistent-chat"},
    {"delivery_id": 7, "session_id": "persistent-chat"},
    {"delivery_id": "a" * 64, "session_id": 7},
    {"delivery_id": "a" * 64, "session_id": ""},
])
def test_selected_delivery_rejects_partial_null_empty_and_malformed_pair(patch):
    params = {"profile": "ops", "message": "hi", **patch}
    with pytest.raises(ValidationError):
        BotRelayDeliverParams.model_validate(params)


def test_published_json_schema_enforces_the_same_pair_and_id_constraints():
    from jsonschema import Draft202012Validator
    validator = Draft202012Validator(BotRelayDeliverParams.model_json_schema())
    legacy = {"profile": "ops", "message": "hi"}
    selected = {**legacy, "session_id": "persistent-chat", "delivery_id": "a" * 64}
    assert validator.is_valid(legacy)
    assert validator.is_valid(selected)
    for invalid in (
        {**legacy, "delivery_id": "a" * 64}, {**legacy, "session_id": "persistent-chat"},
        {**selected, "delivery_id": None}, {**selected, "session_id": None},
        {**selected, "delivery_id": "a" * 32}, {**selected, "delivery_id": "A" * 64},
        {**selected, "delivery_id": "a" * 64 + "\n"}, {**selected, "session_id": ""},
    ):
        assert not validator.is_valid(invalid), invalid
