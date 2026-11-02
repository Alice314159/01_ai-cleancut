from types import SimpleNamespace as NS
import pytest
from app.models import EditDecision, TranscriptToken
from app.services import suggestions, transcription
from tests.test_transcription import source, engine, job


def words(*texts):
    return [NS(text=t, start_ms=i * 300, end_ms=i * 300 + 200) for i, t in enumerate(texts)]


def test_repetition_preserves_final_copy():
    result = list(suggestions.candidates(words('We', ' can', ' we', ' can', ' we', ' can'), 3000))
    assert len(result) == 1
    assert (result[0]['start_ms'], result[0]['end_ms']) == (0, 1100)
    assert '1.200' in result[0]['reason']


@pytest.mark.parametrize('texts,count', [(('嗯', '你好'), 1), (('嗯你好',), 0), (('Um,',), 1), (('那个',), 0), (('你好', '你好'), 1), (('Hello,', ' hello!'), 1), (('a', 'b', 'a'), 0), (('恩', '你好'), 1), (('额', '你好'), 1)])
def test_conservative_rules(texts, count):
    assert len(list(suggestions.candidates(words(*texts), 3000))) == count


def test_invalid_and_distant_timings():
    tokens = words('a', 'a')
    tokens[1].start_ms, tokens[1].end_ms = 2000, 2200
    assert not list(suggestions.candidates(tokens, 3000))
    tokens[1].end_ms = None
    assert not list(suggestions.candidates(tokens, 3000))


def test_idempotency_and_retranscription(isolated_backend, source, engine):
    job_id = job(isolated_backend, source)
    transcription.run_transcription(job_id)
    with isolated_backend() as db:
        db.query(TranscriptToken).delete()
        for i, t in enumerate(words('Hello', ' hello', 'um')):
            db.add(TranscriptToken(session_id=source[0], job_id=job_id, seq=i, **vars(t)))
        db.commit()
        assert suggestions.generate(db, source[0], job_id) == 2
        assert suggestions.generate(db, source[0], job_id) == 0
        decisions = db.query(EditDecision).order_by(EditDecision.start_ms).all()
        assert all(d.accepted is None for d in decisions)
        decisions[0].accepted = False
        db.commit()
        assert suggestions.generate(db, source[0], job_id) == 0
    transcription.run_transcription(job(isolated_backend, source))
    with isolated_backend() as db:
        assert db.query(EditDecision).one().accepted is False
        with pytest.raises(ValueError, match='转写已变化'):
            suggestions.generate(db, source[0], job_id)


def test_api_skips_protected_ranges_and_rejects_stale_job(isolated_backend, source, engine):
    from fastapi.testclient import TestClient
    from app.main import app
    job_id = job(isolated_backend, source)
    transcription.run_transcription(job_id)
    with isolated_backend() as db:
        db.query(TranscriptToken).delete()
        for i, token in enumerate(words('a', 'a', 'um')):
            db.add(TranscriptToken(session_id=source[0], job_id=job_id, seq=i, **vars(token)))
        db.add(EditDecision(session_id=source[0], start_ms=0, end_ms=250, action='KEEP_PROTECTED', source='user', confidence=1, hard_protected=True))
        db.commit()
    with TestClient(app) as client:
        url = f'/api/sessions/{source[0]}/decisions/suggest'
        result = client.post(url, json={'transcriptionJobId': job_id})
        assert result.status_code == 200
        assert result.json()['addedCount'] == 1
        assert result.json()['decisions'][1]['action'] == 'DELETE_FILLER'
        assert result.json()['decisions'][1]['accepted'] is None
        assert client.post(url, json={'transcriptionJobId': 'stale'}).status_code == 409


def test_bulk_reject_only_pending_unprotected_rules(isolated_backend, source):
    from fastapi.testclient import TestClient
    from app.main import app
    cases = [
        ('transcript_rule', None, False, 'DELETE_FILLER', False),
        ('transcript_rule', True, False, 'DELETE_REPETITION', True),
        ('transcript_rule', False, False, 'DELETE_FILLER', False),
        ('user', None, False, 'DELETE_SELECTION', None),
        ('algorithm', None, False, 'DELETE_REPETITION', None),
        ('transcript_rule', None, True, 'DELETE_FILLER', None),
        ('transcript_rule', None, False, 'KEEP_PROTECTED', None),
    ]
    with isolated_backend() as db:
        for i, (origin, accepted, protected, action, _) in enumerate(cases):
            db.add(EditDecision(id=f'bulk_{i}', session_id=source[0], start_ms=i * 100, end_ms=i * 100 + 50,
                                source=origin, accepted=accepted, hard_protected=protected, action=action, confidence=1))
        db.commit()
    with TestClient(app) as client:
        url = f'/api/sessions/{source[0]}/decisions/reject-suggestions'
        response = client.post(url)
        assert response.status_code == 200
        assert [d['accepted'] for d in response.json()] == [c[-1] for c in cases]
        assert client.post(url).json() == response.json()
        assert client.post('/api/sessions/missing/decisions/reject-suggestions').status_code == 404
