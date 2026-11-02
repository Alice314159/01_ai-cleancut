"""State/atomicity regressions use controlled engines; asr_smoke.py tests the real model."""
from pathlib import Path
import threading
import time
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from app import db as db_module
from app.models import AudioVersion, EditDecision, RecordingSession, RoleAssignment, TranscriptToken, TranscriptionJob
from app.services import storage, transcription


@pytest.fixture()
def source(isolated_backend, clean_webm_file):
    with isolated_backend() as db:
        session = RecordingSession(capture_status='STOPPED', live_analysis_status='FINALIZED', total_duration_ms=3000)
        db.add(session)
        db.flush()
        session_id = session.id
        Path(storage.raw_chunk_path(session_id)).write_bytes(clean_webm_file.read_bytes())
        version = AudioVersion(session_id=session_id, version_type='original', label='original', duration_ms=3000,
                               sample_count=48000, sample_rate=16000, filename='raw.webm')
        db.add(version)
        db.flush()
        source_id = version.id
        db.add(RoleAssignment(session_id=session_id, role='ASR_SOURCE', audio_version_id=source_id))
        db.commit()
    return session_id, source_id


def job(factory, source):
    with factory() as db:
        item = TranscriptionJob(session_id=source[0], source_version_id=source[1], model='base')
        db.add(item)
        db.commit()
        return item.id


def previous_tokens(factory, source):
    with factory() as db:
        token = TranscriptToken(session_id=source[0], text='previous text', start_ms=0, seq=0)
        db.add(token)
        db.commit()
        return token.id


class Engine:
    def transcribe(self, filename, **kwargs):
        assert Path(filename).is_file()
        assert kwargs['word_timestamps'] is True
        assert kwargs['vad_filter'] is True
        assert kwargs['language'] is None
        return iter([NS(start=0.1, end=1.2, text='Hello world', words=[
            NS(start=0.1, end=0.5, word='Hello'), NS(start=0.6, end=1.2, word=' world'),
        ])]), NS(language='en')


@pytest.fixture()
def engine(monkeypatch):
    monkeypatch.setattr(transcription, 'get_model', lambda *args: Engine())
    monkeypatch.setattr(transcription, 'dependency_available', lambda: True)


def test_success_atomically_replaces_old_tokens_without_editing_decisions(isolated_backend, source, engine):
    previous_tokens(isolated_backend, source)
    with isolated_backend() as db:
        db.add(EditDecision(session_id=source[0], start_ms=2000, end_ms=2500, action='DELETE_SELECTION',
                            accepted=False, source='user', confidence=1.0))
        db.commit()
    job_id = job(isolated_backend, source)
    transcription.run_transcription(job_id)
    with isolated_backend() as db:
        result = db.get(TranscriptionJob, job_id)
        assert result.status == 'COMPLETED'
        assert result.progress == 1
        assert result.detected_language == 'en'
        tokens = db.query(TranscriptToken).order_by(TranscriptToken.seq).all()
        assert [(t.text, t.start_ms, t.end_ms) for t in tokens] == [('Hello', 100, 500), (' world', 600, 1200)]
        assert all(t.job_id == job_id for t in tokens)
        assert db.query(EditDecision).one().accepted is False
    transcription.run_transcription(job_id)
    with isolated_backend() as db:
        assert db.query(TranscriptToken).count() == 2


def test_chinese_output_is_converted_to_simplified(isolated_backend, source, monkeypatch):
    """
    faster-whisper's multilingual models default to Traditional-leaning characters for
    Chinese regardless of the requested language (verified against the real base model:
    even language='zh' explicit still came back Traditional) — a Simplified-Chinese user
    would see this as garbled/wrong-looking text. We convert whenever info.language == 'zh',
    independent of what was requested.
    """
    class ChineseEngine:
        def transcribe(self, filename, **kwargs):
            return iter([NS(start=0.0, end=1.0, text='這是測試', words=[
                NS(start=0.0, end=1.0, word='這是測試'),
            ])]), NS(language='zh')

    monkeypatch.setattr(transcription, 'get_model', lambda *args: ChineseEngine())
    monkeypatch.setattr(transcription, 'dependency_available', lambda: True)
    job_id = job(isolated_backend, source)
    transcription.run_transcription(job_id)
    with isolated_backend() as db:
        result = db.get(TranscriptionJob, job_id)
        assert result.status == 'COMPLETED'
        assert result.detected_language == 'zh'
        token = db.query(TranscriptToken).one()
        assert token.text == '这是测试'


