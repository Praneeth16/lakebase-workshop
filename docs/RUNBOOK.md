# Runbook, reproduce the workshop in your own workspace

The self-serve path from an empty Databricks workspace to a working Field Medical Copilot on
Lakebase. It starts **one step earlier** than the attended session: the session pre-provisions
your project; here you create it yourself. Written for someone who was not in the room.

Everything the participants run is browser-native (Databricks notebooks). The **control-plane**
steps below (project creation, synced-table creation, Search enablement, online store) use the
`databricks` CLI or SDK and are normally done once by a platform owner.

---

## 0 · Prerequisites (who grants each)

| Prerequisite | How to get it |
|---|---|
| Databricks workspace with Lakebase enabled | Platform team / account team |
| Unity Catalog catalog you can create schemas in (e.g. `az_workshop`) | UC admin |
| A SQL warehouse | Workspace admin (`databricks warehouses list`) |
| Embedding endpoint (`databricks-gte-large-en`, 1024-dim) | Usually preconfigured; confirm it is `READY` |
| A generation model for the dreaming AI function (`databricks-meta-llama-3-3-70b-instruct`) | Confirm access |
| **Lakebase Search Beta** (module 04 only) | **Account-team request + workspace-admin Previews toggle. Enabling is irreversible per project.** |
| `databricks-feature-engineering >= 0.13.0`, DBR 16.4 LTS ML or serverless (module 05) | `%pip install` in the notebook |
| **`databricks-sdk >= 0.81.0`** (for `w.postgres`) | Installed by each notebook's `%pip` line. The runtime's built-in SDK is older and lacks `w.postgres`, verified in the dry run; the notebooks upgrade it and `%restart_python`. |

## 1 · Create the Lakebase projects (control-plane)

In the UI: **Compute → Lakebase → Go to Lakebase Postgres** (`/lakebase/projects`) → **New project**.
The create dialog confirms what you get: a `production` branch, `8↔16 CU` with scale-to-zero, a
`databricks_postgres` database, Postgres 17.


Equivalent CLI (what the session automation uses):

```bash
# shared application project (the session pre-provisions one for the room; each builder owns a schema)
databricks postgres create-project az-<team>-copilot \
  --json '{"spec": {"display_name": "AZ Copilot <team>"}}' --profile <PROFILE>

# dedicated Search demo project, enable Search on THIS one only, never a shared project
databricks postgres create-project az-demo-search \
  --json '{"spec": {"display_name": "AZ Search demo"}}' --profile <PROFILE>
```

Then read back the ids you will paste into `config.py`:

```bash
databricks postgres list-projects --profile <PROFILE>
databricks postgres list-endpoints projects/<PROJECT_ID>/branches/production --profile <PROFILE>
```

## 2 · Edit `workshop/config.py`, the only file you change

Set `project_id`, `search_project_id`, `uc_catalog`, `uc_schema`, `warehouse_id`, and `team_id`.
For notebook runs, edit the defaults in `config.py` (a variable exported in a cell is lost on `%restart_python`). Run `python workshop/config.py` (or
`cfg.show()` in a notebook) to confirm resolved values; `cfg.validate()` lists every problem at
once.

## 3 · Generate the data

Run `data/generate_synthetic_pharma.py`. Writes six tables to `<catalog>.<schema>`. It enables
Change Data Feed on `hcp_features` (Online Feature Store) and `hcp_interactions` (Triggered sync),
sets the non-null PK on `hcp_features`, and plants the four patterns the dreaming module needs.
Its final cell verifies all of this, do not proceed on a red assertion.

## 4 · Validate

Run `workshop/00_validate_environment.py`. Ten-plus pass/fail rows; it never aborts. Fix red rows
before building. The DDL canary proves you can create/write/read/drop under your own identity, if it fails, you have connect-but-not-create permission and need `USAGE` + `CREATE` on the schema.

## 5 · Build, in order

| Step | Notebook | You should see |
|---|---|---|
| M1 | `01_fundamentals.py` | PostgreSQL 16/17 version over the notebook connection |
| M2 | `02_synced_tables.py` | app schema you own; three mirrored tables; writes to a synced table rejected |
| M3 | `03_agent_memory_build.py` | recall returns this rep's turns and not another's; one fact with provenance |
| M3b | `03b_agent_memory_dreaming.py` | a Cardiology cross-rep theme after the dream that did not exist before; the wrong claim in the review queue, not retrievable |
| M4 | `04_search_hybrid.py` | hybrid RRF results; `EXPLAIN` shows the index in use |
| M5 | `05_online_feature_store.py` | a keys-only request returns a scored prediction (200) |

