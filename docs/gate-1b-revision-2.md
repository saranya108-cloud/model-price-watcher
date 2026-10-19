> Record note (2026-10-19): The specification below is reproduced verbatim as authored, including its historical pending-status wording. Read it with [Normative Amendment 1](gate-1b-normative-amendment-1.md) and the [final Human Chair clarifications and approval provenance](gate-1b-human-chair-clarifications.md). The amendment and final rulings supersede the identified earlier clauses. Implementation remains separately gated.

# GATE 1b — REVISED PROPOSED SPECIFICATION — PENDING ASTRA ACCEPTANCE AND HUMAN CHAIR APPROVAL

**Model Price Watch — Step 1, Gate 1b: Offers and Interpretation Policies (Revision 2)**
Author: Claude (configured `claude-opus-5-5`) · Date: 2026‑10‑19 · Mode: read-only · No implementation authorized

This revision fixes the issues Astra raised in its review (disposition: BLOCK PENDING REVISION). The parts Astra accepted are unchanged: the additive read-side `offers/` package, no persistence, schema v2, no changes to storage, acquisition, scanners or the legacy module, versioned deterministic policies, Cheaper Inference (CI) v2 kept evidence-only, provenance, the split between validity and freshness, overlapping classifications, and observed price changes kept apart from discounts. This document replaces the first proposal entirely.

---

## A. Executive design summary

**Architecture.** Gate 1b adds one new read-side package, `model_price_watcher/offers/`, downstream of storage. It reads retained evidence only through storage's existing public readers. It interprets that evidence through a closed, static set of pure, versioned policies. Nothing it produces is persisted. Every result is recomputed from the evidence, an explicit policy manifest, the identity scheme and an explicit evaluation time.

**Where interpretation lives.** Only in `offers/`. Storage never imports `offers`. `offers` adds nothing to `contracts.py` and adds no validation to storage.

**Evidence flow (four layers)**

| Layer | Object | Location |
|---|---|---|
| 1. Retained source evidence | `SnapshotRecord`, `ObservationRecord` (existing) | `storage.py`, unchanged |
| 2. Normalized pricing observation | `NormalizedObservation` | `offers/policies/*` |
| 3. Interpreted commercial offer | `OfferKey`, `Offer`, `OfferState`, `StateOccurrence` | `offers/reduce.py` |
| 4. Derived results | factual classifications, component comparisons, savings qualifications, observed price changes, deal candidates | `offers/classify.py`, `offers/compare.py`, `offers/changes.py` |

**Boundaries Gate 1b does not cross.**
- **Gate 1c:** no canonical cross-provider model identity.
- **Step 2:** no public command, API or CLI contract.
- **Provider work:** no DeepInfra work.
- **Later, evidence-backed work:** no production curated-rule execution, no recurring-schedule engine, no identity-lineage machinery.

**Why it fits the repository and preserves Gate 1a.** The repository already keeps evidence first: raw rows in `source_record_json`, uninterpreted pricing in `conditions_json`, and meaning derived in read-side modules (`detection`, `advertised_detection`). Gate 1b follows the same pattern. It needs no changes to schema, storage dispatch, contracts, the legacy validator, acquisition or scanners. The existing static boundary tests stay valid without edits, and new tests add the reverse rule that nothing imports `offers`. **No scope-stop condition is triggered.**

---

## B. Repository findings

### B.1 Verified repository facts (read-only inspection, 2026‑10‑19)

**Repository state**
- Local `HEAD` is `d311857d0ad8fbed49bafa9c5bd52ca1b0a1ea1e`. The tracked tree is clean and only `scratch/` is untracked; I neither read nor touched `scratch/`. Astra has confirmed the published `main` separately.
- There is no CLI. `pyproject.toml` requires Python ≥ 3.11 and declares no dependencies or scripts.

**Storage (`storage.py`)**
- Schema v2 has two tables: `snapshots(id, provider, started_at, completed_at, source_url, source_metadata_json)` and `observations(snapshot_id, offering_id, observed_at, input_per_million, output_per_million, conditions_json, source_record_json, advertised_quote_json)`, with primary key `(snapshot_id, offering_id)`.
- Only complete successful snapshots are stored. A successful empty snapshot is a valid absence barrier.
- JSON, Decimal and timestamp encodings are canonical. JSON integers and fractions decode as `Decimal`, so OpenRouter's `total_count` reads back as a `Decimal`.
- `get_successful_history` returns a fully validated tuple, or it raises. It does not stream rows that could be recovered individually.
- Storage checks contract invariants. For the unreserved provider `"openrouter"` it does **not** check normalized columns against the raw row.

**Contracts (`contracts.py`)**
- Records are declarative and frozen. `PROVIDER_RECORDS` contains only Cheaper Inference. The closed `LEGACY_POLICIES` holds the two Revision 3 policy IDs and may only shrink.

**OpenRouter**
- `providers/openrouter.py` normalizes only `pricing.prompt` and `pricing.completion`, from USD per token to USD per 1M tokens by an exact decimal shift.
- Missing, null, malformed, negative or non-finite values become `None` with a diagnostic. That is a legitimate normalization outcome, not corruption.
- Every other pricing key is kept unchanged in `unsupported_pricing`. A `pricing` field that is present but not an object rejects the whole catalog.
- `acquisition.scan_openrouter` stores the source as `SourceMetadata(OPENROUTER_CATALOG_URL, {"acquisition_contract": "openrouter-models-v1", "total_count": n})`, where `n` must equal the row count. Remote empty catalogs are rejected before writing.

**Cheaper Inference**
- `scan_cheaper_inference` writes v2 metadata.
- v1 (`ci-advertised-base-token-v1`) stores an `advertised_quote` only for eligible text rows. The quote basis is `(stream, price_kind, "USD", "per_million_tokens", "text_token", "base", policy, conditions_key)`. `discount_percent` and `reference_*` are not interpreted.
- v2 (`ci-advertised-evidence-only-v2`) prohibits all numeric pricing.
- The retained G1b fixture (97 rows) includes:
  - `pricing_scopes` with `serviceTier` values standard, priority, flex and ultrafast, plus `protocol`, `zdr`, `region` and `pricingVariant`;
  - context-banded `tiers`, plus `makerTiers` and `discount`;
  - `effectiveAt`/`expiresAt` — exactly seven days apart in the row inspected;
  - `zero_data_retention_route` values `discounted`, `rail_only` and `none`.

**Read-side modules**
- `advertised_detection.analyze_advertised_history` is public and pure. It checks adjacent frames for:
  - `TIME_ORDER_RESET`: current start before previous completion, or a non-increasing observed time;
  - `QUOTE_UNSUPPORTED`;
  - `BASIS_CHANGED`;
  - `UNKNOWN_COMPONENT`.

  A `DECREASE` creates an event stamped with the current row's `observed_at`. `UNCHANGED` keeps the earlier event. `INCREASE` or `MIXED` clears it. Absence produces `NO_LONGER_OBSERVED`, and a return produces `RETURN_AFTER_ABSENCE`.
- `detection.compare_component(previous, current)` reports percentage `(previous − current) / previous × 100`. It uses 34-digit half-even precision and reports one of five statuses: `EXACT`, `ROUNDED`, `ZERO_BASE`, `UNKNOWN_PRICE` or `NUMERIC_RANGE`. It also returns a `meaningful` flag based on the old 5% threshold.
- The `detection` "deal" means an observed decrease (`DealStatus`).

**Tests**
- `unittest` with offline fixtures: 13 modules, 297 tests. AST boundary tests fix storage's import set and the legacy module's hash.

**Baseline caveat.** The diagnostic shell has Python 3.10.12 only, below the required ≥ 3.11. The run reported 16 failures and 180 errors. 179 errors were `sqlite3.DatabaseError: not authorized` raised from Python 3.10's `iterdump`. The remaining 16 failures and 1 `AttributeError` were **not individually diagnosed**. No passing baseline is claimed; U0 is a mandatory prerequisite.

### B.2 Design assumptions (proposed, not implemented)

1. Derived results are computed on read and never persisted. Schema v2 is sufficient.
2. Model identity is provider-local and exact. Gate 1b establishes **no** cross-provider or cross-variant equivalence; that belongs to Gate 1c.
3. Curated evidence is a distinct evidence kind in the domain model. Gate 1b ships no production curated loader, merger, rules or execution.
4. Any future provider (e.g. DeepInfra) gets its own storage contract, acquisition and policy in separately reviewed work. Gate 1b aims to accommodate it but does not claim its objects will need no changes.
5. Replay of interpretation uses a pinned code revision (Option A, §D.6).

---

## C. Proposed domain objects

All objects are frozen dataclasses built from exact built-in types, `Decimal` (never `float`) and UTC-aware `datetime`. Collections are canonically sorted tuples. Interpretation never mutates, and never returns references to, the nested dictionaries inside storage records. Values are deep-copied or canonicalized before use.

### C.0 Value availability (separate from origin and role)

`Value[T]` describes **availability only**:

