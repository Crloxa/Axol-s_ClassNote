"""生成：范围物化、发送预览、一次性确认令牌、本地规则整理、模型调用。

关键规则（需求 §5.4 / §6.3 / §7）：
- 预览接口不联网；生成前必须先预览拿到一次性令牌，令牌过期（15 分钟）或已使用即失效。
- 模型请求只包含选中范围的提取文本：选中 PPT 页文本、勾选的参考资料文本、
  关联到选中章节的转写 + （全部模式下）未关联转写。原始文件与音频永不出本机。
- 未关联转写只在“全部内容”范围进入生成，避免单章生成被无关内容污染，也不静默丢弃。
"""
from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timedelta

from . import db as store
from . import parsers, providers

FIXED_PROMPT = """你是课堂复习整理 Agent。只使用本次提供的 PPT 页文本、选中的参考资料文本、课堂转写和用户指令。
不得编造资料中没有的事实、定义、公式、例子或结论。
保留给定 PPT 章节顺序。每个重要条目注明 PPT 页码、参考资料位置或课堂时间范围。
将 PPT 既有内容与“课堂补充（PPT 外）”分开。
转写不清、材料冲突或无法确认时标记“待核对”。
详细提纲优先覆盖全部关键事实，删除口头重复和明显识别噪声即可。
只输出 Markdown 结果，不输出推理过程。"""

MODES = {
    "key_points": {
        "label": "重点讲义",
        "task": "任务：生成重点讲义——按章节整理核心概念、结论、老师强调的内容、例子、易错点和待核对项。",
    },
    "supplement": {
        "label": "PPT 外补充",
        "task": "任务：只输出课堂转写中出现、且选中 PPT 没有等价内容的有效补充、答疑和提醒（PPT 外补充），不要复述 PPT 已有内容。",
    },
    "outline": {
        "label": "详细提纲",
        "task": "任务：生成详细提纲——覆盖选中范围的分级提纲，优先保证不遗漏关键事实，删除口头重复和明显识别噪声。",
    },
}

TOKEN_TTL_MIN = 15


def normalize_scope(conn, lesson_id: str, payload: dict) -> dict:
    mode = payload.get("mode")
    engine = payload.get("engine")
    if mode not in MODES:
        raise ValueError(f"生成模式无效: {mode}")
    if engine not in ("local", "model"):
        raise ValueError(f"生成引擎无效: {engine}")
    provider = payload.get("provider")
    if engine == "model" and provider not in providers.PROVIDERS:
        raise ValueError("远程生成需要有效的服务商")
    if engine == "local":
        provider = None

    slide_idxs = payload.get("slide_idxs")
    if slide_idxs is not None:
        have = {r["idx"] for r in
                conn.execute("SELECT idx FROM slides WHERE lesson_id=?", (lesson_id,))}
        slide_idxs = sorted({int(i) for i in slide_idxs})
        bad = [i for i in slide_idxs if i not in have]
        if bad:
            raise ValueError(f"章节不存在: {bad}")
        if not slide_idxs:
            raise ValueError("选择了“选定章节”，但未勾选任何章节")

    source_ids = [int(i) for i in (payload.get("source_ids") or [])]
    have_src = {r["id"] for r in
                conn.execute("SELECT id FROM sources WHERE lesson_id=?", (lesson_id,))}
    missing = [i for i in source_ids if i not in have_src]
    if missing:
        raise ValueError(f"参考资料不存在: {missing}")

    return {
        "mode": mode,
        "engine": engine,
        "provider": provider,
        "slide_idxs": slide_idxs,
        "source_ids": source_ids,
        "instruction": (payload.get("instruction") or "").strip()[:2000],
    }


