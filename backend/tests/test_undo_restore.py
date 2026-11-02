"""撤销 / 重做所用的 /decisions/restore：整体校验修订号、重建与删除、保护区间不可动。"""
from app.models import EditDecision
from tests.test_rendering import add_decision, client, recording  # noqa: F401


def state(client, session_id, decision_id):
    return next(d for d in client.get(f"/api/sessions/{session_id}/decisions").json() if d["id"] == decision_id)


def test_undo_add_then_redo_recreates_same_decision(client, recording):
    session_id = recording[0]
    url = f"/api/sessions/{session_id}/decisions"
    created = client.post(url, json={"startMs": 500, "endMs": 900}).json()
    # 撤销“添加”：删除
    after = client.post(url + "/restore", json={"remove": [created["id"]], "expectedRevisions": {created["id"]: created["revision"]}})
    assert after.status_code == 200, after.text
    assert after.json() == []
    # 重做：按原 id 和内容重建
    redo = client.post(url + "/restore", json={"target": [created]})
    assert redo.status_code == 200, redo.text
    [restored] = redo.json()
    assert (restored["id"], restored["startMs"], restored["endMs"], restored["accepted"]) == (created["id"], 500, 900, True)
    assert client.post(f"/api/sessions/{session_id}/render").status_code == 201


def test_undo_accept_change_and_stale_revision_rejected(client, recording, isolated_backend):
    session_id = recording[0]
    decision_id = add_decision(isolated_backend, session_id, 100, 400, accepted=None, source="transcript_rule", action="DELETE_FILLER")
    url = f"/api/sessions/{session_id}/decisions"
    before = state(client, session_id, decision_id)
    changed = client.patch(f"{url}/{decision_id}", json={"accepted": True}).json()
    undo = client.post(url + "/restore", json={"target": [before], "expectedRevisions": {decision_id: changed["revision"]}})
    assert undo.status_code == 200
    assert state(client, session_id, decision_id)["accepted"] is None
    # 修订号对不上：整体拒绝，什么都不改
    stale = client.post(url + "/restore", json={"target": [changed], "expectedRevisions": {decision_id: changed["revision"]}})
    assert stale.status_code == 409
    assert state(client, session_id, decision_id)["accepted"] is None
    # 缺少修订号同样拒绝
    assert client.post(url + "/restore", json={"target": [changed]}).status_code == 409


def test_restore_never_touches_protected_or_foreign_decisions(client, recording, isolated_backend):
    session_id = recording[0]
    protected = add_decision(isolated_backend, session_id, 100, 400, action="KEEP_PROTECTED", hard_protected=True)
    url = f"/api/sessions/{session_id}/decisions/restore"
    assert client.post(url, json={"remove": [protected], "expectedRevisions": {protected: 0}}).status_code == 409
    # 不能凭空造出保护区间或越界区间
    fake = {"id": "d_fake", "startMs": 0, "endMs": 500, "action": "KEEP_PROTECTED", "source": "user", "accepted": True}
    assert client.post(url, json={"target": [fake]}).status_code == 422
    assert client.post(url, json={"target": [{**fake, "action": "DELETE_SELECTION", "endMs": 99999}]}).status_code == 422
    # 别的会话的决策 → 404
    other = client.post("/api/sessions", json={}).json()["id"]
    assert client.post(f"/api/sessions/{other}/decisions/restore", json={"remove": [protected], "expectedRevisions": {protected: 0}}).status_code == 404
    with isolated_backend() as db:
        assert db.query(EditDecision).count() == 1


def test_bulk_undo_of_confirm_all_is_atomic(client, recording, isolated_backend):
    session_id = recording[0]
    ids = [add_decision(isolated_backend, session_id, s, s + 100, accepted=None, source="silence_rule", action="COMPRESS_SILENCE") for s in (100, 600, 1200)]
    url = f"/api/sessions/{session_id}/decisions"
    before = client.get(url).json()
    after = client.post(url + "/confirm").json()
    revisions = {d["id"]: d["revision"] for d in after}
    # 其中一条在别处又被改了 → 整体拒绝
    client.patch(f"{url}/{ids[1]}", json={"accepted": False})
    assert client.post(url + "/restore", json={"target": before, "expectedRevisions": revisions}).status_code == 409
    assert [d["accepted"] for d in client.get(url).json()] == [True, False, True]
