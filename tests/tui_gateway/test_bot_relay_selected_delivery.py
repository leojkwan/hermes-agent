"""Selected Bot Chat delivery refuses mutable or wrongly scoped native intent."""

import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest


def _delivery_id(profile, session_id, nonce="a" * 32):
    bound = hashlib.sha256(f"{profile}\0{session_id}".encode()).hexdigest()[:32]
    return bound + nonce


def _seed_selected_owner(home, session_id, *, source="desktop", title="Bot Chat", live="desktop-live"):
    from hermes_cli.active_sessions import try_acquire_active_session
    from hermes_state import SessionDB

    home.mkdir(parents=True, exist_ok=True)
    db = SessionDB(db_path=home / "state.db")
    db.create_session(session_id=session_id, source=source)
    db.set_session_title(session_id, title)
    lease, refusal = try_acquire_active_session(
        session_id=session_id, surface="desktop", config={}, registry_home=home,
        metadata={"live_session_id": live, "bot_live_delivery_consumer": True},
    )
    assert refusal is None and lease is not None
    return db, lease


def _selected_rpc(server, profile, session_id, delivery_id, message="native regression"):
    return server.handle_request({"jsonrpc": "2.0", "id": 29, "method": "bot_relay.deliver", "params": {
        "profile": profile, "session_id": session_id, "delivery_id": delivery_id, "message": message,
    }})


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


@pytest.mark.parametrize(("bad_source", "bad_title"), [
    ("desktop", "Other Chat"),
    ("cli", "Bot Chat"),
])
def test_existing_mailbox_revalidates_canonical_desktop_title_and_source(tmp_path, bad_source, bad_title):
    """An unrelated earlier receipt must not bypass the canonical native target check."""
    from tools import bot_live_delivery as mailbox

    db, good_lease = _seed_selected_owner(tmp_path, "canonical")
    try:
        author = {"id": "bot:relay", "name": "G", "is_bot": True}
        good_key = _delivery_id("ops", "canonical")
        assert mailbox.deliver_to_selected_live_owner(
            tmp_path, "ops", "canonical", "earlier admitted traffic", delivery_id=good_key, author=author,
        )["sequence"] == 1

        if bad_title != "Bot Chat":
            db.set_session_title("canonical", bad_title)
        else:
            db._write_sql("UPDATE sessions SET source = ? WHERE id = ?", (bad_source, "canonical"))
        key = _delivery_id("ops", "canonical", nonce="b" * 32)
        with pytest.raises(ValueError, match="canonical Desktop"):
            mailbox.deliver_to_selected_live_owner(
                tmp_path, "ops", "canonical", "must refuse", delivery_id=key, author=author,
            )
        assert not (mailbox._root(tmp_path) / f"{key}.json").exists()
        assert mailbox._read(mailbox._root(tmp_path) / f"{good_key}.json")["sequence"] == 1
    finally:
        good_lease.release()
        db.close()


def test_existing_mailbox_revalidates_current_compression_tip(tmp_path):
    from tools import bot_live_delivery as mailbox
    db, lease = _seed_selected_owner(tmp_path, "bot-root")
    try:
        author = {"id": "bot:relay", "name": "G", "is_bot": True}
        first_key = _delivery_id("ops", "bot-root")
        mailbox.deliver_to_selected_live_owner(
            tmp_path, "ops", "bot-root", "earlier admitted traffic", delivery_id=first_key, author=author,
        )
        db.end_session("bot-root", "compression")
        db.create_session(session_id="bot-tip", source="desktop", parent_session_id="bot-root")
        wrong_key = _delivery_id("ops", "bot-root", nonce="b" * 32)
        with pytest.raises(ValueError, match="compression tip"):
            mailbox.deliver_to_selected_live_owner(
                tmp_path, "ops", "bot-root", "must refuse", delivery_id=wrong_key, author=author,
            )
        assert not (mailbox._root(tmp_path) / f"{wrong_key}.json").exists()
        assert db.get_compression_tip("bot-root") == "bot-tip"
    finally:
        lease.release()
        db.close()


