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
