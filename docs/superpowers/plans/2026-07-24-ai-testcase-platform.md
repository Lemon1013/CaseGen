# CaseGen AI 测试用例平台 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付可演示的 MVP：上传文档 → 编译 LLM-Wiki → 按需求检索增强生成测试用例 → AI 评审 → 优化 Prompt 再生成 → 终版确认。

**Architecture:** 单仓 `backend/`（FastAPI + SQLite + 本地 Markdown Wiki）与 `frontend/`（Vue3 + Element Plus）。知识层自研轻量 LLM-Wiki（两步编译 + 关键词检索）；业务层任务状态机驱动生成闭环；所有 LLM 经 OpenAI 兼容 Gateway。

**Tech Stack:** Python 3.11+、FastAPI、SQLModel、httpx、pypdf、PyYAML、pytest；Vue 3、TypeScript、Vite、Element Plus、vue-router、pinia、markdown-it。

**Spec:** `docs/superpowers/specs/2026-07-24-ai-testcase-platform-design.md`

**Working directory:** 仓库根目录 `CaseGen/`（远程 `git@github.com:Lemon1013/CaseGen.git`）

---

## File map（实现前锁定）

```
CaseGen/
  backend/
    pyproject.toml                 # 或 requirements.txt + pytest.ini
    app/
      __init__.py
      main.py                      # FastAPI app, CORS, routers
      config.py                    # APP_DATA_DIR, timeouts, top_k
      db.py                        # engine, session, init_db
      models/
        __init__.py
        entities.py                # SQLModel tables
      schemas/
        __init__.py
        common.py
        documents.py
        wiki.py
        models_cfg.py
        prompts.py
        requirements.py
        tasks.py
      services/
        llm.py                     # LLM Gateway
        paths.py                   # data dir helpers
        parse_document.py          # md/txt/pdf → text
        retrieve.py                # keyword score retrieve
        wiki_ingest.py             # two-step compile
        wiki_index.py              # rewrite index.md
        prompts_seed.py            # default prompt templates
        task_pipeline.py           # generate/review/optimize/regenerate/finalize
        task_events.py             # append events
      api/
        documents.py
        wiki.py
        models_cfg.py
        prompts.py
        requirements.py
        tasks.py
      default_prompts/
        generate.md
        review.md
        optimize.md
        wiki_analyze.md
        wiki_write.md
    tests/
      conftest.py
      test_retrieve.py
      test_llm_gateway.py
      test_task_state.py
      test_documents_api.py
      test_prompts_api.py
      test_wiki_ingest_unit.py
      test_review_parse.py
  frontend/
    package.json
    vite.config.ts
    index.html
    src/
      main.ts
      App.vue
      router/index.ts
      api/client.ts
      api/*.ts
      layouts/MainLayout.vue
      views/
        WorkbenchView.vue
        TaskListView.vue
        TaskDetailView.vue
        DocumentsView.vue
        WikiView.vue
        PromptsView.vue
        ModelsView.vue
      components/
        MarkdownView.vue
        TaskTimeline.vue
        CitationList.vue
        ReviewCard.vue
  data/                            # runtime, gitignored
  docs/...
```

---

### Task 1: Backend scaffold + health check

**Files:**
- Create: `backend/requirements.txt`
- Create: `backend/app/__init__.py`
- Create: `backend/app/config.py`
- Create: `backend/app/main.py`
- Create: `backend/tests/conftest.py`
- Create: `backend/tests/test_health.py`

- [ ] **Step 1: Create requirements**

```text
fastapi>=0.115.0
uvicorn[standard]>=0.30.0
sqlmodel>=0.0.22
httpx>=0.27.0
pypdf>=4.0.0
python-multipart>=0.0.9
pyyaml>=6.0
pytest>=8.0.0
pytest-asyncio>=0.24.0
```

- [ ] **Step 2: Write config + app**