### Control-plane commands the notebooks reference

**Synced tables** (module 02), **create from the Catalog UI** (verified working in testing on 2026-09-15;
the notebook then verifies them):

1. **Catalog Explorer → open the source table** (e.g. `<catalog>.<schema>.hcp_interactions`) →
   **Create → Synced table**.
2. In the dialog: **Database type = Autoscaling**; pick your **Project**, **Branch = production**,
   **Postgres database = databricks_postgres**; primary key auto-detects (`interaction_id`);
   **Sync mode = On-demand** (Snapshot) for `hcp_master`/`brand_metrics`, or for a CDF source;
   leave **LTAP Direct Writes off**; **Create**. Initial sync takes ~2 to 3 min → status **Online**.
3. The synced table lands in a **Postgres schema equal to the online view's UC schema** (the schema
   you chose in the dialog's Name), **not** necessarily your app schema. Point module 02's
   verification at that schema (it uses `cfg.uc_schema` by default).

> ⚠️ **Dry-run findings (2026-09-15), read before relying on the CLI or the read-only rule:**
> - **The `databricks postgres create-synced-table` CLI (v1.14.1) did not work**: it rejects the
>   documented body fields and `get-synced-table` returns `No API found for GET /postgres/<id>`.
>   The legacy SDK `w.database.create_synced_database_table` rejects Autoscaling projects
>   (`Database instance is not found`). **Use the Catalog UI above.** A fresh Autoscaling project
>   also has **no Tables/SQL-editor UI** of its own (only Dashboard/Branches/Settings).
> - **Synced tables are NOT read-only at the Postgres layer on current Autoscaling Lakebase.** A
>   synced table is a **writable** partitioned Postgres table, `UPDATE`/`INSERT`/`DELETE` all
>   succeeded and a re-sync did not revert them. Treat read-only as a **governance rule enforced
>   with GRANTs**, not an engine guarantee. Module 02 was corrected to teach this.

**Search enablement** (module 04), request Beta, then in **project Settings → Lakebase Search**,
**Enable Lakebase Search** on the **demo project only**. Read the warning first: it restarts all
computes, makes `lakebase_vector` / `lakebase_text` available, and **cannot be turned off once
enabled**. Then: `CREATE EXTENSION lakebase_vector CASCADE;` `CREATE EXTENSION lakebase_text;`. If
`CREATE EXTENSION` fails, check the project's `shared_preload_libraries` (see TROUBLESHOOTING).


**Online store + serving endpoint** (module 05), `fe.create_online_store(name=..., capacity="CU_2")`
provisions its **own** Lakebase instance; the UC catalog name must equal that instance's Postgres
database name or the serving endpoint silently fails to deploy. The one-time model + endpoint
lifecycle (run before the session; the endpoint deploy is slow, so it is pre-provisioned before
the room rather than built live):

```python
from databricks.feature_engineering import FeatureEngineeringClient, FeatureLookup
fe = FeatureEngineeringClient()
lookups = [FeatureLookup(table_name="<catalog>.<schema>.hcp_features", lookup_key="hcp_id",
           feature_names=["engagement_90d","days_since_last_interaction","propensity_score"])]
ts = fe.create_training_set(df=<labelled_df>, feature_lookups=lookups, label="label")
fe.log_model(model=<trained_model>, artifact_path="copilot_propensity",
             flavor=mlflow.sklearn, training_set=ts, registered_model_name="<catalog>.<schema>.copilot_propensity")
# then create a serving endpoint for that registered model (w.serving_endpoints.create(...))
```
Module 05 itself is **fail-closed**: it checks the endpoint is `READY` and errors clearly if not,
rather than presenting a swallowed failure as a success.

## 6 · Tear down

Run `workshop/99_teardown.py`. It defaults to `DRY_RUN = True` (lists only). Set `False` to delete.
**The online feature store does not scale to zero. Delete it first**, or it bills indefinitely.

---

## If Lakebase is unreachable from your laptop
That is a recorded limitation, not a misconfiguration. The whole path above runs in the browser;
you never need a local Postgres client. `psql` appears only in optional facilitator material.