def test_generator_failure_keeps_previous_success(isolated_backend, source, monkeypatch):
    previous_id = previous_tokens(isolated_backend, source)
    class BrokenEngine:
        def transcribe(self, *args, **kwargs):
            def chunks():
                yield NS(start=0, end=0.5, text='partial', words=None)
                raise RuntimeError('model interrupted')
            return chunks(), NS(language='en')
    monkeypatch.setattr(transcription, 'get_model', lambda *args: BrokenEngine())
    job_id = job(isolated_backend, source)
    transcription.run_transcription(job_id)
    with isolated_backend() as db:
        assert db.query(TranscriptToken).one().id == previous_id
        assert db.get(TranscriptionJob, job_id).status == 'FAILED'
        assert 'model interrupted' in db.get(TranscriptionJob, job_id).error_message


def test_success_without_speech_returns_empty_transcript(isolated_backend, source, monkeypatch):
    previous_tokens(isolated_backend, source)
    monkeypatch.setattr(transcription, 'get_model', lambda *args: NS(transcribe=lambda *a, **k: (iter([]), NS(language='en'))))
    job_id = job(isolated_backend, source)
    transcription.run_transcription(job_id)
    with isolated_backend() as db:
        assert db.query(TranscriptToken).count() == 0
        assert db.get(TranscriptionJob, job_id).status == 'COMPLETED'


def test_timestamps_are_finite_bounded_and_nonempty():
    segment = NS(words=[NS(word='negative', start=-0.1, end=0.2), NS(word='tail', start=2.9, end=4),
                        NS(word='invalid', start=float('nan'), end=1), NS(word='zero', start=1, end=1),
                        NS(word='outside', start=4, end=5), NS(word=' ', start=1, end=2)])
    assert list(transcription.timed_words(segment, 3000)) == [
        dict(text='negative', start_ms=0, end_ms=200), dict(text='tail', start_ms=2900, end_ms=3000),
    ]


def test_timed_words_restores_punctuation_from_segment_text():
    # Word-level timestamps never carry punctuation; it only survives in segment.text.
    segment = NS(text='嗨，大家好！我们走吧。', words=[
        NS(word='嗨', start=0, end=0.3), NS(word='大家好', start=0.4, end=0.9),
        NS(word='我们', start=1.0, end=1.3), NS(word='走吧', start=1.3, end=1.6),
    ])
    assert [w['text'] for w in transcription.timed_words(segment, 2000)] == ['嗨，', '大家好！', '我们', '走吧。']


def test_timed_words_keeps_original_text_when_alignment_fails():
    # segment.text doesn't actually contain the words (e.g. mismatched/garbled input) —
    # restoring punctuation must fail closed, never invent or scramble text.
    segment = NS(text='completely unrelated', words=[NS(word='Hello', start=0, end=0.5)])
    assert [w['text'] for w in transcription.timed_words(segment, 1000)] == ['Hello']


def test_source_changed_during_inference_keeps_old_text(isolated_backend, source, monkeypatch):
    previous_id = previous_tokens(isolated_backend, source)
    class ChangingEngine(Engine):
        def transcribe(self, *args, **kwargs):
            with isolated_backend() as db:
                db.query(RoleAssignment).filter_by(session_id=source[0]).delete()
                db.commit()
            return super().transcribe(*args, **kwargs)
    monkeypatch.setattr(transcription, 'get_model', lambda *args: ChangingEngine())
    job_id = job(isolated_backend, source)
    transcription.run_transcription(job_id)
    with isolated_backend() as db:
        assert db.query(TranscriptToken).one().id == previous_id
        assert db.get(TranscriptionJob, job_id).status == 'FAILED'


def test_restart_marks_interrupted_jobs_failed(isolated_backend, source):
    job_id = job(isolated_backend, source)
    old_id = previous_tokens(isolated_backend, source)
    transcription.recover_interrupted_jobs()
    with isolated_backend() as db:
        assert db.get(TranscriptionJob, job_id).status == 'FAILED'
        assert db.query(TranscriptToken).one().id == old_id


@pytest.fixture()
def client(isolated_backend):
    from app.main import app
    with TestClient(app) as client:
        yield client


