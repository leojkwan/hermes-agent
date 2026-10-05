"""Selected Bot Chat delivery refuses mutable or wrongly scoped native intent."""

import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest


def _delivery_id(profile, session_id, nonce="a" * 32):
    bound = hashlib.sha256(f"{profile}\0{session_id}".encode()).hexdigest()[:32]
    return bound + nonce


def test_selected_mailbox_requires_canonical_desktop_owner_and_replays_exact_receipt(tmp_path):
    from hermes_cli.active_sessions import try_acquire_active_session
    from hermes_state import SessionDB
    from tools import bot_live_delivery as mailbox

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="persistent-chat", source="desktop")
    db.set_session_title("persistent-chat", "Bot Chat")
    metadata = {"live_session_id": "desktop-live", "bot_live_delivery_consumer": True}
    lease, refusal = try_acquire_active_session(
        session_id="persistent-chat", surface="desktop", config={}, registry_home=tmp_path,
        metadata=metadata,
    )
    assert refusal is None and lease is not None
    try:
        author = {"id": "bot:relay", "name": "G", "is_bot": True}
        key = _delivery_id("ops", "persistent-chat")
        first = mailbox.deliver_to_selected_live_owner(
            tmp_path, "ops", "persistent-chat", "keep exact text", delivery_id=key, author=author,
        )
        assert first["status"] == "queued"
        assert first["sequence"] == 1
        receipt_path = mailbox._root(tmp_path) / f"{key}.json"
        receipt_bytes = receipt_path.read_bytes()

        # A selected retry returns the exact record even after owner release; it never rewrites it.
        lease.release()
        replay = mailbox.deliver_to_selected_live_owner(
            tmp_path, "ops", "persistent-chat", "keep exact text", delivery_id=key, author=author,
        )
        assert {**replay, "delivery_unknown": False} == first
        assert replay["delivery_unknown"] is True
        assert receipt_path.read_bytes() == receipt_bytes
        with pytest.raises(ValueError):
            mailbox.deliver_to_selected_live_owner(
                tmp_path, "ops", "persistent-chat", "changed", delivery_id=key, author=author,
            )
        with pytest.raises(ValueError):
            mailbox.deliver_to_selected_live_owner(
                tmp_path, "ops", "persistent-chat", "keep exact text", delivery_id=key,
                author={"id": "bot:other", "name": "Other", "is_bot": True},
            )
        with pytest.raises(ValueError):
            mailbox.deliver_to_selected_live_owner(
                tmp_path, "other-profile", "persistent-chat", "keep exact text", delivery_id=key,
                author=author,
            )
        with pytest.raises(ValueError):
            mailbox.deliver_to_selected_live_owner(
                tmp_path, "ops", "persistent-chat", "keep exact text", delivery_id=key[:32],
                author=author,
            )
        with pytest.raises(ValueError):
            mailbox.deliver_to_selected_live_owner(
                tmp_path, "ops", "persistent-chat", "keep exact text", delivery_id=key[:32],
                author=author,
            )
    finally:
        lease.release()
        db.close()


def test_selected_mailbox_refuses_unsupported_or_noncanonical_targets_without_admission(tmp_path):
    from hermes_cli.active_sessions import try_acquire_active_session
    from hermes_state import SessionDB
    from tools import bot_live_delivery as mailbox

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="persistent-chat", source="cli")
    db.set_session_title("persistent-chat", "Bot Chat")
    metadata = {"live_session_id": "desktop-live", "bot_live_delivery_consumer": True}
    lease, refusal = try_acquire_active_session(
        session_id="persistent-chat", surface="desktop", config={}, registry_home=tmp_path,
        metadata=metadata,
    )
    assert refusal is None and lease is not None
    try:
        key = _delivery_id("ops", "persistent-chat")
        with pytest.raises(ValueError):
            mailbox.deliver_to_selected_live_owner(
                tmp_path, "ops", "persistent-chat", "message", delivery_id=key,
                author={"id": "bot:relay", "name": "G", "is_bot": True},
            )
        assert not mailbox.has_mailbox(tmp_path)
    finally:
        lease.release()
        db.close()


