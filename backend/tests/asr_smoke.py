"""Real offline ASR + word-cut smoke test. macOS say generates test speech.
Run .venv/bin/python tests/asr_smoke.py after scripts/prepare_asr.py.
"""
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import config, db as db_module
from app.models import AudioVersion, RecordingSession, RoleAssignment
from app.services import audio_analysis


def main():
    with tempfile.TemporaryDirectory(prefix='cleancut-asr-smoke-') as temporary:
        root = Path(temporary)
        config.AUDIO_DIR = root / 'audio'
        config.ASR_LOCAL_FILES_ONLY = True
        engine = create_engine(f'sqlite:///{root / "test.db"}', connect_args={'check_same_thread': False})
        db_module.engine = engine
        db_module.SessionLocal = sessionmaker(bind=engine)
        from app.main import app
        with TestClient(app) as client:
            for language, voice, spoken, expected in [
                ('en', 'Samantha', 'Hello, this is a recording test. We can edit the audio and save the result.', 'recording'),
                ('zh', 'Tingting', '你好，这是一个录音测试。我们可以编辑音频，然后保存结果。', '测试'),
            ]:
                session_id = client.post('/api/sessions', json={}).json()['id']
                directory = config.AUDIO_DIR / session_id
                directory.mkdir(parents=True)
                speech = directory / 'speech.aiff'
                subprocess.run(['say', '-v', voice, '-r', '145', '-o', str(speech), spoken], check=True)
                pcm = audio_analysis.decode_pcm(speech)
                path = directory / 'speech.wav'
                audio_analysis.encode_wav(pcm, path)
                duration_ms = int(len(pcm) * 1000 / audio_analysis.ANALYSIS_SAMPLE_RATE)
                with db_module.SessionLocal() as db:
                    session = db.get(RecordingSession, session_id)
                    session.capture_status = 'STOPPED'
                    session.live_analysis_status = 'FINALIZED'
                    session.total_duration_ms = duration_ms
                    version = AudioVersion(session_id=session_id, version_type='original', label='speech',
                                           duration_ms=duration_ms, sample_count=len(pcm), sample_rate=16000, filename=path.name)
                    db.add(version)
                    db.flush()
                    for role in ['TIMEBASE', 'ASR_SOURCE', 'RENDER_SOURCE', 'EXPORT_SOURCE']:
                        db.add(RoleAssignment(session_id=session_id, role=role, audio_version_id=version.id))
                    db.commit()
                response = client.post(f'/api/sessions/{session_id}/transcription', json={'language': language})
                assert response.status_code == 202, response.text
                job_id = response.json()['id']
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    job = client.get(f'/api/sessions/{session_id}/transcription/{job_id}').json()
                    if job['status'] in ['COMPLETED', 'FAILED']:
                        break
                    time.sleep(0.2)
                assert job['status'] == 'COMPLETED', job
                tokens = client.get(f'/api/sessions/{session_id}/transcript').json()
                transcript = ''.join(token['text'] for token in tokens)
                print(f'{language}: {transcript}', flush=True)
                assert expected in transcript.lower(), transcript
                assert all(0 <= token['startMs'] < token['endMs'] <= duration_ms for token in tokens)
                word = tokens[len(tokens) // 2]
                decision = client.post(f'/api/sessions/{session_id}/decisions', json={
                    'startMs': word['startMs'], 'endMs': word['endMs'],
                })
                assert decision.status_code == 201, decision.text
                rendered = client.post(f'/api/sessions/{session_id}/render')
                assert rendered.status_code == 201, rendered.text
                assert rendered.json()['removedDurationMs'] == word['endMs'] - word['startMs']
                print(f'PASS: {language} actual CPU model, {len(tokens)} timed words, word-based render', flush=True)
                if '--master' in sys.argv:
                    import json
                    replacement = directory / 'replacement.aiff'
                    updated_speech = 'This replacement recording has updated words. The final subtitles should follow this audio.' if language == 'en' else '这是重新录制的内容。新的字幕应该和成片音频保持一致。'
                    subprocess.run(['say', '-v', voice, '-r', '145', '-o', str(replacement), updated_speech], check=True)
                    decisions = client.get(f'/api/sessions/{session_id}/decisions').json()
                    roles = client.get(f'/api/sessions/{session_id}/roles').json()
                    source_id = next(r['audioVersionId'] for r in roles if r['role'] == 'RENDER_SOURCE')
                    retake = client.post(f'/api/sessions/{session_id}/retakes', data={
                        'startMs': 0, 'endMs': duration_ms, 'sourceVersionId': source_id,
                        'snapshot': json.dumps({'expectedRevisions': {d['id']: d['revision'] for d in decisions}}),
                    }, files={'file': ('replacement.aiff', replacement.read_bytes(), 'audio/aiff')})
                    assert retake.status_code == 201, retake.text
                    master = client.post(f'/api/sessions/{session_id}/render')
                    assert master.status_code == 201, master.text
                    master_id = master.json()['version']['id']
                    final = client.get(f'/api/sessions/{session_id}/render/{master_id}/transcript').json()
                    assert final['job']['status'] == 'COMPLETED', final
                    final_text = ''.join(t['text'] for t in final['tokens'])
                    print(f'{language} master: {final_text}', flush=True)
                    assert ('updated' if language == 'en' else '字幕') in final_text.lower()
                    assert all(0 <= t['startMs'] < t['endMs'] <= master.json()['version']['durationMs'] for t in final['tokens'])
                    subtitles = client.get(f'/api/sessions/{session_id}/render/{master_id}/subtitles')
                    assert subtitles.status_code == 200 and ' --> ' in subtitles.text
                    assert ''.join(t['text'] for t in client.get(f'/api/sessions/{session_id}/transcript').json()) == transcript
                    print(f'PASS: {language} real retake recognition, master subtitles and original text preserved', flush=True)

        engine.dispose()


if __name__ == '__main__':
    main()
