# Troubleshooting

Keyed to the **literal error string** so it is searchable at the moment of failure. Ctrl-F the
text you see. Dry-run errors get appended here with their exact text, those are the ones
the room will actually hit.

---

### `'WorkspaceClient' object has no attribute 'postgres'`
Found in the dry run (2026-09-14) running the notebooks on serverless. The Databricks
notebook/serverless runtime ships a **databricks-sdk older than 0.81.0**, which predates the
`w.postgres` module the connection helper uses (`w.postgres.get_endpoint` /
`generate_database_credential`). A `%pip install "psycopg[binary]"` alone does not fix it, the
ambient SDK is still the old one.
**Fix (already applied):** every connecting notebook's `%pip` line installs
`"databricks-sdk>=0.81.0"` and then `%restart_python`, so the upgraded SDK with `w.postgres` is
loaded. If you add a new notebook that imports `workshop.lakebase`, include the SDK in its pip line.

### `there is no unique or exclusion constraint matching the ON CONFLICT specification`
Found in the dry run (2026-09-14). An `ON CONFLICT (col)` cannot infer a **partial** unique
index (one defined `... WHERE col IS NOT NULL`) unless the same predicate is repeated in the
statement. The memory modules use `ON CONFLICT (ext_id)` / `ON CONFLICT (source_key)` for
idempotency, so a partial index there breaks every insert on real Postgres.
**Fix (already applied):** the two indexes (`uq_turns_ext`, `uq_facts_source`) are **full** unique
indexes, no `WHERE` predicate. A plain UNIQUE index still allows unlimited NULLs (Postgres treats
NULLs as distinct), so the semantics are identical and `ON CONFLICT (col)` infers it. If you
re-introduce a partial index, either drop the predicate or write
`ON CONFLICT (col) WHERE col IS NOT NULL`.

### Writes to a synced table are NOT rejected (they succeed), expected on Autoscaling
Verified in testing on 2026-09-15. On current **Autoscaling** Lakebase a synced table is a **writable**
partitioned Postgres table: `UPDATE`/`INSERT`/`DELETE` succeed, and a re-sync did not revert them.
This is a **change** from Provisioned-era behavior (which rejected writes). So if you expected an
error and got none, that is correct now. Do not rely on the engine for read-only, enforce it with
`REVOKE INSERT, UPDATE, DELETE ON <synced> FROM <role>`. UC remains the source of truth; a local
write does not propagate back to UC. Module 02 teaches this.

### `create-synced-table` CLI fails / `get-synced-table` → `No API found for GET /postgres/<id>`
Verified in testing on 2026-09-15 with CLI v1.14.1. The `databricks postgres` synced-table subcommands
are broken/in-flux, and the legacy SDK `w.database.create_synced_database_table` rejects Autoscaling
projects with `Database instance is not found`. **Create synced tables from the Catalog UI instead:**
Catalog Explorer → open the source table → **Create → Synced table** (Autoscaling; pick project /
branch / `databricks_postgres`). Verified working end-to-end (Online, rows synced).

### `<ext> must be loaded via shared_preload_libraries` (e.g. `lakebase_vector`)
Verified live: on a project **without Lakebase Search enabled**, `shared_preload_libraries` does
not include `lakebase_vector` / `lakebase_text`, so `CREATE EXTENSION lakebase_vector CASCADE`
fails with this text even though the extension is listed in `pg_available_extensions`.
**Fix:** enable **Lakebase Search** on the project (project **Settings → Lakebase Search →
Enable**, irreversible, adds the libraries and restarts computes), then retry the `CREATE
EXTENSION`. Only ever do this on the dedicated demo project, never a shared team project.

### `permission denied for schema ...` (SQLSTATE 42501)
The schema is owned by a user, not the app service principal. Cause: someone ran the app locally
before deploying it, so their credentials created and own the schema.
**Fix:** deploy the app first so the SP creates and owns the schema. If it already happened, do
**not** drop the schema without checking for data, export first if needed, then drop and
redeploy. In the workshop the app schema is pre-created for each team, so this should not appear;
if it does, you are pointed at the wrong project or branch, re-check `config.py`.

