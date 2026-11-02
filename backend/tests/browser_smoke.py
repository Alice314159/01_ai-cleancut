"""Optional real-browser regression suite; requires Python playwright + Chromium.
Run from backend: python tests/browser_smoke.py
Uses temporary storage and its own API/Vite processes; never touches data/.
"""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import wave
import time
from urllib.request import urlopen

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_http(url, process):
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError(f"server exited: {process.returncode}")
        try:
            with urlopen(url, timeout=1):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(url)


def main():
    test_asr = '--asr' in sys.argv
    test_master = '--master-transcript' in sys.argv
    api_port, ui_port = free_port(), free_port()
    api_base = f"http://127.0.0.1:{api_port}"
    ui_base = f"http://127.0.0.1:{ui_port}"
    with tempfile.TemporaryDirectory(prefix="cleancut-browser-") as temp:
        temp_path = Path(temp)
        speech_wav = temp_path / 'speech.wav'
        if test_asr:
            speech_aiff = temp_path / 'speech.aiff'
            subprocess.run(['say', '-v', 'Samantha', '-r', '145', '-o', str(speech_aiff),
                            'Hello, this is a recording test. We can edit the audio and save the result.'], check=True)
            subprocess.run(['ffmpeg', '-v', 'error', '-i', str(speech_aiff), '-ar', '48000', '-ac', '1', str(speech_wav)], check=True)
        server = temp_path / "server.py"
        server.write_text('''import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from app import config
config.AUDIO_DIR = Path(sys.argv[2]) / 'audio'
config.AUDIO_DIR.mkdir()
config.DATABASE_URL = 'sqlite:///' + str(Path(sys.argv[2]) / 'test.db')
config.CORS_ORIGINS = [sys.argv[4]]
from app.main import app
if sys.argv[5] == '1':
    from types import SimpleNamespace as NS
    from app.services import transcription
    class FixtureEngine:
        def transcribe(self, filename, **kwargs):
            word = 'Replacement audio.' if Path(filename).name.startswith('master_') else 'Original audio.'
            return iter([NS(end=.8, words=[NS(start=.1, end=.8, word=word)])]), NS(language='en')
    transcription.dependency_available = lambda: True
    transcription.get_model = lambda *a: FixtureEngine()
import uvicorn
uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[3]), log_level='warning')
''')
        with (temp_path / "servers.log").open("w+") as log:
            api_python = str(ROOT / 'backend/.venv/bin/python') if test_asr else sys.executable
            api_process = subprocess.Popen([api_python, str(server), str(ROOT / 'backend'), temp,
                                            str(api_port), ui_base, '1' if test_master else '0'], stdout=log, stderr=log,
                                            env={**os.environ, 'CLEANCUT_ASR_OFFLINE': '1'})
            frontend = subprocess.Popen(['npm', 'run', 'dev', '--', '--host', '127.0.0.1', '--port', str(ui_port), '--strictPort'],
                                        cwd=ROOT / 'frontend', env={**os.environ, 'VITE_API_BASE': api_base}, stdout=log, stderr=log)
            try:
                wait_http(api_base + '/api/health', api_process)
                wait_http(ui_base, frontend)
                with sync_playwright() as p:
                    browser_args = ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream']
                    if test_asr:
                        browser_args.append(f'--use-file-for-fake-audio-capture={speech_wav}')
                    browser = p.chromium.launch(headless=True, args=browser_args)
                    context = browser.new_context(permissions=['microphone'], viewport={"width": 1440, "height": 1000})
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.add_init_script('''
                        window.__sockets = []; window.__streams = []; window.__failSocket = false;
                        const NativeSocket = window.WebSocket;
                        window.WebSocket = class extends NativeSocket {
                            constructor(url, protocols) {
                                super(window.__failSocket ? 'ws://127.0.0.1:1' : url, protocols);
                                window.__sockets.push(this);
                            }
                        };
                        const getMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
                        navigator.mediaDevices.getUserMedia = async (...args) => {
                            const stream = await getMedia(...args); window.__streams.push(stream); return stream;
                        };
                    ''')
                    page.goto(ui_base)
                    def navigate(name):
                        page.locator('.rail__nav button').filter(has_text=name).click()
                    def create():
                        navigate('会话')
                        with page.expect_response(lambda res: res.url == api_base + '/api/sessions' and res.request.method == 'POST') as response:
                            page.get_by_role('button', name='开始新录制', exact=True).click()
                        return response.value.json()['id']
                    def session(session_id):
                        return context.request.get(f'{api_base}/api/sessions/{session_id}').json()
                    def finalized(session_id):
                        for _ in range(100):
                            current = session(session_id)
                            if current['liveAnalysisStatus'] in ['FINALIZED', 'FAILED']:
                                assert current['liveAnalysisStatus'] == 'FINALIZED', current
                                return current
                            page.wait_for_timeout(100)
                        raise AssertionError('finalization timed out')
                    first = create()
                    page.get_by_role('button', name='开始录制', exact=True).click()
                    expect(page.get_by_role('button', name='暂停', exact=True)).to_be_visible()
                    page.wait_for_timeout(1600)
                    page.get_by_role('button', name='暂停', exact=True).click()
                    expect(page.get_by_role('button', name='继续', exact=True)).to_be_visible()
                    page.get_by_role('button', name='继续', exact=True).click()
                    page.wait_for_timeout(1600)
                    # Navigation must flush the final MediaRecorder event and stop the microphone.
                    navigate('增强')
                    current = finalized(first)
                    assert current['totalDurationMs'] >= 2500
                    expect(page.get_by_role('heading', name='音频增强', exact=True)).to_be_visible()
                    page.wait_for_function("window.__streams.every(s => s.getTracks().every(t => t.readyState === 'ended'))")
                    print('PASS: pause/resume, navigation flush, microphone cleanup, report polling', flush=True)

                    def enhance():
                        with page.expect_response(lambda res: res.url == f'{api_base}/api/sessions/{first}/enhancement' and res.request.method == 'POST') as response:
                            page.get_by_role('button', name='开始增强', exact=True).click()
                        job_id = response.value.json()['id']
                        expect(page.get_by_text('增强完成', exact=True)).to_be_visible(timeout=15000)
                        expect(page.get_by_role('button', name='开始增强', exact=True)).to_be_enabled()
                        result = context.request.get(f'{api_base}/api/sessions/{first}/enhancement/{job_id}').json()
                        assert result['status'] == 'COMPLETED', result
                        return result
                    job1 = enhance()
                    audio_url = f"{api_base}/api/sessions/{first}/versions/{job1['resultVersionId']}/audio"
                    old_audio = context.request.get(audio_url).body()
                    page.locator('button').filter(has_text='较强').click()
                    job2 = enhance()
                    assert job1['id'] != job2['id']
                    assert context.request.get(audio_url).body() == old_audio
                    assert context.request.get(f'{api_base}/api/sessions/{first}/enhancement').json()['id'] == job2['id']
                    page.locator('audio').first.evaluate('(audio) => audio.play()')
                    page.wait_for_function("Array.from(document.querySelectorAll('audio')).some(a => a.currentTime > 0)")
                    print('PASS: repeated enhancement, job polling, immutable audio history, playback', flush=True)

                    navigate('复核')
                    expect(page.get_by_role('heading', name='确认剪辑', exact=True)).to_be_visible()
                    expect(page.get_by_role('region', name='剪辑流程')).to_be_visible()
                    next_action = page.locator('.workflow-actions .btn--primary')
                    expect(next_action).to_be_enabled()
                    target = next_action.get_attribute('data-target')
                    next_action.click()
                    expect(page.locator(f'#{target}').locator('select, input:not(:disabled), button:not(:disabled)').first).to_be_focused()
                    page.get_by_text('高级剪辑：按时间输入删除区间或重录', exact=True).click()
                    page.get_by_label('开始时间（秒）').fill('0.5')
                    page.get_by_label('结束时间（秒）').fill('1.0')
                    page.get_by_role('button', name='添加删除区间', exact=True).click()
                    expect(page.get_by_text('手动选段', exact=True)).to_be_visible()
                    expect(page.locator('.gutter--decisions .seg--cut')).to_have_count(1)
                    with page.expect_response(lambda res: res.url == f'{api_base}/api/sessions/{first}/render' and res.request.method == 'POST') as response:
                        page.get_by_role('button', name='生成剪辑音频', exact=True).click()
                    assert response.value.status == 201, response.value.text()
                    output = response.value.json()
                    assert output['removedDurationMs'] == 500
                    expect(page.get_by_text('成片已就绪。', exact=False)).to_be_visible()
                    page.get_by_label('成片试听').evaluate('(audio) => audio.play()')
                    page.wait_for_function("document.querySelector('audio[aria-label=\"成片试听\"]').currentTime > 0")
                    with page.expect_download() as download:
                        page.get_by_role('link', name='下载成片 WAV', exact=True).click()
                    downloaded = temp_path / 'export.wav'
                    download.value.save_as(downloaded)
                    with wave.open(str(downloaded)) as audio:
                        assert audio.getnframes() == output['version']['sampleCount']
                        assert audio.getframerate() == output['version']['sampleRate']
                    decision = context.request.get(f'{api_base}/api/sessions/{first}/decisions').json()[0]
                    changed = context.request.patch(f"{api_base}/api/sessions/{first}/decisions/{decision['id']}",
                                                    data={'accepted': None, 'expectedRevision': decision['revision']})
                    assert changed.status == 200
                    page.get_by_role('button', name='拒绝（快捷键 R）', exact=True).click()
                    expect(page.get_by_role('heading', name='剪辑选择冲突', exact=True)).to_be_visible()
                    for choice in ('采用最新选择', '应用我的选择', '改为待确认', '暂不处理'):
                        expect(page.get_by_role('button', name=choice, exact=True)).to_be_visible()
                    page.get_by_role('button', name='应用我的选择', exact=True).click()
                    expect(page.get_by_role('heading', name='剪辑选择冲突', exact=True)).to_have_count(0)
                    expect(page.get_by_text('选择或音频源已变化', exact=False)).to_be_visible()
                    expect(page.locator('.gutter--decisions .seg--cut')).to_have_count(0)
                    with page.expect_response(lambda res: res.url == f'{api_base}/api/sessions/{first}/render' and res.request.method == 'POST') as response:
                        page.get_by_role('button', name='生成剪辑音频', exact=True).click()
                    assert response.value.status == 201
                    restored = response.value.json()
                    assert restored['removedDurationMs'] == 0
                    assert restored['version']['durationMs'] == output['version']['durationMs'] + 500
                    previous_audio = context.request.get(f"{api_base}/api/sessions/{first}/versions/{output['version']['id']}/audio")
                    assert previous_audio.body() == downloaded.read_bytes()
                    navigate('会话')
                    expect(page.locator('.session-row').filter(has_text=first)).to_be_visible()
                    navigate('复核')
                    expect(page.get_by_role('link', name='下载成片 WAV', exact=True)).to_be_visible()
                    print('PASS: manual cuts, render, playback, WAV download, stale detection, repeated rendering', flush=True)

                    page.set_viewport_size({'width': 1000, 'height': 900})
                    panel = page.locator('#review-tools-panel')
                    fab = page.locator('.review-tools-fab')
                    expect(fab).to_be_visible()
                    expect(panel).to_have_attribute('aria-hidden', 'true')
                    fab.click()
                    expect(panel).to_have_attribute('aria-modal', 'true')
                    assert page.locator('.review-transcript-column').evaluate('(e) => e.inert')
                    page.keyboard.press('Shift+Tab')
                    assert panel.evaluate('(e) => e.contains(document.activeElement)')
                    page.keyboard.press('Escape')
                    expect(fab).to_be_focused()
                    expect(panel).to_have_attribute('aria-hidden', 'true')
                    page.set_viewport_size({'width': 390, 'height': 844})
                    fab_box = fab.bounding_box()
                    assert fab_box['y'] + fab_box['height'] < page.locator('.tabbar').bounding_box()['y']
                    assert page.locator('.main__scroll').evaluate('(e) => e.scrollWidth <= e.clientWidth')
                    page.set_viewport_size({'width': 1440, 'height': 1000})
                    page.wait_for_function('!document.querySelector("#review-tools-panel").inert')
                    print('PASS: narrow inspector, keyboard focus, Escape, mobile clearance and desktop restoration', flush=True)

                    if test_asr:
                        page.get_by_label('识别语言').select_option('en')
                        page.get_by_role('button', name='开始本机转写', exact=True).click()
                        expect(page.get_by_text('转写完成 · en', exact=True)).to_be_visible(timeout=120000)
                        with page.expect_response(lambda res: res.url.endswith('/decisions/suggest')) as suggested:
                            page.get_by_role('button', name='查找重复与语气词', exact=True).click()
                        assert suggested.value.status == 200, suggested.value.text()
                        for decision in suggested.value.json()['decisions']:
                            if decision['accepted'] is None:
                                context.request.patch(f"{api_base}/api/sessions/{first}/decisions/{decision['id']}", data={'accepted': False})
                        navigate('会话')
                        navigate('复核')
                        words = context.request.get(f'{api_base}/api/sessions/{first}/transcript').json()
                        assert len(words) >= 3, words
                        for extension in ('TXT', 'SRT'):
                            with page.expect_download() as exported:
                                page.locator('.export-menu summary').click()
                                page.get_by_role('menuitem', name=f'{extension} 文本', exact=True).click()
                            download = exported.value
                            assert download.suggested_filename == f'transcript-original.{extension.lower()}'
                            exported_text = Path(download.path()).read_text()
                            assert words[0]['text'].strip() in exported_text
                            if extension == 'SRT':
                                assert ' --> ' in exported_text
                        page.locator('.transcript-word').nth(0).click()
                        page.wait_for_function("document.querySelector('audio[aria-label=\"剪辑前试听\"]').paused", timeout=10000)
                        preview_time = page.get_by_label('剪辑前试听').evaluate('(audio) => audio.currentTime')
                        assert words[0]['endMs'] / 1000 <= preview_time < words[0]['endMs'] / 1000 + 0.6
                        page.locator('.transcript-word').nth(2).click()
                        expect(page.locator('.transcript-word.tok--selected')).to_have_count(3)
                        expect(page.get_by_label('开始时间（秒）')).to_have_value(f"{words[0]['startMs'] / 1000:.3f}")
                        expect(page.get_by_label('结束时间（秒）')).to_have_value(f"{max(w['endMs'] for w in words[:3]) / 1000:.3f}")
                        page.get_by_role('button', name='删除选中文字', exact=True).click()
                        expect(page.locator('.transcript-word.tok--cut')).to_have_count(3)
                        page.get_by_role('checkbox', name='平滑剪辑', exact=False).check()
                        with page.expect_response(lambda res: res.url == f'{api_base}/api/sessions/{first}/render' and res.request.method == 'POST') as response:
                            page.get_by_role('button', name='生成剪辑音频', exact=True).click()
                        assert response.value.status == 201, response.value.text()
                        assert response.value.json()['removedDurationMs'] == max(w['endMs'] for w in words[:3]) - words[0]['startMs']
                        with page.expect_download() as master_subtitles:
                            page.get_by_role('button', name='导出成片 SRT', exact=True).click()
                        assert master_subtitles.value.suggested_filename == 'transcript-master.srt'
                        assert ' --> ' in Path(master_subtitles.value.path()).read_text()
                        print('PASS: actual local ASR, two-click word selection, text-based cut and render', flush=True)

                    second = create()
                    page.get_by_role('button', name='开始录制', exact=True).click()
                    expect(page.get_by_role('button', name='暂停', exact=True)).to_be_visible()
                    page.wait_for_timeout(1600)
                    page.evaluate("window.__sockets.at(-1).close()")
                    page.wait_for_function("window.__streams.every(s => s.getTracks().every(t => t.readyState === 'ended'))")
                    assert finalized(second)['captureStatus'] == 'STOPPED'
                    expect(page.get_by_text('录制连接中断', exact=False)).to_be_visible()
                    print('PASS: unexpected connection closure finalizes received audio', flush=True)

                    third = create()
                    page.evaluate('window.__failSocket = true')
                    page.get_by_role('button', name='开始录制', exact=True).click()
                    expect(page.get_by_text('WebSocket 连接失败', exact=True)).to_be_visible()
                    page.evaluate('window.__failSocket = false')
                    page.get_by_role('button', name='开始录制', exact=True).click()
                    expect(page.get_by_role('button', name='暂停', exact=True)).to_be_visible()
                    page.wait_for_timeout(1600)
                    page.get_by_role('button', name='停止并终态化', exact=True).click()
                    assert finalized(third)['captureStatus'] == 'STOPPED'
                    page.get_by_role('button', name='下一步：转写与剪辑', exact=True).click()
                    expect(page.get_by_role('heading', name='确认剪辑', exact=True)).to_be_visible()
                    print('PASS: failed connection rejects, retry succeeds, explicit stop works', flush=True)
                    # Hold the previous session's role response until after selecting another session.
                    old_roles = context.request.get(f'{api_base}/api/sessions/{first}/roles').json()
                    new_roles = context.request.get(f'{api_base}/api/sessions/{third}/roles').json()
                    expected_source = next(r['audioVersionId'] for r in new_roles if r['role'] == 'RENDER_SOURCE')
                    held = []
                    pattern = f'{api_base}/api/sessions/{first}/roles'
                    page.route(pattern, lambda route: held.append(route))
                    navigate('会话')
                    page.locator('.session-row').filter(has_text=first).locator('.session-row__title').click()
                    for _ in range(50):
                        if held:
                            break
                        page.wait_for_timeout(100)
                    assert held
                    navigate('会话')
                    page.locator('.session-row').filter(has_text=third).locator('.session-row__title').click()
                    expect(page.locator('.timebase__val[title]').first).to_have_attribute('title', expected_source)
                    for route in held:
                        route.fulfill(json=old_roles)
                    page.unroute(pattern)
                    page.wait_for_timeout(300)
                    expect(page.locator('.timebase__val[title]').first).to_have_attribute('title', expected_source)
                    print('PASS: delayed previous-session response cannot replace current audio roles', flush=True)
                    navigate('会话')
                    with page.expect_file_chooser() as chooser:
                        page.get_by_role('button', name='导入音频', exact=True).click()
                    chooser.value.set_files(downloaded)
                    expect(page.get_by_role('heading', name='确认剪辑', exact=True)).to_be_visible(timeout=30000)
                    expect(page.get_by_role('region', name='剪辑流程')).to_be_visible()
                    print('PASS: recording next step and imported audio enter editing directly', flush=True)
                    # Re-record directly while editing an imported recording.
                    page.get_by_text('高级剪辑：按时间输入删除区间或重录', exact=True).click()
                    page.get_by_label('开始时间（秒）').fill('0.5')
                    page.get_by_label('结束时间（秒）').fill('1.0')
                    page.get_by_role('button', name='重录此时间范围', exact=True).click()
                    dialog = page.get_by_role('dialog', name='重录选中片段')
                    expect(dialog).to_be_visible()
                    dialog.get_by_role('button', name='开始重录', exact=True).click()
                    expect(dialog.get_by_role('button', name='停止重录', exact=True)).to_be_visible()
                    page.wait_for_timeout(1200)
                    dialog.get_by_role('button', name='停止重录', exact=True).click()
                    expect(dialog.get_by_label('新录音试听')).to_be_visible()
                    page.wait_for_function("window.__streams.every(s => s.getTracks().every(t => t.readyState === 'ended'))")
                    dialog.get_by_label('新录音试听').evaluate('(audio) => audio.play()')
                    page.wait_for_function("document.querySelector('audio[aria-label=\"新录音试听\"]').currentTime > 0")
                    with page.expect_response(lambda res: res.url.endswith('/retakes') and res.request.method == 'POST') as retake_response:
                        dialog.get_by_role('button', name='应用重录替换', exact=True).click()
                    assert retake_response.value.status == 201, retake_response.value.text()
                    take = retake_response.value.json()
                    expect(dialog).not_to_be_visible()
                    page.locator('.decision__row').filter(has_text='重录替换').click()
                    expect(page.get_by_role('button', name='撤销重录', exact=True)).to_be_visible()
                    expect(page.get_by_label('已保存的重录试听')).to_be_visible()
                    with page.expect_response(lambda res: res.url.endswith('/render') and res.request.method == 'POST') as replaced_response:
                        page.get_by_role('button', name='生成剪辑音频', exact=True).click()
                    assert replaced_response.value.status == 201, replaced_response.value.text()
                    replaced = replaced_response.value.json()
                    assert replaced['hasReplacements'] is True
                    assert abs(replaced['version']['durationMs'] - (output['version']['durationMs'] - 500 + take['replacementDurationMs'])) <= 1
                    if test_master:
                        expect(page.get_by_text('成片转写完成，字幕可下载。', exact=True)).to_be_visible(timeout=20000)
                        page.get_by_text('查看成片文字', exact=False).click()
                        expect(page.locator('.master-transcript__text')).to_contain_text('Replacement audio.')
                        for extension in ('SRT', 'TXT'):
                            with page.expect_download() as master_download:
                                page.get_by_role('button', name=f'导出成片 {extension}', exact=True).click()
                            assert 'Replacement audio.' in Path(master_download.value.path()).read_text()
                        print('PASS: automatic master recognition, final text preview, TXT and SRT download', flush=True)
                    else:
                        expect(page.get_by_role('region', name='成片转写', exact=True)).to_be_visible()
                    page.get_by_role('button', name='撤销重录', exact=True).click()
                    expect(page.get_by_role('button', name='使用此重录', exact=True)).to_be_visible()
                    with page.expect_response(lambda res: res.url.endswith('/render') and res.request.method == 'POST') as undone:
                        page.get_by_role('button', name='生成剪辑音频', exact=True).click()
                    assert undone.value.status == 201
                    assert undone.value.json()['hasReplacements'] is False
                    assert undone.value.json()['version']['durationMs'] == output['version']['durationMs']
                    # Cancel a running take: no saved mutation and no live microphone remains.
                    page.get_by_label('开始时间（秒）').fill('0.5')
                    page.get_by_label('结束时间（秒）').fill('1.0')
                    page.get_by_role('button', name='重录此时间范围', exact=True).click()
                    dialog.get_by_role('button', name='开始重录', exact=True).click()
                    expect(dialog.get_by_role('button', name='停止重录', exact=True)).to_be_visible()
                    dialog.get_by_role('button', name='取消重录', exact=True).click()
                    expect(dialog).not_to_be_visible()
                    page.wait_for_function("window.__streams.every(s => s.getTracks().every(t => t.readyState === 'ended'))")
                    expect(page.get_by_label('已保存的重录试听')).to_have_count(1)
                    print('PASS: retake recording, preview, apply, actual rendering, undo and cancellation cleanup', flush=True)
                    assert not errors, errors
                    browser.close()
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read(), file=sys.stderr)
                raise
            finally:
                for process in [frontend, api_process]:
                    process.terminate()
                for process in [frontend, api_process]:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


if __name__ == '__main__':
    main()
