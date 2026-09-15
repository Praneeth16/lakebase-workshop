# Databricks notebook source
# MAGIC %md
# MAGIC # Synthetic pharma dataset generator
# MAGIC
# MAGIC Writes six tables into the Unity Catalog catalog/schema named in `workshop/config.py`.
# MAGIC All data is **synthetic** — invented brand names, generated HCP identities, no PHI, no
# MAGIC production data. Deterministic (fixed seed): every team gets identical data, so a
# MAGIC facilitator can reason about what any team is seeing.
# MAGIC
# MAGIC Run this once before the workshop. It is idempotent — re-running replaces cleanly.
# MAGIC
# MAGIC | Table | Serves |
# MAGIC |---|---|
# MAGIC | `hcp_master` | synced tables, agent context |
# MAGIC | `hcp_interactions` | synced tables, memory seed, search corpus |
# MAGIC | `brand_metrics` | synced tables |
# MAGIC | `medical_content` | Lakebase Search corpus |
# MAGIC | `hcp_features` | Online Feature Store (non-null PK + Change Data Feed) |
# MAGIC | `seed_agent_turns` | Dreaming (module 03b) — carries four planted patterns |

# COMMAND ----------

import random
import datetime as dt

from pyspark.sql import functions as F
from pyspark.sql.types import (
    StructType, StructField, StringType, IntegerType, DoubleType, DateType, TimestampType,
)

# Repo root is on sys.path inside a Databricks Git folder.
import sys
sys.path.insert(0, "..")
from workshop.config import cfg

cfg.show()

CATALOG, SCHEMA = cfg.uc_catalog, cfg.uc_schema
FQ = lambda name: f"{CATALOG}.{SCHEMA}.{name}"  # noqa: E731

# Scale knobs: "lab" is small enough to run fast in a room; "demo" adds realism.
SCALE = cfg.dataset_scale
N_HCP = {"lab": 60, "demo": 300}[SCALE]
N_REPS = {"lab": 12, "demo": 24}[SCALE]
TARGET_TURNS = {"lab": 800, "demo": 4000}[SCALE]

RNG = random.Random(42)  # deterministic
TODAY = dt.date(2026, 9, 16)

print(f"scale={SCALE}  hcps={N_HCP}  reps={N_REPS}  target_turns={TARGET_TURNS}")

# COMMAND ----------

# MAGIC %md ## Ensure catalog/schema
# MAGIC The catalog is assumed to exist (created by the platform team — see the setup checklist).
# MAGIC Only the schema is created here.

# COMMAND ----------

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")
spark.sql(f"USE {CATALOG}.{SCHEMA}")

# COMMAND ----------

# MAGIC %md ## Reference vocabulary (all invented)

# COMMAND ----------

SPECIALTIES = ["Oncology", "Cardiology", "Endocrinology", "Neurology", "Immunology"]
# Invented brand per specialty — close enough to feel real, unmistakably synthetic.
BRAND_BY_SPECIALTY = {
    "Oncology": "ONCOVYX",
    "Cardiology": "CARDIOVYX",
    "Endocrinology": "ENDOVYX",
    "Neurology": "NEUROVYX",
    "Immunology": "IMMUVYX",
}
TERRITORIES = ["IN-North", "IN-South", "IN-West", "IN-East"]
CITIES = ["Chennai", "Bengaluru", "Mumbai", "Delhi", "Hyderabad", "Pune", "Kolkata"]
INSTITUTIONS = ["Apollo", "Fortis", "Manipal", "Narayana", "AIIMS", "Medanta", "Tata Memorial"]
SEGMENTS = ["A", "B", "C"]
CHANNELS = ["in-person", "email", "call", "conference"]
SENTIMENTS = ["positive", "neutral", "negative"]

FIRST = ["Aarav", "Vivaan", "Diya", "Ananya", "Ishaan", "Kabir", "Meera", "Rohan",
         "Saanvi", "Advait", "Priya", "Arjun", "Neha", "Karan", "Riya", "Aditya"]
