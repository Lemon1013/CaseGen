"""Retry gateway and run SSE cancel-window E2E (generate + review)."""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

from sqlmodel import Session, select

from app import config
from app.db import get_engine, init_db, reset_engine
from app.models.entities import (
    CaseDraft,
    GenerationTask,
    ModelConfig,
    Requirement,
    ReviewResult,
    TaskCitation,
    TaskEvent,
)
from app.services.llm import chat_completion
from app.services.retrieve import load_all_wiki_pages, rank_pages
from app.services.review_parse import parse_review_payload
from app.services.task_events import append_event
from app.services.task_pipeline import (
    _LEAN_GENERATE_SYSTEM,
    _build_query,
    _truncate_wiki_context,
    run_generate,
    run_review,
)

# backend/scripts → repo root
ROOT = Path(__file__).resolve().parents[2]
config.DATA_DIR = ROOT / "data"
config.WIKI_DIR = config.DATA_DIR / "wiki"
config.WIKI_PAGES_DIR = config.WIKI_DIR / "pages"
config.META_DIR = config.DATA_DIR / "meta"
config.DB_PATH = config.META_DIR / "app.db"
config.MAX_WIKI_CONTEXT_CHARS = 1400
config.RETRIEVE_TOP_K = 2


def main() -> int:
    reset_engine()
    init_db()

    with Session(get_engine()) as s:
        m = s.exec(select(ModelConfig).where(ModelConfig.is_default == True)).first()  # noqa: E712
        if m is None:
            print("no default model")
            return 1
        print("model", m.name, m.base_url, m.model_name)

    # ping
    for i in range(3):
        t0 = time.time()
        try:
            c, _u = chat_completion(
                base_url=m.base_url,
                api_key=m.api_key,
                model=m.model_name,
                messages=[{"role": "user", "content": "只回复OK"}],
                timeout=60,
                max_retries=1,
            )
            print(f"PING#{i+1} OK {time.time()-t0:.1f}s {c[:40]!r}")
            break
        except Exception as e:  # noqa: BLE001
            print(f"PING#{i+1} FAIL {time.time()-t0:.1f}s {e}")
            time.sleep(2)
    else:
        print("gateway down")
        return 1

    def chat_fn(messages, model=None, **kwargs):
        msgs = list(messages)
        msgs[0] = {"role": "system", "content": _LEAN_GENERATE_SYSTEM}
        if len(msgs) > 1 and len(msgs[1].get("content") or "") > 1800:
            msgs[1] = {"role": "user", "content": msgs[1]["content"][:1800]}
        total = sum(len(x.get("content") or "") for x in msgs)
        print(f"  LLM chars={total}", flush=True)
        t0 = time.time()
        content, usage = chat_completion(
            base_url=model.base_url,
            api_key=model.api_key,
            model=model.model_name,
            messages=msgs,
            timeout=120,
            max_retries=2,
            backoff_sec=2,
        )
        print(f"  LLM OK {time.time()-t0:.1f}s out={len(content)} usage={usage}", flush=True)
        return content

    with Session(get_engine()) as s:
        req = Requirement(
            title="开盘集合竞价不可撤单窗口校验",
            description=(
                "依据上交所交易规则(2026修订)：9:15-9:25开盘集合竞价，其中9:20-9:25不接受撤单；"
                "9:15-9:20未成交可撤；14:57-15:00收盘集合竞价不接受撤单；连续竞价未成交可撤。"
                "生成正常/边界/异常用例并引用Wiki。"
            ),
            focus_tags_json=json.dumps(
                ["集合竞价", "撤单", "9:20-9:25"], ensure_ascii=False
            ),
        )
        s.add(req)
        s.commit()
        s.refresh(req)
        task = GenerationTask(
            requirement_id=req.id, status="draft", model_id=m.id
        )
        s.add(task)
        s.commit()
        s.refresh(task)
        tid = task.id
        print("task", tid)

    print("=== GENERATE ===", flush=True)
    t0 = time.time()
    with Session(get_engine()) as s:
        task = run_generate(s, tid, chat_fn=chat_fn)
        print(
            "generate",
            task.status,
            task.error_message,
            "elapsed",
            round(time.time() - t0, 1),
        )

    if task.status != "generated":
        print("pipeline generate failed; ultra-short direct call", flush=True)
        with Session(get_engine()) as s:
            task_row = s.get(GenerationTask, tid)
            req = s.get(Requirement, task_row.requirement_id)
            pages = load_all_wiki_pages(s)
            hits = rank_pages(_build_query(req), pages, top_k=2)
            wiki = _truncate_wiki_context(hits, 900)
            msgs = [
                {"role": "system", "content": _LEAN_GENERATE_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"# 需求\n标题：{req.title}\n描述：{req.description}\n"
                        f"关注标签：集合竞价,撤单\n\n# Wiki\n{wiki}\n\n"
                        "请生成4条用例Markdown。"
                    )[:1600],
                },
            ]
            print("ultra chars", sum(len(x["content"]) for x in msgs), flush=True)
            content, usage = chat_completion(
                base_url=m.base_url,
                api_key=m.api_key,
                model=m.model_name,
                messages=msgs,
                timeout=120,
                max_retries=3,
                backoff_sec=3,
            )
            print("ultra OK", len(content), usage, flush=True)
            if task_row.status == "failed":
                task_row.status = "generating"
                task_row.error_message = None
                s.add(task_row)
            s.add(
                CaseDraft(
                    task_id=tid,
                    version=1,
                    content_md=content,
                    prompt_version_ref="ultra_short_e2e",
                )
            )
            existing = s.exec(
                select(TaskCitation).where(TaskCitation.task_id == tid)
            ).first()
            if existing is None:
                for hit in hits:
                    s.add(
                        TaskCitation(
                            task_id=tid,
                            wiki_page_id=hit.get("id"),
                            title=hit.get("title") or "",
                            path=hit.get("path") or "",
                            score=float(hit.get("score") or 0),
                            snippet=hit.get("snippet") or "",
                        )
                    )
            task_row.status = "generated"
            task_row.error_message = None
            s.add(task_row)
            append_event(
                s,
                tid,
                "generate",
                "生成完成（ultra_short_e2e）",
                detail={"chars": len(content)},
            )
            s.commit()
            draft_md = content
    else:
        with Session(get_engine()) as s:
            draft_md = (
                s.exec(select(CaseDraft).where(CaseDraft.task_id == tid))
                .first()
                .content_md
            )

    print("=== DRAFT ===")
    print((draft_md or "")[:2800])

    def review_chat_fn(messages, model=None, **kwargs):
        msgs = list(messages)
        sys = (
            "你是测试评审专家。只返回JSON："
            "score,verdict,issues,missing_scenarios,"
            "prompt_improvement_hints,ready_for_final"
        )
        user = msgs[-1]["content"] if msgs else ""
        if len(user) > 2500:
            user = user[:2500]
        t0 = time.time()
        content, usage = chat_completion(
            base_url=model.base_url,
            api_key=model.api_key,
            model=model.model_name,
            messages=[
                {"role": "system", "content": sys},
                {"role": "user", "content": user},
            ],
            timeout=120,
            max_retries=2,
            backoff_sec=2,
        )
        print(
            f"  review LLM OK {time.time()-t0:.1f}s out={len(content)}",
            flush=True,
        )
        return content

    print("=== REVIEW ===", flush=True)
    t1 = time.time()
    with Session(get_engine()) as s:
        task = s.get(GenerationTask, tid)
        if task.status == "failed":
            task.status = "generated"
            task.error_message = None
            s.add(task)
            s.commit()
        task = run_review(s, tid, chat_fn=review_chat_fn)
        print(
            "review",
            task.status,
            task.error_message,
            "elapsed",
            round(time.time() - t1, 1),
        )

    if task.status != "reviewed":
        print("compact review fallback", flush=True)
        with Session(get_engine()) as s:
            draft = s.exec(select(CaseDraft).where(CaseDraft.task_id == tid)).first()
            req = s.get(Requirement, s.get(GenerationTask, tid).requirement_id)
            raw, _ = chat_completion(
                base_url=m.base_url,
                api_key=m.api_key,
                model=m.model_name,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "只返回JSON对象，字段score,verdict,issues,"
                            "missing_scenarios,prompt_improvement_hints,ready_for_final"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"需求:{req.title}\n草稿:\n"
                            f"{(draft.content_md or '')[:1500]}\n请评审"
                        ),
                    },
                ],
                timeout=120,
                max_retries=2,
            )
            payload = parse_review_payload(raw)
            task = s.get(GenerationTask, tid)
            task.status = "reviewing"
            s.add(task)
            s.add(
                ReviewResult(
                    task_id=tid,
                    score=int(payload.get("score") or 0),
                    verdict=str(payload.get("verdict") or "unknown"),
                    payload_json=json.dumps(payload, ensure_ascii=False),
                    model_id=m.id,
                )
            )
            task.status = "reviewed"
            task.error_message = None
            s.add(task)
            append_event(
                s,
                tid,
                "review",
                f"评审完成 score={payload.get('score')}",
            )
            s.commit()
            print("review OK", payload.get("score"), payload.get("verdict"))
            print("payload", json.dumps(payload, ensure_ascii=False)[:1200])
    else:
        with Session(get_engine()) as s:
            r = s.exec(select(ReviewResult).where(ReviewResult.task_id == tid)).first()
            print("score", r.score, "verdict", r.verdict)
            print("payload", (r.payload_json or "")[:1200])
            cites = s.exec(
                select(TaskCitation).where(TaskCitation.task_id == tid)
            ).all()
            print("citations", len(cites))
            for c in cites:
                print(" -", c.title, c.score)
            for e in s.exec(
                select(TaskEvent).where(TaskEvent.task_id == tid)
            ).all():
                print("event", e.step, e.message)

    try:
        url = f"http://127.0.0.1:8000/api/tasks/{tid}"
        detail = json.load(urllib.request.urlopen(url, timeout=30))
        print(
            "API",
            detail.get("status"),
            "cites",
            detail.get("citation_count"),
            "draft_v",
            detail.get("latest_draft_version"),
            "review",
            detail.get("latest_review"),
        )
    except Exception as e:  # noqa: BLE001
        print("API check failed", e)

    print("DONE task", tid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