```python
# backend/app/config.py
from pathlib import Path
from pydantic_settings import BaseSettings  # if not wanted, use os.environ

# Prefer simple dataclass/os to avoid extra dep if pydantic_settings missing:
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("APP_DATA_DIR", ROOT / "data"))
RAW_DIR = DATA_DIR / "raw" / "sources"
WIKI_DIR = DATA_DIR / "wiki"
WIKI_PAGES_DIR = WIKI_DIR / "pages"
META_DIR = DATA_DIR / "meta"
DB_PATH = META_DIR / "app.db"
LLM_DEFAULT_TIMEOUT_SEC = int(os.getenv("LLM_DEFAULT_TIMEOUT_SEC", "120"))
RETRIEVE_TOP_K = int(os.getenv("RETRIEVE_TOP_K", "6"))
MAX_WIKI_CONTEXT_CHARS = int(os.getenv("MAX_WIKI_CONTEXT_CHARS", "12000"))
FINAL_SCORE_THRESHOLD = int(os.getenv("FINAL_SCORE_THRESHOLD", "80"))
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_EXTENSIONS = {".md", ".txt", ".pdf"}

def ensure_data_dirs() -> None:
    for p in (RAW_DIR, WIKI_PAGES_DIR, META_DIR):
        p.mkdir(parents=True, exist_ok=True)
    index = WIKI_DIR / "index.md"
    if not index.exists():
        index.write_text("# Wiki Index\n\n", encoding="utf-8")
```

```python
# backend/app/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import ensure_data_dirs

def create_app() -> FastAPI:
    ensure_data_dirs()
    app = FastAPI(title="CaseGen API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    return app

app = create_app()
```

- [ ] **Step 3: Write health test**

```python
# backend/tests/test_health.py
from fastapi.testclient import TestClient
from app.main import create_app

def test_health():
    client = TestClient(create_app())
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
```

- [ ] **Step 4: Install and run test**

Run (from `backend/`):

```bash
python -m venv .venv
source .venv/Scripts/activate  # Git Bash on Windows
pip install -r requirements.txt
pip install pydantic-settings  # only if used; otherwise skip
pytest tests/test_health.py -v
```

Expected: PASS

Note: `TestClient` needs `httpx`; if import path fails, set `PYTHONPATH=.` or install package editable.

- [ ] **Step 5: Commit**

```bash
git add backend/requirements.txt backend/app backend/tests
git commit -m "chore: scaffold FastAPI backend with health endpoint"
```

---

### Task 2: Database models + session

**Files:**
- Create: `backend/app/db.py`
- Create: `backend/app/models/__init__.py`
- Create: `backend/app/models/entities.py`
- Create: `backend/tests/test_db_init.py`

- [ ] **Step 1: Define SQLModel entities**

Implement tables matching spec §6:

- `ModelConfig` → table `models`  
  fields: `id: Optional[int] PK`, `name: str`, `base_url: str`, `api_key: str`, `model_name: str`, `is_default: bool = False`, `created_at`, `updated_at`
- `PromptTemplate` → `prompt_templates`  
  `id`, `name`, `type` (str), `content`, `version: int`, `is_active: bool`, timestamps
- `Document` → `documents`  
  `id`, `filename`, `stored_path`, `content_type`, `sha256`, `status`, `char_count`, `error_message` optional, timestamps
- `IngestJob` → `ingest_jobs`  
  `id`, `document_id`, `status`, `step_log_json` (str/JSON), `error_message` optional, timestamps
- `WikiPageRow` → `wiki_pages`  
  `id`, `path`, `title`, `page_type`, `source_document_id` optional, `tags_json`, timestamps
- `Requirement` → `requirements`  
  `id`, `title`, `description`, `focus_tags_json`, timestamps
- `GenerationTask` → `generation_tasks`  
  `id`, `requirement_id`, `status`, `model_id` optional, `review_model_id` optional, `prompt_template_id` optional, `temp_prompt_content` optional, `error_message` optional, timestamps
- `TaskCitation` → `task_citations`  
  `id`, `task_id`, `wiki_page_id` optional, `title`, `path`, `score: float`, `snippet`
- `CaseDraft` → `case_drafts`  
  `id`, `task_id`, `version: int`, `content_md`, `prompt_version_ref` optional, `created_at`
- `ReviewResult` → `review_results`  
  `id`, `task_id`, `draft_id`, `score: int`, `verdict: str`, `payload_json`, `created_at`
- `PromptRevision` → `prompt_revisions`  
  `id`, `task_id`, `base_prompt_id` optional, `new_content`, `status`, `created_at`
- `TaskEvent` → `task_events`  
  `id`, `task_id`, `step`, `message`, `detail_json` optional, `created_at`

Use `datetime.utcnow` defaults via `Field(default_factory=...)`.

