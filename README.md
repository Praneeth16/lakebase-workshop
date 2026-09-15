# Lakebase Field Workshop, Agent Memory, Search & Online Features

A hands-on Databricks workshop that builds **one application**, a *Field Medical Copilot*, on
Lakebase (serverless Postgres), and uses it to teach four kinds of retrieval that look identical
and are not:

| Question the copilot must answer | Capability | This repo |
|---|---|---|
| *What is true right now about this HCP?* | Synced tables (UC → Lakebase) | hands-on |
| *What happened between this rep and this HCP before?* | **Agent memory** (built from scratch) | hands-on core |
| *What does the organisation know that no single conversation contains?* | **Dreaming**, offline memory consolidation | hands-on |
| *What does our content say about this question?* | **Lakebase Search** (in-database vector + keyword) | demo |
| *What does the model need, in milliseconds, to score?* | **Online Feature Store** (Lakebase-backed) | demo |

Everything runs **in the browser, inside Databricks notebooks**, no local Postgres client
required. All data is **synthetic pharma**; there is no PHI, no GxP-controlled data, and nothing
from any production system.

---

## Who this is for and the two ways to use it

- **Attended session.** You join a team, connect to your team's pre-provisioned Lakebase project,
  and build alongside a facilitator. Start at `workshop/00_validate_environment.py`.
- **Unattended replay.** You (an AstraZeneca engineer) reproduce the whole workshop in your own
  workspace after the session. Start with **`docs/RUNBOOK.md`**, which begins one step earlier than
  the session does, it includes creating the Lakebase project the session pre-provisions for you.

Either way, **the only file you edit is `workshop/config.py`.**

---

## Quick start

1. **Clone into a Databricks Git folder** (the repo root goes on `sys.path`, so
   `from workshop.config import cfg` works from any notebook).
2. **Edit `workshop/config.py`**, set your Lakebase project id, Unity Catalog catalog/schema, and
   SQL warehouse id. Every value can also come from a `LB_*` / `WS_*` environment variable.
3. **Generate the data:** run `data/generate_synthetic_pharma.py` once. It writes six tables into
   your configured catalog/schema.
4. **Validate:** run `workshop/00_validate_environment.py`. It prints one pass/fail row per
   prerequisite and **never aborts**, you see every gap at once, not one at a time.
5. **Build in order:** `01` → `02` → `03` → `03b`, then the demos `04` and `05`.
6. **Tear down:** run `workshop/99_teardown.py` so nothing is left billing.

Each module ends with a **verification cell** that prints the shape you should see, so a solo
reader always knows whether a step succeeded or failed silently.

---

## Layout

```
workshop/
  config.py                     THE portability contract, the only file you edit
  00_validate_environment.py    pass/fail table; runs first; blocks nothing
  01_fundamentals.py            hierarchy, connect, first queries, cost posture
  02_synced_tables.py           UC → Lakebase mirror; sync modes; read-only rule
  03_agent_memory_build.py      HANDS-ON CORE: schema → write → recall → scope isolation
  03b_agent_memory_dreaming.py  consolidate → abstract → decay, with a provenance + review gate
  04_search_hybrid.py           DEMO: lakebase_vector + lakebase_text + RRF (dedicated project)
  05_online_feature_store.py    DEMO: publish → serve → keys-only 200
  99_teardown.py                cost control, explicit and safe
  solutions/                    completed 01 to 03b (demos ship code, not solutions)
data/
  generate_synthetic_pharma.py  in-workspace generator, scale-parameterized, deterministic
docs/
  RUNBOOK.md                    the portable tutorial, the replay path
  AGENDA.md                     customer-facing two-day agenda
  AZ-PLATFORM-SETUP-CHECKLIST.md   what the AstraZeneca platform team does before demo day
  GOVERNANCE-CHECKLIST.md       project creation, naming, branch lifecycle, scale-down
  TROUBLESHOOTING.md            keyed to literal error strings
  RUN-OF-SHOW.md                facilitator timings, cut-list, ownership
  screenshots/                  UI walkthrough for the control-plane steps (create project, branch reset, governance settings, Search enable, serving)
```

## Capability status, stated honestly

| Pillar | Status | Note |
|---|---|---|
| Online Feature Store (Lakebase-backed) | **GA** | The only GA pillar of the three. |
| Lakebase Search | **Beta** | Gated (account-team request + admin Previews toggle). **Enabling is irreversible per project**, only ever enabled on a dedicated demo project, never a shared team project. |
| Managed Agent Memory | **Beta** | A Unity Catalog securable, **not** Lakebase-backed. Shown as the contrast to the from-scratch build. |

No latency, recall, throughput or retention figures are quoted anywhere in this repo, none are
published for Search or Agent Memory, and inventing them would be the fastest way to lose a
technical audience.
