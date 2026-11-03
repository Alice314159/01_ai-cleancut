"""
录制中的实时提示（可选，默认关闭）：每隔 INTERVAL_S 秒转写刚录下的 WINDOW_S 秒，
用与录后剪辑建议相同的文字规则找相邻重复、口吃、语气词，通过 WebSocket 提示录音者
（例如“刚才那句有重复，要不要重说”）。

只是提示：不写剪辑决策，也不写数据库——录音结束后的完整转写会照常生成剪辑建议。
永远不影响录音本身：模型不可用、正被录后转写占用、解码失败时都只跳过这一轮。
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import tempfile

from app import config, db as db_module
from app.models import RecordingSession
from app.services import audio_analysis, storage, suggestions, transcription

INTERVAL_S = 15
WINDOW_S = 20
# 窗口末尾的几秒可能有被截断的词，下一轮再看
TAIL_GUARD_MS = 3000

KIND = {"DELETE_REPETITION": "repetition", "DELETE_STUTTER": "stutter", "DELETE_FILLER": "filler"}
LABEL = {"repetition": "疑似重复", "stutter": "疑似口吃", "filler": "语气词"}


def available() -> bool:
    return transcription.dependency_available()


def _transcribe_tail(samples, window_start_ms: int) -> list:
    """在工作线程里跑：转写最近一段，返回带绝对时间的词（与转写 token 同形）。拿不到模型锁就跳过。"""
    if not transcription._model_lock.acquire(blocking=False):
        return []
    try:
        model = transcription.get_model(config.LIVE_HINT_MODEL, str(config.ASR_MODEL_DIR), config.ASR_LOCAL_FILES_ONLY)
        with tempfile.TemporaryDirectory(prefix="cleancut-live-") as folder:
            path = Path(folder) / "tail.wav"
            audio_analysis.encode_wav(samples, path)
            segments, info = model.transcribe(str(path), language=None, beam_size=1, word_timestamps=True,
                                              vad_filter=True, condition_on_previous_text=False)
            duration_ms = round(len(samples) / audio_analysis.ANALYSIS_SAMPLE_RATE * 1000)
            words = []
            for segment in segments:
                for word in transcription.timed_words(segment, duration_ms):
                    text = transcription.to_simplified(word["text"]) if info.language == "zh" else word["text"]
                    words.append(SimpleNamespace(text=text, start_ms=window_start_ms + word["start_ms"],
                                                 end_ms=window_start_ms + word["end_ms"]))
            return words
    finally:
        transcription._model_lock.release()


def hints_from_words(words: list, since_ms: int, until_ms: int) -> list[dict]:
    """在 [since_ms, until_ms) 内找规则命中的片段，转成提示消息。"""
    if not words:
        return []
    horizon = max(w.end_ms for w in words)
    found = []
    for candidate in suggestions.candidates(words, horizon):
        kind = KIND.get(candidate["action"])
        if kind is None or not since_ms <= candidate["start_ms"] < until_ms:
            continue
        found.append({
            "type": "live_hint", "kind": kind, "text": candidate["text"].strip(),
            "startMs": candidate["start_ms"], "endMs": candidate["end_ms"],
            "message": f"{LABEL[kind]}「{candidate['text'].strip()}」",
        })
    return found


async def run_live_hints(session_id: str, stop_event: asyncio.Event, send) -> None:
    if not available():
        await send({"type": "live_hints_unavailable", "message": "本机未安装语音转写，无法实时提示；录完后仍可转写并生成剪辑建议。"})
        return
    raw_path = Path(storage.raw_chunk_path(session_id))
    checked_until_ms = 0
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=INTERVAL_S)
            break
        except asyncio.TimeoutError:
            pass
        with db_module.SessionLocal() as db:
            session = db.get(RecordingSession, session_id)
            if session is None or session.capture_status != "RECORDING":
                continue
            recorded_ms = session.committed_duration_ms
        try:
            samples = await asyncio.to_thread(audio_analysis.decode_tail_pcm, raw_path, seconds=WINDOW_S)
        except audio_analysis.DecodeError:
            continue
        window_ms = round(len(samples) / audio_analysis.ANALYSIS_SAMPLE_RATE * 1000)
        end_ms = max(recorded_ms, window_ms)
        try:
            words = await asyncio.to_thread(_transcribe_tail, samples, end_ms - window_ms)
        except Exception:  # 模型加载失败等：这一轮跳过，不影响录音
            continue
        until_ms = end_ms - TAIL_GUARD_MS
        for hint in hints_from_words(words, checked_until_ms, until_ms):
            if stop_event.is_set():
                break
            await send(hint)
        checked_until_ms = max(checked_until_ms, until_ms)