@pytest.mark.parametrize("tip_source", ["desktop", "cli", "gateway"])
def test_selected_compression_tip_has_its_own_desktop_source(tmp_path, tip_source):
    from hermes_cli.active_sessions import try_acquire_active_session
    from tools import bot_live_delivery as mailbox
    db, original = _seed_selected_owner(tmp_path, "bot-root")
    original.release()
    db.end_session("bot-root", "compression")
    db.create_session(session_id="bot-tip", source=tip_source, parent_session_id="bot-root")
    held, refusal = try_acquire_active_session(
        session_id="bot-tip", surface="desktop", config={}, registry_home=tmp_path,
        metadata={"live_session_id": "tip-live", "bot_live_delivery_consumer": True})
    assert held is not None and refusal is None
    key = _delivery_id("default", "bot-tip")
    try:
        def admit():
            return mailbox.deliver_to_selected_live_owner(
                tmp_path, "default", "bot-tip", "compressed selection", delivery_id=key,
                author={"id": "bot:proof", "name": "Proof", "is_bot": True})
        if tip_source == "desktop":
            assert admit()["status"] == "queued"
        else:
            with pytest.raises(ValueError, match="canonical Desktop"):
                admit()
            assert not (mailbox._root(tmp_path) / f"{key}.json").exists()
    finally:
        held.release()
        db.close()


def test_selected_rpc_binds_full_profile_runtime_for_a_b_a_and_preserves_environment(tmp_path, monkeypatch):
    import os

    from agent.secret_scope import current_secret_scope, current_secret_scope_home
    from hermes_constants import get_hermes_home
    from tools import bot_live_delivery as mailbox
    from tools.terminal_scope import get_terminal_scope, terminal_env
    from tui_gateway import server
    from tui_gateway.transport import bind_transport

    home_a, home_b = tmp_path / "default", tmp_path / "profiles" / "ops"
    db_a, lease_a = _seed_selected_owner(home_a, "chat-a", live="live-a")
    db_b, lease_b = _seed_selected_owner(home_b, "chat-b", live="live-b")
    (home_a / "config.yaml").write_text("terminal:\n  backend: local\n", encoding="utf-8")
    (home_b / "config.yaml").write_text("terminal:\n  backend: docker\n", encoding="utf-8")
    (home_a / ".env").write_text("REVIEW_SCOPE=profile-a\n", encoding="utf-8")
    (home_b / ".env").write_text("REVIEW_SCOPE=profile-b\n", encoding="utf-8")
    observations = []
    native_deliver = mailbox.deliver_to_selected_live_owner

    def observe(*args, **kwargs):
        observations.append({
            "target": str(args[0]), "home": str(get_hermes_home()),
            "secret_bound": current_secret_scope() is not None,
            "secret_home": current_secret_scope_home(),
            "secret_marker": (current_secret_scope() or {}).get("REVIEW_SCOPE"),
            "terminal_bound": get_terminal_scope() is not None,
            "terminal_backend": terminal_env("TERMINAL_ENV"),
        })
        return native_deliver(*args, **kwargs)

    monkeypatch.setattr(server, "_hermes_home", home_a)
    monkeypatch.setattr(server, "_profile_home", lambda profile: home_b if profile == "ops" else None)
    monkeypatch.setattr("tools.bot_mode_probe._roster", lambda _root: [("default", home_a), ("ops", home_b)])
    monkeypatch.setattr(mailbox, "deliver_to_selected_live_owner", observe)
    monkeypatch.setattr(server, "_served_profile_homes", set())
    transport_token = bind_transport(None)
    try:
        with monkeypatch.context() as scoped:
            scoped.setenv("HERMES_HOME", str(home_a))
            scoped.setenv("TERMINAL_ENV", "local")
            before = dict(os.environ)
            replies = [
                _selected_rpc(server, profile, sid, _delivery_id(profile, sid, nonce=chr(97 + i) * 32))
                for i, (profile, sid) in enumerate((("default", "chat-a"), ("ops", "chat-b"), ("default", "chat-a")))
            ]
            assert all(response.get("result", {}).get("status") == "queued" for response in replies)
            assert [(row["home"], row["secret_home"], row["secret_marker"], row["terminal_backend"])
                    for row in observations] == [
                (str(home_a), str(home_a), "profile-a", "local"),
                (str(home_b), str(home_b), "profile-b", "docker"),
                (str(home_a), str(home_a), "profile-a", "local"),
            ]
            assert all(row["secret_bound"] and row["terminal_bound"] for row in observations)
            assert dict(os.environ) == before
    finally:
        from tui_gateway.transport import reset_transport
        reset_transport(transport_token)
        lease_a.release()
        lease_b.release()
        db_a.close()
        db_b.close()


