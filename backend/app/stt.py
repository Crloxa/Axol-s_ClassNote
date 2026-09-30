"""本地离线转写：faster-whisper（CPU）+ Silero VAD。

流程：
- 浏览器用 MediaRecorder 采集 webm/opus，分块 POST 到 /audio，后端按序追加进会话音频文件。
- 录音期间前端定期调用 flush；flush 时先解码整段音频（廉价），用 VAD 找出语音段，
  只转写“已完整结束”（其后跟停顿）且尚未处理过的语音段，增量产出带时间戳的转写。
- 时间轴 = 音频文件内位置（秒）。页面标记 markers 使用同一时间轴，用于把转写片段归属到 PPT 页。
"""
from __future__ import annotations

import threading
from pathlib import Path

import numpy as np

from . import db as store
from .config import lesson_dir

SR = 16000
TAIL_COMPLETE_SEC = 0.4      # 语音段结束后留出该余量才视为“已完整”
MIN_SPEECH_SEC = 0.5
_lock = threading.Lock()


class _ModelHolder:
    size: str | None = None
    model = None


def _get_model(size: str):
    if _ModelHolder.model is not None and _ModelHolder.size == size:
        return _ModelHolder.model
    from faster_whisper import WhisperModel

    _ModelHolder.model = WhisperModel(size, device="cpu", compute_type="int8")
    _ModelHolder.size = size
    return _ModelHolder.model


def _decode(path: Path) -> np.ndarray:
    """流式解码为 16k 单声道 float32；录音中的尾部残缺 cluster 直接忽略。"""
    import av

    resampler = av.AudioResampler(format="s16", layout="mono", rate=SR)
    pieces: list[np.ndarray] = []
    try:
        with av.open(str(path)) as container:
            for frame in container.decode(audio=0):
                for rf in resampler.resample(frame):
                    pieces.append(np.asarray(rf.to_ndarray()).reshape(-1))
    except Exception:
        pass
    if not pieces:
        return np.zeros(0, dtype=np.float32)
    pcm = np.concatenate(pieces)
    return pcm.astype(np.float32) / 32768.0


def _vad_speech(audio: np.ndarray) -> list[tuple[int, int]]:
    if audio.size == 0:
        return []
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        opts = VadOptions(min_silence_duration_ms=500, speech_pad_ms=60)
        ts = get_speech_timestamps(audio, opts, sampling_rate=SR)
        return [(int(t["start"]), int(t["end"])) for t in ts]
    except Exception:
        return _energy_vad(audio)


def _energy_vad(audio: np.ndarray, frame_sec: float = 0.05,
                thresh: float = 0.008) -> list[tuple[int, int]]:
    """VAD 不可用时的兜底：基于能量的简易语音切分。"""
    frame = int(SR * frame_sec)
    n = len(audio) // frame
    out: list[tuple[int, int]] = []
    start = None
    for i in range(n):
        rms = float(np.sqrt(np.mean(audio[i * frame:(i + 1) * frame] ** 2)))
        if rms > thresh:
            if start is None:
                start = i * frame
        elif start is not None and (i + 1) * frame - start > SR * MIN_SPEECH_SEC:
            out.append((start, i * frame))
            start = None
    if start is not None:
        out.append((start, n * frame))
    return out


def start_session(lesson_id: str) -> int:
    d = lesson_dir(lesson_id) / "audio"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"session-{store.now().replace(':', '-')}.webm"
    with store.db() as conn:
        conn.execute(
            "INSERT INTO sessions(lesson_id, audio_path, started_at, processed_end, active) "
            "VALUES(?,?,?,0,1)",
            (lesson_id, str(path), store.now()),
        )
        sid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    return sid


def append_audio(lesson_id: str, data: bytes) -> bool:
    with store.db() as conn:
        sess = conn.execute(
            "SELECT * FROM sessions WHERE lesson_id=? AND active=1 ORDER BY id DESC LIMIT 1",
            (lesson_id,),
        ).fetchone()
    if sess is None:
        return False
    with open(sess["audio_path"], "ab") as f:
        f.write(data)
    return True


def flush(lesson_id: str, force: bool = False) -> list[dict]:
    """转写新完成的语音段；force=True 时结束会话并处理全部剩余语音。"""
    with _lock:
        return _flush_locked(lesson_id, force)


def _flush_locked(lesson_id: str, force: bool) -> list[dict]:
    with store.db() as conn:
        sess = conn.execute(
            "SELECT * FROM sessions WHERE lesson_id=? AND active=1 ORDER BY id DESC LIMIT 1",
            (lesson_id,),
        ).fetchone()
        if sess is None:
            return []
        settings = store.get_settings(conn)

    path = Path(sess["audio_path"])
    if not path.exists():
        if force:
            _close_session(sess, total=0.0, delete=True)
        return []

    audio = _decode(path)
    total = len(audio) / SR
    processed = float(sess["processed_end"])

    todo: list[tuple[int, int]] = []
    consumed = processed
    for st, en in _vad_speech(audio):
        s_sec, e_sec = st / SR, en / SR
        if e_sec <= processed + 0.05:
            continue
        if not force and s_sec < processed - 0.5:
            continue  # 上一轮已消费过开头的语音段，避免重复
        if force or e_sec <= total - TAIL_COMPLETE_SEC:
            todo.append((st, en))
            consumed = max(consumed, e_sec)

    new_rows: list[dict] = []
    if todo:
        model = _get_model(settings["stt_model"])
        lang = settings["stt_language"] or None
        for st, en in todo:
            offset = st / SR
            try:
                segs, _info = model.transcribe(
                    audio[st:en], language=lang, beam_size=1,
                    condition_on_previous_text=False, vad_filter=False,
                )
                for seg in segs:
                    text = (seg.text or "").strip()
                    if text:
                        new_rows.append({"start": round(seg.start + offset, 2),
                                         "end": round(seg.end + offset, 2),
                                         "text": text, "status": "final"})
            except Exception as e:  # 单段失败不拖垮整个会话
                new_rows.append({"start": round(offset, 2), "end": round(en / SR, 2),
                                 "text": f"（本段转写失败：{e}）", "status": "error"})

    with store.db() as conn:
        for r in new_rows:
            conn.execute(
                "INSERT INTO segments(lesson_id, start, end, text, status, highlight, "
                "manual_slide, created_at) VALUES(?,?,?,?,?,0,NULL,?)",
                (lesson_id, r["start"], r["end"], r["text"], r["status"], store.now()),
            )
        if force:
            _close_session(sess, total=total, delete=settings["keep_audio"] != "true",
                           conn=conn)
        else:
            conn.execute("UPDATE sessions SET processed_end=? WHERE id=?",
                         (consumed, sess["id"]))
    return new_rows


def _close_session(sess, total: float, delete: bool, conn=None):
    def _run(c):
        c.execute("UPDATE sessions SET active=0, ended_at=?, processed_end=? WHERE id=?",
                  (store.now(), total, sess["id"]))
        if delete:
            try:
                Path(sess["audio_path"]).unlink(missing_ok=True)
            except OSError:
                pass
    if conn is None:
        with store.db() as c:
            _run(c)
    else:
        _run(conn)