- [ ] **Step 2: db session helpers**

```python
# backend/app/db.py
from sqlmodel import SQLModel, Session, create_engine
from app.config import DB_PATH, ensure_data_dirs

_engine = None

def get_engine():
    global _engine
    if _engine is None:
        ensure_data_dirs()
        _engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
    return _engine

def init_db() -> None:
    from app.models import entities  # noqa: F401
    SQLModel.metadata.create_all(get_engine())

def get_session():
    with Session(get_engine()) as session:
        yield session
```

Call `init_db()` inside `create_app()`.

- [ ] **Step 3: Test init**

```python
# backend/tests/test_db_init.py
import os
from pathlib import Path
import tempfile

def test_init_db(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    # re-import config/db after env set — or call ensure + init with overridden path
    from app import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "META_DIR", tmp_path / "meta")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "meta" / "app.db")
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw" / "sources")
    monkeypatch.setattr(config, "WIKI_DIR", tmp_path / "wiki")
    monkeypatch.setattr(config, "WIKI_PAGES_DIR", tmp_path / "wiki" / "pages")
    from app.db import init_db, get_engine
    import app.db as dbmod
    dbmod._engine = None
    init_db()
    assert (tmp_path / "meta" / "app.db").exists() or Path(get_engine().url.database).exists()
```

Prefer a cleaner `conftest.py` that sets `APP_DATA_DIR` to tmp and resets engine before each test module.

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_db_init.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/db.py backend/app/models backend/tests/test_db_init.py backend/tests/conftest.py
git commit -m "feat: add SQLModel entities and SQLite init"
```

---

### Task 3: LLM Gateway (mocked)

**Files:**
- Create: `backend/app/services/llm.py`
- Create: `backend/tests/test_llm_gateway.py`

- [ ] **Step 1: Write failing test**

```python
# backend/tests/test_llm_gateway.py
import httpx
import respx  # if not in requirements, use httpx MockTransport instead
import pytest
from app.services.llm import chat_completion, LLMError

@pytest.mark.anyio
async def test_chat_completion_success():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })
    transport = httpx.MockTransport(handler)
    content, usage = await chat_completion(
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        model="gpt-test",
        messages=[{"role": "user", "content": "hi"}],
        transport=transport,
    )
    assert content == "hello"
    assert usage["prompt_tokens"] == 1

@pytest.mark.anyio
async def test_chat_completion_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")
    transport = httpx.MockTransport(handler)
    with pytest.raises(LLMError):
        await chat_completion(
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            model="gpt-test",
            messages=[{"role": "user", "content": "hi"}],
            transport=transport,
        )
```

If async is heavy for Demo, implement **sync** `chat_completion` with `httpx.Client` and drop anyio.

**Preferred for Demo simplicity: sync API.**

```python
def test_chat_completion_success_sync():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })
    transport = httpx.MockTransport(handler)
    content, usage = chat_completion(
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        model="gpt-test",
        messages=[{"role": "user", "content": "hi"}],
        transport=transport,
    )
    assert content == "hello"
```

- [ ] **Step 2: Implement gateway**

```python
# backend/app/services/llm.py
from __future__ import annotations
from typing import Any
import httpx
from app.config import LLM_DEFAULT_TIMEOUT_SEC

class LLMError(Exception):
    pass

def chat_completion(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float = 0.2,
    timeout: float | None = None,
    transport: httpx.BaseTransport | None = None,
) -> tuple[str, dict[str, Any]]:
    root = base_url.rstrip("/")
    url = f"{root}/chat/completions" if not root.endswith("/chat/completions") else root
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    try:
        with httpx.Client(timeout=timeout or LLM_DEFAULT_TIMEOUT_SEC, transport=transport) as client:
            resp = client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as e:
        raise LLMError(f"LLM request failed: {e}") from e
    if resp.status_code >= 400:
        raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"Unexpected LLM response shape: {data!r}") from e
    if not content:
        raise LLMError("Empty LLM content")
    usage = data.get("usage") or {}
    return content, usage
```

- [ ] **Step 3: Run tests**

```bash
pytest tests/test_llm_gateway.py -v
```

Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/llm.py backend/tests/test_llm_gateway.py
git commit -m "feat: add OpenAI-compatible LLM gateway"
```

---

### Task 4: Keyword retrieve service

