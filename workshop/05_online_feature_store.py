# Databricks notebook source
# MAGIC %md
# MAGIC # 05 · Online Feature Store — publish → serve → keys-only 200 (DEMO)
# MAGIC
# MAGIC Serves features for inference in **under one minute**, the online tier a batch-only feature
# MAGIC pipeline cannot meet. This is the online serving path for a model that needs fresh features at
# MAGIC request time.
# MAGIC
# MAGIC **Status: GA** — the only GA pillar of the three, which is why it should be weighted
# MAGIC differently from Search and managed memory.
# MAGIC
# MAGIC The chain: offline feature table in UC → online store on Lakebase → a model logged with its
# MAGIC feature spec → a request carrying **only primary keys** returns a scored prediction.

# COMMAND ----------

# MAGIC %pip install "databricks-sdk>=0.81.0" "databricks-feature-engineering>=0.13.0" --quiet
# MAGIC %restart_python

# COMMAND ----------

import sys
sys.path.insert(0, "..")
from workshop.config import cfg
cfg.validate(required={"ofs"})

ONLINE_STORE = cfg.online_store_name                    # its OWN Lakebase instance (see note below)
SERVING_ENDPOINT = cfg.feature_serving_endpoint
SOURCE = cfg.table("hcp_features")
FEATURE_TABLE = SOURCE                                  # offline source of truth in UC
# The online table's UC catalog MUST equal the online store's backing Postgres DB name, or the
# serving endpoint fails to deploy. publish_table creates that catalog by default, so name the
# online table in a catalog matching the store (cfg.online_store_name), not the source catalog.
ONLINE_TABLE = f"{cfg.online_store_name}.{cfg.uc_schema}.hcp_features_online"

# COMMAND ----------

# MAGIC %md ## Preflight — checked, not discovered live
# MAGIC Every prerequisite that fails silently at publish or deploy time, verified up front.

# COMMAND ----------

pre = []
# runtime / package
try:
    import databricks.feature_engineering as fe_pkg
    pre.append(("databricks-feature-engineering >= 0.13.0", getattr(fe_pkg, "__version__", "present")))
except Exception as e:
    pre.append(("databricks-feature-engineering", f"MISSING: {e}"))
# non-null PK + CDF on the source
pk_nulls = spark.sql(f"SELECT count(*) c FROM {SOURCE} WHERE hcp_id IS NULL").collect()[0].c
props = {r.key: r.value for r in spark.sql(f"SHOW TBLPROPERTIES {SOURCE}").collect()}
pre.append(("hcp_id non-null PK", "ok" if pk_nulls == 0 else f"{pk_nulls} nulls — publish will reject"))
pre.append(("Change Data Feed on source", props.get("delta.enableChangeDataFeed", "MISSING")))
# an actual PRIMARY KEY constraint must exist, not merely non-null values
pk = spark.sql(f"""SELECT count(*) c FROM {cfg.uc_catalog}.information_schema.table_constraints
                   WHERE table_schema='{cfg.uc_schema}' AND table_name='hcp_features'
                     AND constraint_type='PRIMARY KEY'""").collect()[0].c
pre.append(("PRIMARY KEY constraint exists", "ok" if pk >= 1 else "MISSING — publish will reject"))
# catalog name must equal the backing Postgres database name, or the endpoint silently fails to
# deploy. The online store does not exist yet at preflight, so this is an INFORMATIONAL reminder,
# not a verified comparison — recheck it against the store's DB name after create_online_store.
pre.append(("UC catalog vs online-store DB name (INFO — verify post-create)",
            f"catalog={cfg.uc_catalog} — after create, confirm the store's backing DB name equals this"))
for k, v in pre:
    print(f"  {k:45s} : {v}")

# COMMAND ----------

# MAGIC %md ## Create the online store — and the governance consequence
# MAGIC `create_online_store` **provisions its own Lakebase instance**, separate from your
# MAGIC application project. A team running both an app backend and online serving ends up with
# MAGIC **two Lakebase instances** to name, tag, budget and monitor — say it before they find it in a
# MAGIC cost report. `capacity` uses `CU_1`–`CU_8` naming, which does **not** match the Lakebase
# MAGIC project range of 0.5–112 CU — do not assume they are interchangeable.
# MAGIC
# MAGIC ⚠️ **The online store does not scale to zero.** The generic 30–60 min idle-timeout guidance
# MAGIC does not apply — it bills until you **delete** it through the supported SDK path (see 99_teardown).

# COMMAND ----------

from databricks.feature_engineering import FeatureEngineeringClient
from databricks.sdk.errors import NotFound
fe = FeatureEngineeringClient()

# fail-closed: the store is pre-provisioned before the session. create_online_store is too slow
# to run live and the store does not scale to zero, so we never create it here — we require it.
try:
    online_store = fe.get_online_store(name=ONLINE_STORE)
    print(f"online store {ONLINE_STORE} found (pre-provisioned)")