LAST = ["Rao", "Nair", "Iyer", "Menon", "Reddy", "Gupta", "Sharma", "Patel",
        "Bose", "Chandra", "Kumar", "Verma"]


def hcp_id(i):  return f"HCP-{i:04d}"
def rep_id(i):  return f"REP-{i:03d}"


# COMMAND ----------

# MAGIC %md ## hcp_master

# COMMAND ----------

hcps = []
for i in range(1, N_HCP + 1):
    specialty = SPECIALTIES[i % len(SPECIALTIES)]
    hcps.append((
        hcp_id(i),
        f"Dr. {RNG.choice(FIRST)} {RNG.choice(LAST)}",
        specialty,
        RNG.choice(INSTITUTIONS),
        RNG.choice(CITIES),
        RNG.choice(TERRITORIES),
        RNG.choice(SEGMENTS),
    ))

hcp_schema = StructType([
    StructField("hcp_id", StringType(), False),
    StructField("full_name", StringType(), True),
    StructField("specialty", StringType(), True),
    StructField("institution", StringType(), True),
    StructField("city", StringType(), True),
    StructField("territory", StringType(), True),
    StructField("segment_tier", StringType(), True),
])
(spark.createDataFrame(hcps, hcp_schema)
      .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(FQ("hcp_master")))
print("hcp_master:", spark.table(FQ("hcp_master")).count())

# COMMAND ----------

# MAGIC %md ## hcp_interactions

# COMMAND ----------

TOPICS = ["efficacy data", "dosing", "reimbursement", "safety profile",
          "clinical trial", "patient selection", "formulary", "peer experience"]

interactions = []
k = 0
for i in range(1, N_HCP + 1):
    n = RNG.randint(2, 8)
    for _ in range(n):
        k += 1
        days_ago = RNG.randint(1, 180)
        interactions.append((
            f"INT-{k:05d}",
            hcp_id(i),
            rep_id(RNG.randint(1, N_REPS)),
            TODAY - dt.timedelta(days=days_ago),
            RNG.choice(CHANNELS),
            RNG.choice(TOPICS),
            RNG.choice(SENTIMENTS),
            f"Discussed {RNG.choice(TOPICS)}; follow-up requested.",
        ))

int_schema = StructType([
    StructField("interaction_id", StringType(), False),
    StructField("hcp_id", StringType(), True),
    StructField("rep_id", StringType(), True),
    StructField("interaction_date", DateType(), True),
    StructField("channel", StringType(), True),
    StructField("topic", StringType(), True),
    StructField("sentiment", StringType(), True),
    StructField("notes", StringType(), True),
])
(spark.createDataFrame(interactions, int_schema)
      .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(FQ("hcp_interactions")))
# hcp_interactions is synced to Lakebase in **Triggered** mode (module 02), which requires
# Change Data Feed on the source. Enable it here so the sync does not fail on its prerequisite.
spark.sql(f"ALTER TABLE {FQ('hcp_interactions')} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)")
print("hcp_interactions:", spark.table(FQ("hcp_interactions")).count(), "| CDF set (Triggered sync source)")

# COMMAND ----------

# MAGIC %md ## brand_metrics
# MAGIC NBRx / TRx / share shape echoes real commercial analysis without a real brand.

# COMMAND ----------

REGIONS = ["APAC", "EMEA", "AMER"]
PERIODS = [f"2026-{m:02d}" for m in range(1, 10)]  # Jan–Sep 2026

metrics = []
for brand in BRAND_BY_SPECIALTY.values():
    for region in REGIONS:
        base = RNG.randint(200, 900)
        for p in PERIODS:
            nbrx = base + RNG.randint(-40, 80)
            trx = nbrx * RNG.randint(3, 6)
            metrics.append((brand, p, region, nbrx, trx, round(RNG.uniform(0.05, 0.35), 3)))