| State | Meaning | Gate 1b emitters |
|---|---|---|
| `KNOWN(v)` | Value established under a policy rule | OpenRouter prompt/completion; CI v1 quote components |
| `MISSING` | An expected field is absent from the record | OpenRouter row without `pricing` or `pricing.prompt` |
| `NULL` | The field is present and null | OpenRouter `prompt: null` |
| `MALFORMED(raw)` | The field is present, but invalid, negative, non-finite or out of range | OpenRouter `"not-money"` |
| `UNSUPPORTED(raw)` | The field is present, but its structure is outside the policy's accepted shape | — (reserved) |
| `UNINTERPRETED(pointer, field_class)` | The field is present, but its meaning is not established | OpenRouter `input_cache_read`, `request`, `discount`; CI v1 `discount_percent`, `reference_*` |
| `UNKNOWN(reason)` | The value was already reduced to unknown upstream, with the reason kept | CI v1 quote component `None` |
| `NOT_REPRESENTED` | No retained field maps to this dimension under this policy; **no claim about the provider** | OpenRouter batch price, service tier |
| `NOT_PUBLISHED` | Source semantics affirmatively establish that the provider does not publish it | — (reserved; no current source qualifies) |
| `UNAVAILABLE` | The source affirmatively states it is not offered | — (reserved) |
| `NOT_APPLICABLE` | Meaningless for the offer | — (reserved) |
| `CONFLICT(values, refs)` | Several evidence items disagree | — (reserved; needs multi-source merging, which is deferred) |

`field_class` for `UNINTERPRETED` is one of:
- `CHARGE_POSSIBLE`: might be an additional charge;
- `TERMS_POSSIBLE`: might change the terms or applicability of other components.

**Field classes may only withhold claims; they never create one.**

**Invariants**
- Only `KNOWN` values take part in arithmetic.
- `KNOWN(0)` requires a field the policy has established as a monetary rate. A zero in an uninterpreted field stays `UNINTERPRETED`.
- Origin (`SOURCE_DECLARED` or `DERIVED(transform)`) is recorded in provenance, never in the availability state.

### C.1 `EvidenceRef`

| Field | Type | Req. | Notes |
|---|---|---|---|
| `kind` | `FEED_OBSERVATION \| CURATED` | ✓ | `CURATED` is designed for future compatibility, not implemented in Gate 1b |
| `snapshot_id`, `offering_id` | int, str | feed | Storage primary key |
| `snapshot_provider` | str | feed | e.g. `openrouter`, `cheaper_inference.public.standard` |
| `source_location` | str | ✓ | |
| `source_contract` | str \| None | ✓ | Contract identifier from metadata |
| `field_path` | str | ✓ | JSON path into `source_record`, `conditions`, the quote, or a normalized column name |
| `observed_at` | datetime | feed | |

Invariant: every ref resolves against evidence that was actually read. Refs are never created after interpretation.

### C.2 Existing identities, named precisely

- **Observation identity** `(snapshot_id, offering_id)`: one row in one snapshot.
- **Source-record key** `(snapshot_provider, source_contract, offering_id)`: the same catalog row across snapshots. Used for continuity, never as offer identity.

### C.3 `ModelRef` (provider-local)

| Field | Type | Req. | Notes |
|---|---|---|---|
| `seller_id` | str | ✓ | |
| `commercial_channel_id` | str | ✓ | |
| `exact_provider_catalog_id` | str | ✓ | Exact suffixes and case kept |
| `display_name` | Value[str] | — | From evidence; never used for identity |

Two `ModelRef`s are the **same model** only when all three key fields are equal. Gate 1b establishes no other equivalence. That includes different sellers, and variant IDs at the same seller such as `x` and `x:free`, or `Example/Model` and `example/model`. Comparisons that would need equivalence return `INSUFFICIENT_EVIDENCE(MODEL_EQUIVALENCE_UNRESOLVED)` (§E.5). **Gate 1b defines comparison mechanics but does not solve cross-provider model identity.**

### C.4 `OfferKey` and `offer_id` (`mpw-offer-identity-v1`)

| Field | Type | Req. | Rule |
|---|---|---|---|
| `scheme` | `"mpw-offer-identity-v1"` | ✓ | |
| `seller_id` | str | ✓ | Commercial seller namespace (`openrouter`, `cheaper_inference`) |
| `commercial_channel_id` | str | ✓ | Commercial channel, not a URL or acquisition contract (`openrouter.api`, `cheaper_inference.api`); source and stream live in provenance |
| `exact_provider_catalog_id` | str | ✓ | Exact |
| `product_discriminator` | `ProductDiscriminator \| None` | ✓ | Present **only** when interpreted evidence establishes several distinct selectable products under one catalog ID |

`ProductDiscriminator`:
- `kind` comes from a closed enum: `EXECUTION_MODE_PRODUCT`, `SERVICE_TIER_PRODUCT`, `PRICING_VARIANT_PRODUCT`, `ROUTE_PRODUCT`, `SUBSCRIPTION_PRODUCT`.
- `value` is a str.
- `rule_id` is the policy rule that established the distinction.

It is not an open map. **No current policy emits a discriminator.** It exists so that evidence showing several products is never collapsed into one. It is exercised only by fictional policies in tests.

`offer_id = "mpwo1:" + sha256(canonical([scheme, seller_id, commercial_channel_id, exact_provider_catalog_id, discriminator or null])).hex`. The full key is kept next to the hash.

**Treatment of commercial dimensions**

| Dimension | Gate 1b treatment |
|---|---|
| seller | Identity |
| commercial channel | Identity, narrowly defined |
| exact catalog ID | Identity |
| execution mode | Applicability of a price group (C.6); identity only through `EXECUTION_MODE_PRODUCT` |
| service tier | Applicability; identity only through `SERVICE_TIER_PRODUCT` |
| pricing variant | Applicability or price components; identity only through `PRICING_VARIANT_PRODUCT` |
| region | Applicability, availability or eligibility |
| ZDR / data retention | Applicability or a service attribute; identity only through `ROUTE_PRODUCT` |
| account class | Eligibility |
| context band | Price-group applicability (piecewise schedule) |
| program or promotion ID | `ProgramReference` in the offer state (C.6) |

**Identity rules**
1. A missing discriminator or attribute means nothing semantically. It does not mean standard, global, no ZDR, realtime or default tier.
2. A new promotion ID, campaign ID, discount percentage, price, set of promotional dates or eligibility, a recurring promotion, or a temporary discount never changes `offer_id`.
3. A program identifier may become a discriminator only when supported source semantics establish that it selects a separately purchasable product, route, subscription or enrollment product.
4. If evidence does not settle whether two programs are distinct products, the evidence is kept and applicability is reported as `PROGRAM_APPLICABILITY_UNRESOLVED`. There is no automatic split and no automatic merge into an unconditional price claim.
5. Raising an interpretation-policy version must never change `offer_id`s. This is enforced by golden identity tests (§I).
6. Correcting identity later — splitting one identity into several, or merging several — needs an explicit, reviewed identity-correction gate. No lineage machinery is built now, and nothing assumes a one-to-one mapping.

### C.5 `PriceComponent`

| Field | Type | Req. | Notes |
|---|---|---|---|
| `dimension` | enum `INPUT_TOKENS, OUTPUT_TOKENS, CACHED_INPUT_READ, CACHE_WRITE, REQUEST, IMAGE, OTHER(str)` | ✓ | |
| `commercial_role` | `LIST \| EFFECTIVE \| REFERENCE_CLAIM` | ✓ | `REFERENCE_CLAIM` is a third-party or maker reference, never the seller's own regular price unless a policy establishes it |
| `amount` | Value[Decimal] | ✓ | |
| `currency` | Value[str] | ✓ | Comes from the established source contract |
| `unit` | Value[BillingUnit] | ✓ | `BillingUnit(kind, quantity)`, e.g. `(token, 1000000)` |
| `provenance` | ProvenanceRecord | ✓ | Records the origin (`SOURCE_DECLARED` or `DERIVED`) |

### C.6 `OfferState` (commercial content) and its parts

- **`PriceGroup`**: `{applicability: Applicability, components: tuple[PriceComponent]}`.
- **`Applicability`**: `execution_mode`, `service_tier`, `pricing_variant`, `context_band`, `region`, `data_retention` and `time_restriction`. Each is a `Value`, with `NOT_REPRESENTED` where the source is silent.
- **`TimeRestriction`**: one of
  - `NO_DECLARED_RESTRICTION`;
  - `BOUNDED(start: Value[datetime], end: Value[datetime], raw_text: Value[str])`;
  - `RECURRING(evidence_pointer)`, kept as a concept only, with evaluation deferred;
  - `RESTRICTION_UNRESOLVED(pointer, reason)`.
- **`ProgramReference`**: `(reference: str, kind: PROMOTION | CAMPAIGN | PRICE_PROGRAM, established_meaning: bool, provenance)`.
- **`Eligibility(audience: Value[str], requirements)`** and **`UsageLimit(metric, limit: Value[Decimal], period: Value[str], provenance)`**.

