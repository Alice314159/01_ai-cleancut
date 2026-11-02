from types import SimpleNamespace as NS

from fastapi.testclient import TestClient
from app.main import app
from app.services.transcript_export import subtitle_cues, timestamp
from app.services import transcription
from tests.test_transcription import source, engine, job


def test_timestamps_and_sentence_boundaries():
    assert timestamp(3661002) == '01:01:01,002'
    tokens = [NS(text='你好。', start_ms=0, end_ms=500), NS(text=' Hello', start_ms=500, end_ms=900), NS(text=' world!', start_ms=900, end_ms=1200)]
    assert list(subtitle_cues(tokens)) == [(0, 500, '你好。'), (500, 1200, 'Hello world!')]


def test_overlap_is_grouped_and_pause_splits():
    tokens = [NS(text='Hello.', start_ms=0, end_ms=1000), NS(text=' there', start_ms=900, end_ms=1100), NS(text='Next', start_ms=2000, end_ms=2300)]
    assert list(subtitle_cues(tokens)) == [(0, 1100, 'Hello. there'), (2000, 2300, 'Next')]


def test_export_download_and_stale_version(isolated_backend, source, engine):
    job_id = job(isolated_backend, source)
    transcription.run_transcription(job_id)
    with TestClient(app) as client:
        url = f'/api/sessions/{source[0]}/transcript/export'
        params = {'transcriptionJobId': job_id, 'format': 'txt'}
        response = client.get(url, params=params)
        assert response.status_code == 200
        assert response.text == 'Hello world\n'
        assert 'attachment' in response.headers['content-disposition']
        params['format'] = 'srt'
        assert client.get(url, params=params).text == '1\n00:00:00,100 --> 00:00:01,200\nHello world\n\n'
        assert client.get(url, params={**params, 'format': 'pdf'}).status_code == 422
        transcription.run_transcription(job(isolated_backend, source))
        assert client.get(url, params=params).status_code == 409
        assert client.get('/api/sessions/missing/transcript/export', params=params).status_code == 404


def test_subtitle_text_cannot_create_extra_blocks():
    tokens = [NS(text='Hello\n\nworld', start_ms=0, end_ms=500)]
    assert list(subtitle_cues(tokens)) == [(0, 500, 'Hello world')]


def test_master_subtitles_map_actual_cuts_and_reject_partial_words():
    import pytest
    from app.services.transcript_export import export_render
    output = NS(decisions=[], transcript_snapshot=[dict(text='Hello', start_ms=100, end_ms=500),
                                     dict(text=' world', start_ms=600, end_ms=1000)],
                cut_ranges=[dict(start_ms=0, end_ms=500)])
    version = NS(sample_rate=48000, duration_ms=1000)
    assert export_render(output, version) == '1\n00:00:00,100 --> 00:00:00,500\nworld\n\n'
    output.cut_ranges = [dict(start_ms=200, end_ms=300)]
    with pytest.raises(ValueError, match='穿过'):
        export_render(output, version)
    output.transcript_snapshot = None
    with pytest.raises(ValueError, match='快照'):
        export_render(output, version)


def test_master_snapshot_survives_retranscription(isolated_backend, source, engine):
    from app.models import RoleAssignment, TranscriptToken
    from app.services.rendering import render_session
    job_id = job(isolated_backend, source)
    transcription.run_transcription(job_id)
    with isolated_backend() as db:
        db.add(RoleAssignment(session_id=source[0], role='RENDER_SOURCE', audio_version_id=source[1]))
        db.commit()
        output = render_session(db, source[0])
        version_id = output.version_id
        assert output.transcript_snapshot[0]['job_id'] == job_id
        db.query(TranscriptToken).delete()
        db.commit()
    with TestClient(app) as client:
        response = client.get(f'/api/sessions/{source[0]}/render/{version_id}/subtitles')
        assert response.status_code == 200
        assert 'Hello world' in response.text
        assert client.get(f'/api/sessions/missing/render/{version_id}/subtitles').status_code == 404
