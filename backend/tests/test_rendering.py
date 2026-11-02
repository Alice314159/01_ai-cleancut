"""Render real PCM and assert exact retained samples, snapshot isolation and download."""
import io
from pathlib import Path
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.models import AudioVersion, EditDecision, EnhancementJob, RecordingSession, RenderOutput, RoleAssignment
from app.services import rendering, storage

RATE = 48000


@pytest.fixture()
def recording(isolated_backend):
    with isolated_backend() as db:
        session = RecordingSession(capture_status="STOPPED", live_analysis_status="FINALIZED",
                                   total_duration_ms=3000, actual_sample_rate=RATE, channel_count=2, mime_type="audio/wav")
        db.add(session)
        db.flush()
        session_id = session.id
        values = (np.arange(RATE * 3) % 20000 - 10000).astype('<i2')
        samples = np.column_stack([values, -values])
        path = Path(storage.session_dir(session_id)) / 'source.wav'
        with wave.open(str(path), 'wb') as audio:
            audio.setnchannels(2)
            audio.setsampwidth(2)
            audio.setframerate(RATE)
            audio.writeframes(samples.tobytes())
        version = AudioVersion(session_id=session_id, version_type='original', label='原始', duration_ms=3000,
                               sample_count=len(samples), sample_rate=RATE, filename=path.name)
        db.add(version)
        db.flush()
        source_id = version.id
        for role in ('TIMEBASE', 'RENDER_SOURCE', 'REVIEW_SOURCE', 'EXPORT_SOURCE'):
            db.add(RoleAssignment(session_id=session_id, role=role, audio_version_id=source_id))
        db.commit()
    return session_id, source_id, samples, path


@pytest.fixture()
def client(isolated_backend):
    from app.main import app
    with TestClient(app) as c:
        yield c


def add_decision(factory, session_id, start, end, **kwargs):
    fields = dict(session_id=session_id, start_ms=start, end_ms=end, action='DELETE_SELECTION',
                  accepted=True, source='user', confidence=1.0, hard_protected=False)
    fields.update(kwargs)
    with factory() as db:
        decision = EditDecision(**fields)
        db.add(decision)
        db.commit()
        return decision.id


def read_output(client, session_id, output):
    version = output['version']
    response = client.get(f"/api/sessions/{session_id}/versions/{version['id']}/audio")
    assert response.status_code == 200
    with wave.open(io.BytesIO(response.content)) as audio:
        assert audio.getframerate() == RATE
        assert audio.getnchannels() == 2
        return np.frombuffer(audio.readframes(audio.getnframes()), dtype='<i2').reshape(-1, 2)


def test_manual_cuts_render_exact_samples_and_download(client, recording, isolated_backend):
    session_id, source_id, samples, original_path = recording
    original_bytes = original_path.read_bytes()
    response = client.post(f'/api/sessions/{session_id}/decisions', json={'startMs': 500, 'endMs': 1000})
    assert response.status_code == 201
    assert response.json()['source'] == 'user'
    assert response.json()['accepted'] is True
    response = client.post(f'/api/sessions/{session_id}/render')
    assert response.status_code == 201, response.text
    output = response.json()
    assert output['sourceVersionId'] == source_id
    assert output['removedDurationMs'] == 500
    assert output['version']['durationMs'] == 2500
    assert output['version']['sampleCount'] == RATE * 2.5
    assert output['stale'] is False
    assert output['cutRanges'] == [{'startMs': 500, 'endMs': 1000}]
    np.testing.assert_array_equal(read_output(client, session_id, output), np.concatenate([samples[:24000], samples[48000:]]))
    download = client.get(f"/api/sessions/{session_id}/versions/{output['version']['id']}/audio?download=true")
    assert download.headers['content-disposition'].startswith('attachment;')
    assert download.headers['content-type'].startswith('audio/wav')
    assert original_path.read_bytes() == original_bytes
    with isolated_backend() as db:
        roles = {r.role: r.audio_version_id for r in db.query(RoleAssignment).all()}
        assert roles['EXPORT_SOURCE'] == output['version']['id']
        assert roles['RENDER_SOURCE'] == roles['TIMEBASE'] == source_id
        assert db.query(RenderOutput).one().decisions[0]['accepted'] is True