except NotFound:
    raise RuntimeError(
        f"online store {ONLINE_STORE} not found. It is pre-provisioned before the session "
        "(create_online_store is too slow to run live and does not scale to zero). Stand it up "
        "per docs/RUNBOOK.md, set LB_ONLINE_STORE, then re-run this demo.")

# COMMAND ----------

# MAGIC %md ## Publish features
# MAGIC Publish modes map to freshness: **Triggered** (default incremental), **Continuous**
# MAGIC (streaming immediacy), **Snapshot** (one-time). Triggered/Continuous require CDF (preflight
# MAGIC confirmed it). `publish_mode` is `TRIGGERED`/`CONTINUOUS`/`SNAPSHOT` in Feature Engineering
# MAGIC 0.13+.

# COMMAND ----------

try:
    fe.publish_table(
        online_store=online_store,
        source_table_name=FEATURE_TABLE,
        online_table_name=ONLINE_TABLE,
        publish_mode="TRIGGERED",
    )
    print("published hcp_features →", ONLINE_TABLE)
except Exception as e:
    # idempotent re-run: only an "already exists" is fine. A "does not exist" is a real setup
    # failure (missing source/store) and must NOT be swallowed as if already published.
    if "already exists" in str(e).lower():
        print("online table already published — continuing")
    else:
        raise

# COMMAND ----------

# MAGIC %md ## The model that looks features up automatically
# MAGIC The model is logged with a `FeatureLookup`, so the serving endpoint fetches features from the
# MAGIC online store at request time and the request carries **only the primary key**.
# MAGIC
# MAGIC The serving endpoint is **pre-provisioned** for the session because an endpoint
# MAGIC deploy takes far longer than a demo slot. The one-time provisioning lifecycle —
# MAGIC `fe.create_training_set(df, feature_lookups, label=...)` → `fe.log_model(...)` → register to
# MAGIC UC → `w.serving_endpoints.create(...)` — is documented in `docs/RUNBOOK.md` for your replay.

# COMMAND ----------

from databricks.feature_engineering import FeatureLookup

lookups = [FeatureLookup(table_name=FEATURE_TABLE, lookup_key="hcp_id",
                         feature_names=["engagement_90d", "days_since_last_interaction", "propensity_score"])]
print("FeatureLookup on hcp_id →", lookups[0].feature_names)

# COMMAND ----------

# MAGIC %md ## ✅ The money shot — a keys-only request returns a prediction
# MAGIC One 200 proves the whole chain: the model found its lookups, the online store answered, the
# MAGIC model scored. **Fail closed** — if the endpoint is not ready, stop with a clear message rather
# MAGIC than pretend the chain worked.

# COMMAND ----------

from databricks.sdk import WorkspaceClient
w = WorkspaceClient()

ep = w.serving_endpoints.get(name=SERVING_ENDPOINT)
ready = ep.state.ready.value if ep.state and ep.state.ready else "UNKNOWN"
if ready != "READY":
    raise RuntimeError(
        f"{SERVING_ENDPOINT} is not READY (state={ready}). Stand it up per docs/RUNBOOK.md / "
        "before running this demo — do not present a swallowed failure as success.")

resp = w.serving_endpoints.query(
    name=SERVING_ENDPOINT,
    dataframe_records=[{"hcp_id": "HCP-0007"}])   # KEYS ONLY — no features in the request
print("✅ 200 — keys-only request scored:", resp.as_dict())

# COMMAND ----------

# MAGIC %md ## What breaks in production
# MAGIC - **`No online tables found for required feature tables`** — the #1 serving failure: the
# MAGIC   feature table was never published online. Teach *offline for training, online for serving,
# MAGIC   you need both*.
# MAGIC - **Mixed backend formats** for one model's feature tables — rejected outright.
# MAGIC - **Connection-pool ceiling `max(3, min(10, num_tables))`** — ten connections per pod
# MAGIC   regardless of table count; adding feature tables can raise lookup latency, and a capacity
# MAGIC   upgrade does not necessarily help. The pool-size env var is a *candidate*, not a confirmed fix.
# MAGIC - **Online Tables** are past deprecation and never reached GA — the forward path is this
# MAGIC   Lakebase-backed online store. "Supersedes Online Tables" is true; "supersedes the legacy
# MAGIC   online store" is too strong (third-party online stores remain supported).

# COMMAND ----------

# reproduce the #1 failure deliberately, then explain the fix
print("Deliberate failure demo: querying a model whose feature table was not published online →")
print("  expect: 'No online tables found for required feature tables'")
print("  fix: publish the missing table (above), confirm it reports healthy, re-issue the request.")

# COMMAND ----------

# MAGIC %md ## ✅ Verification
# MAGIC A keys-only request returns a scored prediction; participants have the notebook. Remember:
# MAGIC the online store bills until deleted — run `99_teardown` when done.