def test_selected_rpc_rejects_wrong_full_target_hash_before_profile_registration(tmp_path, monkeypatch):
    from tui_gateway import server

    home = tmp_path / "launch"
    profile_home = tmp_path / "profiles" / "ops"
    profile_home.mkdir(parents=True)
    monkeypatch.setattr(server, "_hermes_home", home)
    monkeypatch.setattr(server, "_served_profile_homes", set())
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "get_profile_dir", lambda name: profile_home)
    monkeypatch.setattr("tools.bot_mode_probe._roster", lambda _root: [("ops", profile_home)])
    wrong_target = _delivery_id("other-profile", "chat", nonce="c" * 32)

    response = _selected_rpc(server, "ops", "chat", wrong_target)

    assert response["error"]["data"]["reason"] == "immutable_conflict"
    assert profile_home not in server._served_profile_homes


def test_selected_rpc_sanitizes_unreadable_native_receipt_error(tmp_path, monkeypatch):
    import os
    from tools import bot_live_delivery as mailbox
    from tui_gateway import server

    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("native unreadable-file probe requires POSIX permissions and a non-root user")

    db, lease = _seed_selected_owner(tmp_path, "canonical")
    try:
        key = _delivery_id("ops", "canonical")
        author = {"id": "server", "name": "Hermes server", "is_bot": True}
        mailbox.deliver_to_selected_live_owner(
            tmp_path, "ops", "canonical", "seed receipt", delivery_id=key, author=author,
        )
        receipt = mailbox._root(tmp_path) / f"{key}.json"
        monkeypatch.setattr(server, "_hermes_home", tmp_path)
        monkeypatch.setattr(server, "_profile_home", lambda _profile: tmp_path)
        monkeypatch.setattr("tools.bot_mode_probe._roster", lambda _root: [("ops", tmp_path)])
        mode = receipt.stat().st_mode & 0o777
        receipt.chmod(0)
        try:
            response = _selected_rpc(server, "ops", "canonical", key, "seed receipt")
        finally:
            receipt.chmod(mode)
        public = response["error"]["message"]
        assert response["error"]["data"]["reason"] == "receipt_uninspectable"
        assert "unavailable" in public
        assert str(tmp_path) not in public
        assert receipt.name not in public
    finally:
        lease.release()
        db.close()