def test_overlaps_are_removed_once_and_rejected_cuts_are_kept(client, recording, isolated_backend):
    session_id, _, samples, _ = recording
    add_decision(isolated_backend, session_id, 500, 1500)
    add_decision(isolated_backend, session_id, 1000, 2000)
    add_decision(isolated_backend, session_id, 2000, 2200, accepted=False)
    output = client.post(f'/api/sessions/{session_id}/render').json()
    assert output['removedDurationMs'] == 1500
    np.testing.assert_array_equal(read_output(client, session_id, output), np.concatenate([samples[:24000], samples[96000:]]))


def test_rerender_uses_uncut_source_and_keeps_old_output(client, recording, isolated_backend):
    session_id, _, samples, _ = recording
    decision_id = add_decision(isolated_backend, session_id, 500, 1000)
    first = client.post(f'/api/sessions/{session_id}/render').json()
    second = client.post(f'/api/sessions/{session_id}/render').json()
    assert first['version']['id'] != second['version']['id']
    assert first['version']['durationMs'] == second['version']['durationMs'] == 2500
    client.patch(f'/api/sessions/{session_id}/decisions/{decision_id}', json={'accepted': False})
    assert client.get(f'/api/sessions/{session_id}/render').json()['stale'] is True
    third = client.post(f'/api/sessions/{session_id}/render').json()
    assert third['version']['durationMs'] == 3000
    np.testing.assert_array_equal(read_output(client, session_id, third), samples)
    assert len(read_output(client, session_id, first)) == RATE * 2.5


@pytest.mark.parametrize('kwargs,status', [
    ({'accepted': None}, 409),
    ({'action': 'COMPRESS_SILENCE'}, 422),
])
def test_unresolved_or_unsupported_edits_do_not_publish(client, recording, isolated_backend, kwargs, status):
    session_id, source_id, _, _ = recording
    add_decision(isolated_backend, session_id, 100, 500, **kwargs)
    assert client.post(f'/api/sessions/{session_id}/render').status_code == status
    with isolated_backend() as db:
        assert db.query(RenderOutput).count() == 0
        assert db.query(RoleAssignment).filter_by(role='EXPORT_SOURCE').one().audio_version_id == source_id


@pytest.mark.parametrize('start,end', [(0, 3000), (2000, 4000)])
def test_invalid_or_all_audio_cuts_do_not_publish(client, recording, isolated_backend, start, end):
    session_id, source_id, _, _ = recording
    add_decision(isolated_backend, session_id, start, end)
    assert client.post(f'/api/sessions/{session_id}/render').status_code == 422
    with isolated_backend() as db:
        assert db.query(AudioVersion).count() == 1
        assert db.query(RoleAssignment).filter_by(role='EXPORT_SOURCE').one().audio_version_id == source_id
    assert list(Path(storage.session_dir(session_id)).glob('master_*.wav')) == []


def test_protected_audio_cannot_be_deleted(client, recording, isolated_backend):
    session_id, _, _, _ = recording
    add_decision(isolated_backend, session_id, 700, 900, hard_protected=True, action='KEEP_PROTECTED', accepted=None)
    response = client.post(f'/api/sessions/{session_id}/decisions', json={'startMs': 500, 'endMs': 1000})
    assert response.status_code == 409
    add_decision(isolated_backend, session_id, 500, 1000)
    assert client.post(f'/api/sessions/{session_id}/render').status_code == 409


@pytest.mark.parametrize('body', [
    {'startMs': -1, 'endMs': 500}, {'startMs': 500, 'endMs': 500},
    {'startMs': 1000, 'endMs': 500}, {'startMs': 0, 'endMs': 4000},
    {'startMs': 0.1, 'endMs': 500},
])
def test_manual_range_validation(client, recording, body):
    assert client.post(f'/api/sessions/{recording[0]}/decisions', json=body).status_code == 422


def test_missing_source_keeps_previous_export(client, recording, isolated_backend):
    session_id, source_id, _, source_path = recording
    source_path.unlink()
    assert client.post(f'/api/sessions/{session_id}/render').status_code == 409
    with isolated_backend() as db:
        assert db.query(RoleAssignment).filter_by(role='EXPORT_SOURCE').one().audio_version_id == source_id


