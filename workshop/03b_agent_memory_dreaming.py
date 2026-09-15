# Databricks notebook source
# MAGIC %md
# MAGIC # 03b · Dreaming — offline memory consolidation
# MAGIC
# MAGIC Module 03 ended with you promoting **one** fact by hand. What happens at eight hundred turns
# MAGIC across twelve reps that nobody has time to curate? **Dreaming**: episodic turns distilled
# MAGIC into semantic facts on a schedule, while nobody is talking to the agent — with **provenance**
# MAGIC and a **review gate** that make it safe for a regulated field-medical context.
# MAGIC
# MAGIC Three passes, run as one job: **consolidate → abstract → decay/reconcile**. Every derived
# MAGIC fact is a **candidate**, not truth: it carries provenance to its source turns and a confidence
# MAGIC score, and anything below threshold lands in a **review queue** — invisible to the agent
# MAGIC until a human approves it.
# MAGIC
# MAGIC "Dreaming" is not a Databricks product name — it is the documented episodic→semantic
# MAGIC transition run on a schedule. Databricks calls *why it pays off* **memory scaling**
# MAGIC (accuracy improves with accumulated context; backed by Databricks research — cited, not quoted).

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
CONF_THRESHOLD = 0.6           # facts below this go to the review queue, not the agent
HIGH_STAKES = ("interaction_claim", "dosing", "safety")

# COMMAND ----------

# MAGIC %md ## Load the accumulated turns into Lakebase (idempotent)
# MAGIC The generator wrote ~800 seeded turns to Unity Catalog. Dreaming reads turns from the **same
# MAGIC Lakebase table the live agent uses**, so we ingest them once here. Re-running does not
# MAGIC duplicate — `ext_id` carries the source turn id with a unique constraint.

# COMMAND ----------

# extend the module-03 schema rather than replacing it (all idempotent; safe if 03 ran or not).
# Ensure the schema + agent_turns exist FIRST, so the ALTER below cannot fail when 03 hasn't run.
lakebase.run(f'CREATE SCHEMA IF NOT EXISTS "{S}"', fetch=False)
lakebase.run(f"""CREATE TABLE IF NOT EXISTS "{S}".agent_turns (
    turn_id bigserial PRIMARY KEY, ext_id text, session_id text NOT NULL, rep_id text NOT NULL,
    hcp_id text NOT NULL, turn_index int NOT NULL, role text NOT NULL, topic text,
    content text NOT NULL, created_at timestamptz NOT NULL DEFAULT now())""", fetch=False)
lakebase.run(f'ALTER TABLE "{S}".agent_turns ADD COLUMN IF NOT EXISTS ext_id text', fetch=False)
lakebase.run(f'DROP INDEX IF EXISTS "{S}".uq_turns_ext', fetch=False)  # drop stale partial index if upgrading
lakebase.run(f'CREATE UNIQUE INDEX IF NOT EXISTS uq_turns_ext ON "{S}".agent_turns (ext_id)', fetch=False)
# facts + provenance may already exist from module 03; ensure they do, with a stable source_key
lakebase.run(f"""CREATE TABLE IF NOT EXISTS "{S}".agent_facts (
    fact_id bigserial PRIMARY KEY, source_key text, rep_id text, hcp_id text,
    scope text NOT NULL DEFAULT 'hcp', fact_text text NOT NULL,
    confidence double precision NOT NULL DEFAULT 1.0, status text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now())""", fetch=False)
lakebase.run(f'ALTER TABLE "{S}".agent_facts ADD COLUMN IF NOT EXISTS source_key text', fetch=False)
lakebase.run(f'DROP INDEX IF EXISTS "{S}".uq_facts_source', fetch=False)  # drop stale partial index if upgrading
lakebase.run(f'CREATE UNIQUE INDEX IF NOT EXISTS uq_facts_source ON "{S}".agent_facts (source_key)', fetch=False)
lakebase.run(f'CREATE TABLE IF NOT EXISTS "{S}".fact_provenance (fact_id bigint NOT NULL, turn_id bigint NOT NULL, PRIMARY KEY (fact_id, turn_id))', fetch=False)
# small hcp dimension so abstraction can reason by specialty without a UC join
lakebase.run(f'CREATE TABLE IF NOT EXISTS "{S}".hcp_dim (hcp_id text PRIMARY KEY, specialty text)', fetch=False)

seed = spark.table(cfg.table("seed_agent_turns")).toPandas()
dim = spark.table(cfg.table("hcp_master")).select("hcp_id", "specialty").toPandas()

