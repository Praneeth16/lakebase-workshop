# Databricks notebook source
# MAGIC %md
# MAGIC # 01 · Lakebase fundamentals
# MAGIC
# MAGIC Connect to **your team's pre-provisioned Lakebase project**, walk the resource hierarchy,
# MAGIC and learn the cost posture — all from the browser, no local client.
# MAGIC
# MAGIC You do **not** create a project here. Project creation is the slowest, most
# MAGIC permission-sensitive step; it is pre-provisioned for you and documented in full in
# MAGIC `docs/RUNBOOK.md` for your own replay.

# COMMAND ----------

# MAGIC %pip install "psycopg[binary]" "databricks-sdk>=0.81.0" --quiet
# MAGIC %restart_python

# COMMAND ----------

import sys
sys.path.insert(0, "..")
from workshop.config import cfg
from workshop import lakebase
cfg.validate(required=frozenset())  # core module: no Search/Feature-Store/warehouse config needed

# COMMAND ----------

# MAGIC %md ## The hierarchy: Project → Branch → (Endpoint, Database, Role)
# MAGIC A project auto-provisions a `production` branch and a `primary` read-write endpoint.

# COMMAND ----------

from databricks.sdk import WorkspaceClient
w = WorkspaceClient()

proj = w.postgres.get_project(name=f"projects/{cfg.project_id}")
print("project :", cfg.project_id, "->", proj.as_dict().get("state", "?"))
print("branches:", [b.as_dict().get("name") for b in w.postgres.list_branches(f"projects/{cfg.project_id}")])
print("endpoints:", [e.as_dict().get("name") for e in w.postgres.list_endpoints(cfg.branch_path)])
print("databases:", [d.as_dict().get("name") for d in w.postgres.list_databases(cfg.branch_path)])

# COMMAND ----------

# MAGIC %md ## Connect from the notebook and run first queries
# MAGIC The connection originates **inside** the platform — this is why the lab does not depend
# MAGIC on Lakebase being reachable from a laptop.

# COMMAND ----------

rows = lakebase.run("SELECT version(), current_database(), current_user")
print(rows[0][0])
print("database:", rows[0][1], "| role:", rows[0][2])

# COMMAND ----------

# MAGIC %md ## Cost posture — taught here, not bolted on at the end
# MAGIC
# MAGIC - **Autoscaling** 0.5–32 CU dynamic (fixed 36–112). Constraint: **max − min ≤ 16 CU**.
# MAGIC - **Scale-to-zero** is on by default; wake is ~100 ms; apps must retry through the wake
# MAGIC   (the connection helper already does).
# MAGIC - A **30 to 60 minute idle timeout** is a reasonable default for scale-to-zero workloads.
# MAGIC - **OAuth database credentials expire after one hour.** The helper mints a fresh credential
# MAGIC   on every connect, so a notebook idle over lunch reconnects cleanly instead of failing auth.
# MAGIC
# MAGIC To see a wake live in a short session, a facilitator sets a ~60-second suspend timeout on a
# MAGIC throwaway **demo** endpoint and lets it sleep — setting the value explicitly avoids quoting a
# MAGIC default that is disputed across sources.

# COMMAND ----------

ep = w.postgres.get_endpoint(name=cfg.endpoint_path).as_dict()
scaling = ep.get("spec", {})
print("endpoint scaling spec:", {k: scaling.get(k) for k in scaling if "cu" in k.lower() or "scale" in k.lower() or "suspend" in k.lower()})
print("(read-only view of your endpoint's autoscaling + scale-to-zero configuration)")

# COMMAND ----------

# MAGIC %md ## ✅ Verification
# MAGIC You should see a PostgreSQL 16 or 17 version string, your database name, and your identity
# MAGIC as the Postgres role. If the version prints, your browser-native connection works.

# COMMAND ----------

v = lakebase.run("SELECT current_setting('server_version')")[0][0]
assert v.split(".")[0] in ("16", "17"), f"unexpected Postgres version: {v}"
print(f"✅ connected to Postgres {v} on {cfg.endpoint_path}")