def test_decode_failure_keeps_previous_export(client, recording, isolated_backend):
    session_id, source_id, _, source_path = recording
    source_path.write_bytes(b'corrupt input')
    assert client.post(f'/api/sessions/{session_id}/render').status_code == 422
    with isolated_backend() as db:
        assert db.query(AudioVersion).count() == 1
        assert db.query(RoleAssignment).filter_by(role='EXPORT_SOURCE').one().audio_version_id == source_id


@pytest.mark.parametrize('change_source', [False, True])
def test_concurrent_changes_reject_obsolete_render(client, recording, isolated_backend, monkeypatch, change_source):
    session_id, source_id, _, _ = recording
    decision_id = add_decision(isolated_backend, session_id, 500, 1000)
    write = rendering.write_render
    def change_during_render(*args, **kwargs):
        result = write(*args, **kwargs)
        with isolated_backend() as db:
            if change_source:
                source = db.get(AudioVersion, source_id)
                version = AudioVersion(session_id=session_id, version_type='denoised', label='new source',
                                       duration_ms=3000, sample_count=RATE * 3, sample_rate=RATE, filename=source.filename)
                db.add(version)
                db.flush()
                db.query(RoleAssignment).filter_by(role='RENDER_SOURCE').one().audio_version_id = version.id
            else:
                db.get(EditDecision, decision_id).accepted = False
            db.commit()
        return result
    monkeypatch.setattr(rendering, 'write_render', change_during_render)
    response = client.post(f'/api/sessions/{session_id}/render')
    assert response.status_code == 409, response.text
    with isolated_backend() as db:
        assert db.query(RenderOutput).count() == 0
        assert db.query(RoleAssignment).filter_by(role='EXPORT_SOURCE').one().audio_version_id == source_id
    assert list(Path(storage.session_dir(session_id)).glob('master_*.wav')) == []


def test_enhancement_running_blocks_render(client, recording, isolated_backend):
    session_id, _, _, _ = recording
    with isolated_backend() as db:
        db.add(EnhancementJob(session_id=session_id, mode='balanced', status='PROCESSING'))
        db.commit()
    assert client.post(f'/api/sessions/{session_id}/render').status_code == 409


def test_new_source_marks_previous_render_stale(client, recording, isolated_backend):
    session_id, source_id, _, _ = recording
    assert client.post(f'/api/sessions/{session_id}/render').status_code == 201
    with isolated_backend() as db:
        source = db.get(AudioVersion, source_id)
        version = AudioVersion(session_id=session_id, version_type='denoised', label='new source',
                               duration_ms=3000, sample_count=RATE * 3, sample_rate=RATE, filename=source.filename)
        db.add(version)
        db.flush()
        db.query(RoleAssignment).filter_by(role='RENDER_SOURCE').one().audio_version_id = version.id
        db.commit()
    assert client.get(f'/api/sessions/{session_id}/render').json()['stale'] is True


def test_unready_and_unknown_sessions_are_rejected(client, isolated_backend):
    with isolated_backend() as db:
        session = RecordingSession()
        db.add(session)
        db.commit()
        session_id = session.id
    assert client.post(f'/api/sessions/{session_id}/render').status_code == 409
    assert client.post('/api/sessions/missing/render').status_code == 404
    assert client.get('/api/sessions/missing/render').status_code == 404


def test_smooth_cuts_preserve_timeline_channels_and_untouched_samples(client, recording):
    session_id, _, samples, _ = recording
    client.post(f'/api/sessions/{session_id}/decisions', json={'startMs': 500, 'endMs': 1000})
    response = client.post(f'/api/sessions/{session_id}/render', json={'smoothCuts': True})
    assert response.status_code == 201
    output = response.json()
    assert output['version']['engine'] == 'pcm_cut_fade'
    assert output['removedDurationMs'] == 500
    actual = read_output(client, session_id, output)
    expected = np.concatenate([samples[:24000], samples[48000:]])
    assert actual.shape == expected.shape
    np.testing.assert_array_equal(actual[:23760], expected[:23760])
    np.testing.assert_array_equal(actual[24240:], expected[24240:])
    np.testing.assert_array_equal(actual[23999:24001], np.zeros((2, 2)))
    np.testing.assert_array_equal(actual[:, 0], -actual[:, 1])
    assert np.all(np.abs(actual.astype(float)) <= np.abs(expected.astype(float)))


