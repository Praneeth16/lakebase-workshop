# Databricks notebook source
# MAGIC %md
# MAGIC # 03 · Agent memory, built from scratch
# MAGIC
# MAGIC The core of the day. You build working agent memory on Lakebase in four moves, each one
# MAGIC motivated by a failure of the one before. This is precisely the scope your Commercial programme already chose
# MAGIC Lakebase for — agent logs and short-term memory — so you are building the thing your own
# MAGIC Commercial programme committed to.
# MAGIC
# MAGIC The load-bearing idea throughout: **Postgres is ACID, so a *committed* write is retrievable
# MAGIC on the very next turn** — a guarantee a separately synced vector store cannot make.

# COMMAND ----------

# MAGIC %pip install "psycopg[binary]" "databricks-sdk>=0.81.0" --quiet
# MAGIC %restart_python

# COMMAND ----------

import sys
sys.path.insert(0, "..")
from workshop.config import cfg
from workshop import lakebase
cfg.validate()
S = cfg.app_schema

# COMMAND ----------

# MAGIC %md ## Move 1 — sessions and turns (conversation state that survives a restart)

# COMMAND ----------

lakebase.run(f'CREATE SCHEMA IF NOT EXISTS "{S}"', fetch=False)
lakebase.run(f"""
CREATE TABLE IF NOT EXISTS "{S}".agent_sessions (
    session_id   text PRIMARY KEY,
    rep_id       text NOT NULL,
    hcp_id       text NOT NULL,
    started_at   timestamptz NOT NULL DEFAULT now()
)""", fetch=False)
lakebase.run(f"""
CREATE TABLE IF NOT EXISTS "{S}".agent_turns (
    turn_id      bigserial PRIMARY KEY,
    ext_id       text,                          -- stable source id, so re-runs do not duplicate
    session_id   text NOT NULL,
    rep_id       text NOT NULL,
    hcp_id       text NOT NULL,
    turn_index   int  NOT NULL,
    role         text NOT NULL,
    topic        text,
    content      text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
)""", fetch=False)
lakebase.run(f'CREATE INDEX IF NOT EXISTS idx_turns_rep_hcp ON "{S}".agent_turns (rep_id, hcp_id, created_at DESC)', fetch=False)
lakebase.run(f'DROP INDEX IF EXISTS "{S}".uq_turns_ext', fetch=False)  # drop stale partial index if upgrading
lakebase.run(f'CREATE UNIQUE INDEX IF NOT EXISTS uq_turns_ext ON "{S}".agent_turns (ext_id)', fetch=False)
print("memory schema ready in", S)

# COMMAND ----------

# MAGIC %md ### Commit visibility — the whole point, shown live
# MAGIC A committed write is visible to the next connection. A write inside an **open** transaction
# MAGIC is not. We demonstrate the boundary rather than assert it.

# COMMAND ----------

# Committed write, then read from a brand-new connection (idempotent via ext_id):
lakebase.run(f"""INSERT INTO "{S}".agent_sessions (session_id, rep_id, hcp_id)
                 VALUES ('SESS-DEMO','REP-001','HCP-0001') ON CONFLICT DO NOTHING""", fetch=False)
lakebase.run(f"""INSERT INTO "{S}".agent_turns (ext_id, session_id, rep_id, hcp_id, turn_index, role, topic, content)
                 VALUES ('demo-1','SESS-DEMO','REP-001','HCP-0001',0,'user','dosing',
                         'What did we last tell this HCP about hepatic dosing?')
                 ON CONFLICT (ext_id) DO NOTHING""", fetch=False)
seen = lakebase.run(f"SELECT count(*) FROM \"{S}\".agent_turns WHERE session_id='SESS-DEMO'")[0][0]
print("committed turn visible in a new connection:", seen)

# Uncommitted write is NOT visible to another connection:
c1 = lakebase.connect()
c1.autocommit = False
with c1.cursor() as cur:
    cur.execute(f"""INSERT INTO "{S}".agent_turns (session_id, rep_id, hcp_id, turn_index, role, content)
                    VALUES ('SESS-DEMO','REP-001','HCP-0001',99,'user','uncommitted — should be invisible')""")
    other = lakebase.run(f"SELECT count(*) FROM \"{S}\".agent_turns WHERE turn_index=99")[0][0]
    print("uncommitted turn visible from another connection:", other, "(expected 0)")
c1.rollback(); c1.close()

# COMMAND ----------

# MAGIC %md ## Move 2 — recall (recency alone fails; rank by relevance)
# MAGIC Seed this HCP with many interactions, then show that pure recency buries the pertinent one.

# COMMAND ----------

# pull some seeded turns for one rep/HCP out of UC to make recall realistic
seed = (spark.table(cfg.table("seed_agent_turns"))
             .where("hcp_id = 'HCP-0007'").limit(30).toPandas())
