"""Exercise real upload, PCM replacement, conflicts and immutable history."""
import io
import json
from pathlib import Path
import wave

import numpy as np
import pytest

from app.models import AudioVersion, EditDecision, RenderOutput, RoleAssignment
from app.services import rendering, retakes
from tests.test_rendering import recording, client, add_decision, read_output, RATE


def wav_bytes(duration=0.25, rate=RATE, channels=2):
    result = io.BytesIO()
    samples = np.full((round(duration * rate), channels), 1234, dtype='<i2')
    with wave.open(result, 'wb') as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(samples.tobytes())
    return result.getvalue(), samples


def upload(client, recording, start=500, end=1000, payload=None, snapshot=None, source_id=None):
    session_id = recording[0]
    if snapshot is None:
        snapshot = {d['id']: d['revision'] for d in client.get(f'/api/sessions/{session_id}/decisions').json()}
    return client.post(f'/api/sessions/{session_id}/retakes', data={
        'startMs': start, 'endMs': end, 'sourceVersionId': source_id or recording[1],
        'snapshot': json.dumps({'expectedRevisions': snapshot}),
    }, files={'file': ('anything.webm', wav_bytes()[0] if payload is None else payload, 'audio/webm')})


def test_retake_exact_splice_undo_and_immutable_rerender(client, recording, isolated_backend):
    session_id, source_id, samples, path = recording
    original = path.read_bytes()
    decision = upload(client, recording)
    assert decision.status_code == 201, decision.text
    decision = decision.json()
    assert decision['action'] == 'REPLACE_AUDIO' and decision['accepted'] is True
    assert decision['replacementDurationMs'] == 250
    first = client.post(f'/api/sessions/{session_id}/render')
    assert first.status_code == 201, first.text
    first = first.json()
    assert first['hasReplacements'] is True
    assert first['version']['durationMs'] == 2750
    assert first['removedDurationMs'] == 250
    expected = np.concatenate([samples[:RATE // 2], wav_bytes()[1], samples[RATE:]])
    np.testing.assert_array_equal(read_output(client, session_id, first), expected)
    second = client.post(f'/api/sessions/{session_id}/render').json()
    np.testing.assert_array_equal(read_output(client, session_id, second), expected)
    assert second['version']['id'] != first['version']['id']
    assert path.read_bytes() == original
    with isolated_backend() as db:
        assert db.query(RoleAssignment).filter_by(role='RENDER_SOURCE').one().audio_version_id == source_id
    assert client.get(f"/api/sessions/{session_id}/render/{first['version']['id']}/subtitles").status_code == 409
    assert client.patch(f"/api/sessions/{session_id}/decisions/{decision['id']}",
                        json={'accepted': False, 'expectedRevision': decision['revision']}).status_code == 200
    assert client.get(f'/api/sessions/{session_id}/render').json()['stale'] is True
    restored = client.post(f'/api/sessions/{session_id}/render').json()
    assert restored['hasReplacements'] is False
    np.testing.assert_array_equal(read_output(client, session_id, restored), samples)
    np.testing.assert_array_equal(read_output(client, session_id, first), expected)


def test_longer_retake_and_multiple_edits_keep_original_coordinates(client, recording, isolated_backend):
    session_id, _, samples, _ = recording
    assert upload(client, recording, 500, 1000, wav_bytes(1)[0]).status_code == 201
    assert upload(client, recording, 1500, 2000, wav_bytes(0.25, rate=16000, channels=1)[0]).status_code == 201
    add_decision(isolated_backend, session_id, 2500, 2750)
    result = client.post(f'/api/sessions/{session_id}/render')
    assert result.status_code == 201, result.text
    result = result.json()
    assert result['version']['durationMs'] == 3000
    actual = read_output(client, session_id, result)
    np.testing.assert_array_equal(actual[:RATE // 2], samples[:RATE // 2])
    np.testing.assert_array_equal(actual[RATE // 2:RATE * 3 // 2], wav_bytes(1)[1])
    np.testing.assert_array_equal(actual[-RATE // 4:], samples[-RATE // 4:])


def test_full_recording_can_be_replaced_and_smoothing_preserves_duration(client, recording):
    assert upload(client, recording, 0, 3000).status_code == 201
    result = client.post(f'/api/sessions/{recording[0]}/render', json={'smoothCuts': True})
    assert result.status_code == 201, result.text
    assert result.json()['version']['durationMs'] == 250
    np.testing.assert_array_equal(read_output(client, recording[0], result.json()), wav_bytes()[1])


def test_retake_supersedes_contained_edits_but_preserves_choices_elsewhere(client, recording, isolated_backend):
    inside = add_decision(isolated_backend, recording[0], 600, 700)
    outside = add_decision(isolated_backend, recording[0], 2000, 2200)
    first = upload(client, recording).json()
    second = upload(client, recording).json()
    assert first['id'] != second['id']
    with isolated_backend() as db:
        assert db.get(EditDecision, inside).accepted is False
        assert db.get(EditDecision, inside).revision == 1
        assert db.get(EditDecision, outside).accepted is True
        assert db.get(EditDecision, first['id']).accepted is False
        assert db.get(EditDecision, second['id']).accepted is True
    assert client.post(f'/api/sessions/{recording[0]}/render').status_code == 201


@pytest.mark.parametrize('kwargs', [dict(start=0, end=600), dict(start=600, end=700, hard_protected=True),
                                    dict(start=600, end=700, action='KEEP_PROTECTED', accepted=False)])
def test_retake_rejects_partial_overlap_and_protection(client, recording, isolated_backend, kwargs):
    add_decision(isolated_backend, recording[0], **kwargs)
    assert upload(client, recording).status_code == 409
    with isolated_backend() as db:
        assert db.query(AudioVersion).filter_by(version_type='retake').count() == 0
        assert db.query(EditDecision).count() == 1


@pytest.mark.parametrize('options,status', [({'payload': b'bad audio'}, 422), ({'payload': b''}, 422),
                                         ({'start': -1}, 422), ({'end': 4000}, 422),
                                         ({'source_id': 'wrong'}, 409)])
def test_invalid_retake_leaves_no_files_or_decisions(client, recording, isolated_backend, options, status):
    assert upload(client, recording, **options).status_code == status
    assert not list(recording[3].parent.glob('retake_*'))
    with isolated_backend() as db:
        assert db.query(EditDecision).count() == 0


def test_stale_snapshot_and_concurrent_change_are_rejected(client, recording, isolated_backend, monkeypatch):
    decision_id = add_decision(isolated_backend, recording[0], 2000, 2200)
    assert upload(client, recording, snapshot={}).status_code == 409
    original = rendering.write_render
    def change_during_decode(*args, **kwargs):
        result = original(*args, **kwargs)
        with isolated_backend() as db:
            decision = db.get(EditDecision, decision_id)
            decision.revision += 1
            db.commit()
        return result
    monkeypatch.setattr(rendering, 'write_render', change_during_decode)
    assert upload(client, recording).status_code == 409
    assert not list(recording[3].parent.glob('retake_*'))


def test_render_rejects_overlapping_or_missing_retake_without_publishing(client, recording, isolated_backend):
    decision = upload(client, recording).json()
    overlapping = add_decision(isolated_backend, recording[0], 800, 1100)
    assert client.post(f'/api/sessions/{recording[0]}/render').status_code == 409
    with isolated_backend() as db:
        db.get(EditDecision, overlapping).accepted = False
        version = db.get(AudioVersion, decision['replacementVersionId'])
        (recording[3].parent / version.filename).unlink()
        db.commit()
    assert client.post(f'/api/sessions/{recording[0]}/render').status_code == 409
    with isolated_backend() as db:
        assert db.query(RenderOutput).count() == 0


def test_upload_limit_and_duration_limit_cleanup(client, recording, monkeypatch):
    monkeypatch.setattr(retakes, 'MAX_UPLOAD_BYTES', 10)
    assert upload(client, recording).status_code == 422
    monkeypatch.setattr(retakes, 'MAX_UPLOAD_BYTES', 60 * 1024 * 1024)
    monkeypatch.setattr(retakes, 'MAX_DURATION_MS', 100)
    assert upload(client, recording).status_code == 422
    assert not list(recording[3].parent.glob('retake_*'))


def test_retake_render_rechecks_decisions_before_publication(client, recording, isolated_backend, monkeypatch):
    decision = upload(client, recording).json()
    old = client.post(f'/api/sessions/{recording[0]}/render').json()
    original = rendering.write_replacement_render
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        with isolated_backend() as db:
            db.get(EditDecision, decision['id']).accepted = False
            db.commit()
        return result
    monkeypatch.setattr(rendering, 'write_replacement_render', changed)
    assert client.post(f'/api/sessions/{recording[0]}/render').status_code == 409
    assert client.get(f'/api/sessions/{recording[0]}/render').json()['version']['id'] == old['version']['id']
    assert len(list(recording[3].parent.glob('master_*'))) == 1


def test_longer_take_reports_net_added_duration(client, recording):
    assert upload(client, recording, payload=wav_bytes(1.25)[0]).status_code == 201
    result = client.post(f'/api/sessions/{recording[0]}/render').json()
    assert result['removedDurationMs'] == -750
    assert result['version']['durationMs'] == 3750


def test_legacy_decisions_migrate_without_losing_choices(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, inspect, text
    from app import db as db_module
    engine = create_engine(f'sqlite:///{tmp_path / "legacy.db"}')
    with engine.begin() as connection:
        connection.execute(text('''CREATE TABLE edit_decisions (
            id VARCHAR PRIMARY KEY, session_id VARCHAR, start_ms INTEGER, end_ms INTEGER,
            action VARCHAR, reason VARCHAR, text VARCHAR, confidence FLOAT, source VARCHAR,
            hard_protected BOOLEAN, review_required BOOLEAN, accepted BOOLEAN, revision INTEGER)'''))
        connection.execute(text("INSERT INTO edit_decisions (id, accepted, revision) VALUES ('old', 0, 7)"))
    monkeypatch.setattr(db_module, 'engine', engine)
    db_module.init_db()
    db_module.init_db()
    assert {'replacement_version_id', 'replacement_duration_ms'} <= {c['name'] for c in inspect(engine).get_columns('edit_decisions')}
    with engine.connect() as connection:
        assert connection.execute(text('SELECT accepted, revision, replacement_version_id, replacement_duration_ms FROM edit_decisions')).one() == (0, 7, None, None)
    engine.dispose()