def test_native_poller_does_not_claim_after_reconnect_transfers_its_snapshot(tmp_path, monkeypatch):
    import threading
    from hermes_cli.active_sessions import transfer_active_session
    from tools import bot_live_delivery as mailbox
    from tui_gateway import server
    db, held = _seed_selected_owner(tmp_path, "canonical", live="old-live")
    author = {"id": "bot:proof", "name": "Proof", "is_bot": True}
    key = _delivery_id("default", "canonical")
    try:
        queued = mailbox.deliver_to_selected_live_owner(
            tmp_path, "default", "canonical", "must stay queued", delivery_id=key, author=author)
        path = mailbox._root(tmp_path) / f"{key}.json"
        before = path.read_bytes()
        native_matches = mailbox._matches
        transfers = []
        def reconnect_after_snapshot(home, record, owner):
            matched = native_matches(home, record, owner)
            if matched and not transfers:
                assert transfer_active_session(held, session_id="canonical", metadata={
                    "live_session_id": "new-live", "bot_live_delivery_consumer": True})
                transfers.append(True)
            return matched
        monkeypatch.setattr(mailbox, "_matches", reconnect_after_snapshot)
        monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **k: pytest.fail("stale runtime started a turn"))
        session = {"profile_home": str(tmp_path), "session_key": "canonical", "agent": object(),
                   "history_lock": threading.RLock(), "active_session_lease": held, "running": False}
        assert server._poll_bot_live_delivery_once("old-live", session) is False
        assert transfers == [True]
        assert path.read_bytes() == before
        assert mailbox.read_delivery_result(tmp_path, queued["id"])["status"] == "queued"
    finally:
        held.release()
        db.close()


def test_valid_selected_receipt_replays_unknown_through_corrupt_native_registry(tmp_path, monkeypatch):
    from tools import bot_live_delivery as mailbox
    from tui_gateway import server
    db, held = _seed_selected_owner(tmp_path, "canonical")
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_profile_home", lambda _profile: tmp_path)
    monkeypatch.setattr("tools.bot_mode_probe._roster", lambda _root: [("ops", tmp_path)])
    key = _delivery_id("ops", "canonical")
    try:
        assert _selected_rpc(server, "ops", "canonical", key)["result"]["status"] == "queued"
        path = mailbox._root(tmp_path) / f"{key}.json"
        before = path.read_bytes()
        registry = tmp_path / "runtime" / "active_sessions.json"
        native_bytes = registry.read_bytes()
        registry.write_text("{")
        try:
            replay = _selected_rpc(server, "ops", "canonical", key)
            assert replay["result"]["status"] == "queued"
            assert replay["result"]["delivery_unknown"] is True
            assert path.read_bytes() == before
            fresh = _selected_rpc(server, "ops", "canonical", _delivery_id("ops", "canonical", "b" * 32))
            assert fresh["error"]["data"]["reason"] == "coordination_uninspectable"
            assert str(tmp_path) not in fresh["error"]["message"]
            assert not (mailbox._root(tmp_path) / f"{_delivery_id('ops', 'canonical', 'b' * 32)}.json").exists()
        finally:
            registry.write_bytes(native_bytes)
    finally:
        held.release()
        db.close()


@pytest.mark.parametrize("pair", [
    {"delivery_id": "a" * 64}, {"session_id": "chat"},
    {"delivery_id": None, "session_id": "chat"},
    {"delivery_id": "bad", "session_id": "chat"},
])
def test_actual_dispatcher_keeps_selected_input_faults_typed(pair):
    from tui_gateway import server
    response = server.handle_request({"jsonrpc": "2.0", "id": 29, "method": "bot_relay.deliver",
        "params": {"profile": "ops", "message": "fixture", **pair}})
    assert response["error"]["code"] == 4090
    assert response["error"]["data"]["reason"] == "invalid_input"


def test_selected_admission_and_replay_do_not_hydrate_external_credentials(tmp_path, monkeypatch):
    from tui_gateway import server
    db, held = _seed_selected_owner(tmp_path, "canonical")
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_profile_home", lambda _profile: tmp_path)
    monkeypatch.setattr("tools.bot_mode_probe._roster", lambda _root: [("ops", tmp_path)])
    monkeypatch.setattr("hermes_cli.env_loader.hydrate_profile_secret_sources",
                        lambda *_: pytest.fail("receipt operation hydrated external credentials"))
    key = _delivery_id("ops", "canonical")
    try:
        assert _selected_rpc(server, "ops", "canonical", key)["result"]["status"] == "queued"
        assert _selected_rpc(server, "ops", "canonical", key)["result"]["status"] == "queued"
    finally:
        held.release()
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