**Files:**
- Create: `backend/app/services/retrieve.py`
- Create: `backend/tests/test_retrieve.py`

- [ ] **Step 1: Failing test**

```python
# backend/tests/test_retrieve.py
from app.services.retrieve import score_text, rank_pages

def test_title_match_outranks_body_only():
    pages = [
        {"id": 1, "title": "账户余额规则", "page_type": "business", "path": "a.md", "content": "无关内容", "tags": []},
        {"id": 2, "title": "其他", "page_type": "business", "path": "b.md", "content": "账户余额规则在清算时校验", "tags": []},
    ]
    ranked = rank_pages("现货 余额 不足", pages, top_k=2)
    assert ranked[0]["id"] == 1
    assert ranked[0]["score"] > ranked[1]["score"]

def test_empty_query_returns_empty():
    assert rank_pages("", [{"id": 1, "title": "x", "page_type": "business", "path": "a.md", "content": "y", "tags": []}], top_k=5) == []
```

- [ ] **Step 2: Implement**

```python
# backend/app/services/retrieve.py
from __future__ import annotations
import re
from typing import Any

def _tokens(text: str) -> list[str]:
    text = text.lower()
    # CJK bigrams + ascii words
    parts = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", text)
    tokens: list[str] = []
    for p in parts:
        if re.fullmatch(r"[\u4e00-\u9fff]+", p):
            if len(p) == 1:
                tokens.append(p)
            else:
                tokens.extend(p[i : i + 2] for i in range(len(p) - 1))
        else:
            tokens.append(p)
    return tokens

def score_text(query: str, *, title: str, content: str, tags: list[str] | None = None) -> float:
    q_tokens = _tokens(query)
    if not q_tokens:
        return 0.0
    title_l = title.lower()
    content_l = content.lower()
    tags_l = " ".join(tags or []).lower()
    score = 0.0
    for t in q_tokens:
        if t in title_l:
            score += 10.0
        if t in tags_l:
            score += 4.0
        if t in content_l:
            score += 1.0
    return score

def rank_pages(query: str, pages: list[dict[str, Any]], top_k: int = 6, types: list[str] | None = None) -> list[dict[str, Any]]:
    if not query.strip():
        return []
    scored = []
    for p in pages:
        if types and p.get("page_type") not in types:
            continue
        s = score_text(query, title=p.get("title") or "", content=p.get("content") or "", tags=p.get("tags") or [])
        if s <= 0:
            continue
        item = dict(p)
        item["score"] = s
        snippet = (p.get("content") or "")[:200]
        item["snippet"] = snippet
        scored.append(item)
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]
```

Also add `load_all_wiki_pages()` that reads `wiki_pages` table + file contents from disk (used by API later).

- [ ] **Step 3: Run tests**

```bash
pytest tests/test_retrieve.py -v
```

Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/retrieve.py backend/tests/test_retrieve.py
git commit -m "feat: add keyword wiki retrieval ranking"
```

---

### Task 5: Document parse + upload API

**Files:**
- Create: `backend/app/services/paths.py`
- Create: `backend/app/services/parse_document.py`
- Create: `backend/app/schemas/documents.py`
- Create: `backend/app/api/documents.py`
- Modify: `backend/app/main.py` (include router)
- Create: `backend/tests/test_documents_api.py`

- [ ] **Step 1: Parser**

```python
# backend/app/services/parse_document.py
from pathlib import Path
from pypdf import PdfReader

