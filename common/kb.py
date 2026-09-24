"""
Knowledge base storage + retrieval (RAG), shared by both agents.

Same three-layer design as the main project's app/rag/* + app/db/vector_store.py
— chunking, Voyage embeddings, pgvector similarity search — collapsed into
one file since this project has no separate db/ vs rag/ split. The one real
difference: the main project scopes KB docs to an integer agents.id (a row
in its own `agents` table, since agents there are DB-configured). Here,
agents are defined in code (tribe_app/, pfc/), so KB docs are scoped by a
plain text `agent_slug` ("tribe-app" / "pfc") instead — one less table,
same isolation guarantee: one agent's KB never leaks into another's.
"""

import psycopg
import voyageai
from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

from common.config import DATABASE_URL, EMBEDDING_DIM, EMBEDDING_MODEL, KB_RETRIEVAL_TOP_K, VOYAGE_API_KEY

CHUNK_SIZE_CHARS = 800
CHUNK_OVERLAP_CHARS = 150

_voyage_client = voyageai.Client(api_key=VOYAGE_API_KEY) if VOYAGE_API_KEY else None


# ---------------------------------------------------------
# Schema
# ---------------------------------------------------------

_schema_ready = False


def _ensure_ready() -> None:
    """Runs init_kb_store() exactly once per process, lazily, the first
    time any function in this module actually needs the tables.

    Why this exists: main.py calls init_kb_store() explicitly at startup,
    but `adk web` (ADK's own dev-UI entrypoint) imports tribe_app/agent.py
    and pfc/agent.py directly — it never runs main.py at all, so that
    explicit call never happens. Without this, every KB read/write fails
    with `UndefinedTable: relation "adk_kb_docs" does not exist` the moment
    you run `adk web` instead of `uvicorn main:app`. Calling this at the
    top of every public function below means the schema gets created
    automatically no matter which entrypoint is used, instead of relying
    on the caller to remember a manual setup step."""
    global _schema_ready
    if not _schema_ready:
        init_kb_store()
        _schema_ready = True