bm_schema = StructType([
    StructField("brand", StringType(), True),
    StructField("period", StringType(), True),
    StructField("region", StringType(), True),
    StructField("nbrx", IntegerType(), True),
    StructField("trx", IntegerType(), True),
    StructField("share", DoubleType(), True),
])
(spark.createDataFrame(metrics, bm_schema)
      .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(FQ("brand_metrics")))
print("brand_metrics:", spark.table(FQ("brand_metrics")).count())

# COMMAND ----------

# MAGIC %md ## medical_content — the search corpus
# MAGIC Doc types: label / protocol / FAQ. Chunk lengths kept sensible for embedding.

# COMMAND ----------

content = []
c = 0
CONTENT_TEMPLATES = {
    "label": [
        "{brand} is indicated for adult patients with the approved condition. "
        "Recommended starting dose is administered once daily; adjust per response.",
        "{brand} hepatic impairment: in moderate hepatic impairment, reduce the dose. "
        "No adjustment is required in mild renal impairment.",
        "{brand} contraindications include known hypersensitivity to the active substance. "
        "Monitor for infusion-related reactions during administration.",
    ],
    "protocol": [
        "Protocol {pid}: eligible patients are 18 years or older with confirmed diagnosis. "
        "Primary endpoint is progression-free survival at 12 months.",
        "Protocol {pid}: exclusion criteria include prior systemic therapy within 4 weeks. "
        "Screening includes hepatic and renal panels before enrollment.",
    ],
    "FAQ": [
        "How is {brand} stored? Store at 2-8 degrees Celsius; do not freeze. "
        "Protect from light until administration.",
        "Can {brand} be co-administered with common concomitant medications? "
        "Review the interaction section of the label before combining therapies.",
    ],
}
for specialty, brand in BRAND_BY_SPECIALTY.items():
    for doc_type, templates in CONTENT_TEMPLATES.items():
        for t in templates:
            c += 1
            pid = f"PROTO-{brand[:4]}-{c:03d}"
            content.append((
                f"DOC-{c:04d}", brand, doc_type, f"{doc_type} section {c}",
                t.format(brand=brand, pid=pid),
            ))

mc_schema = StructType([
    StructField("content_id", StringType(), False),
    StructField("brand", StringType(), True),
    StructField("doc_type", StringType(), True),
    StructField("section", StringType(), True),
    StructField("chunk_text", StringType(), True),
])
(spark.createDataFrame(content, mc_schema)
      .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(FQ("medical_content")))
print("medical_content:", spark.table(FQ("medical_content")).count())

# COMMAND ----------

# MAGIC %md ## hcp_features — Online Feature Store source
# MAGIC Non-nullable primary key + Change Data Feed: both prerequisites for publishing to the
# MAGIC online store. Enforced here so nobody discovers the requirement live.

# COMMAND ----------

feat = []
for i in range(1, N_HCP + 1):
    feat.append((
        hcp_id(i),
        RNG.randint(0, 40),                 # engagement_90d
        RNG.randint(1, 180),                # days_since_last_interaction
        round(RNG.uniform(0.0, 1.0), 4),    # propensity_score
        dt.datetime(2026, 9, 15, 3, 0, 0),  # computed_at
    ))

feat_schema = StructType([
    StructField("hcp_id", StringType(), False),
    StructField("engagement_90d", IntegerType(), True),
    StructField("days_since_last_interaction", IntegerType(), True),
    StructField("propensity_score", DoubleType(), True),
    StructField("computed_at", TimestampType(), True),
])
(spark.createDataFrame(feat, feat_schema)
      .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(FQ("hcp_features")))

# Enforce PK + CDF (idempotent: PK add is wrapped because re-adding errors).
spark.sql(f"ALTER TABLE {FQ('hcp_features')} ALTER COLUMN hcp_id SET NOT NULL")
try:
    spark.sql(f"ALTER TABLE {FQ('hcp_features')} ADD CONSTRAINT pk_hcp_features PRIMARY KEY (hcp_id)")
except Exception as e:
    # only an "already exists" is acceptable on a re-run; anything else (permission, table/column
    # not found, table state) is real and must surface, not be swallowed by a broad "exist" match
    if "already exists" not in str(e).lower():
        raise
    print("PK constraint already present — ok")