**`OfferState` fields:** `offer_id`; `price_groups`; `conditions`; `eligibility`; `usage_limits`; `program_references`; `uninterpreted` (pointers plus canonical values); `zero_declared_rates` (factual flag, C.9); `content_digest`.

`content_digest` is the sha256 of all commercial content. That includes commercial timestamps such as declared validity boundaries, and program references. It **excludes** observation and provenance timestamps, snapshot IDs and evidence refs.

### C.7 `StateOccurrence` (history)

| Field | Notes |
|---|---|
| `offer_id`, `occurrence_index` | Ordinal within the offer's history |
| `content_digest` | Non-unique: A → B → A gives three occurrences, the first and third with equal digests |
| `first_snapshot_id`, `first_observed_at`, `last_snapshot_id`, `last_observed_at` | |
| `presence_episode` | Index of the run of adjacent frames in which the offer was present and interpretable |
| `close_reason` | `OPEN`, `CONTENT_CHANGED`, `ABSENT`, `TIME_ORDER_RESET`, `UNINTERPRETABLE` |
| `evidence` | Refs for every supporting observation |

`Offer = {offer_key, offer_id, model_ref, occurrences, current_status: CurrentStatus, last_known_occurrence}`.

`CurrentStatus` is either `CURRENT` or `NOT_CURRENT(reason)`, where the reason is one of:
- `ABSENT_FROM_LATEST`
- `LATEST_ROW_INCONSISTENT`
- `LATEST_SNAPSHOT_INCONSISTENT`
- `LATEST_SNAPSHOT_UNSUPPORTED`
- `LATEST_EVIDENCE_ONLY`
- `STALE`
- `UNVERIFIABLE`
- `NOT_APPLICABLE_NOW`
- `RESTRICTION_UNRESOLVED`

### C.8 `FreshnessAssessment`

`{category: LIVE_FEED | CURATED_TIME_BOUND | FREE_TIER_LIMITS | CURATED_STABLE, basis_kind: OBSERVED | VERIFIED, basis_time: Value[datetime], deadline, cap_source: LIMIT | PUBLISHED_EXPIRY | PUBLISHED_CHANGE_DATE, state: FRESH | STALE | UNVERIFIABLE}`

The curated categories are pure primitives here, tested on synthetic inputs. No curated rule execution exists.

### C.9 Factual classifications and attributes

`Classification {kind, offer_id, occurrence_index, rule_id, rule_version, justification: tuple[EvidenceRef], depends_on: tuple[component or field pointers]}`

`kind` is one of `NORMAL_LIST`, `DISCOUNT`, `PROMOTION`, `TIME_LIMITED`, `FREE` (pending HC‑1), `BATCH`, `SERVICE_TIER`, `TIME_WINDOW`.

`zero_declared_rates` is a factual attribute, not a classification. It records that every established monetary field is `KNOWN(0)`. It is emitted whatever HC‑1 decides.

### C.10 `SavingsQualification`

`{qualifier: DISCOUNT_SAVINGS | PROMOTION_SAVINGS | BATCH_SAVINGS | SERVICE_TIER_SAVINGS | TIME_WINDOW_SAVINGS, candidate: (offer_id, occurrence, price group), reference: (offer_id, occurrence, price group), scope: tuple[dimension], component_results: tuple[ComponentComparison], outcome, conditions, reasons, evaluated_at}`

`outcome` is one of `QUALIFIED`, `QUALIFIED_WITH_CONDITIONS`, `NOT_QUALIFIED`, `INSUFFICIENT_EVIDENCE` or `NON_COMPARABLE`.

### C.11 `ComponentComparison`

`{left, right (offer_id, occurrence, group, dimension, role), status, reasons, conditions, assumptions, delta, percentage, percentage_status, evaluated_at}`

`delta` and `percentage` are present only for `DIRECTLY_COMPARABLE` or `CONDITIONALLY_COMPARABLE`.

### C.12 `ObservedPriceChange`

`{offer_id, from_occurrence, to_occurrence, from_snapshot_id, to_snapshot_id, observed_at, dimension_changes (compare_component results), aggregate, context_changed: bool, label = "observed price change"}`

This is never a classification.

### C.13 `ProvenanceRecord`

`{evidence, origin: SOURCE_DECLARED | DERIVED(transform), policy: PolicyRef, source_policy: str | None, rule_id, assumptions}`

### C.14 `NormalizedObservation` and `SnapshotInterpretation`

- **`NormalizedObservation`**: `{evidence, policy, outcome: INTERPRETED | PARTIAL | EVIDENCE_ONLY(reason) | ROW_INCONSISTENT(reason) | SUPPRESSED_BY_SNAPSHOT(reason), emitted: tuple[(OfferKey, OfferState)]}`. Several entries in `emitted` form an offer family.
- **`SnapshotInterpretation`**: `{snapshot_id, policy | None, outcome: INTERPRETED | PARTIALLY_INTERPRETED(affected refs, reasons) | EVIDENCE_ONLY | UNSUPPORTED_SOURCE | SNAPSHOT_INCONSISTENT(reason), observations}`.

### C.15 `InterpretationReport`

`{identity_scheme, manifest, manifest_digest, input_digest, semantic_digest, evaluated_at, snapshots, offers, classifications, qualifications, changes, deal_candidates, diagnostics}`

### C.16 Curated evidence (designed for future compatibility, not implemented in Gate 1b)

Only the `EvidenceRef.kind = CURATED` tag and the curated freshness categories exist. There is no rule type, loader, merger, data file or execution path. Supersession, withdrawal, applicability and merging stay unspecified until a real provider needs them. A fresh feed observation must never make stale curated evidence current; that principle is recorded for later work.

### C.17 Removed from Gate 1b

`CanonicalModelRef`, `ModelEquivalenceRule`, `CuratedPricingRule`, generic `program_key`, `market_scope`, `TimeWindowSchedule` evaluation and identity lineage.

---

## D. Interpretation architecture

### D.1 Pipeline

```
storage.get_successful_history(provider)        [existing; errors propagate unchanged]
  → frames, ordered (completed_at, id), including empty snapshots
  → registry.match(snapshot) → policy | UNSUPPORTED_SOURCE
  → snapshot-level checks → SNAPSHOT_INCONSISTENT | proceed
  → policy.interpret_row(snapshot, row) → NormalizedObservation   (layer 2)
  → reduce frame by frame → Offers, StateOccurrences, CurrentStatus (layer 3)
  → classify / compare / qualify / changes at `now`               (layer 4)
  → InterpretationReport
```

### D.2 Entry points (internal; not the Step 2 API)

- `offers.interpret.interpret_frames(frames_by_provider, *, now)`: pure.
- `offers.interpret.read_offers(connection, *, providers, now)`: one read transaction on the caller's connection, with the same ownership and rollback rules as `detection.detect_current`. It never goes around storage readers. If storage refuses to return history, the error propagates and nothing is recovered.

### D.3 Registry and matching (closed and static)

`POLICY_MANIFEST` is an exact tuple of bindings. Each binding has a closed set of required fixed fields and declared variable fields.

| Binding | Provider | Location | Fixed metadata | Variable metadata | Policy |
|---|---|---|---|---|---|
| `openrouter.api.v1` | exactly `"openrouter"` | exactly `OPENROUTER_CATALOG_URL` | `acquisition_contract == "openrouter-models-v1"` | `total_count`: integral, ≥ 0, must equal the row count | `mpw-offers-openrouter@1` |
| `ci.public.v1` | exactly `cheaper_inference.public.standard` | exactly `SOURCE_URL` | the full `V1_SOURCE_METADATA` | none | `mpw-offers-ci-v1@1` |
| `ci.public.v2` | as above | exactly `SOURCE_URL` | the full `SOURCE_METADATA` | none | `mpw-offers-ci-v2@1` (evidence-only) |

**Matching rules**
- The metadata key set must equal exactly the fixed keys plus the variable keys.
- Any other key, a different value, a different location or an unknown provider gives `UNSUPPORTED_SOURCE`.
- A `"openrouter"` snapshot written with any other source is unsupported.
- The registry is validated when imported and when called: unique bindings, no overlaps, exact types.
- There is no runtime registration, no plugin and no entry point.

### D.4 Failure scoping (dependency-scoped)

| Inconsistency | Behavior |
|---|---|
| One row's stored normalized value or conditions disagree with re-derivation from its retained raw record, or its `offering_id` differs from the raw ID | `ROW_INCONSISTENT`. Interpretation that depends on that row is suppressed. Other rows in the snapshot carry on, and the snapshot is `PARTIALLY_INTERPRETED` with the affected refs and reasons. |
| Several offers depend on one defective row (an offer family) | The whole family is suppressed. No current policy produces families; tested with a fictional policy. |
| A snapshot-wide binding, completeness or membership assumption fails (e.g. OpenRouter `total_count` not integral, or not equal to the row count) | `SNAPSHOT_INCONSISTENT`. The whole snapshot is suppressed. |
| A storage reader raises | Propagated unchanged |

