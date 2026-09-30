"""HTTP 接口层。仅由本机前端调用；应用只监听 127.0.0.1（见 run.py）。"""
from __future__ import annotations

import re
import uuid
from urllib.parse import quote

import markdown as mdlib
from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from . import db as store
from . import generate, parsers, providers, stt
from .config import MAX_UPLOAD_BYTES, lesson_dir

router = APIRouter(prefix="/api")


def bad(e: Exception):
    raise HTTPException(status_code=400, detail=str(e))


def _lesson_or_404(conn, lesson_id: str):
    row = conn.execute("SELECT * FROM lessons WHERE id=?", (lesson_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="课堂不存在")
    return row


# ---------- 课堂 ----------

class LessonIn(BaseModel):
    name: str


@router.get("/lessons")
def list_lessons():
    with store.db() as conn:
        rows = conn.execute(
            """SELECT l.*,
                      (SELECT COUNT(*) FROM slides s WHERE s.lesson_id=l.id) AS slide_count,
                      (SELECT COUNT(*) FROM sources src WHERE src.lesson_id=l.id) AS source_count,
                      (SELECT COUNT(*) FROM segments g WHERE g.lesson_id=l.id) AS segment_count
               FROM lessons l ORDER BY l.created_at DESC""").fetchall()
        return [dict(r) for r in rows]


@router.post("/lessons")
def create_lesson(body: LessonIn):
    name = body.name.strip()[:100]
    if not name:
        raise HTTPException(400, "课堂名称不能为空")
    lid = uuid.uuid4().hex[:12]
    with store.db() as conn:
        conn.execute("INSERT INTO lessons(id, name, created_at) VALUES(?,?,?)",
                     (lid, name, store.now()))
    lesson_dir(lid)
    return {"id": lid, "name": name}


@router.get("/lessons/{lesson_id}")
def get_lesson(lesson_id: str):
    with store.db() as conn:
        lesson = _lesson_or_404(conn, lesson_id)
        slides = [dict(r) for r in conn.execute(
            "SELECT idx, title, text FROM slides WHERE lesson_id=? ORDER BY idx", (lesson_id,))]
        sources = []
        for r in conn.execute(
                "SELECT id, name, kind, content, created_at FROM sources "
                "WHERE lesson_id=? ORDER BY id", (lesson_id,)):
            src = dict(r)
            src["content"] = store.load_json(src["content"])
            sources.append(src)
        segments = [dict(r) for r in conn.execute(
            "SELECT * FROM segments WHERE lesson_id=? ORDER BY start", (lesson_id,))]
        markers = [dict(r) for r in conn.execute(
            "SELECT id, slide_idx, t FROM markers WHERE lesson_id=? ORDER BY t", (lesson_id,))]
        results = [dict(r) for r in conn.execute(
            "SELECT id, mode, engine, created_at FROM results "
            "WHERE lesson_id=? ORDER BY id DESC", (lesson_id,))]
        active = conn.execute(
            "SELECT id FROM sessions WHERE lesson_id=? AND active=1", (lesson_id,)).fetchone()
        settings = store.get_settings(conn)
    return {"lesson": dict(lesson), "slides": slides, "sources": sources,
            "segments": segments, "markers": markers, "results": results,
            "recording": active is not None,
            "settings": {"stt_model": settings["stt_model"],
                         "stt_language": settings["stt_language"],
                         "keep_audio": settings["keep_audio"] == "true"}}


# ---------- 文件导入 ----------

def _save_upload(lesson_id: str, sub: str, file: UploadFile) -> tuple[str, bytes]:
    data = file.file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "文件超过 60MB 上限")
    if not data:
        raise HTTPException(400, "文件为空")
    name = file.filename or "unnamed"
    d = lesson_dir(lesson_id) / sub
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]', "_", name)
    path = d / safe
    i = 1
    while path.exists():
        path = d / f"{path.stem}-{i}{path.suffix}"
        i += 1
    path.write_bytes(data)
    return str(path), data


@router.post("/lessons/{lesson_id}/ppt")
def import_ppt(lesson_id: str, file: UploadFile):
    try:
        with store.db() as conn:
            _lesson_or_404(conn, lesson_id)
        if parsers.kind_for(file.filename or "") != "pptx":
            raise HTTPException(400, "主课件必须是 .pptx 文件")
        path, data = _save_upload(lesson_id, "ppt", file)
        slides = parsers.parse_pptx(data)
        with store.db() as conn:
            conn.execute("DELETE FROM slides WHERE lesson_id=?", (lesson_id,))
            for s in slides:
                conn.execute(
                    "INSERT INTO slides(lesson_id, idx, title, text) VALUES(?,?,?,?)",
                    (lesson_id, s["idx"], s["title"], s["text"]))
        return {"slide_count": len(slides), "slides": slides}
    except HTTPException:
        raise
    except Exception as e:
        bad(e)