spark.sql(f"ALTER TABLE {FQ('hcp_features')} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)")
print("hcp_features:", spark.table(FQ("hcp_features")).count(), "| CDF + PK set")

# COMMAND ----------

# MAGIC %md ## seed_agent_turns — the dreaming fuel
# MAGIC ~800 turns across the reps and HCPs, with **four deliberately planted patterns** the
# MAGIC dreaming job (03b) must be able to find. Random text alone would give the abstraction
# MAGIC pass nothing real to surface.

# COMMAND ----------

# Base conversational turns (an MSL copilot answering questions before/after HCP calls).
GENERIC_Q = [
    "What is the latest efficacy data for {brand}?",
    "Any update on the {brand} safety profile?",
    "How should I position {brand} versus standard of care?",
    "What did we last discuss with this HCP?",
]
GENERIC_A = [
    "Summarised the most recent published data and flagged the follow-up.",
    "Provided the labelled safety information and noted monitoring guidance.",
    "Shared the comparative positioning and the peer-experience talking points.",
]

turns = []
t = 0
now = dt.datetime(2026, 9, 15, 9, 0, 0)


def add_turn(session_id, rep, hcp, idx, role, topic, content_text, planted=None):
    global t
    t += 1
    turns.append((
        f"TURN-{t:06d}", session_id, rep, hcp, idx, role, topic, content_text,
        now - dt.timedelta(minutes=t), planted,
    ))


# --- Baseline sessions until we approach the target count --------------------
session_no = 0
while t < TARGET_TURNS - 120:  # leave room for planted patterns
    session_no += 1
    i = RNG.randint(1, N_HCP)
    hcp = hcp_id(i)
    specialty = SPECIALTIES[i % len(SPECIALTIES)]
    brand = BRAND_BY_SPECIALTY[specialty]
    rep = rep_id(RNG.randint(1, N_REPS))
    sid = f"SESS-{session_no:05d}"
    for idx in range(RNG.randint(2, 6)):
        role = "user" if idx % 2 == 0 else "assistant"
        if role == "user":
            add_turn(sid, rep, hcp, idx, role, "general",
                     RNG.choice(GENERIC_Q).format(brand=brand))
        else:
            add_turn(sid, rep, hcp, idx, role, "general", RNG.choice(GENERIC_A))

# --- PATTERN 1: cross-rep theme -------------------------------------------------
# Multiple reps, multiple Cardiology HCPs, all raising CARDIOVYX reimbursement / prior
# authorization. Abstraction pass should surface: "cardiology segment repeatedly raises
# reimbursement for CARDIOVYX" — knowledge no single conversation contains.
cardio_hcps = [hcp_id(i) for i in range(1, N_HCP + 1) if SPECIALTIES[i % len(SPECIALTIES)] == "Cardiology"][:6]
for n, hcp in enumerate(cardio_hcps):
    rep = rep_id((n % N_REPS) + 1)
    sid = f"SESS-P1-{n:03d}"
    add_turn(sid, rep, hcp, 0, "user", "reimbursement",
             "This cardiologist again raised prior authorization and reimbursement hurdles for CARDIOVYX.",
             planted="P1_cross_rep_reimbursement")
    add_turn(sid, rep, hcp, 1, "assistant", "reimbursement",
             "Noted the reimbursement objection; shared the patient-access programme details.",
             planted="P1_cross_rep_reimbursement")

# --- PATTERN 2: per-HCP recurring question -------------------------------------
# One HCP asks about hepatic dosing adjustment at EVERY interaction.
p2_hcp = hcp_id(7)
for s in range(6):
    rep = rep_id(RNG.randint(1, N_REPS))
    sid = f"SESS-P2-{s:03d}"
    add_turn(sid, rep, p2_hcp, 0, "user", "dosing",
             "Once again, this HCP asked specifically about hepatic dosing adjustment for the therapy.",
             planted="P2_recurring_hepatic_dosing")
    add_turn(sid, rep, p2_hcp, 1, "assistant", "dosing",
             "Reiterated the labelled hepatic impairment dose reduction guidance.",
             planted="P2_recurring_hepatic_dosing")

