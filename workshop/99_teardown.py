# Databricks notebook source
# MAGIC %md
# MAGIC # 99 · Teardown — leave nothing billing
# MAGIC
# MAGIC Cost discipline that ships as code gets followed; cost discipline in prose does not. This
# MAGIC notebook drops what the workshop created and — critically — **deletes the online feature
# MAGIC store, which does not scale to zero and bills until removed.**
# MAGIC
# MAGIC Safe by default: `DRY_RUN = True` only **lists** what would be removed. Set it to `False` to
# MAGIC actually delete. It never touches the pre-provisioned team projects or your synthetic UC data
# MAGIC unless you explicitly opt in.

# COMMAND ----------

# MAGIC %pip install "psycopg[binary]" "databricks-sdk>=0.81.0" "databricks-feature-engineering>=0.13.0" --quiet
# MAGIC %restart_python

# COMMAND ----------

import sys
sys.path.insert(0, "..")
from workshop.config import cfg

DRY_RUN = True                 # <-- set False to actually delete
DROP_UC_DATA = False           # <-- set True only if you also want the synthetic UC tables gone
ONLINE_STORE = f"az_workshop_online_{cfg.team_id}"
SERVING_ENDPOINT = f"az-copilot-propensity-{cfg.team_id}"

_errors = []

def act(desc, fn):
    if DRY_RUN:
        print(f"[dry-run] would: {desc}")
        return
    from databricks.sdk.errors import NotFound
    try:
        fn(); print(f"[done]    {desc}")
    except NotFound:
        print(f"[absent]  {desc} — already gone")
    except Exception as e:
        # do NOT treat a real failure as success — the online store bills until actually deleted
        print(f"[FAILED]  {desc} — {str(e).splitlines()[0]}")
        _errors.append((desc, e))

# COMMAND ----------

# MAGIC %md ## 1 — Delete the online feature store (does NOT scale to zero — this is the costly one)

# COMMAND ----------

from databricks.feature_engineering import FeatureEngineeringClient
from databricks.sdk import WorkspaceClient
fe = FeatureEngineeringClient()
w = WorkspaceClient()

act(f"delete serving endpoint {SERVING_ENDPOINT}",
    lambda: w.serving_endpoints.delete(name=SERVING_ENDPOINT))
act(f"delete online store {ONLINE_STORE} (supported SDK path)",
    lambda: fe.delete_online_store(name=ONLINE_STORE))

# COMMAND ----------

# MAGIC %md ## 2 — Drop the workshop schemas inside Lakebase (data created by the lab)

# COMMAND ----------

from workshop import lakebase
act(f'drop app schema "{cfg.app_schema}" on the app project',
    lambda: lakebase.run(f'DROP SCHEMA IF EXISTS "{cfg.app_schema}" CASCADE', fetch=False))

def drop_search():
    c = lakebase.connect_search(); c.autocommit = True
    with c.cursor() as cur:
        cur.execute('DROP SCHEMA IF EXISTS "search_demo" CASCADE')
    c.close()
act('drop "search_demo" schema on the Search demo project', drop_search)

# COMMAND ----------

# MAGIC %md ## 3 — Optional: drop the synthetic UC dataset

# COMMAND ----------

if DROP_UC_DATA:
    act(f"drop UC schema {cfg.uc_path} CASCADE",
        lambda: spark.sql(f"DROP SCHEMA IF EXISTS {cfg.uc_path} CASCADE"))
else:
    print("keeping synthetic UC data (DROP_UC_DATA=False). It is regenerable and cheap to keep.")

# COMMAND ----------

# MAGIC %md ## What is intentionally preserved
# MAGIC - The pre-provisioned **team Lakebase projects** and the **Search demo project** (deleting a
# MAGIC   project is a platform-team action, not a lab teardown step).
# MAGIC - The **synthetic UC dataset**, unless `DROP_UC_DATA=True`.
# MAGIC - The Lakebase Search **enablement** on the demo project — it is irreversible anyway.
# MAGIC
# MAGIC Endpoints on the team projects scale to zero on their own idle timeout; the online store did
# MAGIC not, which is why deleting it is step 1.

# COMMAND ----------

print("DRY_RUN =", DRY_RUN, "— set to False to execute the deletions above.")
if _errors:
    raise RuntimeError(
        "teardown did NOT fully complete — resources may still be billing:\n" +
        "\n".join(f"  - {d}: {str(e).splitlines()[0]}" for d, e in _errors))
elif not DRY_RUN:
    print("✅ teardown complete — nothing left billing.")