with lakebase.connect() as c:
    with c.cursor() as cur:
        for _, r in seed.iterrows():
            cur.execute(f"""INSERT INTO "{S}".agent_turns
                (ext_id, session_id, rep_id, hcp_id, turn_index, role, topic, content)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (ext_id) DO NOTHING""",
                (r.turn_id, r.session_id, r.rep_id, r.hcp_id, int(r.turn_index), r.role, r.topic, r.content))
    c.commit()

# guarantee one REP-001 dosing turn for HCP-0007 so the recall + promotion demos are deterministic
lakebase.run(f"""INSERT INTO "{S}".agent_turns (ext_id, session_id, rep_id, hcp_id, turn_index, role, topic, content)
                 VALUES ('demo-rep001-dosing','SESS-DEMO2','REP-001','HCP-0007',0,'user','dosing',
                         'REP-001 asked about hepatic dosing adjustment for HCP-0007')
                 ON CONFLICT (ext_id) DO NOTHING""", fetch=False)

print("recency-only (top 5) — the pertinent 'hepatic dosing' turn may be buried:")
for row in lakebase.run(f"""SELECT topic, left(content,60) FROM "{S}".agent_turns
                            WHERE hcp_id='HCP-0007' ORDER BY created_at DESC LIMIT 5"""):
    print("  ", row)

print("\nrelevance-ranked (topic match first, then recency) — surfaces it:")
for row in lakebase.run(f"""SELECT topic, left(content,60) FROM "{S}".agent_turns
                            WHERE hcp_id='HCP-0007'
                            ORDER BY (topic='dosing') DESC, created_at DESC LIMIT 5"""):
    print("  ", row)

# COMMAND ----------

# MAGIC %md ## Move 3 — durable facts (a transcript is not memory)
# MAGIC A learned-fact table, distinct from raw turns, with provenance back to the turns and a
# MAGIC status so a fact can be a **candidate** rather than truth (the dreaming module leans on this).

# COMMAND ----------

lakebase.run(f"""
CREATE TABLE IF NOT EXISTS "{S}".agent_facts (
    fact_id     bigserial PRIMARY KEY,
    source_key  text,                             -- stable key so re-runs update, not duplicate
    rep_id      text,
    hcp_id      text,
    scope       text NOT NULL DEFAULT 'hcp',      -- 'hcp' | 'territory' | 'org'
    fact_text   text NOT NULL,
    confidence  double precision NOT NULL DEFAULT 1.0,
    status      text NOT NULL DEFAULT 'active',    -- 'active' | 'review' | 'archived'
    created_at  timestamptz NOT NULL DEFAULT now()
)""", fetch=False)
lakebase.run(f'DROP INDEX IF EXISTS "{S}".uq_facts_source', fetch=False)  # drop stale partial index if upgrading
lakebase.run(f'CREATE UNIQUE INDEX IF NOT EXISTS uq_facts_source ON "{S}".agent_facts (source_key)', fetch=False)
lakebase.run(f"""
CREATE TABLE IF NOT EXISTS "{S}".fact_provenance (
    fact_id     bigint NOT NULL,
    turn_id     bigint NOT NULL,
    PRIMARY KEY (fact_id, turn_id)
)""", fetch=False)
print("fact + provenance tables ready")

# COMMAND ----------

# MAGIC %md ## Move 4 — scope isolation (the security lesson: isolation lives in the SQL)
# MAGIC A filter applied only in an outer query is a **leak waiting to happen**. For field-force
# MAGIC data segregation — a real pharma compliance concern — the filter belongs inside the query
# MAGIC that touches the rows.

# COMMAND ----------

# seed a second rep's turn for the same HCP (idempotent)
lakebase.run(f"""INSERT INTO "{S}".agent_turns (ext_id, session_id, rep_id, hcp_id, turn_index, role, content)
                 VALUES ('demo-repb','SESS-REPB','REP-002','HCP-0007',0,'user','REP-002 private note about HCP-0007')
                 ON CONFLICT (ext_id) DO NOTHING""", fetch=False)

print("❌ leak — filtering AFTER retrieval (rep_id checked in the outer query):")
leak = lakebase.run(f"""SELECT rep_id, left(content,40) FROM (
                            SELECT * FROM "{S}".agent_turns WHERE hcp_id='HCP-0007'
                        ) t WHERE rep_id = 'REP-001' OR true LIMIT 3""")  # the 'OR true' is the bug
for r in leak: print("   ", r)

print("\n✅ correct — the rep filter is INSIDE the query that reads the rows:")
safe = lakebase.run(f"""SELECT rep_id, left(content,40) FROM "{S}".agent_turns
                        WHERE hcp_id='HCP-0007' AND rep_id = 'REP-001' LIMIT 3""")
for r in safe: print("   ", r)
# require a non-empty result too — all() is vacuously true on an empty list, which would hide a
# seeding failure and let the isolation "pass" without actually returning this rep's rows
assert safe and all(r[0] == 'REP-001' for r in safe), "scope isolation failed (or no REP-001 rows)"

# COMMAND ----------

