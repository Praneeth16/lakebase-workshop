"""Workshop portability contract — the ONE file you edit for your workspace.

Every notebook in ``workshop/`` imports ``cfg`` from here and never inlines an
environment value. Point this at your own Lakebase project + Unity Catalog once,
run the notebooks in order, and the whole workshop reproduces unattended.

Two ways to set a value (env var wins over the inline default):
  1. Edit the default string on the right of each ``env(...)`` call below, or
  2. Export the matching ``LB_*`` / ``WS_*`` environment variable.

Nothing here is a secret — these are resource identifiers, not credentials.
Database credentials are minted at run time via the Databricks CLI/SDK and are
never stored in this repo.

Usage inside a notebook (Databricks Git folder puts the repo root on sys.path):

    from workshop.config import cfg
    cfg.show()          # print resolved values as a table
    cfg.validate()      # fail fast with ALL problems at once, no network calls
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields


def env(name: str, default: str) -> str:
    """Environment variable if set and non-empty, else the inline default."""
    val = os.getenv(name, "")
    return val if val.strip() else default


# RFC 1123 label: lowercase letter start, lowercase/digits/hyphen, 1-63 chars.
_RFC1123 = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
# Unity Catalog identifier: letters/digits/underscore, may contain one dot for schema.
_UC_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# team_id becomes part of Postgres identifiers (e.g. schema copilot_{team_id}) and resource names,
# so constrain it to a safe identifier fragment — this is the boundary check against injection.
_TEAM_ID = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
# serving-endpoint names are interpolated into an ai_query() SQL literal — restrict the charset.
_ENDPOINT = re.compile(r"^[A-Za-z0-9._-]{1,120}$")

_VALID_SCALES = ("lab", "demo")


@dataclass
class Config:
    # --- Databricks connection --------------------------------------------
    profile: str = field(default_factory=lambda: env("LB_PROFILE", "fe-vm-lakebase-praneeth"))

    # --- Lakebase project (copy straight out of `databricks postgres` output)
    project_id: str = field(default_factory=lambda: env("LB_PROJECT_ID", "REPLACE_ME_project_id"))
    branch: str = field(default_factory=lambda: env("LB_BRANCH", "production"))
    endpoint_id: str = field(default_factory=lambda: env("LB_ENDPOINT_ID", "primary"))
    database: str = field(default_factory=lambda: env("LB_DATABASE", "databricks_postgres"))

    # --- Dedicated Lakebase Search demo project (pre-enabled; NEVER a team project)
    search_project_id: str = field(default_factory=lambda: env("LB_SEARCH_PROJECT_ID", "REPLACE_ME_search_project_id"))

    # --- Unity Catalog (synthetic data lands here; also OFS source of truth)
    uc_catalog: str = field(default_factory=lambda: env("WS_UC_CATALOG", "az_workshop"))
    uc_schema: str = field(default_factory=lambda: env("WS_UC_SCHEMA", "lakebase_session"))

    # --- Compute ----------------------------------------------------------
    warehouse_id: str = field(default_factory=lambda: env("WS_WAREHOUSE_ID", "REPLACE_ME_warehouse_id"))
    embedding_endpoint: str = field(default_factory=lambda: env("WS_EMBEDDING_ENDPOINT", "databricks-gte-large-en"))
    generation_endpoint: str = field(default_factory=lambda: env("WS_GENERATION_ENDPOINT", "databricks-meta-llama-3-3-70b-instruct"))

    # --- Workshop knobs ---------------------------------------------------
    team_id: str = field(default_factory=lambda: env("WS_TEAM_ID", "team01"))
    dataset_scale: str = field(default_factory=lambda: env("WS_DATASET_SCALE", "lab"))

    # ---- Derived resource paths (mirror the `databricks postgres` CLI shape)
    @property
    def branch_path(self) -> str:
        return f"projects/{self.project_id}/branches/{self.branch}"

    @property
    def endpoint_path(self) -> str:
        return f"{self.branch_path}/endpoints/{self.endpoint_id}"

    @property
    def database_path(self) -> str:
        return f"{self.branch_path}/databases/{self.database}"

    @property
    def search_endpoint_path(self) -> str:
        return f"projects/{self.search_project_id}/branches/{self.branch}/endpoints/{self.endpoint_id}"

    @property
    def app_schema(self) -> str:
        """Postgres schema the copilot owns inside Lakebase, one per team."""
        return f"copilot_{self.team_id}"

    @property
    def online_store_name(self) -> str:
        """Online Feature Store instance (its OWN Lakebase instance), one per team."""
        return f"az_workshop_online_{self.team_id}"

    @property
    def feature_serving_endpoint(self) -> str:
        """Model-serving endpoint that looks features up from the online store."""
        return f"az-copilot-propensity-{self.team_id}"

    @property
    def uc_path(self) -> str:
        return f"{self.uc_catalog}.{self.uc_schema}"

    def table(self, name: str) -> str:
        """Fully-qualified UC table name."""
        return f"{self.uc_catalog}.{self.uc_schema}.{name}"

    # ---- Validation: no network calls, report EVERY problem at once -------
    def problems(self) -> list[str]:
        errs: list[str] = []

        def unset(v: str) -> bool:
            return (not v) or v.startswith("REPLACE_ME")

        if unset(self.project_id):
            errs.append("project_id is not set — set LB_PROJECT_ID or edit config.py "
                        "(from `databricks postgres list-projects`).")
        elif not _RFC1123.match(self.project_id):
            errs.append(f"project_id '{self.project_id}' is not a valid Lakebase id "
                        "(lowercase letter start; lowercase/digits/hyphen; <=63 chars).")

        if unset(self.search_project_id):
            errs.append("search_project_id is not set — the dedicated Lakebase Search demo project. "
                        "Set LB_SEARCH_PROJECT_ID. (Only needed for module 04.)")
        elif not _RFC1123.match(self.search_project_id):
            errs.append(f"search_project_id '{self.search_project_id}' is not a valid Lakebase id.")

        for name in ("branch", "endpoint_id"):
            val = getattr(self, name)
            if not _RFC1123.match(val):
                errs.append(f"{name} '{val}' is not a valid RFC-1123 id.")

        for name in ("uc_catalog", "uc_schema"):
            val = getattr(self, name)
            if not _UC_IDENT.match(val):
                errs.append(f"{name} '{val}' is not a valid Unity Catalog identifier "
                            "(letters/digits/underscore, no leading digit).")

        if unset(self.warehouse_id):
            errs.append("warehouse_id is not set — set WS_WAREHOUSE_ID "
                        "(from `databricks warehouses list`). Needed to query UC from SQL.")

        if not _TEAM_ID.match(self.team_id):
            errs.append(f"team_id '{self.team_id}' is not a safe identifier — lowercase letter start, "
                        "then lowercase/digits/underscore, <=31 chars (it becomes a Postgres schema name).")

        for name in ("embedding_endpoint", "generation_endpoint"):
            val = getattr(self, name)
            if not val:
                errs.append(f"{name} is empty — needs a serving endpoint name "
                            "(e.g. databricks-gte-large-en for embeddings).")
            elif not _ENDPOINT.match(val):
                errs.append(f"{name} '{val}' has invalid characters — endpoint names are "
                            "letters/digits/dot/underscore/hyphen (it is used in an ai_query SQL literal).")

        if self.dataset_scale not in _VALID_SCALES:
            errs.append(f"dataset_scale '{self.dataset_scale}' invalid — one of {_VALID_SCALES}.")

        return errs

    def validate(self) -> "Config":
        errs = self.problems()
        if errs:
            bullets = "\n".join(f"  - {e}" for e in errs)
            raise ValueError(
                "Workshop config is not ready. Fix every item below "
                "(edit workshop/config.py or export the env var), then re-run:\n" + bullets
            )
        return self

    def show(self) -> None:
        rows = []
        for f in fields(self):
            rows.append((f.name, getattr(self, f.name)))
        derived = [
            ("branch_path", self.branch_path),
            ("endpoint_path", self.endpoint_path),
            ("database_path", self.database_path),
            ("app_schema", self.app_schema),
            ("uc_path", self.uc_path),
        ]
        width = max(len(k) for k, _ in rows + derived)
        print("Workshop configuration (resolved) — no secrets shown")
        print("-" * (width + 40))
        for k, v in rows:
            print(f"{k:<{width}} : {v}")
        print("-- derived " + "-" * (width + 28))
        for k, v in derived:
            print(f"{k:<{width}} : {v}")
        ready = "READY" if not self.problems() else "NOT READY — run cfg.validate() for details"
        print("-" * (width + 40))
        print(f"status: {ready}")


# Module-level singleton every notebook imports.
cfg = Config()


if __name__ == "__main__":
    cfg.show()
    cfg.validate()