def test_api_deduplicates_jobs_and_does_not_block_health(client, source, engine, monkeypatch):
    entered = threading.Event()
    released = threading.Event()
    original = transcription.get_model
    def slow_model(*args):
        entered.set()
        assert released.wait(5), 'ASR blocked request loop'
        return original(*args)
    monkeypatch.setattr(transcription, 'get_model', slow_model)
    try:
        response = client.post(f'/api/sessions/{source[0]}/transcription', json={'language': 'auto'})
        assert response.status_code == 202, response.text
        job_id = response.json()['id']
        assert entered.wait(2)
        assert client.get('/api/health').status_code == 200
        assert client.post(f'/api/sessions/{source[0]}/transcription', json={}).status_code == 409
        assert client.get(f'/api/sessions/wrong/transcription/{job_id}').status_code == 404
    finally:
        released.set()
    for _ in range(100):
        result = client.get(f'/api/sessions/{source[0]}/transcription/{job_id}').json()
        if result['status'] in ['COMPLETED', 'FAILED']:
            break
        time.sleep(0.01)
    assert result['status'] == 'COMPLETED', result
    assert client.get(f'/api/sessions/{source[0]}/transcription').json()['id'] == job_id
    assert client.get(f'/api/sessions/{source[0]}/transcript').json()[0]['endMs'] == 500


def test_missing_dependency_and_unready_sessions(client, source, monkeypatch):
    monkeypatch.setattr(transcription, 'dependency_available', lambda: False)
    assert client.post(f'/api/sessions/{source[0]}/transcription', json={}).status_code == 503
    session_id = client.post('/api/sessions', json={}).json()['id']
    assert client.post(f'/api/sessions/{session_id}/transcription', json={}).status_code == 409
    assert client.post('/api/sessions/missing/transcription', json={}).status_code == 404
    assert client.post(f'/api/sessions/{source[0]}/transcription', json={'language': 'invalid'}).status_code == 422


def test_old_transcript_table_migration_preserves_legacy_provenance(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path / "old.db"}')
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE transcript_tokens (id TEXT PRIMARY KEY, text TEXT)'))
        connection.execute(text("INSERT INTO transcript_tokens VALUES ('old', 'legacy candidate')"))
    monkeypatch.setattr(db_module, 'engine', engine)
    db_module.init_db()
    db_module.init_db()
    assert {'job_id', 'end_ms'} <= {c['name'] for c in inspect(engine).get_columns('transcript_tokens')}
    with engine.connect() as connection:
        assert connection.execute(text('SELECT text, end_ms, job_id FROM transcript_tokens')).one() == ('legacy candidate', None, None)
    engine.dispose()


def test_interrupted_worker_cannot_publish_after_recovery(isolated_backend, source, monkeypatch):
    previous_id = previous_tokens(isolated_backend, source)
    class InterruptedEngine(Engine):
        def transcribe(self, *args, **kwargs):
            transcription.recover_interrupted_jobs()
            return super().transcribe(*args, **kwargs)
    monkeypatch.setattr(transcription, 'get_model', lambda *args: InterruptedEngine())
    job_id = job(isolated_backend, source)
    transcription.run_transcription(job_id)
    with isolated_backend() as db:
        assert db.get(TranscriptionJob, job_id).status == 'FAILED'
        assert db.query(TranscriptToken).one().id == previous_id


def test_transcription_publishes_repetition_hints_and_preserves_rejection(isolated_backend, source, monkeypatch):
    class RepeatedEngine:
        def transcribe(self, *args, **kwargs):
            return iter([NS(start=0, end=2.5, words=[
                NS(start=0, end=0.4, word='Hello'), NS(start=0.4, end=0.8, word=' world'),
                NS(start=1, end=1.4, word='Hello'), NS(start=1.4, end=1.8, word=' world'),
                NS(start=2, end=2.5, word=' um'),
            ])]), NS(language='en')
    monkeypatch.setattr(transcription, 'get_model', lambda *args: RepeatedEngine())
    for retry in range(3):
        job_id = job(isolated_backend, source)
        transcription.run_transcription(job_id)
        with isolated_backend() as db:
            assert db.get(TranscriptionJob, job_id).status == 'COMPLETED'
            hint = db.query(EditDecision).one()
            assert hint.action == 'DELETE_REPETITION'
            assert (hint.start_ms, hint.end_ms) == (0, 800)
            assert hint.source == 'transcript_rule'
            assert hint.review_required is True
            assert hint.accepted is (False if retry == 2 else None)
            if retry == 1:
                hint.accepted = False
                db.commit()
