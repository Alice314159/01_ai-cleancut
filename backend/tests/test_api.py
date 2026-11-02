"""
REST 层的集成测试：走真实的 FastAPI TestClient，用 isolated_backend fixture 换掉
底层数据库和存储目录，不碰共享的 data/。不测 WebSocket 相关的实时流程（见
test_pipeline.py），这里只测同步的 CRUD 和状态查询路径。
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(isolated_backend):
    # 必须在 isolated_backend 生效之后再导入 app，确保 app.main 里任何模块级状态
    # （目前没有，但以防万一）看到的是已经打了 patch 的 db/config。
    from app.main import app

    with TestClient(app) as c:
        yield c


class TestHealth:
    def test_health_ok(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestSessionCrud:
    def test_create_session_returns_full_shape(self, client):
        resp = client.post("/api/sessions", json={"title": "测试会话", "captureMode": "live"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["title"] == "测试会话"
        assert body["captureMode"] == "live"
        assert body["captureStatus"] == "IDLE"
        assert body["processingStatus"] == "NOT_STARTED"
        assert body["warnings"] == []
        assert "captureProcessing" in body
        assert body["clockDrift"] is None

    def test_get_session_returns_the_title(self, client):
        """
        RecordingSessionOut（会话详情）曾经没有 title 字段——模型和列表视图都有，
        唯独详情视图漏了；前端的 SessionDetail 类型却一直声称有这个字段，运行时
        其实是 undefined，只是当时没人用它才没暴露。
        """
        created = client.post("/api/sessions", json={"title": "详情标题"}).json()
        assert created["title"] == "详情标题"
        fetched = client.get(f"/api/sessions/{created['id']}").json()
        assert fetched["title"] == "详情标题"

    def test_create_session_honors_non_default_capture_mode(self, client):
        """
        CreateSessionIn 曾经继承 BaseModel 而不是 CamelModel，导致 captureMode 等
        camelCase 字段被静默忽略、永远落回 "live" 默认值——因为此前所有调用方
        恰好都传 "live"，这个回归一直没被测出来。这里显式传 "batch" 才能验证
        请求体真的被读取了。
        """
        resp = client.post("/api/sessions", json={"captureMode": "batch"})
        assert resp.status_code == 201
        assert resp.json()["captureMode"] == "batch"

    def test_create_session_with_defaults(self, client):
        resp = client.post("/api/sessions", json={})
        assert resp.status_code == 201
        assert resp.json()["captureMode"] == "live"

    def test_analysis_locality_is_always_local(self, client, isolated_backend):
        """
        没有真正实现过云端分析路径；即使显式请求 "cloud"，或者数据库里是建表时
        遗留的旧值，输出边界也要强制纠正成 "local"（和 local_persistence_status
        同一套处理方式），不能让前端看到一个从未存在过的能力。
        """
        resp = client.post("/api/sessions", json={"analysisLocality": "cloud"})
        assert resp.status_code == 201
        assert resp.json()["analysisLocality"] == "local"

        from app.models import RecordingSession
        with isolated_backend() as db:
            legacy = RecordingSession(analysis_locality="cloud")
            db.add(legacy)
            db.commit()
            legacy_id = legacy.id
        assert client.get(f"/api/sessions/{legacy_id}").json()["analysisLocality"] == "local"

    def test_get_session_by_id(self, client):
        created = client.post("/api/sessions", json={"title": "会话A"}).json()
        resp = client.get(f"/api/sessions/{created['id']}")
        assert resp.status_code == 200
        assert resp.json()["id"] == created["id"]

    def test_get_nonexistent_session_404s(self, client):
        resp = client.get("/api/sessions/rs_does_not_exist")
        assert resp.status_code == 404

    def test_list_sessions_returns_summary_shape(self, client):
        client.post("/api/sessions", json={"title": "会话1"})
        client.post("/api/sessions", json={"title": "会话2"})
        resp = client.get("/api/sessions")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2
        for item in body:
            assert set(item.keys()) == {"id", "title", "projectId", "captureMode", "processingStatus", "totalDurationMs", "captureStatus", "localPersistenceStatus", "uploadStatus", "serverMergeStatus", "liveAnalysisStatus", "createdAt", "coverVersion"}
            assert item["createdAt"].endswith("+00:00")

    def test_list_sessions_newest_first(self, client):
        """id 是随机 hex，不能作为排序依据；列表必须按创建先后倒序。"""
        titles = [f"会话{i}" for i in range(8)]
        for title in titles:
            client.post("/api/sessions", json={"title": title})
        body = client.get("/api/sessions").json()
        assert [item["title"] for item in body] == list(reversed(titles))

    def test_legacy_sessions_without_timestamp_keep_insertion_order(self, client, isolated_backend):
        from app.models import RecordingSession
        with isolated_backend() as db:
            for title in ("旧1", "旧2", "旧3"):
                db.add(RecordingSession(title=title, created_at_ns=0))
                db.commit()
        client.post("/api/sessions", json={"title": "新"})
        body = client.get("/api/sessions").json()
        assert [item["title"] for item in body] == ["新", "旧3", "旧2", "旧1"]
        assert body[1]["createdAt"] is None

    def test_list_sessions_exposes_project_id_for_project_filtering(self, client):
        client.post("/api/sessions", json={"title": "会话1", "projectId": "proj_podcast"})
        client.post("/api/sessions", json={"title": "会话2"})
        body = client.get("/api/sessions").json()
        projects = {item["title"]: item["projectId"] for item in body}
        assert projects == {"会话1": "proj_podcast", "会话2": "proj_default"}

    def test_stop_on_idle_session_is_a_noop(self, client):
        """
        /stop 是给"WS 异常断线后仍要把会话标记为已停止"这种场景用的兜底端点
        （见 routers/sessions.py 里 stop_session 的 docstring），只从 RECORDING/PAUSED
        转到 STOPPED。从没录制过的 IDLE 会话调用它不应该凭空把状态改成 STOPPED——
        那会让"这个会话真的录过东西吗"这个问题变得不可信。
        """
        created = client.post("/api/sessions", json={}).json()
        resp = client.post(f"/api/sessions/{created['id']}/stop")
        assert resp.status_code == 200
        assert resp.json()["captureStatus"] == "IDLE"

    def test_stop_transitions_recording_session_to_stopped(self, client, isolated_backend):
        """真正验证 RECORDING -> STOPPED 这条路径，直接在 DB 里把状态摆成 RECORDING
        （REST 层本身没有能做到这一步的端点，那是 WS 控制消息的职责，见 test_pipeline.py）。"""
        from app.models import RecordingSession

        created = client.post("/api/sessions", json={}).json()
        db = isolated_backend()
        session = db.get(RecordingSession, created["id"])
        session.capture_status = "RECORDING"
        db.commit()
        db.close()

        resp = client.post(f"/api/sessions/{created['id']}/stop")
        assert resp.status_code == 200
        assert resp.json()["captureStatus"] == "STOPPED"

    def test_stop_nonexistent_session_404s(self, client):
        resp = client.post("/api/sessions/rs_nope/stop")
        assert resp.status_code == 404


class TestUpdateSession:
    def test_rename_title(self, client):
        created = client.post("/api/sessions", json={"title": "旧标题"}).json()
        resp = client.patch(f"/api/sessions/{created['id']}", json={"title": "新标题"})
        assert resp.status_code == 200
        assert resp.json()["title"] == "新标题"
        assert client.get(f"/api/sessions/{created['id']}").json()["title"] == "新标题"

    def test_move_to_another_project(self, client):
        created = client.post("/api/sessions", json={"projectId": "proj_a"}).json()
        resp = client.patch(f"/api/sessions/{created['id']}", json={"projectId": "proj_b"})
        assert resp.status_code == 200
        assert resp.json()["projectId"] == "proj_b"

    def test_empty_title_is_rejected(self, client):
        created = client.post("/api/sessions", json={}).json()
        resp = client.patch(f"/api/sessions/{created['id']}", json={"title": "   "})
        assert resp.status_code == 422
        assert client.get(f"/api/sessions/{created['id']}").json()["title"] != ""

    def test_update_nonexistent_session_404s(self, client):
        resp = client.patch("/api/sessions/rs_nope", json={"title": "x"})
        assert resp.status_code == 404


class TestDeleteSession:
    def test_delete_removes_the_session(self, client):
        created = client.post("/api/sessions", json={}).json()
        resp = client.delete(f"/api/sessions/{created['id']}")
        assert resp.status_code == 204
        assert client.get(f"/api/sessions/{created['id']}").status_code == 404
        assert created["id"] not in [row["id"] for row in client.get("/api/sessions").json()]

    def test_delete_is_irreversible_and_cascades_related_rows_and_files(self, client, isolated_backend):
        """
        没有配置外键级联、SQLite 也没开外键约束，所以这条路径必须手动删干净关联表，
        否则会留下孤儿数据。这里构造一个"录制完成、有决策、有警告"的真实会话，
        删除后逐一确认相关表和磁盘目录都清空了，而不是只删了 recording_sessions 这一行。
        """
        from pathlib import Path

        from app.models import AudioVersion, EditDecision, RecordingSession, RoleAssignment, Warning as WarningModel
        from app.services import storage

        with isolated_backend() as db:
            session = RecordingSession(capture_status="STOPPED", live_analysis_status="FINALIZED", total_duration_ms=1000)
            db.add(session)
            db.flush()
            session_id = session.id
            version = AudioVersion(session_id=session_id, version_type="original", label="original",
                                   duration_ms=1000, sample_count=16000, sample_rate=16000, filename="raw.webm")
            db.add(version)
            db.flush()
            db.add(RoleAssignment(session_id=session_id, role="TIMEBASE", audio_version_id=version.id))
            db.add(EditDecision(session_id=session_id, start_ms=0, end_ms=100, action="DELETE_SELECTION",
                                accepted=True, source="user", confidence=1.0, hard_protected=False))
            db.add(WarningModel(session_id=session_id, scope="session", code="x", message="x"))
            db.commit()
            audio_dir = Path(storage.session_dir(session_id))
            (audio_dir / "raw.webm").write_bytes(b"fake")
            assert audio_dir.exists()

        resp = client.delete(f"/api/sessions/{session_id}")
        assert resp.status_code == 204

        with isolated_backend() as db:
            assert db.get(RecordingSession, session_id) is None
            assert db.query(AudioVersion).filter_by(session_id=session_id).count() == 0
            assert db.query(RoleAssignment).filter_by(session_id=session_id).count() == 0
            assert db.query(EditDecision).filter_by(session_id=session_id).count() == 0
            assert db.query(WarningModel).filter_by(session_id=session_id).count() == 0
        assert not audio_dir.exists()

    def test_delete_while_recording_is_rejected(self, client, isolated_backend):
        from app.models import RecordingSession

        created = client.post("/api/sessions", json={}).json()
        db = isolated_backend()
        session = db.get(RecordingSession, created["id"])
        session.capture_status = "RECORDING"
        db.commit()
        db.close()

        resp = client.delete(f"/api/sessions/{created['id']}")
        assert resp.status_code == 409
        assert client.get(f"/api/sessions/{created['id']}").status_code == 200

    def test_delete_nonexistent_session_404s(self, client):
        resp = client.delete("/api/sessions/rs_nope")
        assert resp.status_code == 404

    def test_delete_while_importing_is_rejected(self, client, isolated_backend):
        from app.models import RecordingSession

        created = client.post("/api/sessions", json={"captureMode": "batch"}).json()
        with isolated_backend() as db:
            session = db.get(RecordingSession, created["id"])
            session.capture_status = "IMPORTING"
            db.commit()

        resp = client.delete(f"/api/sessions/{created['id']}")
        assert resp.status_code == 409
        assert client.get(f"/api/sessions/{created['id']}").status_code == 200

    def test_delete_while_finalizing_is_rejected(self, client, isolated_backend):
        from app.models import RecordingSession

        created = client.post("/api/sessions", json={}).json()
        with isolated_backend() as db:
            session = db.get(RecordingSession, created["id"])
            session.live_analysis_status = "FINALIZING"
            db.commit()

        resp = client.delete(f"/api/sessions/{created['id']}")
        assert resp.status_code == 409
        assert client.get(f"/api/sessions/{created['id']}").status_code == 200

    @pytest.mark.parametrize("status", ["QUEUED", "PROCESSING", "QUALITY_CHECKING"])
    def test_delete_while_enhancement_job_active_is_rejected(self, client, isolated_backend, status):
        from app.models import EnhancementJob, RecordingSession

        created = client.post("/api/sessions", json={}).json()
        with isolated_backend() as db:
            db.get(RecordingSession, created["id"]).capture_status = "STOPPED"
            db.add(EnhancementJob(session_id=created["id"], mode="auto", status=status))
            db.commit()

        resp = client.delete(f"/api/sessions/{created['id']}")
        assert resp.status_code == 409
        assert client.get(f"/api/sessions/{created['id']}").status_code == 200

    @pytest.mark.parametrize("status", ["QUEUED", "LOADING_MODEL", "TRANSCRIBING"])
    def test_delete_while_transcription_job_active_is_rejected(self, client, isolated_backend, status):
        from app.models import RecordingSession, TranscriptionJob

        created = client.post("/api/sessions", json={}).json()
        with isolated_backend() as db:
            session = db.get(RecordingSession, created["id"])
            session.capture_status = "STOPPED"
            db.add(TranscriptionJob(session_id=created["id"], source_version_id="av_missing",
                                    model="base", requested_language="auto", status=status))
            db.commit()

        resp = client.delete(f"/api/sessions/{created['id']}")
        assert resp.status_code == 409
        assert client.get(f"/api/sessions/{created['id']}").status_code == 200

    def test_delete_succeeds_once_enhancement_job_is_no_longer_active(self, client, isolated_backend):
        """确认这不是把会话永久锁死——任务真正结束（不在活动状态集合里）之后照常能删。"""
        from app.models import EnhancementJob, RecordingSession

        created = client.post("/api/sessions", json={}).json()
        with isolated_backend() as db:
            db.get(RecordingSession, created["id"]).capture_status = "STOPPED"
            db.add(EnhancementJob(session_id=created["id"], mode="auto", status="COMPLETED"))
            db.commit()

        resp = client.delete(f"/api/sessions/{created['id']}")
        assert resp.status_code == 204


class TestQualityAndWaveformBeforeReady:
    """录制还没跑完流水线时，这些端点应该诚实地 404，不该返回假数据充数。"""

    def test_quality_404_before_pipeline_runs(self, client):
        created = client.post("/api/sessions", json={}).json()
        resp = client.get(f"/api/sessions/{created['id']}/quality")
        assert resp.status_code == 404

    def test_waveform_404_before_pipeline_runs(self, client):
        created = client.post("/api/sessions", json={}).json()
        resp = client.get(f"/api/sessions/{created['id']}/waveform")
        assert resp.status_code == 404

    def test_versions_empty_before_pipeline_runs(self, client):
        created = client.post("/api/sessions", json={}).json()
        resp = client.get(f"/api/sessions/{created['id']}/versions")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_enhancement_404_before_any_job(self, client):
        created = client.post("/api/sessions", json={}).json()
        resp = client.get(f"/api/sessions/{created['id']}/enhancement")
        assert resp.status_code == 404

    def test_enhancement_rejected_before_analysis_is_finalized(self, client):
        created = client.post("/api/sessions", json={}).json()
        resp = client.post(f"/api/sessions/{created['id']}/enhancement", json={"mode": "auto"})
        assert resp.status_code == 409

    def test_enhancement_rejects_unknown_mode(self, client):
        created = client.post("/api/sessions", json={}).json()
        resp = client.post(f"/api/sessions/{created['id']}/enhancement", json={"mode": "turbo"})
        assert resp.status_code == 422

    def test_audio_404_for_version_with_no_file(self, client):
        created = client.post("/api/sessions", json={}).json()
        resp = client.get(f"/api/sessions/{created['id']}/versions/av_fake/audio")
        assert resp.status_code == 404


class TestDecisionsEndpoints:
    def test_decisions_empty_for_new_session(self, client):
        created = client.post("/api/sessions", json={}).json()
        resp = client.get(f"/api/sessions/{created['id']}/decisions")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_confirm_decisions_on_empty_session_is_noop(self, client):
        created = client.post("/api/sessions", json={}).json()
        resp = client.post(f"/api/sessions/{created['id']}/decisions/confirm")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_update_nonexistent_decision_404s(self, client):
        created = client.post("/api/sessions", json={}).json()
        resp = client.patch(
            f"/api/sessions/{created['id']}/decisions/d_fake", json={"accepted": True}
        )
        assert resp.status_code == 404

    def test_read_endpoints_404_for_nonexistent_session(self, client):
        assert client.get("/api/sessions/rs_nope/decisions").status_code == 404
        assert client.get("/api/sessions/rs_nope/transcript").status_code == 404


def test_legacy_server_save_is_not_browser_persistence(client, isolated_backend):
    from app.models import RecordingSession
    session_id = client.post('/api/sessions', json={}).json()['id']
    with isolated_backend() as db:
        session = db.get(RecordingSession, session_id)
        session.local_persistence_status = 'PERSISTED'
        session.capture_status = 'STOPPED'
        session.server_merge_status = 'FAILED'
        session.upload_status = 'RECEIVING'
        db.commit()
    detail = client.get(f'/api/sessions/{session_id}').json()
    row = client.get('/api/sessions').json()[0]
    assert detail['localPersistenceStatus'] == row['localPersistenceStatus'] == 'NOT_SUPPORTED'
    assert row['captureStatus'] == 'STOPPED'
    assert row['serverMergeStatus'] == 'FAILED'
    assert row['uploadStatus'] == 'RECEIVING'


def test_sqlite_connections_use_wal_and_busy_timeout(isolated_backend):
    """后台线程与请求并发写库：需要 WAL + busy_timeout，避免瞬时锁冲突直接报错。"""
    from sqlalchemy import text
    with isolated_backend() as db:
        assert db.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"
        assert db.execute(text("PRAGMA busy_timeout")).scalar() == 5000