with lakebase.connect() as c:
    with c.cursor() as cur:
        cur.executemany(
            f'INSERT INTO "{S}".hcp_dim (hcp_id, specialty) VALUES (%s,%s) ON CONFLICT (hcp_id) DO NOTHING',
            list(dim.itertuples(index=False, name=None)))
        cur.executemany(f"""
            INSERT INTO "{S}".agent_turns (ext_id, session_id, rep_id, hcp_id, turn_index, role, topic, content, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (ext_id) DO NOTHING""",
            [(r.turn_id, r.session_id, r.rep_id, r.hcp_id, int(r.turn_index), r.role, r.topic, r.content, r.created_at)
             for r in seed.itertuples(index=False)])
    c.commit()

total = lakebase.run(f'SELECT count(*) FROM "{S}".agent_turns WHERE ext_id IS NOT NULL')[0][0]
print(f"ingested seeded turns into Lakebase: {total} (idempotent)")
assert total >= 700, "expected ~800 seeded turns — check the generator ran at lab scale"

# COMMAND ----------

# MAGIC %md ## Pass 1 — Consolidate
# MAGIC Group recent turns by HCP + topic, distil each cluster into a candidate semantic fact, and
# MAGIC write it with provenance back to every source turn. Confidence rises with the number of
# MAGIC supporting turns; a high-stakes topic with thin support is held for review.

# COMMAND ----------

# group WITHIN the authorization scope (rep + hcp), so one rep's private turns are never
# consolidated into another rep's fact — the isolation lesson from module 03 must survive dreaming
clusters = lakebase.run(f"""
    SELECT rep_id, hcp_id, topic, count(*) n, array_agg(turn_id) turn_ids,
           string_agg(left(content,160), ' | ') sample
    FROM "{S}".agent_turns
    WHERE topic IS NOT NULL AND ext_id IS NOT NULL
    GROUP BY rep_id, hcp_id, topic
    HAVING count(*) >= 3
    ORDER BY n DESC LIMIT 12""")


def distil(sample_text, topic):
    """Distil a cluster into one fact. Uses ai_query; falls back to a template if unavailable.
    Returns (fact_text, used_fallback). A fallback fact is a placeholder, not model output, so it
    must never be surfaced to the agent as active truth — the caller forces it to 'review'."""
    try:
        prompt = ("Summarise these field-medical conversation snippets into ONE durable, factual "
                  f"sentence about this HCP regarding '{topic}'. No preamble.\n\n" + sample_text)
        df = spark.sql(f"SELECT ai_query('{cfg.generation_endpoint}', :p) AS r", args={"p": prompt})
        return df.collect()[0].r.strip(), False
    except Exception:
        return f"Recurring '{topic}' discussion pattern observed for this HCP.", True


made = 0
for rep_id, hcp_id, topic, n, turn_ids, sample in clusters:
    fact, used_fallback = distil(sample, topic)
    # confidence rises with supporting turns; a high-stakes topic with thin support is held for review
    conf = min(1.0, 0.4 + 0.1 * n)
    if topic in HIGH_STAKES and n < 4:
        conf = min(conf, 0.5)
    status = "active" if conf >= CONF_THRESHOLD else "review"
    # a templated fallback (ai_query unavailable) is not real model output — never surface it as truth
    if used_fallback:
        status = "review"
    source_key = f"consolidate:{rep_id}:{hcp_id}:{topic}"
    with lakebase.connect() as c:
        with c.cursor() as cur:
            # idempotent: a re-run updates the fact in place rather than creating a duplicate
            cur.execute(f"""INSERT INTO "{S}".agent_facts (source_key, rep_id, hcp_id, scope, fact_text, confidence, status)
                            VALUES (%s,%s,%s,'hcp',%s,%s,%s)
                            ON CONFLICT (source_key) DO UPDATE
                              SET fact_text=EXCLUDED.fact_text, confidence=EXCLUDED.confidence, status=EXCLUDED.status
                            RETURNING fact_id""",
                        (source_key, rep_id, hcp_id, fact, round(conf, 3), status))
            fid = cur.fetchone()[0]
            cur.executemany(f'INSERT INTO "{S}".fact_provenance (fact_id, turn_id) VALUES (%s,%s) ON CONFLICT DO NOTHING',
                            [(fid, t) for t in turn_ids])
        c.commit()
    made += 1
print(f"consolidate: wrote/updated {made} rep-scoped candidate facts, each with provenance")

# COMMAND ----------