def init_kb_store() -> None:
    """Creates the `vector` extension and both KB tables if missing. Safe
    to call on every startup — every statement is IF NOT EXISTS. Uses a
    plain (unregistered) connection for the CREATE EXTENSION step, same
    reasoning as the main project: the `vector` type doesn't exist yet on
    a brand new database until that statement runs, so register_vector()
    would fail if called first.

    Table names are prefixed `adk_` — NOT cosmetic. This project shares
    one Neon database with the main (LangGraph) project on purpose (see
    the module docstring), but the two projects' KB schemas are
    incompatible: the main project's `app/db/vector_store.py` already
    owns a table literally named `kb_chunks`, with its `doc_id` foreign
    key pointing at ITS docs table (`knowledge_base_docs`), not this
    project's old `kb_docs`. Before this prefix existed, this project's
    own `CREATE TABLE IF NOT EXISTS kb_chunks` was a silent no-op against
    the main project's already-existing table — every insert here then
    failed with `violates foreign key constraint ... Key (doc_id)=(5) is
    not present in table "knowledge_base_docs"`, because doc_id 5 was a
    row in THIS project's kb_docs, not the main project's docs table.
    Unique table names side-step the collision entirely instead of
    trying to reconcile two different foreign-key schemas sharing one
    physical table."""
    conn = psycopg.connect(DATABASE_URL, autocommit=False)
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        # One-time migration: this project's OWN docs table used to be
        # named `kb_docs` (never shared with the main project — only
        # kb_chunks collided). Renaming preserves any docs already
        # uploaded (e.g. doc_id 5) instead of orphaning them. Safe to run
        # every startup: no-op once the rename has already happened.
        cur.execute("""
            SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'kb_docs')
               AND NOT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'adk_kb_docs')
        """)
        if cur.fetchone()[0]:
            cur.execute("ALTER TABLE kb_docs RENAME TO adk_kb_docs")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS adk_kb_docs (
                id SERIAL PRIMARY KEY,
                agent_slug TEXT NOT NULL,
                filename TEXT NOT NULL,
                content TEXT NOT NULL,
                active BOOLEAN NOT NULL DEFAULT TRUE,
                uploaded_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Deliberately NOT `kb_chunks` — see the docstring above. This is
        # a brand-new table the first time this migration runs, so any
        # rows that failed to insert under the old name are simply gone;
        # re-upload/re-index the affected doc(s) after deploying this.
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS adk_kb_chunks (
                id SERIAL PRIMARY KEY,
                doc_id INTEGER NOT NULL REFERENCES adk_kb_docs(id) ON DELETE CASCADE,
                agent_slug TEXT NOT NULL,
                chunk_text TEXT NOT NULL,
                embedding VECTOR({EMBEDDING_DIM})
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS usage_logs (
                id SERIAL PRIMARY KEY,
                agent_slug TEXT,
                session_id TEXT,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # No ivfflat/hnsw index yet — same reasoning as the main project:
        # at this KB scale a sequential scan is exact and effectively
        # instant, and an approximate index this early can silently hurt
        # recall. Add one back only once adk_kb_chunks is genuinely large
        # (tens of thousands of rows).
    conn.commit()
    conn.close()


def _get_vector_conn() -> psycopg.Connection:
    conn = psycopg.connect(DATABASE_URL, autocommit=False)
    register_vector(conn)
    return conn


# ---------------------------------------------------------
# Chunking (identical algorithm to app/rag/chunking.py)
# ---------------------------------------------------------

def chunk_text(text: str, chunk_size: int = CHUNK_SIZE_CHARS, overlap: int = CHUNK_OVERLAP_CHARS) -> list[str]:
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return []

    chunks: list[str] = []
    current = ""

    def flush():
        nonlocal current
        if current:
            chunks.append(current)
            current = current[-overlap:] if overlap < len(current) else current

    for para in paragraphs:
        if len(para) > chunk_size:
            flush()
            current = ""
            for i in range(0, len(para), chunk_size - overlap):
                piece = para[i:i + chunk_size]
                if piece:
                    chunks.append(piece)
            continue

        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= chunk_size:
            current = candidate
        else:
            flush()
            current = f"{current}\n\n{para}" if current else para

    if current:
        chunks.append(current)

    return chunks


# ---------------------------------------------------------
# Embeddings (Voyage AI — same model as the main project)
# ---------------------------------------------------------

def embed_documents(texts: list[str]) -> list[list[float]]:
    if not texts or _voyage_client is None:
        return []
    result = _voyage_client.embed(texts, model=EMBEDDING_MODEL, input_type="document")
    return result.embeddings


def embed_query(text: str) -> list[float] | None:
    if _voyage_client is None:
        return None
    result = _voyage_client.embed([text], model=EMBEDDING_MODEL, input_type="query")
    return result.embeddings[0]


# ---------------------------------------------------------
# KB docs (admin CRUD)
# ---------------------------------------------------------

def list_kb_docs(agent_slug: str) -> list[dict]:
    _ensure_ready()
    conn = psycopg.connect(DATABASE_URL)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, filename, active, uploaded_at FROM adk_kb_docs "
            "WHERE agent_slug = %s ORDER BY uploaded_at DESC",
            (agent_slug,),
        )
        rows = cur.fetchall()
    conn.close()
    return rows


def insert_kb_doc(agent_slug: str, filename: str, content: str) -> int:
    _ensure_ready()
    conn = psycopg.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO adk_kb_docs (agent_slug, filename, content, active) "
            "VALUES (%s, %s, %s, TRUE) RETURNING id",
            (agent_slug, filename, content),
        )
        new_id = cur.fetchone()[0]
    conn.commit()
    conn.close()
    return new_id


def toggle_kb_doc(doc_id: int) -> bool | None:
    _ensure_ready()
    conn = psycopg.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("SELECT active FROM adk_kb_docs WHERE id = %s", (doc_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return None
        new_state = not row[0]
        cur.execute("UPDATE adk_kb_docs SET active = %s WHERE id = %s", (new_state, doc_id))
    conn.commit()
    conn.close()
    return new_state


def delete_kb_doc(doc_id: int) -> None:
    _ensure_ready()
    conn = psycopg.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM adk_kb_docs WHERE id = %s", (doc_id,))
    conn.commit()
    conn.close()


def get_active_kb_text(agent_slug: str) -> str:
    """Full-dump fallback — used when retrieval hasn't been indexed yet,
    same graceful-degradation behavior as the main project."""
    _ensure_ready()
    conn = psycopg.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT content FROM adk_kb_docs WHERE agent_slug = %s AND active = TRUE",
            (agent_slug,),
        )
        rows = cur.fetchall()
    conn.close()
    return "\n\n---\n\n".join(r[0] for r in rows)


# ---------------------------------------------------------
# Chunks / vector search
# ---------------------------------------------------------

def index_kb_doc(doc_id: int, agent_slug: str, content: str) -> int:
    """Chunks, embeds, and stores a doc's content. Returns chunk count.
    If VOYAGE_API_KEY isn't set, embed_documents() returns [] and this is
    a silent no-op — the doc stays usable via get_active_kb_text()'s
    full-dump fallback instead."""
    _ensure_ready()
    chunks = chunk_text(content)
    if not chunks:
        return 0
    embeddings = embed_documents(chunks)
    if not embeddings:
        return 0

    conn = _get_vector_conn()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM adk_kb_chunks WHERE doc_id = %s", (doc_id,))
        for chunk_text_, embedding in zip(chunks, embeddings):
            cur.execute(
                "INSERT INTO adk_kb_chunks (doc_id, agent_slug, chunk_text, embedding) VALUES (%s, %s, %s, %s)",
                (doc_id, agent_slug, chunk_text_, Vector(embedding)),
            )
    conn.commit()
    conn.close()
    return len(chunks)


def has_any_chunks(agent_slug: str) -> bool:
    _ensure_ready()
    conn = psycopg.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("SELECT EXISTS (SELECT 1 FROM adk_kb_chunks WHERE agent_slug = %s LIMIT 1)", (agent_slug,))
        exists = cur.fetchone()[0]
    conn.close()
    return exists


def search_similar_chunks(agent_slug: str, query_embedding: list[float], top_k: int = KB_RETRIEVAL_TOP_K) -> list[str]:
    _ensure_ready()
    conn = _get_vector_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT adk_kb_chunks.chunk_text
            FROM adk_kb_chunks
            JOIN adk_kb_docs ON adk_kb_docs.id = adk_kb_chunks.doc_id
            WHERE adk_kb_docs.active = TRUE AND adk_kb_chunks.agent_slug = %s
            ORDER BY adk_kb_chunks.embedding <=> %s
            LIMIT %s
        """, (agent_slug, Vector(query_embedding), top_k))
        rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows]


def retrieve_relevant_kb_text(agent_slug: str, user_question: str) -> str | None:
    """Returns the most relevant KB chunks for a question, scoped to this
    agent's own docs, or None if this agent's KB hasn't been indexed yet
    (caller should fall back to get_active_kb_text())."""
    if not has_any_chunks(agent_slug):
        return None
    query_embedding = embed_query(user_question)
    if query_embedding is None:
        return None
    chunks = search_similar_chunks(agent_slug, query_embedding)
    if not chunks:
        return "(no matching knowledge base content found for this question)"
    return "\n\n---\n\n".join(chunks)


def get_kb_context(agent_slug: str, user_question: str | None) -> str:
    """What common/booking_gate.py calls each turn to fill the agent's
    {kb_context} instruction placeholder. RAG retrieval when possible,
    full-dump fallback otherwise — mirrors app/agent/prompts.py exactly."""
    if user_question:
        try:
            retrieved = retrieve_relevant_kb_text(agent_slug, user_question)
            if retrieved is not None:
                return retrieved
        except Exception:
            pass  # RAG is an enhancement, not a dependency — degrade, don't break
    text = get_active_kb_text(agent_slug)
    return text or "(no active knowledge base documents)"


# ---------------------------------------------------------
# Usage logging (same shape as the main project's log_usage)
# ---------------------------------------------------------

def log_usage(agent_slug: str, session_id: str, model: str, input_tokens: int, output_tokens: int) -> None:
    _ensure_ready()
    conn = psycopg.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO usage_logs (agent_slug, session_id, model, input_tokens, output_tokens) "
            "VALUES (%s, %s, %s, %s, %s)",
            (agent_slug, session_id, model, input_tokens, output_tokens),
        )
    conn.commit()
    conn.close()


def get_usage_summary(agent_slug: str | None = None) -> list[tuple]:
    """If agent_slug is given, restricts to that agent; otherwise
    summarizes across all agents. Same shape as the main project's
    get_usage_summary — routers/admin.py turns this into the
    per-model {requests, tokens, estimated_cost_usd} rows the dashboard's
    UsagePanel expects."""
    _ensure_ready()
    conn = psycopg.connect(DATABASE_URL)
    with conn.cursor() as cur:
        if agent_slug is not None:
            cur.execute("""
                SELECT model, COUNT(*) as requests, SUM(input_tokens) as total_input,
                       SUM(output_tokens) as total_output
                FROM usage_logs WHERE agent_slug = %s GROUP BY model
            """, (agent_slug,))
        else:
            cur.execute("""
                SELECT model, COUNT(*) as requests, SUM(input_tokens) as total_input,
                       SUM(output_tokens) as total_output
                FROM usage_logs GROUP BY model
            """)
        rows = cur.fetchall()
    conn.close()
    return rows