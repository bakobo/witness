"""The Registrar's store: extend-only heads, subscriptions and batch numbering, across restarts."""

import pytest

from witness.errors import RegistrarFork, RegistrarStale
from witness.registrar.store import RegistrarStore


def _publish(store, registry, chain, *, issuer="EIssuer", kel=b"kel"):
    return store.publish(registry=registry, issuer=issuer, digest=chain.hex(),
                         chain=chain, kel=kel)


@pytest.fixture
def store(tmp_path):
    opened = RegistrarStore(tmp_path / "registrar.sqlite3")
    yield opened
    opened.close()


def test_a_head_is_kept_only_while_each_snapshot_extends_it(store):
    assert _publish(store, "r1", b"rip") == b"rip".hex()
    assert _publish(store, "r1", b"rip+iss") == b"rip+iss".hex()
    assert _publish(store, "r1", b"rip+iss") == b"rip+iss".hex(), "a repeat is re-acknowledged"
    with pytest.raises(RegistrarStale) as stale:
        _publish(store, "r1", b"rip")
    assert stale.value.code == "e.state.conflict.registrar.stale.f"
    with pytest.raises(RegistrarFork) as fork:
        _publish(store, "r1", b"rip+other")
    assert fork.value.code == "e.state.conflict.registrar.fork.f"
    assert store.head("r1")["chain"] == b"rip+iss"
    assert store.head("unknown") is None


def test_a_subscription_starts_full_then_carries_only_changes(store):
    _publish(store, "r1", b"a")
    _publish(store, "r2", b"b")
    store.subscribe("EObserver", "http://127.0.0.1:1/batch")
    first = store.compose("EObserver")
    assert (first.number, first.full) == (1, True)
    assert sorted(head["registry"] for head in first.heads) == ["r1", "r2"]
    quiet = store.compose("EObserver")
    assert (quiet.number, quiet.full, quiet.heads) == (2, False, ())
    _publish(store, "r2", b"b+rev")
    _publish(store, "r3", b"c")
    changed = store.compose("EObserver")
    assert changed.number == 3
    assert sorted((head["registry"], head["chain"]) for head in changed.heads) == \
        [("r2", b"b+rev"), ("r3", b"c")]
    assert store.subscribers() == [("EObserver", "http://127.0.0.1:1/batch")]


def test_resubscribing_restarts_numbering_with_a_full_batch(store):
    _publish(store, "r1", b"a")
    store.subscribe("EObserver", "http://127.0.0.1:1/one")
    store.compose("EObserver")
    store.compose("EObserver")
    store.subscribe("EObserver", "http://127.0.0.1:1/two")
    again = store.compose("EObserver")
    assert (again.number, again.full, len(again.heads)) == (1, True, 1)
    assert store.subscribers() == [("EObserver", "http://127.0.0.1:1/two")]


def test_unsubscribing_ends_batches_and_reports_whether_there_was_one(store):
    store.subscribe("EObserver", "http://127.0.0.1:1/batch")
    assert store.unsubscribe("EObserver") is True
    assert store.unsubscribe("EObserver") is False
    assert store.subscribers() == []


def test_heads_subscriptions_numbers_and_key_survive_a_restart(tmp_path):
    path = tmp_path / "registrar.sqlite3"
    first = RegistrarStore(path)
    aid = first.key().aid
    _publish(first, "r1", b"rip+iss+rev")
    first.subscribe("EObserver", "http://127.0.0.1:1/batch")
    first.compose("EObserver")
    first.close()

    second = RegistrarStore(path)
    try:
        assert second.key().aid == aid, "Observers pin the Registrar's AID"
        with pytest.raises(RegistrarStale):
            _publish(second, "r1", b"rip+iss")
        assert second.subscribers() == [("EObserver", "http://127.0.0.1:1/batch")]
        assert second.compose("EObserver").number == 2, "never reuse a batch number"
    finally:
        second.close()


def test_composing_for_an_unknown_subscriber_is_a_named_refusal(store):
    from witness.errors import RegistrarNoSubscription
    with pytest.raises(RegistrarNoSubscription) as missing:
        store.compose("ENobody")
    assert missing.value.code == "e.state.missing.subscription.f"
