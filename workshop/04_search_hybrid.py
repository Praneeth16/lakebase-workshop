# Databricks notebook source
# MAGIC %md
# MAGIC # 04 · Lakebase Search — hybrid retrieval (DEMO)
# MAGIC
# MAGIC In-database vector **and** keyword search over the copilot's own content — answering *what
# MAGIC does our content say about this question?* Runs on the **dedicated, pre-enabled Search demo
# MAGIC project**, never a team project.
# MAGIC
# MAGIC > **One-way door, stated out loud.** Enabling Lakebase Search restarts all of a project's
# MAGIC > compute, drops active connections, and **cannot be reversed**. Never enable it on a shared
# MAGIC > lab project. This notebook connects to the demo project that was enabled ahead of time.
# MAGIC
# MAGIC **Status:** Beta (announced 16 June 2026), gated behind an account-team request plus a
# MAGIC workspace-admin Previews toggle. No latency or recall figures are quoted — none are
# MAGIC published, and recall is tunable. For a regulated production workload this quarter, Search is
# MAGIC the direction worth prototyping; **AI Search (GA)** is the production choice today.

# COMMAND ----------

# MAGIC %pip install "psycopg[binary]" "databricks-sdk>=0.81.0" --quiet
# MAGIC %restart_python

# COMMAND ----------

import sys
sys.path.insert(0, "..")
from workshop.config import cfg
from workshop import lakebase
cfg.validate(required={"search"})
SS = "search_demo"   # schema on the Search demo project

# COMMAND ----------

# MAGIC %md ## Enable-trap gate — clear this before anything else
# MAGIC Field-reported: `CREATE EXTENSION lakebase_vector` can fail unless the extensions are in the
# MAGIC project's `shared_preload_libraries`, which the Previews toggle does not always set. Public
# MAGIC docs do not mention it. If this cell fails, check `shared_preload_libraries` and escalate on
# MAGIC the Lakebase Search channel — **do not debug it live in the room.**

# COMMAND ----------

conn = lakebase.connect_search()
conn.autocommit = True
ok = True
with conn.cursor() as cur:
    cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{SS}"')
    for ext in ("lakebase_vector", "lakebase_text"):
        try:
            cur.execute(f"CREATE EXTENSION IF NOT EXISTS {ext} CASCADE")
            print(f"✅ extension {ext}")
        except Exception as e:
            ok = False
            print(f"❌ CREATE EXTENSION {ext} failed: {str(e).splitlines()[0]}")
            print("   → check shared_preload_libraries on this project; escalate before continuing.")
if not ok:
    conn.close()
    raise RuntimeError(
        "Lakebase Search extensions are unavailable on this project. Check shared_preload_libraries "
        "and escalate on the Lakebase Search channel before continuing — do NOT proceed into the "
        "CREATE TABLE / index steps, which would throw a less actionable error.")

# COMMAND ----------

# MAGIC %md ## Load content into a table we own, then embed in place
# MAGIC The searchable row must be the served row. Content arrives as **text**; we embed the chunk
# MAGIC with a Databricks embedding endpoint and write the vector into the **same row**. We load into
# MAGIC a **normal table we own here**, not a synced table: a synced table mirrors a UC source (UC is
# MAGIC the source of truth, and edits get reconciled away on the next sync), so the search corpus —
# MAGIC which we embed and mutate in place — belongs in its own table. (Note: on current Autoscaling
# MAGIC Lakebase a synced table would not *reject* the write, but it's still the wrong place for it.)

# COMMAND ----------

content = spark.table(cfg.table("medical_content")).toPandas()

with conn.cursor() as cur:
    cur.execute(f"""CREATE TABLE IF NOT EXISTS "{SS}".medical_content (
        content_id text PRIMARY KEY, brand text, doc_type text, section text,
        chunk_text text, embedding vector(1024))""")
    cur.execute(f'TRUNCATE "{SS}".medical_content')
    for r in content.itertuples(index=False):
        cur.execute(f"""INSERT INTO "{SS}".medical_content (content_id, brand, doc_type, section, chunk_text)
                        VALUES (%s,%s,%s,%s,%s)""",
                    (r.content_id, r.brand, r.doc_type, r.section, r.chunk_text))
print("loaded", len(content), "content rows (text only, not yet embedded)")

# COMMAND ----------

from databricks.sdk import WorkspaceClient
w = WorkspaceClient()

def embed(texts):
    resp = w.serving_endpoints.query(name=cfg.embedding_endpoint, input=list(texts))
    d = resp.as_dict()
    # OpenAI-compatible: {"data":[{"embedding":[...]}]}; fallback: {"predictions":[[...]]}
    if "data" in d:
        return [row["embedding"] for row in d["data"]]
    return d["predictions"]

sample_vec = embed([content.iloc[0].chunk_text])[0]
assert len(sample_vec) == 1024, f"embedding dim {len(sample_vec)} != vector(1024) — fix the column type or the model"
print("embedding dimension asserted: 1024 ✓")