### `No online tables found for required feature tables`
The **number-one** serving failure. The feature table exists offline in UC but was never
published to the online store. Offline is for training; online is for serving; you need **both**.
**Fix:** publish the feature table to the online store (module 05), confirm the online table
reports healthy, then re-issue the request.

### Serving endpoint rejects mixed backend formats
One model's feature tables must share a backend format. Mixing (e.g. an online-store table with a
differently-backed table) is rejected outright.
**Fix:** publish all of a model's feature tables to the same online store backend.

### `... requires Change Data Feed ...` / CDF errors on sync or publish
Triggered and Continuous synced tables, and Triggered/Continuous online-store publishes, require
Change Data Feed on the **source** Delta table.
**Fix:** `ALTER TABLE <t> SET TBLPROPERTIES (delta.enableChangeDataFeed = true)`. The generator
already enables CDF on `hcp_features`; if you built your own source table, enable it there.

### Token expired / auth failure after ~1 hour on a Lakebase connection
OAuth database credentials **expire after 1 hour**. A notebook left idle over lunch will fail on
its next query.
**Fix:** re-generate the credential (the connection helper does this on reconnect). In app/agent
code, refresh the token before it expires, never cache it for the process lifetime.

### Connection refused / timeout right after idle
The endpoint scaled to zero. Wake is ~100 ms but the first connection can be refused.
**Fix:** retry with a short backoff, the connection helper already retries. This is expected
behaviour, not a permission problem.

### `CREATE EXTENSION lakebase_vector` fails (Lakebase Search)
Field-reported: the extension can fail to create unless it is in the project's
`shared_preload_libraries`, which the Previews-page toggle does not always set. Public docs do not
mention this.
**Fix:** check the project's `shared_preload_libraries`; if the Search extensions are absent,
escalate on the Lakebase Search channel. **This is a dry-run gate**, clear it on the demo
project before the session; never debug it live in the room.

### Embedding dimension mismatch on vector insert
The embedding model output dimension does not match the `vector(N)` column. Both
`databricks-gte-large-en` and `databricks-bge-large-en` are **1024-dim**.
**Fix:** define the column as `vector(1024)` and assert the embedding length before insert (module
04 does this explicitly rather than letting Postgres reject the row later).

### `storage_catalog` pipeline failure on synced-table creation
`new_pipeline_spec.storage_catalog` must be a **regular UC catalog**, not the Lakebase catalog, DLT cannot write pipeline event logs to a Postgres-backed schema.
**Fix:** point `storage_catalog` at a normal UC catalog.

### BM25 / vector index build fails or returns nothing
Index built before rows were loaded.
**Fix:** load rows first, then create the index. `EXPLAIN` the query to confirm the index is used, a sequential scan still returns *correct* results, so only latency reveals the mistake.

### DAB `synced_database_tables` resource fails
Deprecated; maps to a legacy API that fails on current Lakebase.
**Fix:** use `databricks postgres create-synced-table` instead. `postgres_synced_tables` DAB
support is not yet available.

### Branch reset has no CLI/API
Reset is **UI-only**. There is no `databricks postgres reset-branch`.
**Fix:** delete-and-recreate from the parent branch, or create a point-in-time branch.

### Autoscaling range rejected
`max − min` cannot exceed **16 CU** on the dynamic range.
**Fix:** narrow the range.

### SSL required
Every Lakebase connection needs `sslmode=require`.

### "Workshop config is not ready ..."
`config.py` has unset or malformed values.
**Fix:** the error lists every problem at once, set each `LB_*`/`WS_*` value or edit `config.py`,
then re-run. Run `python workshop/config.py` (or `cfg.show()` in a notebook) to see resolved
values.

### Lakebase unreachable from an AstraZeneca managed laptop
A recorded account limitation. The lab is **browser-only by design**, everything runs inside
Databricks notebooks where the connection originates in the platform. If you are trying to connect
a local `psql`/Python client and it fails, that is the known limitation, not a misconfiguration, use the notebook path.

### Online Tables appear referenced somewhere
Legacy Online Tables are past their deprecation date, effectively inaccessible, and never reached
GA. The forward path is a **Lakebase-backed online feature store** (module 05). If something in
production still points at Online Tables, that is an incident to triage separately, not a
workshop step.