**Rules**
- Offers whose latest evidence is suppressed become `NOT_CURRENT(LATEST_ROW_INCONSISTENT | LATEST_SNAPSHOT_INCONSISTENT)`. Being present-but-inconsistent is kept distinct from being absent.
- **No fallback** to an older occurrence for the affected offers. Their last-known state stays available, labeled as historical.
- Disagreement is never settled by picking whichever representation looks better.
- Legitimate normalization outcomes are not inconsistencies. A raw value that is missing, null or malformed, stored as `None`, is consistent.
- Exceptions raised inside policy code propagate. They are never turned into "no offers".

### D.5 Frame-by-frame reduction

1. Process every successful snapshot of a provider history in storage order, including empty, unsupported, evidence-only and inconsistent snapshots.
2. For each offer, compare it with the immediately preceding frame. An occurrence is extended only when all of these hold:
   - the offer is present and interpretable in both frames;
   - the frames are adjacent;
   - the time order is valid (current start ≥ previous completion, and observed time strictly increases);
   - `content_digest` is equal.

   Otherwise the occurrence closes with the matching `close_reason`, and a new one opens when the offer is present again.
3. Repeated unchanged observations extend one occurrence. They never create new occurrences.
4. "Current" is based only on the **latest successful snapshot**:
   - latest is empty → every offer is `ABSENT_FROM_LATEST`;
   - latest is unsupported → `LATEST_SNAPSHOT_UNSUPPORTED`;
   - latest is evidence-only → `LATEST_EVIDENCE_ONLY`.

### D.6 Versioning and replay (Option A)

| Token | Meaning |
|---|---|
| Source contract / source policy | From retained metadata (e.g. `ci-advertised-base-token-v1`); recorded in provenance |
| `POLICY_MANIFEST` | Bindings with `(policy_id, version)`, plus the classification-rule manifest, including the HC‑1 rule state |
| `manifest_digest` | sha256 of the canonical manifest; included in every report |
| Identity scheme | `mpw-offer-identity-v1`, separate from policy versions |

- **Replay** means running `interpret_frames` from a pinned package or code revision, which fixes the manifest. Superseded policy versions are **not** selectable at runtime.
- **Cross-version differences** are checked by golden canonical outputs committed alongside tests. They are not checked by loading old policies at runtime.
- Interpretation never detects or reports the code revision itself; the operator records it.

### D.7 Determinism and canonical encoding

- No wall-clock reads (`now` is a parameter), no randomness, no `float`, and a fixed local `Decimal` context.
- Outputs are sorted by `(offer_id, occurrence_index)`.
- **Canonical encoding:** JSON with UTF-8 `ensure_ascii`, sorted keys and no whitespace. Numbers become strings in the same canonical scientific form as storage: trailing zeros removed, `0` as `"0"`, exponent written `e±N`. Timestamps use storage's `YYYY-MM-DDTHH:MM:SS.ffffffZ` form. Null stays `null`.
- **`semantic_digest`** covers every result field that does not depend on `now`. It is byte-identical for identical inputs and manifest.

### D.8 Interpretation-input fingerprint

`input_digest` is the sha256 of the canonical encoding of every retained input that was read. For each requested provider, in storage order, each snapshot contributes:
- its envelope `[id, provider, started_at, completed_at, source_url, canonical source metadata]`, and
- each observation in offering-ID order: `[snapshot_id, offering_id, observed_at, input_per_million, output_per_million, conditions, source_record, advertised_quote or null]`.

Empty and unsupported snapshots are included as envelopes with their observation lists. The digest covers **canonical retained values**, not original transport bytes, which storage does not keep.

### D.9 Unsupported and partial evidence

- An unsupported snapshot is kept as a barrier and produces no offers.
- A partial row produces an offer state whose components have their true availability states. Classifications, comparisons and qualifications that depend on those components are withheld, or return `INSUFFICIENT_EVIDENCE`.

---

## E. Classification, comparison and savings rules

### E.1 Factual classifications (no claim of savings)

| Kind | Required evidence |
|---|---|
| `NORMAL_LIST` | A policy-established listed catalog price (`LIST` role) for the offer. It makes no claim that the price is undiscounted against any reference, and no claim about tier or execution mode beyond what is represented. |
| `DISCOUNT` | Seller or source evidence of a discount on **this offer**: either (a) `LIST` and `EFFECTIVE` declared for the same dimension and group, with effective < list; or (b) a declared discount rate plus a declared basis that the policy establishes as the seller's own regular price. An observed decrease, a cheaper price elsewhere, or a `REFERENCE_CLAIM` is never enough. |
| `PROMOTION` | A `ProgramReference` with `established_meaning = true` and kind `PROMOTION`. An arbitrary program ID, or the expiry of a price version, is not enough. |
| `TIME_LIMITED` | `TimeRestriction = BOUNDED` with `end` `KNOWN` |
| `BATCH` | A price group, or a discriminated product, whose `execution_mode` is `KNOWN(BATCH)` |
| `SERVICE_TIER` | A price group, or a discriminated product, whose `service_tier` is `KNOWN` and not standard |
| `TIME_WINDOW` | A price group whose time restriction is `BOUNDED` or `RECURRING` |
| `FREE` | **PENDING HUMAN CHAIR HC‑1 RULING.** Candidate rule in E.6 |

Classifications form a set on one occurrence of one offer, and overlap is allowed.

### E.2 Component comparison (one named component)

Checks are made in order and **all** reason codes are collected. The status follows a fixed precedence: **NON_COMPARABLE > INSUFFICIENT_EVIDENCE > CONDITIONALLY_COMPARABLE > DIRECTLY_COMPARABLE**.

| Class | Triggers |
|---|---|
| NON_COMPARABLE (hard incompatibility) | `DIMENSION_MISMATCH` (input vs output; cached vs uncached input); `CURRENCY_MISMATCH` (both known, different — no FX); `UNIT_KIND_MISMATCH` (per-token vs per-request) |
| INSUFFICIENT_EVIDENCE | `MODEL_EQUIVALENCE_UNRESOLVED`; a value not `KNOWN` (`VALUE_<state>`); `CURRENCY_UNKNOWN`; `UNIT_UNKNOWN`; `STALE`, `UNVERIFIABLE`, `NOT_APPLICABLE_NOW`, `RESTRICTION_UNRESOLVED`, `NOT_CURRENT`; `TERMS_UNRESOLVED` (a `TERMS_POSSIBLE` uninterpreted field on either side); `APPLICABILITY_UNRESOLVED` (either group's applicability attribute is not `KNOWN` where the two groups' attributes are compared) |
| CONDITIONALLY_COMPARABLE | Known, explicitly stated differences: execution mode, service tier, context band, bounded time window, usage limits, `LIST` vs `EFFECTIVE` role |

**Further rules**
- Unit quantities are normalized only by exact Decimal scaling.
- A `CHARGE_POSSIBLE` uninterpreted field does not block a single-component comparison. It is recorded in `assumptions` as `other_charges_unresolved`.
- A supported component claim reads, for example, "lower input-token rate". It is never "cheaper overall".

### E.3 Savings qualification (economic claim)

**Required scope.** The union of:
- every charge-bearing dimension present in either price group, whether known or unresolved;
- every `CHARGE_POSSIBLE` or `TERMS_POSSIBLE` uninterpreted field on either side;
- the policy's required billable dimensions (`INPUT_TOKENS` and `OUTPUT_TOKENS` for text-token offers).

**Requirements for a positive outcome**
- Every scoped dimension is `KNOWN` on both sides.
- Currency and unit are identical, with exact scaling only.
- No unresolved field exists on either side.
- Both occurrences are `CURRENT`, `FRESH` and applicable now.
- The candidate is ≤ the reference on every scoped dimension and < on at least one.
- The only allowed known difference is the qualifier's defining difference. Any other known, stated condition gives `QUALIFIED_WITH_CONDITIONS` and is listed.

**Outcome precedence:** **NON_COMPARABLE > NOT_QUALIFIED > INSUFFICIENT_EVIDENCE > QUALIFIED_WITH_CONDITIONS > QUALIFIED**.
- `NOT_QUALIFIED` means some scoped dimension is known to be higher, or nothing is lower. A known disqualifier settles the result even when other values are unknown.
- Unknown values block only positive claims.

**Scope by qualifier**

| Qualifier | Candidate | Reference (same `ModelRef`) | Defining difference |
|---|---|---|---|
| `BATCH_SAVINGS` | Group or product with `execution_mode = KNOWN(BATCH)` | Group or product with `execution_mode = KNOWN(REALTIME)` and `service_tier = KNOWN(standard)`. Otherwise `INSUFFICIENT_EVIDENCE(COUNTERPART_APPLICABILITY_UNRESOLVED)` | Execution mode |
| `SERVICE_TIER_SAVINGS` | `service_tier = KNOWN(t ≠ standard)` | Same execution mode (known), `service_tier = KNOWN(standard)` | Service tier |
| `TIME_WINDOW_SAVINGS` | `BOUNDED` window applicable now | Same group's terms with `NO_DECLARED_RESTRICTION` | Time window. `RECURRING` gives `INSUFFICIENT_EVIDENCE(SCHEDULE_EVALUATION_DEFERRED)` |
| `DISCOUNT_SAVINGS` | `EFFECTIVE` components | `LIST` components of the same group and occurrence; both roles required for every scoped dimension | Role |
| `PROMOTION_SAVINGS` | As for discount, plus an established `PROMOTION` reference | As for discount | Role, under the promotion |