def materialize(conn, lesson_id: str, scope: dict) -> dict:
    """按范围收集文本块（不联网），附带来源位置。"""
    all_mode = scope["slide_idxs"] is None
    selected = set(scope["slide_idxs"] or [])

    slide_rows = conn.execute(
        "SELECT idx, title, text FROM slides WHERE lesson_id=? ORDER BY idx",
        (lesson_id,),
    ).fetchall()
    slides = [{"idx": r["idx"], "title": r["title"], "text": r["text"]}
              for r in slide_rows if all_mode or r["idx"] in selected]

    markers = conn.execute(
        "SELECT slide_idx, t FROM markers WHERE lesson_id=? ORDER BY t", (lesson_id,)
    ).fetchall()

    def derive(t: float):
        cur = None
        for m in markers:
            if m["t"] <= t + 1e-6:
                cur = m["slide_idx"]
            else:
                break
        return cur

    by_slide: dict[int, list] = {}
    unassoc: list = []
    t_min, t_max = None, None
    transcript_chars = 0
    for s in conn.execute(
        "SELECT * FROM segments WHERE lesson_id=? ORDER BY start", (lesson_id,)
    ):
        slide = s["manual_slide"] if s["manual_slide"] is not None else derive(s["start"])
        included = slide is None and all_mode or slide is not None and (all_mode or slide in selected)
        if not included:
            continue
        item = {"start": s["start"], "end": s["end"], "text": s["text"],
                "highlight": bool(s["highlight"]), "slide": slide}
        transcript_chars += len(s["text"])
        if slide is None:
            unassoc.append(item)
        else:
            by_slide.setdefault(slide, []).append(item)
        t_min = s["start"] if t_min is None else min(t_min, s["start"])
        t_max = s["end"] if t_max is None else max(t_max, s["end"])

    source_blocks = []
    source_chars = 0
    if scope["source_ids"]:
        for sid in scope["source_ids"]:
            row = conn.execute(
                "SELECT name, kind, content FROM sources WHERE id=?", (sid,)
            ).fetchone()
            if row is None:
                continue
            items = parsers.source_items(row["name"], row["kind"], store.load_json(row["content"]))
            source_blocks.append({"name": row["name"], "kind": row["kind"], "items": items})
            source_chars += sum(len(i["text"]) for i in items)

    ppt_chars = sum(len(s["title"]) + len(s["text"]) for s in slides)
    total = ppt_chars + transcript_chars + source_chars
    tokens_est = math.ceil(total / 1.7) if total else 0
    cost_est = None
    if scope["engine"] == "model":
        with store.db() as c2:
            cfg = providers.get_provider_cfg(c2, scope["provider"])
        price = float(cfg.get("input_price") or 0)
        cost_est = round(tokens_est / 1_000_000 * price, 4) if price > 0 else None

    return {
        "slides": slides,
        "by_slide": by_slide,
        "unassoc": unassoc,
        "sources": source_blocks,
        "stats": {
            "ppt_chars": ppt_chars,
            "transcript_chars": transcript_chars,
            "source_chars": source_chars,
            "total_chars": total,
            "tokens_est": tokens_est,
            "cost_est": cost_est,
            "segment_count": sum(len(v) for v in by_slide.values()) + len(unassoc),
            "transcript_range": [t_min, t_max],
        },
    }


def build_user_message(scope: dict, mat: dict) -> str:
    parts: list[str] = []
    parts.append("# 用户补充指令\n" + (scope["instruction"] or "（无）"))
    parts.append("# 任务\n" + MODES[scope["mode"]]["task"])

    parts.append("# PPT 内容（保持章节顺序）")
    if not mat["slides"]:
        parts.append("（本范围未包含 PPT 页）")
    for s in mat["slides"]:
        body = s["text"] or "（本页未提取到文字，待核对）"
        parts.append(f"[PPT 第 {s['idx']} 页] {s['title']}\n{body}")

    if mat["sources"]:
        parts.append("# 选中的参考资料")
        for src in mat["sources"]:
            parts.append(f"## 资料：{src['name']}")
            for item in src["items"]:
                if item["text"]:
                    parts.append(f"[{src['name']}：{item['loc']}]\n{item['text']}")
                else:
                    parts.append(f"[{src['name']}：{item['loc']}]（未提取到文字，待核对）")

    parts.append("# 课堂转写（已按 PPT 页关联）")
    any_assoc = any(mat["by_slide"].values())
    if not any_assoc:
        parts.append("（本范围没有已关联到章节的转写）")
    for idx in sorted(mat["by_slide"]):
        for s in mat["by_slide"][idx]:
            star = "【重点】" if s["highlight"] else ""
            parts.append(f"[课堂 {store.fmt_clock(s['start'])}-{store.fmt_clock(s['end'])}]"
                         f"（关联：PPT 第 {idx} 页）{star}{s['text']}")

    if mat["unassoc"]:
        parts.append("# 未关联转写（未标记所属 PPT 页）")
        for s in mat["unassoc"]:
            star = "【重点】" if s["highlight"] else ""
            parts.append(f"[课堂 {store.fmt_clock(s['start'])}-{store.fmt_clock(s['end'])}]{star}{s['text']}")

    return "\n\n".join(parts)