def parse_file(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt"}:
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".pdf":
        reader = PdfReader(str(path))
        parts = []
        for page in reader.pages:
            parts.append(page.extract_text() or "")
        return "\n".join(parts).strip()
    raise ValueError(f"Unsupported extension: {suffix}")
```

- [ ] **Step 2: Upload API (sync)**

Endpoints:
- `POST /api/documents` multipart file → save raw, sha256, status=`parsed` if parse ok else `failed`
- `GET /api/documents`
- `GET /api/documents/{id}`

Mask nothing special; store relative `stored_path`.

- [ ] **Step 3: Test upload md**

```python
def test_upload_markdown(client, tmp_data_dir):
    files = {"file": ("rules.md", b"# 余额规则\n余额不足应拒绝下单", "text/markdown")}
    r = client.post("/api/documents", files=files)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("parsed", "uploaded", "ready")
    assert body["filename"] == "rules.md"
    listed = client.get("/api/documents").json()
    assert len(listed) >= 1
```

- [ ] **Step 4: Run tests + commit**

```bash
pytest tests/test_documents_api.py -v
git add backend/app/services/parse_document.py backend/app/api/documents.py backend/app/schemas backend/tests/test_documents_api.py backend/app/main.py
git commit -m "feat: document upload and text parsing"
```

---

### Task 6: Default prompts seed + Models/Prompts CRUD

**Files:**
- Create: `backend/app/default_prompts/*.md` (5 files)
- Create: `backend/app/services/prompts_seed.py`
- Create: `backend/app/api/models_cfg.py`
- Create: `backend/app/api/prompts.py`
- Create: `backend/app/schemas/models_cfg.py`
- Create: `backend/app/schemas/prompts.py`
- Create: `backend/tests/test_prompts_api.py`
- Create: `backend/tests/test_models_api.py`

- [ ] **Step 1: Default prompt files (Chinese, finance/exchange testing oriented)**

`generate.md` system rules: output required markdown skeleton; use cited wiki; steps executable; expected results observable.

`review.md`: return **only JSON** with keys score, verdict, issues, missing_scenarios, prompt_improvement_hints, ready_for_final.

`optimize.md`: rewrite generate prompt incorporating review hints; output full new prompt text only.

`wiki_analyze.md`: output JSON analysis schema from spec §3.4.

`wiki_write.md`: output one or more markdown pages with YAML frontmatter.

- [ ] **Step 2: Seed on startup**

If no active prompt for a type, insert version=1 active from file.

- [ ] **Step 3: APIs**

Models:
- CRUD `/api/models`
- `POST /api/models/{id}/ping` → minimal chat "ping" or models list; on failure 400 with message
- List responses **mask** `api_key` as `***` + last 4

Prompts:
- `GET /api/prompts?type=`
- `POST /api/prompts` creates new version; if `is_active`, deactivate siblings of same type
- `PUT /api/prompts/{id}` update content → optionally bump version policy: MVP create new row as new version

- [ ] **Step 4: Tests**

```python
def test_only_one_active_prompt_per_type(client):
    r1 = client.post("/api/prompts", json={"name": "g1", "type": "generate", "content": "A", "is_active": True})
    r2 = client.post("/api/prompts", json={"name": "g2", "type": "generate", "content": "B", "is_active": True})
    assert r2.status_code == 200
    items = client.get("/api/prompts", params={"type": "generate"}).json()
    actives = [p for p in items if p["is_active"]]
    assert len(actives) == 1
    assert actives[0]["content"] == "B"
```

- [ ] **Step 5: Commit**

```bash
git commit -m "feat: model and prompt management with default seeds"
```

---

### Task 7: Wiki ingest (two-step) + wiki list/retrieve API

**Files:**
- Create: `backend/app/services/wiki_ingest.py`
- Create: `backend/app/services/wiki_index.py`
- Create: `backend/app/api/wiki.py`
- Create: `backend/tests/test_wiki_ingest_unit.py`
- Create: `backend/tests/test_wiki_api.py`

- [ ] **Step 1: Unit-test pure helpers without LLM**

```python
# parse LLM write output into pages
def test_split_markdown_pages():
    raw = """---
title: T1
type: source_summary
sources: ["raw/sources/a.md"]
tags: ["余额"]
---
body1
---
title: T2
type: business
sources: ["raw/sources/a.md"]
tags: []
---
body2
"""
    pages = split_wiki_pages(raw)
    assert len(pages) == 2
    assert pages[0]["title"] == "T1"
```

Implement `split_wiki_pages` robustly (frontmatter blocks).

- [ ] **Step 2: ingest_document(session, document_id, llm_client_fn)**

Flow:
1. status → ingesting; create IngestJob
2. load text via parse_file
3. load active `wiki_analyze` prompt; call LLM → JSON
4. load active `wiki_write` prompt + analysis + index excerpt; call LLM
5. split pages; write files under `wiki/pages/{slug}-{id}.md`
6. upsert wiki_pages rows; rebuild index.md
7. document status ready; job success
8. on error: failed + error_message + events

For tests, inject fake LLM:

```python
def fake_llm(messages, **kwargs):
    if "分析" in messages[0]["content"] or "analyze" in messages[0]["content"].lower():
        return ('{"summary_title":"余额","key_rules":["余额不足拒单"],"api_points":[],"test_hints":["余额0下单"],"entities":["余额"],"suggested_page_types":["business"]}', {})
    return ("""---
title: 余额规则摘要
type: source_summary
sources: ["x"]
tags: ["余额"]
---
余额不足应拒绝下单。
""", {})
```

- [ ] **Step 3: API**

- `POST /api/documents/{id}/ingest`
- `GET /api/ingest-jobs/{id}`
- `GET /api/wiki/pages`
- `GET /api/wiki/pages/{id}`
- `GET /api/wiki/index`
- `POST /api/wiki/retrieve` body `{query, top_k?, types?}`

- [ ] **Step 4: Tests with fake LLM + commit**

```bash
pytest tests/test_wiki_ingest_unit.py tests/test_wiki_api.py -v
git commit -m "feat: LLM-Wiki ingest and retrieve APIs"
```

---

### Task 8: Task state machine helpers

**Files:**
- Create: `backend/app/services/task_state.py`
- Create: `backend/tests/test_task_state.py`

- [ ] **Step 1: Tests**

```python
from app.services.task_state import can_transition, InvalidTransition

def test_allowed_path():
    assert can_transition("draft", "retrieving")
    assert can_transition("generated", "reviewing")
    assert can_transition("reviewed", "finalized")

def test_disallow_skip():
    assert not can_transition("draft", "finalized")
```

- [ ] **Step 2: Implement transition map**

```python
ALLOWED = {
    "draft": {"retrieving", "failed"},
    "retrieving": {"generating", "failed"},
    "generating": {"generated", "failed"},
    "generated": {"reviewing", "regenerating", "finalized", "failed"},
    "reviewing": {"reviewed", "failed"},
    "reviewed": {"optimizing", "regenerating", "finalized", "failed"},
    "optimizing": {"reviewed", "failed"},  # after prompt revision ready, stay inspectable
    "regenerating": {"generating", "failed"},
    "finalized": set(),
    "failed": {"retrieving", "generating", "reviewing", "optimizing", "regenerating"},
}
```

Refine `optimizing` → after optimize completes set status `reviewed` again with revision pending (document in code comments).

- [ ] **Step 3: Commit**

```bash
git commit -m "feat: generation task state transitions"
```

---

### Task 9: Generation pipeline (retrieve + generate)

**Files:**
- Create: `backend/app/services/task_events.py`
- Create: `backend/app/services/task_pipeline.py`
- Create: `backend/app/api/requirements.py`
- Create: `backend/app/api/tasks.py`
- Create: `backend/app/schemas/requirements.py`
- Create: `backend/app/schemas/tasks.py`
- Create: `backend/tests/test_generate_pipeline.py`

- [ ] **Step 1: Events helper**

`append_event(session, task_id, step, message, detail=None)`

- [ ] **Step 2: `run_generate(session, task_id)`**

1. transition → retrieving  
2. build query from requirement  
3. rank wiki pages; save TaskCitation rows  
4. if no hits, event warning  
5. transition generating  
6. build messages: active/temp generate prompt + requirement + numbered wiki contents (truncate to MAX_WIKI_CONTEXT_CHARS)  
7. LLM call via selected ModelConfig  
8. save CaseDraft version = max+1  
9. status generated  
10. failures → failed + error_message  

- [ ] **Step 3: API**

- CRUD requirements (minimal: create/list/get)
- `POST /api/tasks` `{requirement_id or inline requirement, model_id?, prompt_template_id?, auto_review?: false}`
- `POST /api/tasks/{id}/generate`
- `GET /api/tasks`, `GET /api/tasks/{id}` (include latest draft summary, status)
- `GET /api/tasks/{id}/drafts`
- `GET /api/tasks/{id}/events`

- [ ] **Step 4: Test with fake llm + seeded wiki page on disk**

```python
def test_generate_creates_draft(client, monkeypatch):
    # upload+ingest with fake llm, create model pointing unused, monkeypatch chat_completion
    ...
    r = client.post("/api/tasks", json={"title": "余额不足下单", "description": "现货限价单余额不足应失败", "model_id": mid})
    tid = r.json()["id"]
    g = client.post(f"/api/tasks/{tid}/generate")
    assert g.status_code == 200
    detail = client.get(f"/api/tasks/{tid}").json()
    assert detail["status"] == "generated"
    drafts = client.get(f"/api/tasks/{tid}/drafts").json()
    assert len(drafts) == 1
    assert "用例" in drafts[0]["content_md"] or len(drafts[0]["content_md"]) > 0
```

- [ ] **Step 5: Commit**

```bash
git commit -m "feat: task generate pipeline with wiki citations"
```

---

### Task 10: Review + optimize prompt + regenerate + finalize

**Files:**
- Modify: `backend/app/services/task_pipeline.py`
- Create: `backend/app/services/review_parse.py`
- Create: `backend/tests/test_review_parse.py`
- Create: `backend/tests/test_review_pipeline.py`

- [ ] **Step 1: review JSON parse**

```python
def parse_review_payload(text: str) -> dict:
    # try json.loads whole text
    # else extract ```json ... ```
    # else return {"score": 0, "verdict": "unknown", "issues": [], "missing_scenarios": [], "prompt_improvement_hints": [], "ready_for_final": False, "raw": text}
```

Tests for pure JSON, fenced JSON, garbage fallback.

- [ ] **Step 2: run_review**

Uses latest draft + requirement + citations; active review prompt; save ReviewResult; status reviewed.

- [ ] **Step 3: run_optimize_prompt**

Uses review payload + current generate prompt; creates PromptRevision status=pending; status back to reviewed (or optimizing→reviewed).

- [ ] **Step 4: apply_prompt**

Body: `{mode: "global"|"task_temp", revision_id}`  
- global: new PromptTemplate version active for generate  
- task_temp: set task.temp_prompt_content  

- [ ] **Step 5: regenerate / finalize**

- regenerate → new draft version  
- finalize → status finalized (require draft exists)

- [ ] **Step 6: API routes**

```
POST /api/tasks/{id}/review
POST /api/tasks/{id}/optimize-prompt
POST /api/tasks/{id}/apply-prompt
POST /api/tasks/{id}/regenerate
POST /api/tasks/{id}/finalize
```

- [ ] **Step 7: Tests + commit**

```bash
pytest tests/test_review_parse.py tests/test_review_pipeline.py -v
git commit -m "feat: review, prompt optimize, regenerate, finalize"
```

---

### Task 11: Frontend scaffold + API client

**Files:**
- Create: `frontend/` Vite Vue-TS app
- Create: `frontend/src/api/client.ts`
- Create: `frontend/src/router/index.ts`
- Create: `frontend/src/layouts/MainLayout.vue`

- [ ] **Step 1: Scaffold**

```bash
cd frontend
npm create vite@latest . -- --template vue-ts
npm install element-plus vue-router pinia markdown-it
npm install -D @types/markdown-it
```

- [ ] **Step 2: Vite proxy**

```ts
// vite.config.ts
server: {
  port: 5173,
  proxy: { "/api": "http://127.0.0.1:8000" }
}
```

- [ ] **Step 3: client.ts**

```ts
export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}
```

For multipart uploads, use raw `fetch` without JSON content-type.

- [ ] **Step 4: Main layout with sidebar routes** matching spec §5.2

- [ ] **Step 5: Commit**

```bash
git add frontend
git commit -m "chore: scaffold Vue3 frontend with Element Plus"
```

---

### Task 12: Frontend — Models & Prompts pages

**Files:**
- Create: `frontend/src/views/ModelsView.vue`
- Create: `frontend/src/views/PromptsView.vue`
- Create: `frontend/src/api/models.ts`
- Create: `frontend/src/api/prompts.ts`

- [ ] **Step 1: ModelsView** — table + dialog form (name, base_url, api_key, model_name, is_default) + 测试连接 button calling ping

- [ ] **Step 2: PromptsView** — filter by type; editor textarea; save; set active

- [ ] **Step 3: Manual check**

```bash
# terminal 1
cd backend && uvicorn app.main:app --reload --port 8000
# terminal 2
cd frontend && npm run dev
```

Open `/models`, `/prompts`.

- [ ] **Step 4: Commit**

```bash
git commit -m "feat(ui): models and prompts management pages"
```

---

### Task 13: Frontend — Documents & Wiki

**Files:**
- Create: `frontend/src/views/DocumentsView.vue`
- Create: `frontend/src/views/WikiView.vue`
- Create: `frontend/src/components/MarkdownView.vue`

- [ ] **Step 1: MarkdownView** — props `content: string`, render via markdown-it

- [ ] **Step 2: DocumentsView** — el-upload; table status; button 编译; show job error

- [ ] **Step 3: WikiView** — search input; list pages; click shows MarkdownView

- [ ] **Step 4: Commit**

```bash
git commit -m "feat(ui): documents upload and wiki browser"
```

---

### Task 14: Frontend — Workbench, Task list, Task detail

**Files:**
- Create: `frontend/src/views/WorkbenchView.vue`
- Create: `frontend/src/views/TaskListView.vue`
- Create: `frontend/src/views/TaskDetailView.vue`
- Create: `frontend/src/components/TaskTimeline.vue`
- Create: `frontend/src/components/CitationList.vue`
- Create: `frontend/src/components/ReviewCard.vue`
- Create: `frontend/src/api/tasks.ts`

- [ ] **Step 1: Workbench** — form title/description/focus tags/model/prompt/auto_review → create task → generate → router push detail

- [ ] **Step 2: TaskDetail** — poll every 2s while status in progress set; show timeline from events; drafts tabs; citations; review card; action buttons by status:

| status | actions |
|--------|---------|
| generated | 评审, 终版 |
| reviewed | 优化Prompt, 再生成, 终版 |
| has pending revision | 查看并应用 |
| failed | 重试当前步 |
| finalized | 只读 |

- [ ] **Step 3: Prompt apply UX** — dialog show new_content; choose 全局启用 / 仅本任务; then optional regenerate

- [ ] **Step 4: Empty retrieval warning banner when citations length 0 after generate

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(ui): workbench and task closed-loop views"
```

---

### Task 15: Root README runbook + demo acceptance

**Files:**
- Modify: `README.md`
- Create: `backend/.env.example` (no secrets)
- Create: `scripts/dev.ps1` or `scripts/dev.sh` optional

- [ ] **Step 1: README sections**

- 快速启动 backend/frontend  
- 配置第一个模型  
- 演示路径（验收清单 from spec §8.2）  
- 目录结构  

- [ ] **Step 2: Run full backend tests**

```bash
cd backend && pytest -v
```

Expected: all PASS

- [ ] **Step 3: Manual demo path**

1. 配置模型  
2. 上传 `fixtures/sample_balance_rules.md`（create a small fixture file in repo）  
3. 编译 Wiki  
4. 工作台生成「现货限价单余额不足」  
5. 评审 → 优化 Prompt → 再生成 → 终版  

- [ ] **Step 4: Commit + push**

```bash
git add README.md backend/.env.example fixtures
git commit -m "docs: runbook and demo fixtures"
git push origin main
```

---

## Spec coverage checklist

| Spec area | Tasks |
|-----------|-------|
| LLM-Wiki upload/parse/compile/index | 5, 7 |
| Lightweight retrieve | 4, 7, 9 |
| Multi-model OpenAI compatible | 3, 6 |
| Prompt management + defaults | 6 |
| Generate with citations | 9 |
| AI review JSON + fallback | 10 |
| Prompt optimize / apply / regenerate | 10 |
| Finalize | 10 |
| Process events timeline | 9, 14 |
| Markdown UI | 13, 14 |
| Pages: workbench/tasks/docs/wiki/prompts/models | 12–14 |
| SQLite + data/ layout | 1, 2 |
| Error handling / mask api_key | 3, 6, 9–10 |
| No login / no Docker wiki middleware | respected (out of scope) |
| Score≥80 final highlight | 14 (frontend) + config threshold |

## Notes for implementers

- Prefer **sync** FastAPI routes + sync httpx for MVP simplicity.  
- All LLM calls must be mockable in tests (inject/monkeypatch `chat_completion`).  
- Never commit real API keys; `data/` gitignored.  
- Keep files small; do not put pipeline + API + models in one module.  
- Chinese UI labels; code identifiers English.

---

## Plan self-review

1. **Spec coverage:** MVP closed loop mapped to tasks 1–15; deferred items not scheduled.  
2. **Placeholders:** removed TBDs; concrete endpoints and core code included.  
3. **Types:** status strings and prompt types consistent with spec (`generate|review|optimize|wiki_analyze|wiki_write`).  
4. **Single plan scope:** one vertical MVP; acceptable as single plan producing demoable software.