def test_simultaneous_selected_retries_allocate_one_receipt_and_sequence(tmp_path):
    from hermes_cli.active_sessions import try_acquire_active_session
    from hermes_state import SessionDB
    from tools import bot_live_delivery as mailbox

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="persistent-chat", source="desktop")
    db.set_session_title("persistent-chat", "Bot Chat")
    lease, refusal = try_acquire_active_session(
        session_id="persistent-chat", surface="desktop", config={}, registry_home=tmp_path,
        metadata={"live_session_id": "desktop-live", "bot_live_delivery_consumer": True},
    )
    assert refusal is None and lease is not None
    try:
        key = _delivery_id("ops", "persistent-chat")
        author = {"id": "bot:relay", "name": "G", "is_bot": True}
        with ThreadPoolExecutor(max_workers=8) as pool:
            records = list(pool.map(lambda _: mailbox.deliver_to_selected_live_owner(
                tmp_path, "ops", "persistent-chat", "one immutable message", delivery_id=key, author=author,
            ), range(8)))
        assert all(record["sequence"] == 1 for record in records)
        path = mailbox._root(tmp_path) / f"{key}.json"
        saved = mailbox._read(path)
        assert saved["sequence"] == 1
        assert saved["message"] == "one immutable message"
        claimed = mailbox.claim_pending_delivery(tmp_path, records[0]["owner"])
        assert claimed and claimed["delivery_id"] == key and claimed["status"] == "claimed"
        assert mailbox.claim_pending_delivery(tmp_path, records[0]["owner"]) is None
    finally:
        lease.release()
        db.close()


def test_selected_profile_switch_a_to_b_to_a_never_retargets_receipt(tmp_path):
    from hermes_cli.active_sessions import try_acquire_active_session
    from hermes_state import SessionDB
    from tools import bot_live_delivery as mailbox

    home_a, home_b = tmp_path / "profile-a", tmp_path / "profile-b"
    home_a.mkdir()
    db = SessionDB(db_path=home_a / "state.db")
    db.create_session(session_id="persistent-chat", source="desktop")
    db.set_session_title("persistent-chat", "Bot Chat")
    lease, refusal = try_acquire_active_session(
        session_id="persistent-chat", surface="desktop", config={}, registry_home=home_a,
        metadata={"live_session_id": "desktop-live", "bot_live_delivery_consumer": True},
    )
    assert refusal is None and lease is not None
    try:
        key = _delivery_id("default", "persistent-chat")
        author = {"id": "bot:relay", "name": "G", "is_bot": True}
        first = mailbox.deliver_to_selected_live_owner(
            home_a, "default", "persistent-chat", "stay in A", delivery_id=key, author=author,
        )
        with pytest.raises(ValueError):
            mailbox.deliver_to_selected_live_owner(
                home_b, "default", "persistent-chat", "stay in A", delivery_id=key, author=author,
            )
        assert not mailbox.has_mailbox(home_b)
        replay = mailbox.deliver_to_selected_live_owner(
            home_a, "default", "persistent-chat", "stay in A", delivery_id=key, author=author,
        )
        assert replay["delivery_id"] == first["delivery_id"]
        assert replay["owner"] == first["owner"]
    finally:
        lease.release()
        db.close()


def test_selected_claimed_crash_stays_unknown_and_terminal_reply_replays(tmp_path):
    from hermes_cli.active_sessions import try_acquire_active_session
    from hermes_state import SessionDB
    from tools import bot_live_delivery as mailbox

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="persistent-chat", source="desktop")
    db.set_session_title("persistent-chat", "Bot Chat")
    metadata = {"live_session_id": "desktop-live", "bot_live_delivery_consumer": True}
    lease, refusal = try_acquire_active_session(
        session_id="persistent-chat", surface="desktop", config={}, registry_home=tmp_path,
        metadata=metadata,
    )
    assert refusal is None and lease is not None
    author = {"id": "bot:relay", "name": "G", "is_bot": True}
    key = _delivery_id("ops", "persistent-chat")
    try:
        queued = mailbox.deliver_to_selected_live_owner(
            tmp_path, "ops", "persistent-chat", "one turn", delivery_id=key, author=author,
        )
        claimed = mailbox.claim_pending_delivery(tmp_path, queued["owner"])
        assert claimed and claimed["status"] == "claimed"
        lease.release()
        restarted_lease, refusal = try_acquire_active_session(
            session_id="persistent-chat", surface="desktop", config={}, registry_home=tmp_path,
            metadata={"live_session_id": "desktop-live-restarted", "bot_live_delivery_consumer": True},
        )
        assert refusal is None and restarted_lease is not None
        replay = mailbox.deliver_to_selected_live_owner(
            tmp_path, "ops", "persistent-chat", "one turn", delivery_id=key, author=author,
        )
        assert replay["status"] == "claimed" and replay["delivery_unknown"] is True
        assert mailbox.claim_pending_delivery(tmp_path, {
            **queued["owner"], "lease_id": restarted_lease.lease_id,
            "live_session_id": "desktop-live-restarted",
        }) is None
        settled = mailbox.complete_delivery(tmp_path, key, status="settled", reply="answer")
        restarted_lease.release()
        replay_settled = mailbox.deliver_to_selected_live_owner(
            tmp_path, "ops", "persistent-chat", "one turn", delivery_id=key, author=author,
        )
        assert replay_settled["status"] == "settled"
        assert replay_settled["reply"] == "answer"
        assert replay_settled["delivery_unknown"] is False
        assert mailbox.read_delivery_result(tmp_path, key) == settled
    finally:
        lease.release()
        db.close()