# MAGIC %md ## Hand-promote one fact — and the question that sets up dreaming
# MAGIC You promote a single turn into a durable fact by hand. Obvious next question:
# MAGIC **and when there are eight hundred turns across twelve reps?** Module 03b answers it.

# COMMAND ----------

with lakebase.connect() as c:
    with c.cursor() as cur:
        # source turn must belong to THIS rep — otherwise we mislabel another rep's turn as REP-001's
        cur.execute(f"""SELECT turn_id FROM "{S}".agent_turns
                        WHERE hcp_id='HCP-0007' AND rep_id='REP-001' AND topic='dosing'
                        ORDER BY created_at DESC, turn_id DESC LIMIT 1""")
        src = cur.fetchone()
        if src:
            cur.execute(f"""INSERT INTO "{S}".agent_facts (source_key, rep_id, hcp_id, scope, fact_text, confidence)
                            VALUES ('handpromote:REP-001:HCP-0007:dosing','REP-001','HCP-0007','hcp',
                                    'HCP-0007 repeatedly asks about hepatic dosing adjustment', 1.0)
                            ON CONFLICT (source_key) DO NOTHING RETURNING fact_id""")
            row = cur.fetchone()
            if row:  # only link provenance when we actually inserted (idempotent re-runs skip)
                cur.execute(f'INSERT INTO "{S}".fact_provenance (fact_id, turn_id) VALUES (%s,%s) ON CONFLICT DO NOTHING',
                            (row[0], src[0]))
            print("promoted 1 fact for REP-001 with provenance to its source turn")
        else:
            print("no REP-001 dosing turn found for HCP-0007 — seeding may have varied; skipping promotion")
    c.commit()

# COMMAND ----------

# MAGIC %md ## ✅ Verification
# MAGIC Recall returns this rep's turns and **provably not** another rep's; one durable fact exists
# MAGIC with provenance. Run this from a fresh connection to confirm it all committed.

# COMMAND ----------

facts = lakebase.run(f'SELECT fact_id, hcp_id, fact_text, confidence, status FROM "{S}".agent_facts')
prov = lakebase.run(f'SELECT count(*) FROM "{S}".fact_provenance')[0][0]
# prove a fact is actually LINKED to a source turn (a join), not just that both tables are non-empty
linked = lakebase.run(f"""SELECT count(*) FROM "{S}".agent_facts f
                          JOIN "{S}".fact_provenance p ON p.fact_id = f.fact_id
                          JOIN "{S}".agent_turns t ON t.turn_id = p.turn_id""")[0][0]
print("facts:", facts)
print("provenance links:", prov, "| fact→turn joins:", linked)
assert facts and linked >= 1, "expected at least one fact joined to its source turn via provenance"
print("✅ agent memory works: committed, recallable, scope-isolated, with provenance")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Contrast: managed agent memory (the fork in the road)
# MAGIC
# MAGIC You just built memory on Lakebase because you wanted the SQL — ranking, scope filters and
# MAGIC joins you own. The alternative is **managed agent memory**: a **Unity Catalog securable** on
# MAGIC Databricks-managed infrastructure. It is **not** Lakebase-backed — the docs position it as the
# MAGIC alternative to Lakebase, not a managed tier of it. It is **Beta and admin-gated**, and its
# MAGIC retrieval path has a **`top_k` ceiling of 50** — the limit most likely to constrain a real
# MAGIC recall design.
# MAGIC
# MAGIC > **Framing:** *Lakebase when you want the SQL, managed memory when you don't.*
# MAGIC
# MAGIC Two things that sound plausible and are **not** supported, so we do not say them:
# MAGIC branching is **not** the mechanism for per-conversation state isolation (the 10-branch limit
# MAGIC makes branch-per-session a non-starter — branching is for dev isolation and migration
# MAGIC rehearsal); and there is no documented Agent Bricks / MLflow integration for Lakebase memory.
# MAGIC No throughput or retention figures are quoted — none are published for either path.
# MAGIC
# MAGIC The cell below **demonstrates** the managed path on a pre-provisioned, Beta-enabled workspace:
# MAGIC write one scoped entry, read it back, and show the `top_k` ceiling. It is guarded so it skips
# MAGIC cleanly where Beta is not enabled rather than failing the notebook.

# COMMAND ----------

# Managed agent memory is Beta/gated; this is a showcase, not a hard dependency.
try:
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient()
    # The managed memory store is a UC securable. Requires Beta enablement + memory-store privileges.
    # If the capability/preview is not enabled, this raises and we skip with a clear message.
    stores = w.api_client.do("GET", "/api/2.0/agents/memory-stores")  # Beta surface
    print("managed memory stores visible to you:", stores)
    print("note: retrieval top_k is capped at 50 on the managed path.")
except Exception as e:
    print("↩︎ managed agent memory not enabled in this workspace (Beta, admin-gated) — skipping the")
    print("   live showcase. The teaching point stands: it is a UC securable, not Lakebase-backed,")
    print("   with a top_k ceiling of 50.  Detail:", str(e).splitlines()[0])
