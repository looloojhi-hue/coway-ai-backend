# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

코웨이 전사 AI 챗봇 "코봇" — a FastAPI backend deployed on Google Cloud Run that serves an internal employee chatbot. It uses LangGraph for multi-agent orchestration and Gemini 3.5 Flash as the AI engine.

## Repository

- **GitHub**: https://github.com/looloojhi-hue/coway-ai-backend.git
- **Branch**: `master` (default push/pull target)

## Commands

```bash
# Local development
uvicorn main:app --host 0.0.0.0 --port 8080 --reload

# Docker build and run
docker build -t coway-ai-backend .
docker run -p 8080:8080 coway-ai-backend

# Test RAG search independently
python rag_node.py

# Trigger knowledge base sync pipeline manually
python embed_and_load.py

# Test document parser
python document_parser.py

# Install dependencies
pip install -r requirements.txt

# Deploy to Cloud Run
gcloud run deploy coway-cobot-fullstack --source . --region asia-northeast3
```

GCP authentication (required for local dev): `gcloud auth application-default login`

> **Note**: Ignore the `venv/` folder entirely — it is a local virtual environment and should not be read, edited, or referenced.

## Architecture

### Request Flow

1. `main.py` receives a POST `/api/chat` request
2. IAP header `X-Goog-Authenticated-User-Email` is extracted for the user identity; falls back to `looloojhi@coway.com` locally
3. `coway_agent_app.invoke()` (LangGraph) runs with the user's message
4. `main.py` post-processes the raw LLM text: strips `[CHART_DATA]{...}`, `[SOURCE_REPORTS][...]`, and `|||SUGGESTIONS|||` delimiters before returning a structured JSON payload

### LangGraph Agent Graph (`graph.py`)

The graph has a `Supervisor` node that classifies intent into one of 9 routes:

| Intent | Node | Description |
|---|---|---|
| `RAG` | `RAG_Search_Node` → `Reasoner` | Hybrid BQ vector search + Gemini answer generation |
| `BQ` | `BQ_Node` (→ `BQ_Corrector_Node` on failure, max 2 retries) | Gemini generates SQL, executes on BigQuery |
| `GENERAL` | `GENERAL_Node` | Simple conversational response |
| `EMAIL_WRITE/READ` | `EMAIL_*_Node` | Gmail drafts and inbox summaries via OAuth |
| `CALENDAR_WRITE/READ` | `CALENDAR_*_Node` | Google Calendar CRUD via OAuth |
| `TASK_WRITE/READ` | `TASK_*_Node` | Google Tasks CRUD via OAuth |

LangGraph state is persisted in Firestore via `langgraph_checkpoint_firestore.FirestoreSaver`.

### RAG Pipeline (`rag_node.py`)

Hybrid search against `hrga_rag_data.knowledge_master` in BigQuery:
- Vector search using `VECTOR_SEARCH()` with cosine distance (3072-dim `gemini-embedding-2` embeddings)
- ACL filtering: `allowed_groups = 'employee_all@coway.com' OR LIKE '%user_email%'`
- Keyword boost: +0.15 to hybrid score if core keyword appears in `content`
- Returns top-3 docs plus the `dept_code` of the highest-scoring document

### Knowledge Base Sync (`embed_and_load.py`)

Triggered via POST `/api/sync-knowledge` (scheduled at 2 AM):
- Recursively traverses Google Drive shared folder `TARGET_SHARED_FOLDER_ID`
- Infers `dept_code` from folder names ending in `팀` or `TF`
- Three processing paths: Spreadsheet (FAQ row-by-row) → PDF (parsed via Gemini) → Docs/PPT (text export)
- Incremental: skips files unchanged since last sync (compares `modifiedTime` vs BQ `last_modified`)
- Purges BQ rows for Drive files that no longer exist

### Document Parsing (`document_parser.py`)

- PDFs and images: passed directly to Gemini as bytes for markdown conversion
- Excel/XLSX: parsed with `openpyxl`, hyperlinks preserved as markdown links
- Team name extraction: inferred from the folder path (rightmost segment ending in `팀`)

## GCP Infrastructure

- **Project**: `gcp-cw-ai-chatbot`
- **Model**: `gemini-3.5-flash` (via `google-genai` SDK, enterprise client)
- **Model Armor**: `asia-northeast3` (Seoul) — sanitizes both user prompts and model responses
- **BigQuery datasets**:
  - `hrga_rag_data.knowledge_master` — vector knowledge base
  - `hrga_travel_data.travel_master_db` — employee travel records
  - `hrga_cost_data.{budget_master, budget_raw, execution_detail}` — cost/budget records
  - `chatbot_analytics.query_analytics_v2` — query logs (auto-created on first run)
  - `chatbot_analytics.feedback_logs` — satisfaction feedback (auto-created on first run)
- **Firestore collections**:
  - `coway_chat_sessions` — full conversation history (legacy path)
  - `user_history/{email}/sessions` — 6-slot rolling sidebar history (active path)
  - `user_tokens/{email}` — individual OAuth refresh tokens for Workspace APIs

## Key Conventions

**Structured output markers** in LLM responses (stripped by `extract_structured_payload` in `main.py`):
- `[CHART_DATA]{...}` — ApexCharts-compatible JSON for frontend rendering
- `[SOURCE_REPORTS][...]` — cited document list
- `|||SUGGESTIONS|||` — follow-up question suggestions (newline-separated after this delimiter)

**OAuth for Google Workspace** (`get_workspace_service` in `graph.py`): uses per-user refresh tokens stored in Firestore `user_tokens`. Raises `ValueError("AUTH_REQUIRED_FOR:{email}")` when no token exists — the frontend handles this to prompt OAuth consent.

**BQ SQL routing**: The Supervisor strictly separates travel queries (`hrga_travel_data`) from cost/budget queries (`hrga_cost_data`). When editing `bq_node`, maintain this separation — the prompt contains explicit routing rules.

**Embedding dimensions**: Always use `output_dimensionality=3072` with `gemini-embedding-2`. The BQ table schema and VECTOR_SEARCH are hard-coded to this dimension.

**API ↔ Frontend sync**: `templates/index.html` calls the FastAPI endpoints directly via `fetch`. Whenever an endpoint path, request body field, or response field in `main.py` is changed, check and update the corresponding `fetch` calls in `templates/index.html` to keep them in sync.
