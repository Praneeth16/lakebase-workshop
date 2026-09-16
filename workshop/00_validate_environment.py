# Databricks notebook source
# MAGIC %md
# MAGIC # 00 · Environment validation
# MAGIC
# MAGIC Run this **first**. It prints one pass/fail row per prerequisite and **never aborts** — a
# MAGIC single red row does not stop the other checks, so a facilitator can glance at one table per
# MAGIC screen and know who needs help and why. Fix red rows here, not mid-lab.
# MAGIC
# MAGIC It is also the first thing to run on an unattended replay: it tells an AstraZeneca engineer
# MAGIC exactly which prerequisites are missing and who fixes each.

# COMMAND ----------

# MAGIC %pip install "psycopg[binary]" "databricks-sdk>=0.81.0" --quiet
# MAGIC %restart_python

# COMMAND ----------

import sys
sys.path.insert(0, "..")

from workshop.config import cfg

results = []  # (check, status, detail/remediation)


def check(name):
    """Decorator: run a check, capture pass/fail, never let it abort the notebook."""
    def wrap(fn):
        try:
            ok, detail = fn()
        except Exception as e:  # a failing check is data, not a crash
            ok, detail = False, f"{type(e).__name__}: {str(e).splitlines()[0]}"
        results.append((name, "✅ PASS" if ok else "❌ FAIL", detail))
        return fn
    return wrap


cfg.show()

# COMMAND ----------

# 1 — Python dependency
@check("psycopg (Postgres driver)")
def _():
    import psycopg  # noqa
    return True, f"psycopg {psycopg.__version__}"


# 2 — Notebook-native identity (no local CLI profile needed)
@check("Databricks identity (notebook-native)")
def _():
    from databricks.sdk import WorkspaceClient
    me = WorkspaceClient().current_user.me()
    return True, f"as {me.user_name}"


# 3 — Config shape (no network)
@check("config.py resolved and valid")
def _():
    probs = cfg.problems(required=frozenset({"warehouse"}))  # 00 checks warehouse + core; not Search/Feature-Store
    return (len(probs) == 0), ("all values set" if not probs else "; ".join(probs))


# 4 — SQL warehouse reachable
@check("SQL warehouse reachable")
def _():
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient()
    wh = w.warehouses.get(id=cfg.warehouse_id)
    return (str(wh.state.value) in ("RUNNING", "STARTING", "STOPPED")), f"{wh.name}: {wh.state.value}"


# 5 — UC catalog/schema readable + synthetic tables present
@check("Unity Catalog data present")
def _():
    expected = {"hcp_master", "hcp_interactions", "brand_metrics",
                "medical_content", "hcp_features", "seed_agent_turns"}
    have = {r.tableName for r in spark.sql(f"SHOW TABLES IN {cfg.uc_path}").collect()}
    missing = expected - have
    if missing:
        return False, f"missing {sorted(missing)} — run data/generate_synthetic_pharma.py"
    return True, f"{len(expected)} tables in {cfg.uc_path}"


# 6 — CDF where the syncs need it
@check("Change Data Feed on Triggered-sync sources")
def _():
    bad = []
    for t in ("hcp_interactions", "hcp_features"):
        props = {r.key: r.value for r in spark.sql(f"SHOW TBLPROPERTIES {cfg.table(t)}").collect()}
        if props.get("delta.enableChangeDataFeed") != "true":
            bad.append(t)
    return (not bad), ("CDF enabled" if not bad else f"CDF missing on {bad}")


# 7 — Embedding endpoint access (module 04)
@check("Embedding endpoint access")
def _():
    from databricks.sdk import WorkspaceClient
    ep = WorkspaceClient().serving_endpoints.get(cfg.embedding_endpoint)
    state = ep.state.ready.value if ep.state and ep.state.ready else "UNKNOWN"
    return (state == "READY"), f"{cfg.embedding_endpoint}: {state}"


# 8 — Generation model access (module 03b dreaming uses an AI function)
@check("Generation model access (ai_query)")
def _():
    # endpoint name is a trusted config value; prompt is bound as a parameter
    row = spark.sql(
        f"SELECT ai_query('{cfg.generation_endpoint}', :p) AS r",
        args={"p": "Reply with the single word OK"},
    ).collect()[0]
    return (row.r is not None), f"{cfg.generation_endpoint} → {str(row.r)[:40]!r}"


# 9 — Lakebase endpoint reachable + credential generation permission
@check("Lakebase credential generation")
def _():
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient()
    ep = w.postgres.get_endpoint(name=cfg.endpoint_path)
    cred = w.postgres.generate_database_credential(cfg.endpoint_path)
    tok = getattr(cred, "token", None) or cred.as_dict().get("token")
    state = ep.as_dict().get("status", {}).get("state", "?")
    return (bool(tok)), f"endpoint {state}; credential minted"


# 10 — Browser-path Postgres connectivity (the check that matters most)
@check("Postgres connectivity from the notebook")
def _():
    from workshop.lakebase import run
    val = run("SELECT 1 AS ok")
    return (val and val[0][0] == 1), "SELECT 1 succeeded over the notebook connection"


# 11 — Schema create/write/read/drop canary under THIS identity (ownership check)
@check("Schema DDL canary (create/write/read/drop)")
def _():
    from workshop.lakebase import connect
    canary = f"{cfg.app_schema}_canary"
    with connect() as c:
        with c.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{canary}"')
            cur.execute(f'CREATE TABLE IF NOT EXISTS "{canary}".t (id int)')
            cur.execute(f'INSERT INTO "{canary}".t VALUES (1)')
            cur.execute(f'SELECT count(*) FROM "{canary}".t')
            n = cur.fetchone()[0]
            cur.execute(f'DROP SCHEMA "{canary}" CASCADE')
        c.commit()
    return (n == 1), "create → insert → select → drop all succeeded under your identity"


# COMMAND ----------

# MAGIC %md ## Results

# COMMAND ----------

import pandas as pd
df = pd.DataFrame(results, columns=["prerequisite", "status", "detail / remediation"])
fails = df[df.status.str.contains("FAIL")]
print(f"{len(df) - len(fails)}/{len(df)} passed")
if len(fails):
    print("\nRed rows to fix before building:")
    for _, r in fails.iterrows():
        print(f"  ❌ {r['prerequisite']}: {r['detail / remediation']}")
display(df)

# COMMAND ----------

# Every check ran (this notebook never aborts mid-way, so you see ALL gaps at once). But fail the
# notebook at the END if anything is red — otherwise an unattended replay reports green on a broken
# environment. Fix the red rows above, then re-run.
if len(fails):
    raise RuntimeError(f"{len(fails)}/{len(df)} prerequisite(s) failed — see the red rows above.")
print("✅ all prerequisites passed")