def test_selected_admission_refuses_owner_replaced_after_lookup(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from hermes_cli.active_sessions import try_acquire_active_session
    from hermes_state import SessionDB
    from tools import bot_live_delivery as mailbox

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="persistent-chat", source="desktop")
    db.set_session_title("persistent-chat", "Bot Chat")
    lease, refusal = try_acquire_active_session(
        session_id="persistent-chat", surface="desktop", config={}, registry_home=tmp_path,
        metadata={"live_session_id": "before", "bot_live_delivery_consumer": True},
    )
    assert refusal is None and lease is not None
    real_locked = mailbox._locked
    replacements = []

    @contextmanager
    def replace_between_lookup_and_lock(home):
        lease.release()
        replacement, replacement_refusal = try_acquire_active_session(
            session_id="persistent-chat", surface="desktop", config={}, registry_home=tmp_path,
            metadata={"live_session_id": "after", "bot_live_delivery_consumer": True},
        )
        assert replacement_refusal is None and replacement is not None
        replacements.append(replacement)
        with real_locked(home) as root:
            yield root

    monkeypatch.setattr(mailbox, "_locked", replace_between_lookup_and_lock)
    try:
        key = _delivery_id("ops", "persistent-chat")
        with pytest.raises(ValueError, match="changed before admission"):
            mailbox.deliver_to_selected_live_owner(
                tmp_path, "ops", "persistent-chat", "message", delivery_id=key,
                author={"id": "bot:relay", "name": "G", "is_bot": True},
            )
        assert not list(mailbox._root(tmp_path).glob("*.json"))
    finally:
        for replacement in replacements:
            replacement.release()
        db.close()


def test_selected_handler_returns_immediate_receipt_without_legacy_dispatch(tmp_path, monkeypatch):
    from tools import bot_live_delivery
    from tui_gateway import methods_bot_relay
    import tui_gateway.server as server

    home = tmp_path / ".hermes"
    selected_home = home / "profiles" / "ops"
    selected_home.mkdir(parents=True)
    (selected_home / "config.yaml").write_text("{}\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    calls = []
    monkeypatch.setattr(server, "current_transport", lambda: None, raising=False)
    monkeypatch.setattr(server, "_profile_home", lambda profile: selected_home)
    monkeypatch.setattr(server, "_hermes_home", home)
    monkeypatch.setattr("tools.bot_mode_probe._roster", lambda _root: [("ops", selected_home)])
    monkeypatch.setattr("tools.bot_mode_probe._default_home", lambda: str(home))
    monkeypatch.setattr("tools.bot_live_delivery.deliver_to_selected_live_owner",
                        lambda *args, **kwargs: calls.append((args, kwargs)) or {
                            "delivery_id": kwargs["delivery_id"], "status": "queued",
                            "delivery_unknown": False, "message": args[3],
                        })
    def forbidden(*args, **kwargs):
        raise AssertionError("selected mode reached legacy execution")
    monkeypatch.setattr(server, "_run_delivery", forbidden, raising=False)
    monkeypatch.setattr("tools.bot_live_delivery.find_canonical_live_owner", forbidden)
    key = _delivery_id("ops", "persistent-chat")
    result = server._methods["bot_relay.deliver"](9, {
        "profile": "ops", "message": "  exact text  ", "delivery_id": key,
        "session_id": "persistent-chat",
    })
    assert result["result"]["reply"] == ""
    assert result["result"]["status"] == "queued"
    assert result["result"]["session_id"] == "persistent-chat"
    assert calls and calls[0][0][1:3] == ("ops", "persistent-chat")
    assert calls[0][0][3] == "exact text"
