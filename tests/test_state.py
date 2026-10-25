from pathlib import Path

import pytest

from docket.main import bind_mailbox
from docket.models import InboundEmail
from docket.store import Store


def inbound():
    return InboundEmail(
        message_id="<input@example.test>",
        sender="requester@example.test",
        text="Other Documents M12205",
    )


def test_restart_retains_jobs_and_never_replays_uncertain_sends(tmp_path):
    path = tmp_path / "state.sqlite"
    store = Store(path)
    assert store.enqueue("one", inbound())
    assert not store.enqueue("one", inbound())
    job = store.next_job()
    assert job["attempts"] == 1
    store.prepare_replies("one", [(1, "<reply@example.test>", tmp_path / "one.eml")])
    store.set_reply("one", 1, "sending")
    store.close()

    store = Store(path)
    store.recover()
    assert store.pending_replies()[0]["status"] == "uncertain"
    assert store.next_job() is None
    assert store.summary()["jobs"] == {"delivery_uncertain": 1}
    store.set_reply("one", 1, "sent")
    assert store.next_job() is None
    assert store.pending_replies() == []
    assert store.summary()["jobs"] == {"sent": 1}
    store.close()


def test_multiple_parts_finish_only_after_all_are_sent(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    store.enqueue("one", inbound())
    store.prepare_replies("one", [(1, "<a@test>", Path("a.eml")), (2, "<b@test>", Path("b.eml"))])
    store.set_job("one", "ready", matter="M12205")
    store.set_reply("one", 1, "sent")
    assert store.summary()["jobs"] == {"ready": 1}
    response = inbound().model_copy(update={"references": ["<a@test>"]})
    assert store.thread_matter(response) == "M12205"
    store.set_reply("one", 2, "sent")
    assert store.summary()["jobs"] == {"sent": 1}
    store.close()


def test_restart_keeps_mailbox_identity_cutoff_and_cursor(tmp_path, monkeypatch):
    monkeypatch.delenv("MAIL_START_AFTER", raising=False)
    path = tmp_path / "state.sqlite"
    store = Store(path)
    cutoff = bind_mailbox(store, "owner@gmail.com", "owner+docket@gmail.com")
    cursor = '{"uidvalidity":42,"last_uid":99}'
    store.set_setting("inbox_cursor", cursor)
    store.enqueue("gmail:owner@gmail.com:message:1234", inbound())
    store.set_job("gmail:owner@gmail.com:message:1234", "sent")
    store.close()

    store = Store(path)
    try:
        store.recover()
        assert bind_mailbox(store, "OWNER@GMAIL.COM", "OWNER+DOCKET@GMAIL.COM") == cutoff
        assert store.get_setting("inbox_cursor") == cursor
        assert not store.enqueue("gmail:owner@gmail.com:message:1234", inbound())
        assert store.next_job() is None
    finally:
        store.close()


@pytest.mark.parametrize("change", ["address", "alias", "cutoff"])
def test_existing_mailbox_cannot_be_silently_rebound(tmp_path, monkeypatch, change):
    monkeypatch.delenv("MAIL_START_AFTER", raising=False)
    store = Store(tmp_path / "state.sqlite")
    try:
        cutoff = bind_mailbox(store, "owner@gmail.com", "owner+docket@gmail.com")
        address = "other@gmail.com" if change == "address" else "owner@gmail.com"
        alias = "owner+other@gmail.com" if change == "alias" else "owner+docket@gmail.com"
        if change == "cutoff":
            monkeypatch.setenv("MAIL_START_AFTER", "2030-01-01T00:00:00+00:00")
        with pytest.raises(ValueError):
            bind_mailbox(store, address, alias)
        assert store.get_setting("started_at") == cutoff.isoformat()
    finally:
        store.close()
