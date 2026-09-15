# Lakebase Workshop, Sandbox Setup Guide

**Stand up the Field Medical Copilot workshop in an AstraZeneca Databricks workspace.**
Self-serve, browser-native. From an empty workspace to a working agent-memory backend on Lakebase.

> One person with workspace + Unity Catalog admin sets this up once; participants then run the
> notebooks in the browser. Budget ~45 min for first-time setup.

---

## 0 · Prerequisites

| Need | Who grants it | Verify |
|---|---|---|
| Databricks workspace with Lakebase enabled | Platform / account team | `Compute → Lakebase` loads |
| Unity Catalog catalog you can create schemas in (e.g. `az_workshop`) | UC admin | `CREATE SCHEMA` succeeds |
| A SQL warehouse | Workspace admin | `databricks warehouses list` |
| Embedding endpoint `databricks-gte-large-en` (1024-dim) | Usually preconfigured | endpoint is `READY` |
| Generation model `databricks-meta-llama-3-3-70b-instruct` | Confirm access | endpoint is `READY` |
| **Lakebase Search Beta**, module 04 only | Account-team request + admin Previews toggle | **Irreversible per project** |
| `databricks-sdk >= 0.81.0` | Each notebook `%pip` installs it | runtime SDK is older; notebooks upgrade + restart |
| `databricks-feature-engineering >= 0.13.0`, DBR 16.4 LTS ML or serverless, module 05 | `%pip` in notebook |, |

---

## 1 · Create the Lakebase project

UI: **Compute → Lakebase → Go to Lakebase Postgres → New project.** You get a `production`
branch, `8↔16 CU` with scale-to-zero, a `databricks_postgres` database, Postgres 17.

Equivalent CLI:

```bash
# one application project per team
databricks postgres create-project az-<team>-copilot \
  --json '{"spec": {"display_name": "AZ Copilot <team>"}}' --profile <PROFILE>

# dedicated Search demo project, enable Search on THIS one only (module 04)
databricks postgres create-project az-demo-search \
  --json '{"spec": {"display_name": "AZ Search demo"}}' --profile <PROFILE>
```

Read back the ids for the next step:

```bash
databricks postgres list-projects --profile <PROFILE>
databricks postgres list-endpoints projects/<PROJECT_ID>/branches/production --profile <PROFILE>
```

---

## 2 · Edit `workshop/config.py`, the only file you change

Set `project_id`, `search_project_id`, `uc_catalog`, `uc_schema`, `warehouse_id`, `team_id`.
Each also accepts an `LB_*` / `WS_*` env var. Confirm with `cfg.show()`; `cfg.validate()` lists
every problem at once. Nothing here is a secret, DB credentials are minted at run time.

---

## 3 · Generate the data

Run `data/generate_synthetic_pharma.py`. Writes six tables to `<catalog>.<schema>`, enables Change
Data Feed on `hcp_features` + `hcp_interactions`, sets the non-null PK, plants four patterns the
dreaming module needs. Its final cell verifies all of this, **do not proceed on a red assertion.**

---

## 4 · Validate

Run `workshop/00_validate_environment.py`. Ten-plus pass/fail rows; never aborts mid-way, fails at
the end if any row is red. The **DDL canary** proves create/write/read/drop under your own identity, if it fails you have connect-but-not-create; get `USAGE` + `CREATE` on the schema.

---

## 5 · Build, in order

| Step | Notebook | You should see |
|---|---|---|
| M1 | `01_fundamentals.py` | Postgres version over the notebook connection |
| M2 | `02_synced_tables.py` | app schema you own; three mirrored tables; read-only enforced by GRANT |
| M3 | `03_agent_memory_build.py` | recall returns this rep's turns, not another's; one fact with provenance |
| M3b | `03b_agent_memory_dreaming.py` | a Cardiology cross-rep theme after the dream; the wrong claim held in review, not retrievable |
| M4 | `04_search_hybrid.py` | hybrid RRF results; `EXPLAIN` shows the index in use |
| M5 | `05_online_feature_store.py` | keys-only request returns a scored prediction (200) |

---

## 6 · Control-plane gotchas (read before you hit them)

**Synced tables (M2), create from the Catalog UI, not the CLI.**
Catalog Explorer → source table → **Create → Synced table**. Database type = **Autoscaling**; pick
Project, Branch = `production`, DB = `databricks_postgres`; PK auto-detects; Sync mode = On-demand.
Initial sync ~2 to 3 min → **Online**.
- The `postgres create-synced-table` CLI (v1.14.1) does **not** work (rejects the documented body;
  `get-synced-table` → `No API found`). Legacy SDK rejects Autoscaling projects. **Use the UI.**
- Synced tables are **NOT read-only** at the Postgres layer, they are writable partitioned tables.
  Treat read-only as a **governance rule enforced with GRANTs**, not an engine guarantee.

**Search (M4), one-way door.** Request Beta, then project **Settings → Lakebase Search → Enable**
on the **demo project only**. It restarts all compute, drops connections, and **cannot be turned
off.** Then `CREATE EXTENSION lakebase_vector CASCADE;` `CREATE EXTENSION lakebase_text;`. If
`CREATE EXTENSION` fails → check `shared_preload_libraries`.

**Online store (M5), pre-provision.** `create_online_store` provisions its own Lakebase instance
and is slow (too slow to do live). The UC catalog name must equal the online store's backing
Postgres DB name. It does **not** scale to zero, delete it in teardown (`99_teardown.py`).

---

## 7 · Governance defaults (adopt as internal standard)

| Question | Default |
|---|---|
| Who creates projects | Named platform-team group only. Engineers get `CAN_CONNECT_AND_CREATE` on a pre-provisioned project and own schemas inside it. |
| Naming | `az-{team}-{app}-{env}` (RFC-1123: lowercase/digits/hyphen, ≤63). Name the OFS instance too. Schema `copilot_{team}`. |
| Branch lifecycle | 10 unarchived branches/project; TTL required (≤30 days) or `no_expiry`; reset is UI-only; never delete `production`. |
| Scale-down | Idle timeout 30 to 60 min; scale-to-zero on (wake ~100 ms, apps need retry); OAuth DB credentials **expire after 1 hour**, refresh mandatory. |

---

## 8 · If something breaks

| Symptom | Fix |
|---|---|
| `'WorkspaceClient' object has no attribute 'postgres'` | Runtime SDK too old, `%pip install "databricks-sdk>=0.81.0"` + `%restart_python` |
| `no unique or exclusion constraint matching the ON CONFLICT` | Unique index was partial, use a full unique index (already fixed in notebooks) |
| Writes to a synced table succeed | Expected on Autoscaling, enforce read-only with GRANTs |
| `permission denied for schema` (42501) | Connect-but-not-create, need `USAGE` + `CREATE`; deploy app SP before running locally |
| `CREATE EXTENSION lakebase_vector` fails | Check `shared_preload_libraries`; escalate, don't debug live |
| Token expired after ~1 hour | OAuth credential lifetime, refresh |
| Connection refused right after idle | Scale-to-zero wake, retry |
| `Workshop config is not ready …` | Run `cfg.validate()`, fix every listed item |

Full detail: `docs/TROUBLESHOOTING.md`, `docs/RUNBOOK.md`, `docs/GOVERNANCE-CHECKLIST.md`.