Sibling products split by a discriminator count as the same `ModelRef`, because they share one exact catalog ID.

### E.4 Percentage orientation

`savings_percentage = (reference − candidate) / reference × 100`, so a positive value means the candidate is cheaper. It is computed as `detection.compare_component(previous=reference, current=candidate)`. The statuses `EXACT`, `ROUNDED` (34-digit, half-even), `ZERO_BASE`, `UNKNOWN_PRICE` and `NUMERIC_RANGE` are kept exactly as returned. The legacy `meaningful` (5%) flag is ignored by Gate 1b qualification.

### E.5 Model scope in comparisons

- Same `ModelRef`: comparison proceeds.
- Different `ModelRef`: `INSUFFICIENT_EVIDENCE(MODEL_EQUIVALENCE_UNRESOLVED)`, never `NON_COMPARABLE(MODEL_UNRELATED)`. This covers other sellers, other channels, and variant or case-different catalog IDs.

### E.6 FREE — PENDING HUMAN CHAIR HC‑1 RULING

**Candidate rule** (`cls.free.source_scoped@1`, disabled in the manifest until HC‑1). The offer qualifies only if all of these hold:
1. Every required billable monetary dimension of the policy's established pricing scope is present and established. For OpenRouter v1 that is `prompt` and `completion`.
2. Each of those is `KNOWN(0)` and source-declared.
3. There is no known non-zero relevant charge.
4. There is no uninterpreted field of either class. Any present uninterpreted pricing key blocks FREE.
5. Missing, null, malformed, unknown and uninterpreted values never count as zero.
6. Usage limits stay labeled "not represented in source". FREE does not mean unlimited service.
7. The FREE label states its source scope, for example "free under OpenRouter catalog prompt and completion pricing". It does not imply that external costs are absent.

**Consequence to note.** Real OpenRouter `:free` rows that carry additional pricing keys (`request`, `image` and so on) will **not** qualify under policy v1, even when those values are `"0"`. The old "unit-invariant zero" shortcut is removed. If HC‑1 is declined, only the factual `zero_declared_rates` attribute is emitted.

Either outcome is a manifest setting. Neither touches identity, storage, acquisition or schema. CI v1 is excluded either way; its accepted wording remains "zero advertised base token rates; other charges or conditions may apply".

### E.7 Deal candidates (`/deals` semantics only; no Step 2 contract)

- There is one entry per `offer_id`, and only for offers whose current occurrence is `CURRENT`, `FRESH` and applicable now.
- An offer qualifies if it has at least one savings qualification with outcome `QUALIFIED` or `QUALIFIED_WITH_CONDITIONS`, or `FREE` once HC‑1 is approved.
- An offer can also appear because of an `ObservedPriceChange` decrease with `context_changed = false`. Such decreases are attached to the same entry under a separate label.
- Factual classifications alone (e.g. `BATCH`) never make an offer a deal candidate.

### E.8 Observed price changes stay distinct

Changes are emitted only between adjacent occurrences inside one presence episode, with both states interpretable and a valid time order. `context_changed` is true when uninterpreted values or conditions changed; such changes are not eligible as deal candidates. Changes never become `DISCOUNT`.

- **CI v1:** follow the accepted transition table exactly (§G.2).
- **OpenRouter:** events are neutral and document that they are **not** the legacy `DealStatus`. Legacy `detection` stays authoritative for its existing APIs.

---

## F. Temporal and freshness rules

### F.1 Two independent axes

- **Freshness** asks: *is MPW's evidence recent enough to support a current claim?* It looks only at the age of the evidence.
- **Commercial applicability** asks: *does the source-supported offer apply at `now`?* It looks only at the source's commercial conditions.

Neither axis reads the other. Being "current" needs both.

### F.2 Applicability states (instants in UTC, microsecond precision, half-open `[start, end)`)

| `TimeRestriction` | Evaluation |
|---|---|
| `NO_DECLARED_RESTRICTION` | Applicable. This is not a claim that the offer is permanent. |
| `BOUNDED`, both bounds `KNOWN`, `start < end` | `NOT_YET_APPLICABLE` if `now < start`; `APPLICABLE` if `start ≤ now < end`; `EXPIRED` if `now ≥ end`. No grace period. |
| `BOUNDED` with only `end` declared (`start` absent) | `APPLICABLE` if `now < end`, otherwise `EXPIRED` |
| A declared boundary that is `MALFORMED`, `NULL`-where-required, `UNKNOWN` or `CONFLICT`, or with `end ≤ start` | `RESTRICTION_UNRESOLVED`: never treated as satisfied, and not current |
| `RECURRING` | `SCHEDULE_UNEVALUATED`: not current-actionable. Evaluation is deferred. |

### F.3 Freshness (matches ruling D exactly)

| Category | Basis | Deadline | Published cap? |
|---|---|---|---|
| `LIVE_FEED` | `observed_at` of the offer's row in the latest successful snapshot | basis + 24h | **No.** For feeds, a published expiry affects applicability only. |
| `CURATED_TIME_BOUND` | `verified_at` | min(basis + 24h, published expiry) | Yes |
| `FREE_TIER_LIMITS` | `verified_at` (curated) or `observed_at` (feed) | min(basis + 7d, published expiry or change date) | Yes |
| `CURATED_STABLE` | `verified_at` | min(basis + 7d, published expiry or change date) | Yes |

**Rules**
- `FRESH` iff `now < deadline`, so evidence is stale exactly at the deadline.
- A basis time that is not known gives `UNVERIFIABLE`.
- A classification is fresh only if every evidence item it depends on is fresh.

**A failed refresh cannot extend freshness.** Failed scans are never persisted, so the basis stays the latest *successful* observation. Curated reverification is outside Gate 1b. A later successful snapshot that omits the offer ends its current status immediately. An empty snapshot does the same.

### F.4 Current, stale and historical

- **Current** = `CURRENT` status: latest snapshot present and interpretable, `FRESH`, and applicable (`APPLICABLE` or `NO_DECLARED_RESTRICTION`).
- **Stale but applicable** = `NOT_CURRENT(STALE)`. The offer is shown only as labeled last-known information; staleness never establishes expiry.
- **Historical** = every occurrence other than the current one.
- Evaluation requires an aware `now` that is not earlier than the latest `completed_at`; otherwise `ValueError` is raised.

---

## G. Provider projection strategy

### G.1 OpenRouter (`mpw-offers-openrouter@1`; binding in D.3)

| Element | Projection |
|---|---|
| Identity | `(openrouter, openrouter.api, exact offering_id, None)`. Suffixes such as `:free`, `:extended` and case variants are distinct offers; suffixes are never parsed. |
| One `PriceGroup` | Applicability attributes all `NOT_REPRESENTED`; time restriction `NO_DECLARED_RESTRICTION` (no time fields are established under v1) |
| `INPUT_TOKENS`, `OUTPUT_TOKENS` | `LIST`, USD per 1M (accepted Tranche A contract) |
| Raw prompt/completion states | `KNOWN`, `MISSING`, `NULL` or `MALFORMED`, read from the raw row |
| Other pricing keys | `UNINTERPRETED`. Scalars are `CHARGE_POSSIBLE`; structured values and the key `discount` (any shape) are `TERMS_POSSIBLE`. **Never** `NOT_PUBLISHED`. |
| Batch, tier, region, ZDR, limits, eligibility | `NOT_REPRESENTED` |
| Freshness | `LIVE_FEED` |
| Classifications | `NORMAL_LIST`; `FREE` per HC‑1; attribute `zero_declared_rates` |
| Savings qualifications | None reachable today: no batch, tier, window or `EFFECTIVE` evidence |

**Row consistency check (U4 implementation detail, subject to semantic review).** Re-derive normalized values and `unsupported_pricing` from the retained raw row using the accepted OpenRouter normalization: the public `parse_catalog` applied to a single-row catalog rebuilt by the canonical encoder. Compare by Decimal value equality.
- Disagreement → `ROW_INCONSISTENT`.
- A malformed raw value stored as `None` → consistent.

**Snapshot check.** `total_count` must be integral, ≥ 0 and equal to the row count; otherwise `SNAPSHOT_INCONSISTENT`.

**Residual note.** If OpenRouter rows carry top-level fields with time meaning, v1 does not interpret them. A later policy version would have to.

### G.2 Cheaper Inference v1 (`mpw-offers-ci-v1@1`)