def test_smoothing_without_cuts_preserves_audio(client, recording):
    session_id, _, samples, _ = recording
    result = client.post(f'/api/sessions/{session_id}/render', json={'smoothCuts': True})
    assert result.status_code == 201
    np.testing.assert_array_equal(read_output(client, session_id, result.json()), samples)


def test_short_retained_segment_smooths_without_changing_length(tmp_path):
    source, target = tmp_path / 'in.wav', tmp_path / 'out.wav'
    with wave.open(str(source), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(48000)
        audio.writeframes(np.full(4800, 10000, dtype='<i2').tobytes())
    result = rendering.write_render(source, target, [(0, 49), (51, 100)], smooth_cuts=True)
    assert result.sample_count == 96
    with wave.open(str(target), 'rb') as audio:
        values = np.frombuffer(audio.readframes(96), dtype='<i2')
    assert values[0] == values[-1] == 0
    assert values[47] == values[48] == 10000


def test_decision_revision_conflict_and_retry(client, recording):
    session_id = recording[0]
    created = client.post(f'/api/sessions/{session_id}/decisions', json={'startMs': 500, 'endMs': 1000}).json()
    url = f"/api/sessions/{session_id}/decisions/{created['id']}"
    assert created['revision'] == 0
    first = client.patch(url, json={'accepted': False, 'expectedRevision': 0})
    assert first.status_code == 200
    assert first.json()['revision'] == 1
    # Returning to the same choice must not erase evidence of an intervening edit.
    client.patch(url, json={'accepted': True, 'expectedRevision': 1})
    conflict = client.patch(url, json={'accepted': None, 'expectedRevision': 0})
    assert conflict.status_code == 409
    current = conflict.json()['detail']['current']
    assert current['accepted'] is True
    assert current['revision'] == 2
    retry = client.patch(url, json={'accepted': None, 'expectedRevision': current['revision']})
    assert retry.status_code == 200
    client.post(f'/api/sessions/{session_id}/decisions/confirm')
    assert client.patch(url, json={'accepted': False, 'expectedRevision': 3}).status_code == 409


def test_batch_snapshot_rejects_unseen_new_decisions(client, recording):
    session_id = recording[0]
    url = f'/api/sessions/{session_id}'
    first = client.post(url + '/decisions', json={'startMs': 100, 'endMs': 200}).json()
    client.patch(url + '/decisions/' + first['id'], json={'accepted': None})
    before = client.get(url + '/decisions').json()
    snapshot = {d['id']: d['revision'] for d in before}
    second = client.post(url + '/decisions', json={'startMs': 300, 'endMs': 400}).json()
    client.patch(url + '/decisions/' + second['id'], json={'accepted': None})
    for endpoint in ('confirm', 'reject-suggestions'):
        assert client.post(url + '/decisions/' + endpoint, json={'expectedRevisions': snapshot}).status_code == 409
    current = client.get(url + '/decisions').json()
    assert all(d['accepted'] is None for d in current)
    response = client.post(url + '/decisions/confirm', json={'expectedRevisions': {d['id']: d['revision'] for d in current}})
    assert response.status_code == 200
    assert all(d['accepted'] is True for d in response.json())


def test_enhancement_restart_cleanup_preserves_completed_versions(recording, isolated_backend):
    from app.services.stub_pipeline import recover_interrupted_enhancements
    session_id, source_id, _, path = recording
    original = path.read_bytes()
    with isolated_backend() as db:
        jobs = [EnhancementJob(session_id=session_id, mode='balanced', status=status) for status in ('QUEUED', 'PROCESSING', 'QUALITY_CHECKING', 'COMPLETED')]
        db.add_all(jobs)
        db.commit()
        ids = [j.id for j in jobs]
    recover_interrupted_enhancements()
    recover_interrupted_enhancements()
    with isolated_backend() as db:
        assert [db.get(EnhancementJob, id).status for id in ids] == ['FAILED', 'FAILED', 'FAILED', 'COMPLETED']
        assert '重启' in db.get(EnhancementJob, ids[0]).error_message
        assert rendering.validate_session(db, session_id).id == session_id
        assert rendering.render_source(db, session_id).id == source_id
    assert path.read_bytes() == original