def make_preview(lesson_id: str, payload: dict) -> dict:
    with store.db() as conn:
        lesson = conn.execute("SELECT * FROM lessons WHERE id=?", (lesson_id,)).fetchone()
        if lesson is None:
            raise ValueError("课堂不存在")
        scope = normalize_scope(conn, lesson_id, payload)
        mat = materialize(conn, lesson_id, scope)
        provider_info = None
        if scope["engine"] == "model":
            cfg = providers.get_provider_cfg(conn, scope["provider"])
            provider_info = {"id": scope["provider"], "label": cfg["label"],
                             "base_url": cfg["base_url"], "model": cfg["model"],
                             "has_key": cfg["has_key"]}
        token = uuid.uuid4().hex
        expires = (datetime.now() + timedelta(minutes=TOKEN_TTL_MIN)).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO tokens(token, lesson_id, scope_json, created_at, expires_at, used) "
            "VALUES(?,?,?,?,?,0)",
            (token, lesson_id, json.dumps(scope, ensure_ascii=False), store.now(), expires),
        )

    slides_brief = [{"idx": s["idx"], "title": s["title"]} for s in mat["slides"]]
    return {
        "token": token,
        "expires_at": expires,
        "scope": scope,
        "provider": provider_info,
        "slides": slides_brief,
        "sources": [{"name": s["name"], "kind": s["kind"]} for s in mat["sources"]],
        "stats": mat["stats"],
        "mode_label": MODES[scope["mode"]]["label"],
    }


def generate(token: str) -> dict:
    with store.db() as conn:
        tok = conn.execute("SELECT * FROM tokens WHERE token=?", (token,)).fetchone()
    if tok is None or tok["used"]:
        raise ValueError("预览令牌无效或已使用，请重新生成预览")
    if tok["expires_at"] < store.now():
        raise ValueError("预览令牌已过期，请重新生成预览")

    scope = json.loads(tok["scope_json"])
    lesson_id = tok["lesson_id"]

    with store.db() as conn:
        lesson = conn.execute("SELECT * FROM lessons WHERE id=?", (lesson_id,)).fetchone()
        if lesson is None:
            raise ValueError("课堂不存在")
        scope = normalize_scope(conn, lesson_id, scope)  # 范围引用的内容被删除则在此报错
        mat = materialize(conn, lesson_id, scope)

    if scope["engine"] == "local":
        content = local_markdown(lesson["name"], scope, mat)
        used_remote = False
    else:
        content = providers.call_model(scope["provider"], FIXED_PROMPT,
                                       build_user_message(scope, mat))
        used_remote = True

    with store.db() as conn:
        conn.execute("UPDATE tokens SET used=1 WHERE token=?", (token,))
        conn.execute(
            "INSERT INTO results(lesson_id, mode, engine, scope_json, content_md, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (lesson_id, scope["mode"], scope["engine"], json.dumps(scope, ensure_ascii=False),
             content, store.now()),
        )
        rid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    return {"result_id": rid, "content_md": content, "used_remote": used_remote,
            "mode_label": MODES[scope["mode"]]["label"]}


def _quote(text: str) -> str:
    return "\n".join("> " + ln for ln in text.splitlines()) if text else "（无）"


def local_markdown(lesson_name: str, scope: dict, mat: dict) -> str:
    label = MODES[scope["mode"]]["label"]
    all_mode = scope["slide_idxs"] is None
    rng = "全部内容" if all_mode else "章节 " + "、".join(str(i) for i in scope["slide_idxs"])
    lines = [
        f"# {label}（本地规则整理）",
        f"- 课堂：{lesson_name}",
        f"- 生成时间：{store.now()}",
        f"- 范围：{rng}",
        f"- 说明：本地规则整理不调用模型，仅按页码/章节汇总已提取的资料与转写。",
        "",
    ]
    supplement_only = scope["mode"] == "supplement"
    for s in mat["slides"]:
        lines.append(f"## [PPT 第 {s['idx']} 页] {s['title']}")
        if not supplement_only:
            lines.append("PPT 原文：")
            lines.append(_quote(s["text"]) if s["text"] else "> 待核对：本页未提取到文字")
        segs = mat["by_slide"].get(s["idx"], [])
        if segs:
            lines.append("课堂转写：")
            for seg in segs:
                star = " ⭐【重点】" if seg["highlight"] else ""
                lines.append(f"- [课堂 {store.fmt_clock(seg['start'])}-"
                             f"{store.fmt_clock(seg['end'])}]{star} {seg['text']}")
        lines.append("")

    if mat["sources"] and not supplement_only:
        lines.append("## 参考资料摘录")
        for src in mat["sources"]:
            for item in src["items"]:
                if item["text"]:
                    lines.append(f"- [{src['name']}：{item['loc']}] {item['text']}")
                else:
                    lines.append(f"- [{src['name']}：{item['loc']}] 待核对：未提取到文字")
        lines.append("")

    lines.append("## 未关联转写")
    if mat["unassoc"]:
        for seg in mat["unassoc"]:
            star = " ⭐【重点】" if seg["highlight"] else ""
            lines.append(f"- [课堂 {store.fmt_clock(seg['start'])}-"
                         f"{store.fmt_clock(seg['end'])}]{star} {seg['text']}")
    else:
        lines.append("（无）")
    lines.append("")
    return "\n".join(lines)
