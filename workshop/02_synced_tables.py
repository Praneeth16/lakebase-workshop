# Databricks notebook source
# MAGIC %md
# MAGIC # 02 · Synced tables — Unity Catalog → Lakebase
# MAGIC
# MAGIC Mirror governed Unity Catalog tables into Postgres so the copilot can read *what is true
# MAGIC right now* about an HCP with a single low-latency query. This answers the **first** of the
# MAGIC four retrieval problems.
# MAGIC
# MAGIC Creating a synced table is a **control-plane operation** done from the **Catalog UI** (verified
# MAGIC in testing on 2026-09-15). In the room these are pre-provisioned (or the facilitator creates them
# MAGIC once from the front); you then **verify and explore** them browser-native below. See
# MAGIC `docs/RUNBOOK.md` for the exact create flow for your own replay.

# COMMAND ----------

# MAGIC %pip install "psycopg[binary]" "databricks-sdk>=0.81.0" --quiet
# MAGIC %restart_python

# COMMAND ----------

import sys
sys.path.insert(0, "..")
from workshop.config import cfg
from workshop import lakebase
cfg.validate()

# COMMAND ----------

# MAGIC %md ## Create the copilot's application schema (you own it)
# MAGIC The schema is created under **your** identity, so you own it and can create/drop tables in
# MAGIC it. (The validation notebook's DDL canary already proved this works for you.)

# COMMAND ----------

lakebase.run(f'CREATE SCHEMA IF NOT EXISTS "{cfg.app_schema}"', fetch=False)
print("app schema ready:", cfg.app_schema)

# COMMAND ----------

# MAGIC %md ## The synced tables this module expects
# MAGIC A synced table lands in a **Postgres schema equal to the UC schema of its online view** (the
# MAGIC schema you pick in the create dialog), **not** necessarily your app schema. So we configure the
# MAGIC synced-table location once here and use it for *every* check below. Adjust `SYNCED_SCHEMA` /
# MAGIC the names if your facilitator created them elsewhere.

# COMMAND ----------

SYNCED_SCHEMA = cfg.uc_schema          # where the Catalog "Create synced table" flow placed them
# postgres synced-table name -> (UC source table, primary key, sync mode)
SYNCED = {
    "hcp_master_synced":       ("hcp_master",       "hcp_id",         "Snapshot"),
    "hcp_interactions_synced": ("hcp_interactions", "interaction_id", "Triggered"),
    "brand_metrics_synced":    ("brand_metrics",    None,             "Snapshot"),
}

# COMMAND ----------

# MAGIC %md ## Sync modes, taught by consequence
# MAGIC
# MAGIC | Source | Mode | Why |
# MAGIC |---|---|---|
# MAGIC | `hcp_master` | **Snapshot** | Reference data; changes rarely. No CDF needed. |
# MAGIC | `brand_metrics` | **Snapshot** | Periodic reporting data. No CDF needed. |
# MAGIC | `hcp_interactions` | **Triggered** | Changes often; needs freshness. **Requires CDF on the source** (the generator enables it). |
# MAGIC
# MAGIC Continuous is the wrong default for a workshop — it holds compute and costs money for a
# MAGIC freshness nobody in a 3-hour lab will observe.
# MAGIC
# MAGIC **Create flow (facilitator / replay — Catalog UI, verified working; the CLI is broken on
# MAGIC current Autoscaling Lakebase):** Catalog Explorer → open the source table → **Create →
# MAGIC Synced table** → Database type **Autoscaling**, pick your **Project** / **Branch=production** /
# MAGIC **Postgres database=databricks_postgres**, primary key auto-detects, **Sync mode = On-demand**
# MAGIC (or Continuous), leave **LTAP Direct Writes off**, **Create**. Initial sync → status **Online**.
# MAGIC The `databricks postgres create-synced-table` CLI (v1.14.1) rejects the documented body and
# MAGIC `get-synced-table` returns "No API found" — do not rely on it. See `docs/RUNBOOK.md`.

# COMMAND ----------

# MAGIC %md ## Verify the mirror: row counts match the source — and FAIL if any are missing
# MAGIC Synced tables are a prerequisite for the copilot's "what is true now" query, so a missing or
# MAGIC mismatched mirror is a hard failure, not a warning. We report every problem, then raise once.

# COMMAND ----------