@router.post("/lessons/{lesson_id}/sources")
def import_source(lesson_id: str, file: UploadFile):
    try:
        with store.db() as conn:
            _lesson_or_404(conn, lesson_id)
        kind = parsers.kind_for(file.filename or "")
        if not kind:
            raise HTTPException(400, "仅支持 PDF/DOCX/Markdown/常见代码文件")
        path, data = _save_upload(lesson_id, "sources", file)
        content = parsers.parse_source(file.filename, kind, data)
        with store.db() as conn:
            conn.execute(
                "INSERT INTO sources(lesson_id, name, kind, path, content, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (lesson_id, file.filename, kind, path, parsers.dumps(content), store.now()))
            sid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        return {"id": sid, "name": file.filename, "kind": kind, "content": content}
    except HTTPException:
        raise
    except Exception as e:
        bad(e)


@router.delete("/sources/{source_id}")
def delete_source(source_id: int):
    with store.db() as conn:
        conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
    return {"ok": True}


# ---------- 录音与转写 ----------

@router.post("/lessons/{lesson_id}/recording/start")
def recording_start(lesson_id: str):
    try:
        with store.db() as conn:
            _lesson_or_404(conn, lesson_id)
            active = conn.execute(
                "SELECT id FROM sessions WHERE lesson_id=? AND active=1",
                (lesson_id,)).fetchone()
            if active:
                raise HTTPException(400, "该课堂已有进行中的录音")
        sid = stt.start_session(lesson_id)
        return {"session_id": sid}
    except HTTPException:
        raise
    except Exception as e:
        bad(e)


@router.post("/lessons/{lesson_id}/audio")
async def upload_audio(lesson_id: str, request: Request):
    data = await request.body()
    if not data:
        raise HTTPException(400, "音频分块为空")
    if not stt.append_audio(lesson_id, data):
        raise HTTPException(400, "没有进行中的录音会话，请先开始录音")
    return {"ok": True}


@router.post("/lessons/{lesson_id}/transcribe/flush")
def transcribe_flush(lesson_id: str):
    try:
        return {"segments": stt.flush(lesson_id, force=False)}
    except Exception as e:
        bad(e)


@router.post("/lessons/{lesson_id}/recording/stop")
def recording_stop(lesson_id: str):
    try:
        return {"segments": stt.flush(lesson_id, force=True)}
    except Exception as e:
        bad(e)


class SegmentPatch(BaseModel):
    text: str | None = None
    highlight: bool | None = None
    manual_slide: int | None = None


@router.patch("/segments/{segment_id}")
def patch_segment(segment_id: int, body: SegmentPatch):
    # 只更新请求中显式给出的字段；manual_slide 显式传 null 表示清除关联
    fields = body.model_dump(exclude_unset=True)
    with store.db() as conn:
        row = conn.execute("SELECT * FROM segments WHERE id=?", (segment_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "转写片段不存在")
        if "text" in fields:
            if not str(fields["text"]).strip():
                raise HTTPException(400, "转写文本不能为空")
            conn.execute("UPDATE segments SET text=? WHERE id=?",
                         (str(fields["text"]).strip(), segment_id))
        if "highlight" in fields:
            conn.execute("UPDATE segments SET highlight=? WHERE id=?",
                         (1 if fields["highlight"] else 0, segment_id))
        if "manual_slide" in fields:
            if fields["manual_slide"] is not None:
                ok = conn.execute("SELECT 1 FROM slides WHERE lesson_id=? AND idx=?",
                                  (row["lesson_id"], fields["manual_slide"])).fetchone()
                if not ok:
                    raise HTTPException(400, "页码不存在")
            conn.execute("UPDATE segments SET manual_slide=? WHERE id=?",
                         (fields["manual_slide"], segment_id))
        fresh = dict(conn.execute("SELECT * FROM segments WHERE id=?",
                                  (segment_id,)).fetchone())
    return fresh


class MarkerIn(BaseModel):
    slide_idx: int
    t: float


@router.post("/lessons/{lesson_id}/markers")
def add_marker(lesson_id: str, body: MarkerIn):
    with store.db() as conn:
        _lesson_or_404(conn, lesson_id)
        conn.execute("INSERT INTO markers(lesson_id, slide_idx, t, created_at) VALUES(?,?,?,?)",
                     (lesson_id, body.slide_idx, body.t, store.now()))
        markers = [dict(r) for r in conn.execute(
            "SELECT id, slide_idx, t FROM markers WHERE lesson_id=? ORDER BY t", (lesson_id,))]
    return {"markers": markers}


# ---------- 设置与模型服务商 ----------

@router.get("/settings")
def get_settings():
    with store.db() as conn:
        return store.get_settings(conn)


class SettingsIn(BaseModel):
    stt_model: str | None = None
    stt_language: str | None = None
    keep_audio: bool | None = None


@router.put("/settings")
def put_settings(body: SettingsIn):
    with store.db() as conn:
        if body.stt_model is not None:
            if body.stt_model not in ("tiny", "base", "small", "medium"):
                raise HTTPException(400, "stt_model 仅支持 tiny/base/small/medium")
            store.set_setting(conn, "stt_model", body.stt_model)
        if body.stt_language is not None:
            store.set_setting(conn, "stt_language", body.stt_language.strip()[:10])
        if body.keep_audio is not None:
            store.set_setting(conn, "keep_audio", "true" if body.keep_audio else "false")
        return store.get_settings(conn)


@router.get("/providers")
def list_providers():
    with store.db() as conn:
        out = []
        for pid in providers.PROVIDERS:
            cfg = providers.get_provider_cfg(conn, pid)
            out.append({"id": pid, "label": cfg["label"], "api": cfg["api"],
                        "base_url": cfg["base_url"], "model": cfg["model"],
                        "input_price": cfg.get("input_price", 0), "has_key": cfg["has_key"]})
        return out


class ProviderIn(BaseModel):
    base_url: str | None = None
    model: str | None = None
    input_price: float | None = None


@router.put("/providers/{pid}")
def put_provider(pid: str, body: ProviderIn):
    if pid not in providers.PROVIDERS:
        raise HTTPException(404, "未知服务商")
    with store.db() as conn:
        if body.base_url is not None:
            url = body.base_url.strip()
            try:
                providers.validate_endpoint(url)
            except ValueError as e:
                raise HTTPException(400, str(e))
            store.set_setting(conn, f"provider:{pid}:base_url", url)
        if body.model is not None:
            store.set_setting(conn, f"provider:{pid}:model", body.model.strip()[:100])
        if body.input_price is not None:
            if body.input_price < 0:
                raise HTTPException(400, "价格不能为负")
            store.set_setting(conn, f"provider:{pid}:input_price", str(body.input_price))
    return {"ok": True}


class KeyIn(BaseModel):
    key: str


@router.put("/providers/{pid}/key")
def put_provider_key(pid: str, body: KeyIn):
    try:
        providers.set_key(pid, body.key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.delete("/providers/{pid}/key")
def delete_provider_key(pid: str):
    providers.delete_key(pid)
    return {"ok": True}


# ---------- 生成与导出 ----------

class PreviewIn(BaseModel):
    lesson_id: str
    mode: str
    engine: str
    provider: str | None = None
    slide_idxs: list[int] | None = None
    source_ids: list[int] = []
    instruction: str = ""


@router.post("/generate/preview")
def generate_preview(body: PreviewIn):
    """计算范围与发送预览，不联网；返回一次性确认令牌。"""
    try:
        return generate.make_preview(body.lesson_id, body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))


class GenerateIn(BaseModel):
    token: str


@router.post("/generate")
def do_generate(body: GenerateIn):
    try:
        return generate.generate(body.token)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/lessons/{lesson_id}/results")
def list_results(lesson_id: str):
    with store.db() as conn:
        _lesson_or_404(conn, lesson_id)
        rows = conn.execute(
            "SELECT id, mode, engine, created_at FROM results WHERE lesson_id=? ORDER BY id DESC",
            (lesson_id,)).fetchall()
        return [dict(r) for r in rows]


@router.get("/results/{result_id}")
def get_result(result_id: int):
    with store.db() as conn:
        row = conn.execute("SELECT * FROM results WHERE id=?", (result_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "结果不存在")
    return dict(row)


class ResultPatch(BaseModel):
    content_md: str


@router.patch("/results/{result_id}")
def patch_result(result_id: int, body: ResultPatch):
    with store.db() as conn:
        row = conn.execute("SELECT id FROM results WHERE id=?", (result_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "结果不存在")
        conn.execute("UPDATE results SET content_md=? WHERE id=?",
                     (body.content_md, result_id))
    return {"ok": True}


def _render_html(md_text: str) -> str:
    body = mdlib.markdown(md_text, extensions=["tables", "fenced_code", "nl2br"])
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>课堂复习资料</title><style>"
        "body{font-family:'Segoe UI','Microsoft YaHei',sans-serif;max-width:860px;"
        "margin:32px auto;padding:0 16px;line-height:1.7;color:#24292f}"
        "h1,h2,h3{border-bottom:1px solid #d8dee4;padding-bottom:6px}"
        "blockquote{color:#57606a;border-left:4px solid #d0d7de;margin:0;padding:0 12px;"
        "background:#f6f8fa}"
        "table{border-collapse:collapse}td,th{border:1px solid #d0d7de;padding:4px 10px}"
        "code{background:#f6f8fa;padding:1px 5px;border-radius:4px}"
        "</style></head><body>" + body + "</body></html>"
    )


@router.get("/results/{result_id}/export")
def export_result(result_id: int, format: str = "md"):
    with store.db() as conn:
        row = conn.execute(
            """SELECT r.content_md, r.mode, l.name AS lesson_name FROM results r
               JOIN lessons l ON l.id=r.lesson_id WHERE r.id=?""", (result_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "结果不存在")
    fname_base = re.sub(r"\s+", "_", f"{row['lesson_name']}-{row['mode']}") or "result"
    if format == "html":
        content, ext, mime = _render_html(row["content_md"]), "html", "text/html"
    else:
        content, ext, mime = row["content_md"], "md", "text/markdown"
    filename = quote(f"{fname_base}.{ext}")
    return Response(
        content=content, media_type=f"{mime}; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )
