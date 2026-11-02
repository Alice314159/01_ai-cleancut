from pathlib import Path
from types import SimpleNamespace as NS
import wave

import pytest

from app.models import RenderTranscriptionJob, TranscriptToken, EditDecision
from app.services import render_transcription, transcription
from tests.test_rendering import recording, client
from tests.test_retakes import upload


class MasterEngine:
    def transcribe(self, filename, **kwargs):
        assert Path(filename).name.startswith('master_')
        assert kwargs['word_timestamps'] is True
        with wave.open(filename) as audio:
            assert audio.getnframes() / audio.getframerate() == 2.75
        return iter([NS(end=2.7, words=[NS(start=.5, end=.75, word='New take.'),
                                     NS(start=2.2, end=2.7, word=' Final words.')])]), NS(language='en')


@pytest.fixture()
def master_engine(monkeypatch):
    monkeypatch.setattr(transcription, 'dependency_available', lambda: True)
    monkeypatch.setattr(transcription, 'get_model', lambda *a: MasterEngine())


def make_render(client, recording):
    assert upload(client, recording).status_code == 201
    response = client.post(f'/api/sessions/{recording[0]}/render')
    assert response.status_code == 201, response.text
    return response.json()['version']['id']


def test_auto_transcribes_actual_master_and_exports_unshifted_subtitles(client, recording, isolated_backend, master_engine):
    with isolated_backend() as db:
        db.add(TranscriptToken(session_id=recording[0], text='original words', start_ms=0, seq=0))
        db.commit()
    version = make_render(client, recording)
    url = f'/api/sessions/{recording[0]}/render/{version}'
    data = client.get(url + '/transcript').json()
    assert data['job']['status'] == 'COMPLETED'
    assert data['versionId'] == version
    assert data['tokens'][1]['startMs'] == 2200
    response = client.get(url + '/subtitles')
    assert response.status_code == 200
    assert '00:00:02,200 --> 00:00:02,700' in response.text
    assert 'New take.' in response.text
    assert client.get(url + '/subtitles?format=txt').text == 'New take. Final words.\n'
    assert client.get(f'/api/sessions/{recording[0]}/render').json()['transcriptReady'] is True
    with isolated_backend() as db:
        assert db.query(TranscriptToken).one().text == 'original words'
        assert db.query(EditDecision).one().accepted is True
        assert db.query(EditDecision).one().revision == 0
    assert client.get(f'/api/sessions/{recording[0]}/transcription').status_code == 404
    assert client.get(f'/api/sessions/wrong/render/{version}/transcript').status_code == 404
    assert client.post(f'/api/sessions/wrong/render/{version}/transcription', json={}).status_code == 404


def test_failed_retry_keeps_previous_master_text(client, recording, master_engine, monkeypatch):
    version = make_render(client, recording)
    url = f'/api/sessions/{recording[0]}/render/{version}'
    previous = client.get(url + '/subtitles').text
    class Broken:
        def transcribe(self, *args, **kwargs):
            def partial():
                yield NS(end=.1, words=[NS(start=0, end=.1, word='partial')])
                raise RuntimeError('interrupted')
            return partial(), NS(language='en')
    monkeypatch.setattr(transcription, 'get_model', lambda *a: Broken())
    assert client.post(url + '/transcription', json={}).status_code == 202
    data = client.get(url + '/transcript').json()
    assert data['job']['status'] == 'FAILED'
    assert data['job']['id'] != data['transcriptJobId']
    assert client.get(url + '/subtitles').text == previous


def test_each_master_keeps_own_transcript_after_new_render(client, recording, master_engine, monkeypatch):
    first = make_render(client, recording)
    first_url = f'/api/sessions/{recording[0]}/render/{first}/subtitles'
    previous = client.get(first_url).text
    class NewEngine(MasterEngine):
        def transcribe(self, *a, **k):
            return iter([NS(end=1, words=[NS(start=0, end=1, word='第二版')])]), NS(language='zh')
    monkeypatch.setattr(transcription, 'get_model', lambda *a: NewEngine())
    second = client.post(f'/api/sessions/{recording[0]}/render').json()['version']['id']
    assert second != first
    assert '第二版' in client.get(f'/api/sessions/{recording[0]}/render/{second}/subtitles').text
    assert client.get(first_url).text == previous


def test_missing_dependency_visible_and_manual_retry_works(client, recording, monkeypatch):
    version = make_render(client, recording)
    url = f'/api/sessions/{recording[0]}/render/{version}'
    data = client.get(url + '/transcript').json()
    assert data['job']['status'] == 'FAILED'
    assert '依赖' in data['job']['errorMessage']
    assert client.get(url + '/subtitles').status_code == 409
    monkeypatch.setattr(transcription, 'dependency_available', lambda: True)
    monkeypatch.setattr(transcription, 'get_model', lambda *a: MasterEngine())
    assert client.post(url + '/transcription', json={}).status_code == 202
    assert client.get(url + '/subtitles').status_code == 200


def test_duplicate_jobs_restart_recovery_and_session_deletion(client, recording, isolated_backend, monkeypatch):
    version = make_render(client, recording)
    monkeypatch.setattr(transcription, 'dependency_available', lambda: True)
    monkeypatch.setattr(render_transcription, 'run', lambda *a: None)
    url = f'/api/sessions/{recording[0]}/render/{version}/transcription'
    first = client.post(url, json={}).json()
    second = client.post(url, json={}).json()
    assert first['id'] == second['id'] and first['status'] == 'QUEUED'
    assert client.delete(f'/api/sessions/{recording[0]}').status_code == 409
    render_transcription.recover_interrupted_jobs()
    with isolated_backend() as db:
        assert db.get(RenderTranscriptionJob, first['id']).status == 'FAILED'
    assert client.delete(f'/api/sessions/{recording[0]}').status_code == 204
    with isolated_backend() as db:
        assert db.query(RenderTranscriptionJob).count() == 0


def test_empty_speech_and_failed_worker_do_not_publish_partial_text(client, recording, master_engine, monkeypatch):
    class Empty:
        def transcribe(self, *a, **k):
            return iter([]), NS(language='en')
    monkeypatch.setattr(transcription, 'get_model', lambda *a: Empty())
    version = make_render(client, recording)
    url = f'/api/sessions/{recording[0]}/render/{version}'
    assert client.get(url + '/transcript').json()['job']['status'] == 'COMPLETED'
    assert client.get(url + '/subtitles').status_code == 409
    assert client.get(f'/api/sessions/{recording[0]}/render').json()['transcriptReady'] is False