# MAGIC %md ## Pass 2 — Abstract (knowledge no single conversation contains)
# MAGIC Find patterns that span reps and HCPs. The planted cross-rep theme — a **cardiology segment
# MAGIC repeatedly raising reimbursement for CARDIOVYX** — surfaces here as an org-scoped fact,
# MAGIC because it is supported by many independent reps.

# COMMAND ----------

# A real cross-rep theme spans several reps AND several HCPs, and is not the generic small-talk
# topic — otherwise the five specialties' 'general' chatter outranks the planted signal.
abstractions = lakebase.run(f"""
    SELECT d.specialty, t.topic,
           count(*) n, count(DISTINCT t.rep_id) reps, count(DISTINCT t.hcp_id) hcps,
           array_agg(t.turn_id) turn_ids
    FROM "{S}".agent_turns t JOIN "{S}".hcp_dim d USING (hcp_id)
    WHERE t.ext_id IS NOT NULL AND t.topic IS NOT NULL AND t.topic <> 'general'
    GROUP BY d.specialty, t.topic
    HAVING count(DISTINCT t.rep_id) >= 3 AND count(DISTINCT t.hcp_id) >= 3
    ORDER BY reps DESC, n DESC LIMIT 5""")

for specialty, topic, n, reps, hcps, turn_ids in abstractions:
    fact = (f"Across the {specialty} segment, '{topic}' is raised repeatedly "
            f"({reps} reps, {hcps} HCPs, {n} turns) — an emerging cross-rep theme.")
    conf = min(1.0, 0.5 + 0.05 * reps)
    source_key = f"abstract:{specialty}:{topic}"
    with lakebase.connect() as c:
        with c.cursor() as cur:
            cur.execute(f"""INSERT INTO "{S}".agent_facts (source_key, rep_id, hcp_id, scope, fact_text, confidence, status)
                            VALUES (%s,NULL,NULL,'org',%s,%s,'active')
                            ON CONFLICT (source_key) DO UPDATE
                              SET fact_text=EXCLUDED.fact_text, confidence=EXCLUDED.confidence
                            RETURNING fact_id""",
                        (source_key, fact, round(conf, 3)))
            fid = cur.fetchone()[0]
            cur.executemany(f'INSERT INTO "{S}".fact_provenance (fact_id, turn_id) VALUES (%s,%s) ON CONFLICT DO NOTHING',
                            [(fid, t) for t in turn_ids])
        c.commit()
    print(f"  org fact: {fact}")
print("abstract: cross-rep themes written as org-scoped facts")

# COMMAND ----------

# MAGIC %md ## Pass 3 — Decay and reconcile
# MAGIC Archive stale facts; where two facts **contradict**, flag both rather than silently
# MAGIC overwriting — a memory system that quietly resolves contradictions quietly loses information.
# MAGIC The planted contradiction about HCP-0012's contact preference (email-only vs phone-only)
# MAGIC is detected and both sides are held for review.

# COMMAND ----------

# contradiction detection on the planted preference conflict (kept simple + deterministic)
conflict = lakebase.run(f"""
    SELECT turn_id, content FROM "{S}".agent_turns
    WHERE hcp_id='HCP-0012' AND topic='preference'""")
if len(conflict) >= 2:
    with lakebase.connect() as c:
        with c.cursor() as cur:
            for turn_id, content in conflict:
                cur.execute(f"""INSERT INTO "{S}".agent_facts (source_key, hcp_id, scope, fact_text, confidence, status)
                                VALUES (%s,'HCP-0012','hcp',%s,0.4,'review')
                                ON CONFLICT (source_key) DO NOTHING RETURNING fact_id""",
                            (f"contradiction:{turn_id}", f"[contradiction — needs review] {content}"))
                row = cur.fetchone()
                if row:  # keep the audit trail — each flagged fact traces to its source turn
                    cur.execute(f'INSERT INTO "{S}".fact_provenance (fact_id, turn_id) VALUES (%s,%s) ON CONFLICT DO NOTHING',
                                (row[0], turn_id))
        c.commit()
    print(f"reconcile: flagged {len(conflict)} contradictory contact-preference statements for review "
          "(both kept with provenance, neither overwrites the other)")

# decay: archive facts with no supporting turn in the last 150 days (none in synthetic data, shown as the mechanism)
archived = lakebase.run(f"""
    UPDATE "{S}".agent_facts f SET status='archived'
    WHERE status='active' AND scope='hcp'
      AND NOT EXISTS (SELECT 1 FROM "{S}".fact_provenance p JOIN "{S}".agent_turns t ON t.turn_id=p.turn_id
                      WHERE p.fact_id=f.fact_id AND t.created_at > now() - interval '150 days')
    """, fetch=False)