with conn.cursor() as cur:
    for r in content.itertuples(index=False):
        vec = embed([r.chunk_text])[0]
        cur.execute(f'UPDATE "{SS}".medical_content SET embedding = %s WHERE content_id = %s',
                    ("[" + ",".join(map(str, vec)) + "]", r.content_id))
print("embeddings written into the same rows")

# COMMAND ----------

# MAGIC %md ## Build indexes AFTER rows exist
# MAGIC A BM25 / vector index build expects data to exist. On a Search-enabled project these are the
# MAGIC managed accelerators (`lakebase_ann`, `lakebase_bm25`); the query below works with pgvector +
# MAGIC full-text and gets faster with the indexes in place.

# COMMAND ----------

with conn.cursor() as cur:
    # vector index (HNSW via pgvector; lakebase_ann is the managed equivalent on enabled projects)
    cur.execute(f'CREATE INDEX IF NOT EXISTS idx_mc_vec ON "{SS}".medical_content USING hnsw (embedding vector_cosine_ops)')
    # keyword index (full-text; lakebase_bm25 is the managed equivalent)
    cur.execute(f'CREATE INDEX IF NOT EXISTS idx_mc_fts ON "{SS}".medical_content USING gin (to_tsvector(\'english\', chunk_text))')
print("indexes created")

# COMMAND ----------

# MAGIC %md ## The narrative: vector alone misses identifiers; keyword alone misses paraphrase
# MAGIC Hybrid needs both, fused with **Reciprocal Rank Fusion** — a SQL pattern you write and own.
# MAGIC Because it is plain SQL over your own rows, a tenant filter (brand / doc_type) composes into
# MAGIC the same query — the thing a separate vector store cannot do.

# COMMAND ----------

def search(query_text, brand_filter=None, k=5):
    qvec = "[" + ",".join(map(str, embed([query_text])[0])) + "]"
    tenant = "AND brand = %(brand)s" if brand_filter else ""
    sql = f"""
    WITH v AS (  -- vector side, tenant filter INSIDE
        SELECT content_id, row_number() OVER (ORDER BY embedding <=> %(qvec)s) AS r
        FROM "{SS}".medical_content
        WHERE embedding IS NOT NULL {tenant}
        ORDER BY embedding <=> %(qvec)s LIMIT 20),
    t AS (       -- keyword side, tenant filter INSIDE; ORDER BY before LIMIT so we keep the top 20
        SELECT content_id, row_number() OVER (
                 ORDER BY ts_rank(to_tsvector('english', chunk_text),
                                  websearch_to_tsquery('english', %(q)s)) DESC) AS r
        FROM "{SS}".medical_content
        WHERE to_tsvector('english', chunk_text) @@ websearch_to_tsquery('english', %(q)s) {tenant}
        ORDER BY ts_rank(to_tsvector('english', chunk_text),
                         websearch_to_tsquery('english', %(q)s)) DESC
        LIMIT 20)
    SELECT mc.doc_type, left(mc.chunk_text,70) AS snippet,
           COALESCE(1.0/(60+v.r),0) + COALESCE(1.0/(60+t.r),0) AS rrf
    FROM "{SS}".medical_content mc
    LEFT JOIN v USING (content_id) LEFT JOIN t USING (content_id)
    WHERE v.r IS NOT NULL OR t.r IS NOT NULL
    ORDER BY rrf DESC LIMIT %(k)s"""
    params = {"qvec": qvec, "q": query_text, "k": k}
    if brand_filter:
        params["brand"] = brand_filter
    return lakebase.run(sql, params, conn=conn)

print("Hybrid search: 'how should CARDIOVYX be stored?' (brand-filtered)")
_hits = search("how should the medicine be stored", brand_filter="CARDIOVYX")
for row in _hits:
    print("  ", row)
assert _hits, ("hybrid search returned no rows for the brand-filtered query — check the corpus "
               "loaded and the Search extensions are enabled on this project")

# COMMAND ----------

# MAGIC %md ## EXPLAIN — confirm the index is used
# MAGIC A sequential scan still returns *correct* results; only latency reveals the mistake, so we
# MAGIC check the plan rather than trusting the output.

# COMMAND ----------

qvec = "[" + ",".join(map(str, embed(["storage conditions"])[0])) + "]"
plan = lakebase.run(f'EXPLAIN SELECT content_id FROM "{SS}".medical_content ORDER BY embedding <=> %s LIMIT 5',
                    [qvec], conn=conn)
print("\n".join(r[0] for r in plan))

# COMMAND ----------

# MAGIC %md ## ✅ Verification & decision framework
# MAGIC The demo ran end to end on the dedicated Search project; participants leave with this SQL.
# MAGIC
# MAGIC **When to use which** (our reasoning, not a Databricks-published tree):
# MAGIC - **AI Search (GA)** — manages ingestion, embedding and reranking for large, mostly-static corpora.
# MAGIC - **Lakebase Search (Beta)** — searches live operational rows in place, immediate freshness, composable SQL filters.
# MAGIC - Many agents use both. For a regulated AZ workload **this quarter**, Search is the direction to prototype, not the production answer — and saying so is what makes the rest credible.

# COMMAND ----------

conn.close()
print("✅ search demo complete")