problems = []
for synced_name, (src_table, _pk, _mode) in SYNCED.items():
    src = spark.table(cfg.table(src_table)).count()
    try:
        dst = lakebase.run(f'SELECT count(*) FROM "{SYNCED_SCHEMA}"."{synced_name}"')[0][0]
    except Exception as e:
        msg = str(e).splitlines()[0]
        print(f"❌ {synced_name:26s} not synced yet — {msg}")
        problems.append(f"{synced_name}: not synced ({msg})")
        continue
    if dst == src:
        print(f"✅ {synced_name:26s} UC={src:6d}  Lakebase={dst:6d}")
    else:
        print(f"⚠️ {synced_name:26s} UC={src:6d}  Lakebase={dst:6d}  MISMATCH")
        problems.append(f"{synced_name}: count mismatch (UC={src}, Lakebase={dst})")

if problems:
    raise RuntimeError(
        "Synced tables are not ready — create/refresh them via the Catalog UI (see docs/RUNBOOK.md), "
        "or fix SYNCED_SCHEMA / names above:\n  - " + "\n  - ".join(problems))
print("\n✅ all configured synced tables present and row-matched")

# COMMAND ----------

# MAGIC %md ## Treat synced tables as read-only — but know what the engine actually enforces
# MAGIC A synced table mirrors a **Unity Catalog source**; UC is the source of truth. The rule is:
# MAGIC **do not write to a synced table** — write to the UC source and let the sync propagate.
# MAGIC
# MAGIC **Verified in testing (2026-09-15), and important to say honestly:** on current *Autoscaling*
# MAGIC Lakebase a synced table is a **writable** Postgres (partitioned) table — an `UPDATE`/`INSERT`
# MAGIC is **not** rejected at the Postgres layer (older Provisioned-era behavior did reject it). A
# MAGIC local write does **not** flow back to UC and can be reconciled away on the next sync. So
# MAGIC "read-only" here is a **governance rule you enforce with GRANTs**, not a guarantee the engine
# MAGIC gives you. We demonstrate the *actual* behavior — inside a transaction we **roll back**, so the
# MAGIC shared demo table is never mutated.

# COMMAND ----------

DEMO_TABLE = "hcp_interactions_synced"      # one of the SYNCED tables above
c = lakebase.connect()
c.autocommit = False
try:
    with c.cursor() as cur:
        cur.execute(f'UPDATE "{SYNCED_SCHEMA}"."{DEMO_TABLE}" SET channel = %s WHERE true', ("LOCAL_EDIT",))
        cur.execute(f"SELECT count(*) FROM \"{SYNCED_SCHEMA}\".\"{DEMO_TABLE}\" WHERE channel='LOCAL_EDIT'")
        changed = cur.fetchone()[0]
    print(f"⚠️ the UPDATE SUCCEEDED inside the transaction ({changed} rows) — current Lakebase does")
    print("   NOT block writes to a synced table. Rolling back so we leave the mirror untouched.")
    print("   Lesson: this edit never reached the UC source; enforce read-only with GRANTs")
    print("   (REVOKE INSERT, UPDATE, DELETE ON <synced> FROM <role>), not by relying on the engine.")
    c.rollback()
except Exception as e:
    c.rollback()
    msg = str(e).splitlines()[0]
    sqlstate = getattr(e, "sqlstate", None)
    if "does not exist" in msg.lower() or "undefined_table" in msg.lower():
        print(f"↩︎ {SYNCED_SCHEMA}.{DEMO_TABLE} is not synced yet — create it (Catalog Explorer →")
        print("   Create → Synced table; see docs/RUNBOOK.md), then re-run. Detail:", msg)
    elif sqlstate == "42501" or "permission denied" in msg.lower():
        print("✅ read-only IS enforced here by a GRANT (insufficient privilege) — writes rejected:", msg)
    else:
        raise
finally:
    c.close()

# COMMAND ----------

# MAGIC %md ## ✅ Verification
# MAGIC You have an application schema you own and mirrored, queryable tables inside your Lakebase
# MAGIC project. Remember: synced tables are read-only **by governance** — UC is the source of truth,
# MAGIC and the Postgres engine does not itself reject writes on current Autoscaling Lakebase.

# COMMAND ----------

got = {r[0] for r in lakebase.run(
    f"SELECT table_name FROM information_schema.tables WHERE table_schema = '{SYNCED_SCHEMA}'")}
print(f"synced tables visible in {SYNCED_SCHEMA} ->", sorted(n for n in SYNCED if n in got))
print(f"your app schema {cfg.app_schema} is ready for module 03")