print(f"decay: archived {archived} stale facts (audit trail preserved)")

# COMMAND ----------

# MAGIC %md ## The review gate — the failure this prevents
# MAGIC The seeded **plausible-but-wrong clinical claim** (ONCOVYX + strong CYP3A4 inhibitor, full
# MAGIC dose, "no interaction") is a single, unverified, high-stakes statement. It must land in the
# MAGIC review queue — **never** in an MSL's context on LLM confidence alone.

# COMMAND ----------

claim_turns = lakebase.run(f"""SELECT turn_id, content FROM "{S}".agent_turns
                               WHERE topic='interaction_claim' AND content ILIKE '%CYP3A4%'""")
for turn_id, content in claim_turns:
    with lakebase.connect() as c:
        with c.cursor() as cur:
            cur.execute(f"""INSERT INTO "{S}".agent_facts (source_key, hcp_id, scope, fact_text, confidence, status)
                            VALUES (%s,NULL,'org',%s,0.35,'review')
                            ON CONFLICT (source_key) DO NOTHING RETURNING fact_id""",
                        (f"claim:{turn_id}", f"[UNVERIFIED clinical claim — do not surface] {content}"))
            row = cur.fetchone()
            if row:
                cur.execute(f'INSERT INTO "{S}".fact_provenance (fact_id, turn_id) VALUES (%s,%s) ON CONFLICT DO NOTHING',
                            (row[0], turn_id))
        c.commit()

# the planted claim (P4) must actually be present, or the gate would "pass" vacuously
assert claim_turns, "planted CYP3A4 claim (P4) not found — check the generator ran at lab scale"
in_review = lakebase.run(f"SELECT count(*) FROM \"{S}\".agent_facts WHERE status='review'")[0][0]
review_claim = lakebase.run(f"""SELECT count(*) FROM "{S}".agent_facts
                                WHERE status='review' AND fact_text ILIKE '%CYP3A4%'""")[0][0]
agent_can_see = lakebase.run(f"""SELECT count(*) FROM "{S}".agent_facts
                                 WHERE status='active' AND fact_text ILIKE '%CYP3A4%'""")[0][0]
print(f"facts in review queue: {in_review}  (CYP3A4 claim among them: {review_claim})")
print(f"wrong claim retrievable by the agent: {agent_can_see}  (must be 0)")
assert agent_can_see == 0, "review gate failed — an unverified claim reached the agent"
assert review_claim >= 1, "the CYP3A4 claim should be HELD in the review queue, not dropped silently"
print("✅ the review gate holds — claim captured for review, invisible to the agent")

# COMMAND ----------

# MAGIC %md ## ✅ The money shot — a question answerable only after the dream
# MAGIC Before dreaming the agent had only raw turns. After dreaming it has an org-scoped fact no
# MAGIC single conversation contained — and every fact traces back to the turns that produced it.

# COMMAND ----------

def agent_knows(query_like, rep_id=None, hcp_id=None):
    # the agent reads ACTIVE facts only, and an HCP-scoped fact only for its owning rep+HCP —
    # org-scoped themes are shared. Passing no rep/hcp returns org facts only (no leak).
    return lakebase.run(f"""SELECT fact_text, confidence, scope FROM "{S}".agent_facts
                            WHERE status='active' AND fact_text ILIKE %s
                              AND (scope='org' OR (scope='hcp' AND rep_id=%s AND hcp_id=%s))
                            ORDER BY confidence DESC LIMIT 3""",
                        [f"%{query_like}%", rep_id, hcp_id])

print("Q: what does the Cardiology segment keep raising about reimbursement?\n")
ans = agent_knows("Cardiology")
for fact, conf, scope in ans:
    print(f"  → [{scope}] ({conf}) {fact}")
print("\nprovenance for that fact (traces to source turns):")
prov = lakebase.run(f"""SELECT t.rep_id, t.hcp_id, left(t.content,60)
                        FROM "{S}".agent_facts f
                        JOIN "{S}".fact_provenance p ON p.fact_id=f.fact_id
                        JOIN "{S}".agent_turns t ON t.turn_id=p.turn_id
                        WHERE f.scope='org' AND f.fact_text ILIKE '%Cardiology%' LIMIT 5""")
for r in prov: print("   ", r)
# verify BOTH concepts, not just that some Cardiology fact came back
assert any("reimbursement" in f.lower() for f, _, _ in ans), \
    "abstraction did not surface the Cardiology reimbursement theme (planted pattern P1)"
print("\n✅ overnight, the copilot learned something no single conversation contained.")