| Element | Projection |
|---|---|
| Identity | `(cheaper_inference, cheaper_inference.api, exact offering_id, None)`. The stream and contract go in provenance. |
| Rows with a quote | `INPUT_TOKENS` and `OUTPUT_TOKENS` with role `LIST` from the quote (USD, per 1M); component `None` becomes `UNKNOWN(quote_component_unknown)`. Quote tier `base`, `price_kind` and `conditions_key` become conditions. A `conditions_key` change changes `content_digest` (a new occurrence). |
| Rows without a quote | `EVIDENCE_ONLY` with the retained reason |
| `discount_percent`, `reference_*` | `UNINTERPRETED` (`TERMS_POSSIBLE`); no `DISCOUNT` |
| `available_until` | Absent → `NO_DECLARED_RESTRICTION`. Non-null → `RESTRICTION_UNRESOLVED`. Null → `NO_DECLARED_RESTRICTION`, recorded as an explicit policy assumption (J‑4). |
| Zero rates | Attribute `zero_declared_rates` with the accepted label; never `FREE` |
| Freshness | `LIVE_FEED` on `observed_at`; upstream freshness is never claimed (`upstream_freshness: unknown`) |
| Provenance | `source_policy = ci-advertised-base-token-v1` plus the MPW policy |

**Current status** follows from presence, barriers, applicability and freshness. It is not assumed historical. If a database's latest stream snapshot is a fresh v1 snapshot, v1 offers can be current. If a later v2 snapshot exists, v1 offers become `NOT_CURRENT(LATEST_EVIDENCE_ONLY)`.

**Change compatibility.**
- The **current** decrease event for each CI v1 offer is *projected* from `advertised_detection.analyze_advertised_history`, which is public and pure, so the accepted logic is reused rather than rewritten.
- The **historical** change list from the offers reducer follows the same transition table (B.1).
- A differential test over **every prefix** of the fixture histories requires per-offering equality with the legacy reducer: presence, reset reasons, event snapshot IDs, `observed_at`, and quotes.

### G.3 Cheaper Inference v2 (`mpw-offers-ci-v2@1`, `EVIDENCE_ONLY`; unchanged posture)

Every row is `EVIDENCE_ONLY(numeric_scope_semantics_unestablished)`. No offer, classification, comparison or `ModelRef` claim is produced. A v2 latest snapshot is a barrier.

Gate 1b does not interpret any of the following: scope rates, service tiers, `discount`, `makerTiers`/reference fields, ZDR values or routes, `effectiveAt`/`expiresAt`, context bands, or the relation between top-level and scope prices. Interpreting them needs new semantic evidence and separate authorization.

### G.4 Future providers (illustrative compatibility only)

A provider such as DeepInfra might use:
- declared list and effective prices → `LIST` and `EFFECTIVE` roles;
- cached input → `CACHED_INPUT_READ`;
- tiers or batch → `PriceGroup.applicability`, or a `ProductDiscriminator` where separate products are established;
- curated batch evidence → `EvidenceRef.kind = CURATED`, once curated execution is designed.

Its storage contract, acquisition, policy and any domain-object changes belong to that provider's own reviewed work. **Gate 1b does not claim that no domain-object changes will be needed.**

---

## H. Implementation boundaries

**Add (interpretation and tests only; no effect on storage, schema, acquisition or CLI)**