# --- PATTERN 3: contradiction ---------------------------------------------------
# Two turns about the same HCP that directly conflict — both must surface flagged,
# neither silently overwrites the other.
p3_hcp = hcp_id(12)
add_turn("SESS-P3-A", rep_id(3), p3_hcp, 0, "user", "preference",
         "This HCP prefers to be contacted by email only and does not take phone calls.",
         planted="P3_contradiction")
add_turn("SESS-P3-B", rep_id(8), p3_hcp, 0, "user", "preference",
         "This HCP asked us never to email and to reach them by phone only.",
         planted="P3_contradiction")

# --- PATTERN 4: plausible-but-wrong clinical claim ------------------------------
# A single, unverified, high-stakes clinical claim. The dreaming confidence gate should
# route it to the review queue (single source + high-stakes topic) — NOT to the agent.
add_turn("SESS-P4", rep_id(5), hcp_id(3), 0, "user", "interaction_claim",
         "A colleague mentioned ONCOVYX can be safely co-administered with strong CYP3A4 "
         "inhibitors at full dose with no interaction — is that right to tell HCPs?",
         planted="P4_unverified_clinical_claim")

turns_schema = StructType([
    StructField("turn_id", StringType(), False),
    StructField("session_id", StringType(), True),
    StructField("rep_id", StringType(), True),
    StructField("hcp_id", StringType(), True),
    StructField("turn_index", IntegerType(), True),
    StructField("role", StringType(), True),
    StructField("topic", StringType(), True),
    StructField("content", StringType(), True),
    StructField("created_at", TimestampType(), True),
    StructField("planted_pattern", StringType(), True),  # NULL for organic turns
])
(spark.createDataFrame(turns, turns_schema)
      .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(FQ("seed_agent_turns")))
print("seed_agent_turns:", spark.table(FQ("seed_agent_turns")).count())

# COMMAND ----------

# MAGIC %md ## Verification — run before the session
# MAGIC Confirms row counts, no null primary keys, CDF on `hcp_features`, and that all four
# MAGIC planted patterns are present and queryable. A dream that finds nothing is worse than
# MAGIC no dream, so these checks are not optional.

# COMMAND ----------

expected = ["hcp_master", "hcp_interactions", "brand_metrics",
            "medical_content", "hcp_features", "seed_agent_turns"]
for name in expected:
    print(f"{name:20s} rows = {spark.table(FQ(name)).count()}")

# No null PKs
assert spark.table(FQ("hcp_features")).filter(F.col("hcp_id").isNull()).count() == 0, "null PK in hcp_features"
assert spark.table(FQ("hcp_master")).filter(F.col("hcp_id").isNull()).count() == 0, "null PK in hcp_master"

# CDF enabled
props = spark.sql(f"SHOW TBLPROPERTIES {FQ('hcp_features')}").toPandas()
cdf = props.loc[props.key == "delta.enableChangeDataFeed", "value"]
assert not cdf.empty and cdf.iloc[0] == "true", "CDF not enabled on hcp_features"
print("CDF on hcp_features: true")

# No empty content chunks
assert spark.table(FQ("medical_content")).filter(F.length("chunk_text") < 20).count() == 0, "empty chunk"

# All four planted patterns present
planted = (spark.table(FQ("seed_agent_turns"))
                .filter(F.col("planted_pattern").isNotNull())
                .groupBy("planted_pattern").count().toPandas())
print(planted.to_string(index=False))
found = set(planted["planted_pattern"])
for p in ["P1_cross_rep_reimbursement", "P2_recurring_hepatic_dosing",
          "P3_contradiction", "P4_unverified_clinical_claim"]:
    assert p in found, f"planted pattern missing: {p}"
print("all four planted patterns present ✓")
print("dataset ready ✓")