| File | Responsibility |
|---|---|
| `offers/__init__.py` | Package marker; no public re-export surface |
| `offers/values.py` | Value availability, provenance, `EvidenceRef` |
| `offers/model.py` | C.3–C.15 types |
| `offers/canonical.py` | Canonical encoding and digests (does not import storage's private helpers) |
| `offers/identity.py` | Identity scheme v1, `offer_id`, `content_digest` |
| `offers/temporal.py` | Applicability and freshness primitives (no recurrence engine) |
| `offers/registry.py` | `POLICY_MANIFEST`, matching, `manifest_digest` |
| `offers/policies/openrouter.py`, `ci_v1.py`, `ci_v2.py` | Pure policies |
| `offers/reduce.py` | Frame-by-frame reduction, occurrences, current status, failure scoping |
| `offers/interpret.py` | Entry points, `input_digest`, report |
| `offers/classify.py`, `compare.py`, `changes.py` | Layer 4 |
| `tests/test_offer_*.py`, `tests/fixtures/offers_*.json` | New tests and fixtures, including fictional-provider fixtures |

**Allowed imports for `offers`:**
- `models`;
- storage's public readers and record types;
- `detection.SnapshotFrame` and `compare_component`;
- `advertised_detection.analyze_advertised_history`;
- Cheaper Inference constants (through the shim);
- `providers.openrouter.parse_catalog`;
- `acquisition.OPENROUTER_CATALOG_URL`.

The literal `"openrouter-models-v1"` is owned by the policy and pinned against `acquisition._CONTRACT` by a test.

**Modify**

| File | Why | Why there | Affects | Risk |
|---|---|---|---|---|
| `README.md` (U8 only) | Accurately describe what the offers layer claims and does not claim | User-facing accuracy | Docs | Low; lightweight review |

**Leave untouched:** `storage.py`, `contracts.py`, `legacy_cheaper_inference.py`, `models.py`, `providers/*`, `acquisition.py`, `cheaper_inference_acquisition.py`, `transport.py`, `detection.py`, `advertised_detection.py`, `queries.py`, `advertised_queries.py`, `tools/`, `docs/`, all existing tests and fixtures, and `scratch/`.

---

## I. Test and acceptance matrix

**Gate boundary**

| ID | Case | Expected |
|---|---|---|
| GB‑1 | AST scan of `offers/` | No `CanonicalModelRef`, `ModelEquivalenceRule`, curated loader or rules file, or schedule engine |
| GB‑2 | Comparison across sellers, and across `x` vs `x:free` | `INSUFFICIENT_EVIDENCE(MODEL_EQUIVALENCE_UNRESOLVED)`; never `MODEL_UNRELATED` |
| GB‑3 | Nothing outside `offers/` imports it; `offers` makes no transport, scan or clock calls | Pass |
| GB‑4 | Existing `StaticBoundaryTests` unchanged | Pass |

**Offer identity**

| ID | Case | Expected |
|---|---|---|
| OI‑1 | Repeated unchanged scans | Same `offer_id`; one occurrence extended |
| OI‑2 | Price change | Same `offer_id`; new occurrence |
| OI‑3 | Promotion ID changes (fictional state) | Same `offer_id`; new occurrence |
| OI‑4 | Campaign dates change | Same `offer_id`; new occurrence |
| OI‑5 | Context-band change alone | Same `offer_id` |
| OI‑6 | Account-class change | Eligibility revision; same `offer_id` |
| OI‑7 | Fictional policy emits two established discriminators | Two `offer_id`s |
| OI‑8 | Program distinction unresolved | `PROGRAM_APPLICABILITY_UNRESOLVED`; no split, no unconditional merged price |
| OI‑9 | Policy version bump (golden identities) | IDs unchanged |
| OI‑10 | Snapshot IDs and timestamps perturbed | IDs unchanged |
| OI‑11 | Missing attribute | Not encoded as standard, global, no-ZDR or realtime |

**History**

| ID | Case | Expected |
|---|---|---|
| HI‑1 | A → B → A | Three occurrences; the first and third share a digest but have distinct indexes |
| HI‑2 | A, A, A | One occurrence; no extra revisions |
| HI‑3 | Successful empty snapshot appended | `input_digest` changes; all offers `ABSENT_FROM_LATEST` |
| HI‑4 | Unsupported snapshot appended | `LATEST_SNAPSHOT_UNSUPPORTED`; no fallback |
| HI‑5 | Return after absence | New presence episode; same `offer_id` |
| HI‑6 | Time-order regression | Occurrence closed with `TIME_ORDER_RESET` |

**Inconsistency scope**

| ID | Case | Expected |
|---|---|---|
| IS‑1 | One OpenRouter row with a column that disagrees with its raw record | That row is `ROW_INCONSISTENT`; other rows interpreted; snapshot `PARTIALLY_INTERPRETED` with refs |
| IS‑2 | Malformed raw value stored as `None` | Consistent; `MALFORMED` |
| IS‑3 | `total_count` ≠ row count, or not integral | `SNAPSHOT_INCONSISTENT` |
| IS‑4 | Affected offer had an older valid occurrence | `NOT_CURRENT(LATEST_ROW_INCONSISTENT)`; no stale fallback |
| IS‑5 | Fictional family policy, shared row defective | Whole family suppressed |
| IS‑6 | Storage reader raises | Propagated unchanged |
| IS‑7 | Extra metadata key, or different URL | `UNSUPPORTED_SOURCE` |

**Comparison and savings**

| ID | Case | Expected |
|---|---|---|
| CS‑1 | Same model, known input values | `DIRECTLY_COMPARABLE`; delta and percentage oriented per E.4 |
| CS‑2 | Lower input rate, output unknown | Component claim supported; savings `INSUFFICIENT_EVIDENCE` |
| CS‑3 | Unresolved request fee (`CHARGE_POSSIBLE`) | Component comparison OK with assumption noted; savings `INSUFFICIENT_EVIDENCE` |
| CS‑4 | `TERMS_POSSIBLE` field present | Component comparison `INSUFFICIENT_EVIDENCE(TERMS_UNRESOLVED)` |
| CS‑5 | Known batch vs realtime difference | `CONDITIONALLY_COMPARABLE` |
| CS‑6 | Batch group present, counterpart applicability `NOT_REPRESENTED` | `BATCH` classification present; `BATCH_SAVINGS` `INSUFFICIENT_EVIDENCE(COUNTERPART_APPLICABILITY_UNRESOLVED)` |
| CS‑7 | Batch cheaper on input, dearer on output | `NOT_QUALIFIED` (wins over unknown values) |
| CS‑8 | Currency mismatch plus unknown value | `NON_COMPARABLE` (precedence), with both reasons listed |
| CS‑9 | Per-request vs per-token; cached vs uncached input | `NON_COMPARABLE` |
| CS‑10 | Percentage statuses | `ZERO_BASE`, `ROUNDED` and `NUMERIC_RANGE` kept |
| CS‑11 | Recurring window | `INSUFFICIENT_EVIDENCE(SCHEDULE_EVALUATION_DEFERRED)` |

**Temporal**

| ID | Case | Expected |
|---|---|---|
| TP‑1 | `NO_DECLARED_RESTRICTION` vs malformed boundary | Applicable vs `RESTRICTION_UNRESOLVED` |
| TP‑2 | `now` = start − 1µs, start, end − 1µs, end | `NOT_YET_APPLICABLE`, `APPLICABLE`, `APPLICABLE`, `EXPIRED` |
| TP‑3 | Feed at deadline − 1µs, at deadline | `FRESH`, `STALE` |
| TP‑4 | Stale but applicable | `NOT_CURRENT(STALE)`; applicability unchanged |
| TP‑5 | Curated categories (synthetic basis) | Deadline capped by published expiry or change date; `LIVE_FEED` not capped |
| TP‑6 | No new successful snapshot | Deadline unchanged |
| TP‑7 | Naive `now`, or `now` < latest completion | `ValueError` |

**Provenance and fingerprint**

| ID | Case | Expected |
|---|---|---|
| PF‑1 | Every derived value | Refs resolve to rows read; `policy` and `source_policy` present |
| PF‑2 | Empty snapshot added | `input_digest` changes |
| PF‑3 | Normalized column changed, raw record unchanged | `input_digest` changes |
| PF‑4 | Source metadata changed | `input_digest` changes |
| PF‑5 | Same inputs, varied ambient Decimal context and input order | Identical `semantic_digest` |
| PF‑6 | Transport whitespace | Not testable (not retained); documented |
| PF‑7 | Manifest changed (e.g. HC‑1 rule state) | `manifest_digest` changes |

**Provider projections**

| ID | Case | Expected |
|---|---|---|
| PR‑1 | OpenRouter fixture rows | `example/model`: `NORMAL_LIST` plus uninterpreted extras (`discount` is `TERMS_POSSIBLE`). `:free`: zero attribute. `:extended`: `NULL`/`MISSING`. Case variant: distinct. No `pricing` field: `MISSING`. |
| PR‑2 | CI v1 prefix differential vs `analyze_advertised_history` | Equal per offering at every prefix |
| PR‑3 | CI v1 `BASIS_CHANGED` / `QUOTE_UNSUPPORTED` | New occurrence; event cleared, as legacy |
| PR‑4 | CI v1 latest and fresh | Can be `CURRENT` |
| PR‑5 | CI v1 then v2 | `LATEST_EVIDENCE_ONLY` |
| PR‑6 | G1b fixture under v2 | 97 `EVIDENCE_ONLY`, zero offers |
| PR‑7 | CI v1 `discount_percent` | No `DISCOUNT` |
| PR‑8 | CI v1 zero rates | No `FREE` |

**FREE (pending HC‑1; run with the rule enabled in a test manifest)**

| ID | Case | Expected |
|---|---|---|
| FR‑1 | Established prompt and completion `KNOWN(0)`, no other keys | `FREE`, source-scoped; limits "not represented" |
| FR‑2 | Zeros plus `request: "0"` (uninterpreted) | Not `FREE`; `zero_declared_rates` only |
| FR‑3 | `prompt: null` with `completion: 0` | Not `FREE` |
| FR‑4 | Rule disabled (default manifest) | No `FREE` anywhere |

**Regression.** All 13 existing modules (297 tests) stay unmodified and green on Python ≥ 3.11.

**Removed from the matrix:** curated execution tests, recurrence and DST tests, and equivalence-rule tests.

---

## J. Risks and unresolved decisions

| # | Issue | Why it matters | Options | Recommendation | Astra? | Chair? |
|---|---|---|---|---|---|---|
| J‑1 | HC‑1 FREE semantics | `/free` meaning | Enable the candidate rule (E.6), or keep the zero attribute only | Enable the source-scoped rule. Many real `:free` rows will still not qualify under v1 (E.6). | Technical soundness assessed | **Yes** |
| J‑2 | OpenRouter extra pricing keys (`input_cache_read`, etc.) | They limit cached-input data and FREE coverage | Interpret in a later policy version after semantic review | Defer; `UNINTERPRETED` in v1 | Yes | No |
| J‑3 | OpenRouter neutral events vs legacy `DealStatus` | Two "decrease" notions | Keep them distinct and documented | Keep distinct; legacy authoritative for its APIs | Yes | No |
| J‑4 | CI v1 `available_until: null` read as no declared restriction | A stated assumption | Accept; or treat as `RESTRICTION_UNRESOLVED` | Accept as recorded assumption (v1 fixtures omit the field; historical data only) | Yes | No |
| J‑5 | Older `"openrouter"` snapshots without the exact metadata | They become unsupported | Fail closed, or allow-list specific older shapes | Fail closed | Yes | No |
| J‑6 | `TERMS_POSSIBLE` keyed by structure plus the `discount` key | Conservative heuristic | Accept (withhold-only) | Accept | Yes | No |
| J‑7 | Baseline not reproduced; 17 failures undiagnosed | Acceptance relies on a green suite | — | U0 before any implementation | — | No |

No product ruling is reopened.

---

## K. Proposed implementation sequence

| Unit | Scope | Likely files | Prereqs | Completion criteria | Tests | Review |
|---|---|---|---|---|---|---|
| U0 | Baseline confirmation (no code) | — | Python ≥ 3.11 environment | 297/297 green at `d311857`; any failure diagnosed | Existing suite | Human Chair note |
| U1 | Value states, provenance, `EvidenceRef`, minimal `OfferKey`, `content_digest`, canonical encoding | `values.py`, `model.py`, `identity.py`, `canonical.py` | U0 | OI‑9…OI‑11 scaffolding; encoding golden tests | `test_offer_identity.py` | Astra architectural |
| U2 | Applicability and freshness primitives (no recurrence) | `temporal.py` | U1 | TP‑1…TP‑7 | `test_offer_temporal.py` | Astra focused |
| U3 | Registry and manifest, matching, frame-by-frame reducer, occurrences, current status, dependency-scoped failures, `input_digest`/`manifest_digest`, CI v2 evidence-only | `registry.py`, `reduce.py`, `interpret.py`, `policies/ci_v2.py` | U1, U2 | HI‑1…HI‑6, IS‑5…IS‑7, PF‑2…PF‑5, PF‑7, PR‑6, GB‑1, GB‑3, GB‑4 | `test_offer_pipeline.py`, `test_offer_boundaries.py` | Astra architectural |
| U4 | OpenRouter policy, including row and snapshot consistency checks; FREE rule wired but disabled | `policies/openrouter.py` | U3 | PR‑1, IS‑1…IS‑4, FR‑2…FR‑4; FR‑1 under test manifest | `test_offer_openrouter.py` | Provider-adapter semantic review; FREE enablement needs the HC‑1 ruling |
| U5 | CI v1 historical policy with legacy event projection and prefix differential | `policies/ci_v1.py` | U3 | PR‑2…PR‑5, PR‑7, PR‑8 | `test_offer_ci_v1.py` | Provider-adapter semantic review |
| U6 | Factual classifications, component comparison, savings qualification, observed changes, deal candidates | `classify.py`, `compare.py`, `changes.py` | U4, U5 | CS‑1…CS‑11, OI‑1…OI‑8, GB‑2, PF‑1 | `test_offer_classify.py`, `test_offer_compare.py` | Astra focused semantic |
| U7 | End-to-end acceptance and boundary audit (no new behavior) | tests only | U6 | Full matrix plus 297 regression green | All | Astra focused |
| U8 | README status of the offers layer | `README.md` | U7 accepted | Claims match behavior | — | Lightweight accuracy |

**Excluded from every unit:** production curated execution, Gate 1c equivalence, recurrence engine, lineage machinery, DeepInfra work and the Step 2 interface. Any unit that finds it needs a storage, contract, schema, migration or acquisition change stops and goes back to the Human Chair.

---

## Scope Deferrals (explicit)

| Deferred item | Destination |
|---|---|
| Canonical cross-provider model identity, `ModelEquivalenceRule`, provider-to-provider equivalence, canonical model registry | **Gate 1c** |
| Public command contracts, API freeze, CLI rendering, Hermes consumer interface | **Step 2** |
| DeepInfra acquisition, storage contract, semantic interpretation, curated batch pricing | **Provider-specific work** |
| Production curated-rule execution, recurring commercial schedule engine, executable identity-lineage machinery | **Later evidence-backed work** |

---

## Corrective Change Summary

| Astra finding | Corrective action | Sections changed | Status |
|---|---|---|---|
| Q1: Cross-provider equivalence belongs to Gate 1c | Removed `CanonicalModelRef` and `ModelEquivalenceRule`. `ModelRef` is provider-local and exact. Cross-model comparison gives `INSUFFICIENT_EVIDENCE(MODEL_EQUIVALENCE_UNRESOLVED)`. | A, B.2, C.3, C.17, E.5, G.4, I (GB‑1, GB‑2), K, Deferrals | Resolved |
| Q2: `program_key` conflicts with one-offer-many-classifications | Removed `program_key` and `catalog_standard`. Added `ProgramReference` as offer-state content and the deterministic discriminator rule. | C.4, C.6, E.1, I (OI‑3, OI‑4, OI‑8, HI‑5) | Resolved |
| Q3: Blanket snapshot invalidation | Dependency-scoped failures (row → family → snapshot → storage propagation); no fallback; legitimate `None` distinguished from inconsistency | C.7, C.14, D.4, G.1, I (IS‑1…IS‑7) | Resolved |
| Q4: OfferKey too broad (`market_scope`, many dimensions) | Minimal key `(seller, channel, exact catalog ID, optional closed discriminator)`. Dimension table. Absence ≠ semantic default. No lineage machinery; correction needs review. | C.4, C.6, D.6, I (OI‑5…OI‑11) | Resolved |
| `KNOWN` conflated with origin; `NOT_PUBLISHED` misused | Availability separated from origin and role. Added `MISSING`/`NULL`/`MALFORMED`/`UNSUPPORTED`/`NOT_REPRESENTED`. OpenRouter `discount` is `UNINTERPRETED`. | C.0, C.5, G.1 | Resolved |
| Mutable nested evidence aliasing | Interpretation never mutates or exposes storage's nested dicts | C (preamble) | Resolved |
| Registry metadata matching (`total_count`) | Fixed and variable metadata defined per binding; exact key set; unsupported behavior | D.3, I (IS‑7) | Resolved |
| Replay contradiction | Option A: pinned code revision plus explicit manifest and `manifest_digest`; no runtime selection of superseded versions | D.6, C.15 | Resolved |
| Freshness coupled to applicability; cap inconsistency | Independent axes. Missing vs unresolved boundaries. No grace. Category table authoritative (no `LIVE_FEED` cap). | F.1–F.4, I (TP‑1…TP‑7) | Resolved |
| Recurrence engine unsupported | `RECURRING` kept as a concept only; evaluation deferred; DST tests removed | C.6, F.2, E.3, I, K | Resolved |
| Digest incomplete (empty snapshots, normalized columns) | `input_digest` over canonical retained inputs, including envelopes, metadata, columns, conditions, quotes and records | D.8, I (PF‑2…PF‑6) | Resolved |
| Revision occurrences / A → B → A / frame-by-frame | `content_digest` (commercial content, including validity timestamps) separated from `StateOccurrence`; adjacent-frame reduction | C.6, C.7, D.5, I (HI‑1…HI‑6) | Resolved |
| CI v1 differential cannot rely on `compare_component` alone | Current event projected from `analyze_advertised_history`; history follows the same transition table; prefix differential | G.2, I (PR‑2, PR‑3) | Resolved |
| Comparison vs savings conflated; unknown charges as "conditional" | Component comparison separated from savings qualification; full required scope; `INSUFFICIENT_EVIDENCE` for unresolved charges or terms; deterministic precedence | C.10, C.11, E.2–E.4, I (CS‑1…CS‑11) | Resolved |
| "Exact percentage arithmetic" overstated | One orientation; all five `compare_component` statuses kept; 5% flag ignored | B.1, E.4 | Resolved |
| Factual vs savings classification contradiction; weak promotion rule | Factual classifications separate from qualifiers; `PROMOTION` requires established meaning | C.9, C.10, E.1, E.7 | Resolved |
| Curated execution premature | Curated kept as evidence-kind design only; no loader, merger, rules or execution | B.2, C.16, H, K, I | Resolved |
| CI v1 "automatically historical" wording | Status from presence, barriers, applicability and freshness | G.2, I (PR‑4, PR‑5) | Resolved |
| HC‑1 unsafe uninterpreted-zero shortcut | Shortcut removed; candidate rule with full invariants; disabled until the Chair rules | E.6, G.1, I (FR‑1…FR‑4) | Resolved (pending Chair ruling) |
| G.4 overclaimed DeepInfra fit | Illustrative only; no claim that no changes are needed | G.4, Deferrals | Resolved |
| Sequence and matrix out of date | New U0–U8; matrix rebuilt; deferred-feature tests removed | I, K | Resolved |

---

## Self-audit

| # | Statement | Verified | Where enforced |
|---|---|---|---|
| 1 | No Gate 1c cross-provider identity machinery remains | True | C.3, C.17, E.5, GB‑1 |
| 2 | Promotion IDs alone do not create new offer identities | True | C.4 rule 2, OI‑3 |
| 3 | Offer identity contains no opaque `market_scope` | True | C.4 |
| 4 | Unknown semantic dimensions are not silently turned into identity | True | C.4 rules 1 and 3; discriminator closed and evidence-gated |
| 5 | One inconsistent observation does not invalidate the whole snapshot | True | D.4, IS‑1 |
| 6 | No affected offer falls back to stale older data | True | D.4, IS‑4 |
| 7 | Component comparison is distinct from an overall savings claim | True | E.2 vs E.3 |
| 8 | Unknown possible charges block broad savings claims | True | E.3 scope, CS‑3 |
| 9 | Freshness and commercial applicability are separate | True | F.1 |
| 10 | A → B → A history remains representable | True | C.7, HI‑1 |
| 11 | The fingerprint includes snapshot-level state such as empty snapshots | True | D.8, PF‑2 |
| 12 | Present-but-uninterpreted fields are not called `NOT_PUBLISHED` | True | C.0, G.1 |
| 13 | CI v2 remains evidence-only | True | G.3, PR‑6 |
| 14 | Production curated-rule execution is deferred | True | C.16, H, K |
| 15 | Recurring schedule execution is deferred | True | C.6, F.2 |
| 16 | HC‑1 remains with the Human Chair | True | E.6 (rule disabled by default) |
| 17 | No schema, storage or acquisition changes are required | True | A, H |
| 18 | No implementation was performed | True | Read-only; no files, Git state or `scratch/` changed |

---

## Astra Focused Re-review Handoff

**What changed.**
- Model identity is provider-local, and cross-model comparison returns `MODEL_EQUIVALENCE_UNRESOLVED`; Gate 1c owns equivalence.
- The offer key is minimal, with a closed, evidence-gated discriminator. Promotions are offer-state content.
- Failures are scoped to their dependencies, with no fallback.
- Component comparison and savings qualification are separate, with full-scope rules and fixed precedence.
- Applicability and freshness are independent; recurrence is deferred.
- History uses occurrences and frame-by-frame reduction, with a fingerprint over complete canonical inputs.
- Replay uses a pinned code revision and manifest. Value states are corrected.
- Curated execution is deferred. CI v1 current events are projected from the legacy reducer.

**Blocking findings resolved.** All nine blocking items in Astra §9, plus the non-blocking counterexamples (promotion-ID change, A → B → A, empty-snapshot fingerprint, unknown additional charges), now covered by OI‑3, HI‑1, PF‑2 and CS‑3.

**Areas most deserving scrutiny.**
1. The discriminator rule and the dimension table (C.4).
2. The `TERMS_POSSIBLE` vs `CHARGE_POSSIBLE` field classes (C.0, E.2).
3. Savings scope and outcome precedence (E.3).
4. The OpenRouter row re-derivation through the public parser (G.1).
5. The CI v1 legacy projection plus prefix differential (G.2).

**Genuinely unresolved questions (Astra-resolvable).** J‑2 through J‑6. None requires a schema, storage or acquisition exception.

**HC‑1 status.** PENDING HUMAN CHAIR HC‑1 RULING. The candidate rule is specified, wired and disabled by default. Either outcome can be implemented without changing identity, storage, acquisition or schema. Only enabling FREE in U4 depends on the ruling.

**Recommendation.** I recommend this revised specification go to Astra's focused acceptance review. I do not recommend implementation on the strength of this revision. U0 (a verified baseline on Python ≥ 3.11) and a separate Human Chair implementation authorization are still required.

Governance path: **Claude revised specification → Astra focused acceptance review → Human Chair decision → separate implementation authorization.** No implementation is authorized.
