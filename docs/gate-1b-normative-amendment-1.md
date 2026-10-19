> Record note (2026-10-19): This amendment to [Gate 1b Revision 2](gate-1b-revision-2.md) is reproduced verbatim below, excluding only the conversational prefix "claude's work ". Read it with the [final Human Chair clarifications and approval provenance](gate-1b-human-chair-clarifications.md), which govern the final comparison-scope and mandatory-charge FREE rules. Historical governance and U0-pending statements remain as authored. Implementation remains separately gated.

# GATE 1b — NORMATIVE AMENDMENT 1 TO REVISION 2

**Model Price Watch — Step 1, Gate 1b: Offers and Interpretation Policies**
Author: Claude (configured `claude-opus-5-5`) · Date: 2026‑10‑19 · Mode: read-only · Implementation authority: none

---

## 1. Governance status

- The Human Chair approved **Gate 1b Revision 2 subject to Astra's enumerated required amendments**. Astra's disposition was **ACCEPT WITH REQUIRED AMENDMENTS**.
- The HC‑1 `/free` ruling is **binding** and is incorporated here (AM‑1 to AM‑6, AM‑16). It is no longer pending.
- **Revision 2 plus this Amendment 1 is the proposed authoritative Gate 1b specification.** Where they conflict, this amendment controls. Revision 2 text not superseded here stays in force.
- The accepted architecture is unchanged and is not reopened. That covers the additive read-side `offers/` package, no persistence, schema v2, the unchanged storage, contracts, acquisition, scanners and legacy validator, provider-local identity, dependency-scoped failures, independent freshness and applicability, factual classifications versus savings qualification, provenance, CI v2 evidence-only, and the deferral of curated execution, recurrence and Gate 1c work.
- **Implementation is not authorized.** U0 and a separate Human Chair implementation authorization remain prerequisites. I made no changes to source, tests, documentation, Git state or `scratch/`, and I ran nothing. The details of existing behavior relied on here were confirmed by my earlier read-only inspection:
  - OpenRouter `parse_catalog` decodes JSON integers as Python `int` and fractions as `Decimal`.
  - Storage decodes all JSON numbers as `Decimal` and canonicalizes their text.
  - `analyze_advertised_history` uses `now` only for validation.
  - `zero_advertised_base_token_rate_ids` requires both quote components to equal zero.

Amendment types: **S** semantic · **I** identity · **P** provenance/reproducibility · **V** provider-specific · **T** test-only/editorial.

---

## 2. Normative amendments

### AM‑1 — FREE discovery semantics (binding HC‑1) · Type S

**This amendment supersedes Revision 2 §C.9 (FREE entry), §E.1 (FREE row) and §E.6 in their entirety.**

1. `/free` is a **discovery view**, not a certification that an offering can never generate a charge.
2. FREE is **one factual classification on the existing offer and occurrence**, plus exactly one `FreeAssessment` per qualifying price group. FREE status, assurance and warnings never affect `offer_id` (C.4 is unchanged except as amended in AM‑7).
3. **`FreeAssessment` fields**

   | Field | Content | Depends on `now`? |
   |---|---|---|
   | `offer_id`, `occurrence_index`, `price_group` | Which offer, occurrence and price group | No |
   | `basis_kind` | `ZERO_PRIMARY_RATES`, or `DECLARED_FREE_CONDITION` (reserved; needs established semantics; no Gate 1b emitter) | No |
   | `basis_components` | Evidence refs for the zero components | No |
   | `asserted_scope` | `BillingScope` (AM‑3 §2) and a scope label code | No |
   | `assurance` | `VERIFIED`, `QUALIFIED` or `POSSIBLE` | No |
   | `warnings` | Structured warnings (AM‑2) | No |
   | `known_restrictions`, `usage_limits`, `eligibility` | As carried by the occurrence | No |
   | `unresolved` | Unresolved monetary and non-monetary dimensions and fields | No |
   | `time_restriction` | The free group's restriction | No |
   | `applicability_at_now` | Evaluated applicability of the free group | Yes |
   | `freshness` | Freshness assessment | Yes |
   | `current_discoverable` | Whether the offer is currently discoverable as free | Yes |
   | `rule_id` | `cls.free.discovery@1` (provenance) | No |

4. **Assurance meanings**
   - **VERIFIED:** the asserted scope is completely established as zero, with no applicable known non-zero charge and no unresolved possible charge inside it.
   - **QUALIFIED:** an established free condition with explicit supported restrictions.
   - **POSSIBLE:** reliable, meaningful zero or base-free evidence that does not establish free use across the asserted billing scope.

   These are evidence categories, not confidence scores.
5. FREE is active in the production classification manifest. Revision 2's "disabled until ruling" state is removed.

Tests: FD‑1 to FD‑20 (§3). Units: U6 (§4). Supersedes FR‑1 to FR‑4.

### AM‑2 — Structured FREE warnings · Type S

**This amendment adds to Revision 2 §C and supersedes any Revision 2 FREE wording.**

Warnings are orthogonal to assurance and may overlap. Each warning is a record `(code, subject)`, where `subject` names the affected dimension, field path, condition, limit, window or scope. A bare boolean is not permitted. Each code imposes an **assurance cap**; AM‑3 step 4 applies the caps.

| Code | Subject (required) | Cap |
|---|---|---|
| `ADDITIONAL_CHARGES_UNRESOLVED` | Field paths of uninterpreted pricing fields inside `asserted_scope`, with their field class | POSSIBLE |
| `KNOWN_ADDITIONAL_CHARGE` | Dimension, `KNOWN` amount, currency, unit, and `scope ∈ {WITHIN_FREE_SCOPE, BEYOND_ALLOWANCE}` | POSSIBLE if `WITHIN_FREE_SCOPE`; QUALIFIED if `BEYOND_ALLOWANCE` |
| `USAGE_LIMITED` | Metric, quantity and period (each a `Value`) | QUALIFIED |
| `USAGE_LIMIT_UNKNOWN` | Limit fields that are not `KNOWN` | none |
| `OVERAGE_CHARGES_UNKNOWN` | Limit ref and the unresolved overage dimension | QUALIFIED if the allowance quantity is `KNOWN`, otherwise POSSIBLE |
| `TEMPORARY_FREE` | `KNOWN` end instant of the free group | QUALIFIED |
| `TIME_WINDOW_RESTRICTED` | `KNOWN` start instant (and end, if declared) | QUALIFIED |
| `SCHEDULE_EVALUATION_DEFERRED` | Evidence pointer of the recurring schedule | QUALIFIED; never current-discoverable (AM‑5) |
| `ELIGIBILITY_RESTRICTED` | Audience and requirements | QUALIFIED |
| `PARTIAL_ZERO_RATE_ONLY` | Zero dimensions, and the policy's partial-rate-scope code (e.g. `ci_v1_advertised_base_token_rates`) | POSSIBLE |
| `PRICING_SCOPE_INCOMPLETE` | Required billable dimensions that are not established | POSSIBLE |

**Disclosure rules**
- A known charge is always reported as `KNOWN_ADDITIONAL_CHARGE` with its amount. It is never softened to "fees may apply".
- The `PARTIAL_ZERO_RATE_ONLY` scope code `ci_v1_advertised_base_token_rates` maps to the accepted wording: *"zero advertised base token rates; other charges or conditions may apply."* The final presentation wording belongs to Step 2.

Tests: FD‑2 to FD‑5, FD‑9 to FD‑15. Units: U6.

### AM‑3 — Deterministic FREE decision procedure · Type S

**This amendment supersedes Revision 2 §E.6.** For each price group of each occurrence, the procedure runs in this order.

1. **Primary scope.** Each policy declares a nonempty `primary_scope` of established monetary dimensions:
   - OpenRouter v1 and CI v1: `{INPUT_TOKENS, OUTPUT_TOKENS}`.

   *Affirmative basis* exists iff every dimension in `primary_scope` is `KNOWN(0)` on a policy-established monetary field. Otherwise this is **Case C: no FREE**.
   - `MISSING`, `NULL`, `MALFORMED`, `UNKNOWN`, `UNINTERPRETED`, `NOT_REPRESENTED` and `CONFLICT` never supply a zero.
   - A zero outside `primary_scope` (e.g. a cached-input zero) never supplies the basis.
   - A `KNOWN` non-zero value in `primary_scope` gives Case C.
2. **Billing scope.** Each policy declares a `BillingScope` = its established monetary fields **plus every field present under its declared pricing-record roots**, together with a `scope_complete` flag:
   - OpenRouter v1: root `$.pricing`; `scope_complete = true`.
   - CI v1: the quote's advertised base token rates; `scope_complete = false`.

   `asserted_scope` = this `BillingScope`.
3. **Undermining test** (Case C even when a basis exists). The zero claim is undermined only by evidence, never by mere presence:
   - (a) a primary-scope component is `CONFLICT`;
   - (b) a reviewed policy rule establishes that another field modifies or selects the primary-scope rates, and that field is unresolved or establishes a non-zero effect. No Gate 1b production policy declares such a rule.

   Uninterpreted fields under the default `TERMS_POSSIBLE` class (AM‑9) **do not** undermine the claim. They limit completeness only, through step 4.
4. **Assurance = the minimum cap** (VERIFIED > QUALIFIED > POSSIBLE) over every warning emitted:
   - each uninterpreted field in `asserted_scope` → `ADDITIONAL_CHARGES_UNRESOLVED`;
   - `scope_complete = false` → `PARTIAL_ZERO_RATE_ONLY`;
   - an established required billable dimension in scope that is not `KNOWN` → `PRICING_SCOPE_INCOMPLETE`;
   - a `KNOWN` non-zero component in scope → `KNOWN_ADDITIONAL_CHARGE(WITHIN_FREE_SCOPE)`;
   - limits, overage, time and eligibility warnings per AM‑5.

   With no capping warning: **Case A, VERIFIED**. Otherwise: **Case B, QUALIFIED or POSSIBLE**.
5. Emit FREE plus the `FreeAssessment`. `current_discoverable` follows AM‑5 and AM‑15.

**Production consequences (disclosed)**
- The OpenRouter fixture row `example/model:free` (`prompt "0"`, `completion 0`, no other pricing keys) → **FREE + VERIFIED**, scope "OpenRouter catalog pricing record", warning `USAGE_LIMIT_UNKNOWN`.
- Real OpenRouter `:free` rows that carry further pricing keys (e.g. `request: "0"`) → **FREE + POSSIBLE** with `ADDITIONAL_CHARGES_UNRESOLVED`.

Tests: FD‑1, FD‑5 to FD‑9. Units: U6 (procedure); U4 and U5 (policy declarations).

### AM‑4 — `zero_declared_rates` tightened · Type S

**This amendment supersedes Revision 2 §C.9 (attribute definition) and §C.6 (`zero_declared_rates` field).**

`ZeroRateFact {price_group, zero_dimensions, nonzero_known_dimensions, unresolved_required_dimensions}` is emitted per price group **iff** the set of policy-established monetary components that are `KNOWN(0)` is **nonempty**. It is never vacuously true.

- It is factual evidence only. It implies neither FREE nor VERIFIED.
- It is time-independent and excluded from `content_digest` because it is derived (AM‑8).
- Example: `INPUT_TOKENS = 0` with `OUTPUT_TOKENS` `NULL` gives `ZeroRateFact(zero={INPUT}, unresolved={OUTPUT})` and **no FREE**.

Tests: FD‑8, FD‑19. Units: U6.

### AM‑5 — Time, limits and eligibility under FREE · Type S

**This amendment supplements Revision 2 §F and supersedes the "FREE does not imply unlimited" text in §E.6.**

**Time**
1. **Temporary free.** An established `KNOWN` end on the free group gives `TEMPORARY_FREE`. `TIME_LIMITED` is also emitted per Revision 2 §E.1, and `PROMOTION` when an established promotional `ProgramReference` exists. All of these stay on the same offer.
   - At `now ≥ end` the free condition no longer supports current discovery.
   - A paid price after expiry is asserted only from separate supported evidence. It is never inferred.
2. **Bounded free window.** `[start, end)` is evaluated for **the free price group only**. An inactive free group is never shown as currently free, whatever other groups are doing.
3. **Recurring free window.** The evidence is kept. `SCHEDULE_EVALUATION_DEFERRED` is emitted, and `current_discoverable = false`. The recurrence engine stays deferred.

**Usage limits**
4. A known allowance gives `USAGE_LIMITED` with quantity, metric and period, and caps assurance at QUALIFIED. It never excludes the offer.
5. Unknown usage limits give `USAGE_LIMIT_UNKNOWN`. They do not cap assurance.
6. Unknown overage charges give `OVERAGE_CHARGES_UNKNOWN` with the cap from AM‑2. This always prevents VERIFIED.

**Eligibility**
7. Supported restrictions give `ELIGIBILITY_RESTRICTED` (cap QUALIFIED). There is **no field** asserting that the current user qualifies.

**Current discoverability**
8. `current_discoverable = true` iff all of the following hold:
   - the offer's presence is `PRESENT` (AM‑15);
   - the evidence supporting the assessment is `FRESH`;
   - the free group's applicability at `now` is `APPLICABLE` or `NO_DECLARED_RESTRICTION`.

Tests: FD‑2 to FD‑4, FD‑11 to FD‑15, FD‑18. Units: U2 (group applicability), U6.

### AM‑6 — Cheaper Inference v1 FREE discovery · Type S, V

**This amendment supersedes Revision 2 §G.2 ("Zero rates … never FREE"), §E.6 (CI exclusion), §E.7, §J‑1 and PR‑8.**

1. An eligible CI v1 observation whose quote has both `input_usd_per_million` and `output_usd_per_million` equal to `KNOWN(0)` → **FREE + POSSIBLE**, with:
   - `PARTIAL_ZERO_RATE_ONLY(ci_v1_advertised_base_token_rates)`;
   - `ADDITIONAL_CHARGES_UNRESOLVED` for every uninterpreted pricing field present (e.g. `discount_percent`, `reference_*`).

   This matches the factual basis of `zero_advertised_base_token_rate_ids`.
2. This does **not** expand CI v1 interpretation. `discount_percent`, `reference_*` and every other unresolved field stay `UNINTERPRETED` (`TERMS_POSSIBLE`).
3. Current discoverability is governed by presence, barriers, freshness and temporal applicability (AM‑5 §8, AM‑13).
4. CI v2 produces no `FreeAssessment`.

Tests: FD‑10, FD‑20, PR‑8 (replaced). Units: U5 (declarations), U6.

### AM‑7 — ProductDiscriminator narrowing · Type I

**This amendment supersedes Revision 2 §C.4 (`ProductDiscriminator` definition and the `offer_id` formula).**

1. **Identity encoding.** `offer_id = "mpwo1:" + sha256(tagged_canonical([scheme, seller_id, commercial_channel_id, exact_provider_catalog_id, discriminator]))`, where `discriminator` is `null` or `[kind, value]`. `rule_id`, provenance, policy version and evidence refs are **never** encoded. `rule_id` lives only in `ProvenanceRecord`.
2. **Emission contract.** A policy may emit a discriminator only through a statically declared `DiscriminatorEmission {kind, source_field_path, selection_semantics_ref (reviewed citation), value_mapping}`. `value_mapping` is a frozen, exact map from source value to discriminator value.
   - Registry validation (U3) raises `RegistryError` when a policy declares discriminator output without an emission contract.
   - At runtime, an emitted `(kind, source value)` outside the declared mapping raises a programming error, which propagates.
3. **What justifies a discriminator.** It must identify a **separately selectable commercial product under the same catalog ID**. A price, discount, promotion, eligibility rule, context band, geographic condition, account class, free-form description or arbitrary state change is never enough.
4. **Value form.** `value` is one nonblank, NUL-free string taken from `value_mapping`. It never packs several attributes, never hashes an attribute map, and is never derived from free text.
5. **Unknown distinctions** stay unresolved (`PROGRAM_APPLICABILITY_UNRESOLVED` or `APPLICABILITY_UNRESOLVED`). They never produce an unconditional merged price.
6. Adding a discriminator to a previously undiscriminated identity is an identity correction that requires explicit review.
7. **The Gate 1b production manifest declares zero emission contracts**, enforced by a static test.

Tests: OI‑7 (amended), OI‑12 to OI‑14. Units: U1 (encoding), U3 (registry enforcement).

### AM‑8 — Type-preserving canonical encoding and digest projections · Type P

**This amendment supersedes Revision 2 §D.7 (canonical encoding) and §D.8, and the digest definitions in §C.6 and §C.15.**

1. **Tagged canonical form `mpw-canon-v1`.** Every value is encoded as a JSON array:

   | Value | Encoding |
   |---|---|
   | null | `["n"]` |
   | boolean | `["b", true \| false]` |
   | string | `["s", text]` |
   | JSON number (`int` or `Decimal`, never `bool` or `float`) | `["d", canonical-number]` |
   | array | `["a", [items…]]` |
   | object | `["o", [[key, value]…]]`, keys sorted by Unicode code point |
   | domain timestamp | `["t", "YYYY-MM-DDTHH:MM:SS.ffffffZ"]` |
   | domain enum | `["e", "NAME"]` |
   | domain record | `["r", type_name, [[field, value]…]]`, fields in declared order |

   - **canonical-number:** `"0"` for zero; otherwise trailing zeros stripped and written as `<coefficient>e<exponent>`. This is the same rule as storage's numeric text, so it matches retained canonical values.
   - **Serialization:** `json.dumps(…, ensure_ascii=True, separators=(",", ":"))`. Each digest is `sha256(domain_prefix + "\n" + text)`, with prefixes `mpw-input-v1`, `mpw-content-v1`, `mpw-semantic-v1`, `mpw-evaluation-v1`, `mpw-identity-v1` and `mpw-manifest-v1`.
   - **Distinguished pairs:** `0` vs `"0"`, `false` vs `"false"`, `null` vs `""`, and `{}` vs `[]` all encode differently. Retained storage does not distinguish integer from fraction (both decode as `Decimal`), so the encoding does not either.
2. **`input_digest`** = `["mpw-input-v1", [provider entry …]]`.
   - Provider entries are the requested providers, deduplicated and sorted by code point. Each is `[provider_name, [snapshot …]]`; an **empty history is `[name, []]`**.
   - Snapshots are in storage order (`completed_at` ascending, then `id` ascending). Each is `[id, provider, started_at, completed_at, source_url, metadata, [observation …]]`, including empty, unsupported and inconsistent snapshots.
   - Observations are sorted by `offering_id` code point. Each is `[snapshot_id, offering_id, observed_at, input_per_million, output_per_million, conditions, source_record, advertised_quote]`. A NULL money column is `["n"]`. The quote is a record of its basis fields plus both components, or `["n"]`.
3. **`content_digest`** (commercial-content projection of an `OfferState`).

   **Included:**
   - `price_groups`, sorted by their canonical applicability encoding. Each group has every applicability attribute as a `Value` with its tag, including the `TimeRestriction` and its commercial boundary instants. Components are sorted by `(dimension, commercial_role)`, each as `(dimension, role, amount, currency, unit)`.
   - `conditions`, `eligibility` and `usage_limits`.
   - `program_references` as `(reference, kind, established_meaning)`.
   - **Every** uninterpreted pricing field as `(field_path, field_class, tagged retained value)`. Under AM‑9 these may all affect commercial state.

   **Excluded:** `offer_id`, evidence refs, provenance, observation timestamps, snapshot IDs, policy IDs and versions, `rule_id`, `display_name`, and derived facts (`ZeroRateFact`, classifications, `FreeAssessment`).
4. **`semantic_digest`** (time-independent result projection).

   **Included:** `identity_scheme`, `manifest_digest` and `input_digest`; every `SnapshotInterpretation` with its outcome, reasons and affected refs; every offer (`offer_key`, `offer_id`, `model_ref`); every `StateOccurrence` (index, content projection, `content_digest`, first and last snapshot IDs and `observed_at`, presence episode, `close_reason`, evidence refs); factual classifications with provenance; `ZeroRateFact`s; `ObservedPriceChange`s, including CI legacy-derived events; and the time-independent fields of each `FreeAssessment` (AM‑1 §3).

   **Excluded:** `evaluated_at`, `CurrentStatus` and its reasons, freshness, group applicability at `now`, `current_discoverable`, every `ComponentComparison` and `SavingsQualification`, and deal candidates.
5. **`evaluation_digest`** (new) = sha256 over `semantic_digest`, `evaluated_at` and every excluded `now`-dependent field listed in §4.

Tests: PF‑8 to PF‑15. Units: U1 (encoder, content projection), U3 (input, semantic and evaluation digests).

### AM‑9 — Uninterpreted-field classification · Type S

**This amendment supersedes the `field_class` assignment rules in Revision 2 §C.0, §G.1 (shape and `discount`-key rule) and §J‑6.**

1. Every uninterpreted pricing field is **`TERMS_POSSIBLE` by default**: it may affect charges, rates, applicability, eligibility or interpretation.
2. **`CHARGE_POSSIBLE`** is assigned only by an explicit, reviewed policy rule that names the field path and establishes that the field is an independent possible additional charge, separate from the compared components. JSON shape never decides the class in either direction.
3. **No Gate 1b production policy declares a `CHARGE_POSSIBLE` rule.** Every OpenRouter extra pricing key (including `discount`) and every CI v1 uninterpreted field is `TERMS_POSSIBLE`.
4. **Consequence (disclosed):** under Revision 2 §E.2, a component comparison involving an OpenRouter row that carries any extra pricing key gives `INSUFFICIENT_EVIDENCE(TERMS_UNRESOLVED)`. FREE discovery is unaffected except through AM‑3 step 4.

Tests: CS‑3 (amended), CS‑3b, CS‑4 (amended), PR‑1 (amended). Units: U4, U5 (declarations), U6.

### AM‑10 — Comparison and savings clarifications · Type S

**This amendment supersedes Revision 2 §E.2 ("where the two groups' attributes are compared"), and the outcome rules and "nothing is lower" text in §E.3.**

1. **Applicability requirements.** Each policy declares, statically:
   - `component_applicability_requirements`;
   - `qualifier_applicability_requirements[qualifier]`;
   - `supported_scenarios[qualifier]` (§3 below).

   A required attribute must be `KNOWN` on both sides. Otherwise the result is `INSUFFICIENT_EVIDENCE(APPLICABILITY_UNRESOLVED)`. **Equal `NOT_REPRESENTED` values never satisfy a requirement.**
   - OpenRouter v1 and CI v1 both require `{execution_mode, service_tier}`. Their groups are `NOT_REPRESENTED` for both, so every production component comparison and qualification is `INSUFFICIENT_EVIDENCE` (disclosed consequence). Establishing default applicability for these sources needs a later, reviewed policy version.
2. **Scope completeness.** A savings scope is the union of:
   - Revision 2 §E.3's scope;
   - **every** component, condition or uninterpreted field anywhere in the `OfferState` (including offer-wide entries) whose applicability is not `KNOWN` to exclude the selected group.

   A charge whose applicability to the group is unresolved is in scope as unresolved. Selecting a narrow group never drops an offer-wide fee.
3. **Supported scenarios.** `QUALIFIED_WITH_CONDITIONS` requires that every known condition difference belongs to the qualifier's closed `supported_scenarios` set and is `KNOWN` on both sides and non-contradictory.
   - Contradictory conditions, unresolved applicability, unknown counterpart semantics, or a difference outside the supported set give `INSUFFICIENT_EVIDENCE`.
   - Incompatible billing (currency or unit kind) gives `NON_COMPARABLE`.
   - Listing differences never repairs them.
4. **Savings decision procedure** (deterministic):

   | Step | Rule | Result |
   |---|---|---|
   | (a) | Any scoped comparison is NON_COMPARABLE | **NON_COMPARABLE** |
   | (b) | Any scoped dimension is validly comparable (DIRECT or CONDITIONAL, both values `KNOWN`) with candidate > reference | **NOT_QUALIFIED** (`KNOWN_HIGHER_COMPONENT`) |
   | (c) | Any scoped comparison is INSUFFICIENT, any unresolved field is in scope, or the counterpart or applicability is unestablished | **INSUFFICIENT_EVIDENCE** |
   | (d) | All scoped comparisons are complete and none is strictly lower | **NOT_QUALIFIED** (`NO_LOWER_COMPONENT`); valid only when complete |
   | (e) | Any CONDITIONAL inside a supported scenario | **QUALIFIED_WITH_CONDITIONS** |
   | (f) | Otherwise | **QUALIFIED** |

   - Input equal with output unknown → (c) **INSUFFICIENT_EVIDENCE**.
   - Input higher with output unknown → (b) **NOT_QUALIFIED**.
5. **Meaning of outcomes.** `NOT_QUALIFIED` means only that the componentwise-dominance criterion fails. It is not proof that the candidate is never cheaper for any workload. `QUALIFIED` establishes lower or equal rates on every scoped dimension. It does not guarantee a smaller bill for every workload.
6. The accepted precedence orders are unchanged. **All reason codes are kept** on every result.

Tests: CS‑12 to CS‑17. Units: U4, U5 (declarations), U6.

### AM‑11 — OpenRouter re-derivation narrowed · Type V

**This amendment supersedes the row consistency check in Revision 2 §G.1.**

For each retained OpenRouter row in a recognized, snapshot-consistent snapshot:

1. **Reconstruct** the catalog text `{"data":[retained_row]}` with a **type-preserving JSON writer**. `Decimal` and `int` become JSON number tokens in canonical-number form, which are valid JSON numbers. `str`, `bool`, `null`, objects and arrays keep their types. Keys are sorted. The AM‑8 tagged encoder is **not** used for this.
2. Call the public `providers.openrouter.parse_catalog(text, observed_at=row.observed_at, source=snapshot.source)`.
3. Require `accepted is True`, exactly one observation, and `offering_id ==` the retained `offering_id`.
4. Compare `input_usd_per_million` and `output_usd_per_million` with the stored columns by Decimal value (`None` equals only `None`).
5. Compare the parser's `unsupported_pricing` with the retained `conditions` recursively, with these categories kept separate: number (`int` and `Decimal` compared by Decimal value; `bool` excluded), string, boolean, null, object (same key set) and array (same order).
6. Parser rejection or any mismatch → `ROW_INCONSISTENT`. Unexpected exceptions propagate. A malformed raw value stored as `None` is consistent.
7. Pagination, wire syntax, duplicate raw keys, the original transport and network behavior are **not** reconstructed. The one-row reconstruction is **never** checked against `total_count`. Snapshot membership is guaranteed by storage, and `total_count` is checked separately (AM‑14).

Tests: IS‑1 (amended), IS‑8, IS‑12, IS‑13. Units: U4.

### AM‑12 — CI v1 monetary-transition authority · Type V, P

**This amendment supersedes Revision 2 §G.2 ("Change compatibility") and §E.8 as it applies to CI v1.**

1. `advertised_detection.analyze_advertised_history` is the **sole authority** for CI v1 monetary comparisons, reset reasons and decrease events. Gate 1b does not implement a second CI monetary reducer.
2. For the complete stream history F₁…Fₙ, including empty frames and v2 frames, unfiltered, and for each k = 1…n: call `analyze_advertised_history(F₁…Fₖ, now=Fₖ.snapshot.completed_at)`. This keeps the results independent of `now`, so they belong in `semantic_digest`. From each prefix result, record each offering's `(presence, comparison.reason, comparison.aggregate, reset_reason, decrease_event)` at frame k.
3. Each distinct legacy `decrease_event` becomes **one** `ObservedPriceChange`, deduplicated by its originating `(previous_snapshot_id, current_snapshot_id)`. It records:
   - `legacy_active_through_snapshot_id`: the last prefix still carrying the event;
   - `legacy_cleared_by`: the reset reason, aggregate `INCREASE` or `MIXED`, `ABSENCE`, or `None` if still active.

   Changes are attached to the `StateOccurrence` that contains `current_snapshot_id`.
4. The **current CI legacy event** is the final prefix's `decrease_event`.
5. The generic Gate 1b reducer still builds CI `StateOccurrence` history (Revision 2 §D.5). A `QUOTE_UNSUPPORTED` frame, whether a v1 row without a quote or any v2 frame, is a **barrier**: the open occurrence closes with `UNINTERPRETABLE` and no new occurrence is created for that frame.
6. Differential tests compare legacy projection fields only. They never equate legacy `Presence.CURRENT` with Gate 1b `CurrentStatus`. Gate 1b freshness and applicability never change a legacy transition result.
7. The cost is O(n²) prefixes. Any later optimization must keep this authority and demonstrate equivalence.

Tests: PR‑2 (replaced), PR‑3 (amended), PR‑11 to PR‑15. Units: U5 (new file `offers/changes_ci_v1.py`).

### AM‑13 — CI v1 `available_until` · Type V, S

**This amendment supersedes Revision 2 §G.2 (`available_until` row) and §J‑4.**

| Retained evidence | Result |
|---|---|
| Field absent | `NO_DECLARED_RESTRICTION` |
| Field present and `null` | `RESTRICTION_UNRESOLVED(available_until_null_semantics_unestablished)` |
| Field present and non-null | `RESTRICTION_UNRESOLVED(available_until_uninterpreted)` |

- "Absent" and "null" stay distinct.
- Base-rate evidence, the occurrence, `ZeroRateFact` and the `FreeAssessment` are all kept. Only current applicability is affected.
- The "historical data only" rationale is withdrawn.
- **Disclosed consequence:** if retained CI v1 rows carry `available_until: null`, as all 97 v2 fixture rows do, then CI v1 FREE + POSSIBLE assessments exist but are never `current_discoverable`. Changing that requires establishing the semantics of `null` in a separate, evidence-backed policy decision.

Tests: PR‑9, PR‑10. Units: U5.

### AM‑14 — Registry recognition, validation and absence precedence · Type V, P

**This amendment supersedes Revision 2 §D.3 (matching rules) and §D.4 (row 3 example), and clarifies §D.5 step 4.**

1. **Recognition** (each failure gives `UNSUPPORTED_SOURCE(reason)`, checked in this order):
   1. provider exactly matches some binding (`UNKNOWN_PROVIDER`);
   2. location exactly matches (`LOCATION_MISMATCH`);
   3. the metadata key set equals the binding's fixed keys plus variable keys exactly (`KEY_SET_MISMATCH`);
   4. every fixed value is an exact `str` equal to the binding's value (`CONTRACT_VALUE_MISMATCH`).

   Registry validation guarantees at most one binding is recognized (the CI v1 and v2 bindings are disjoint).
2. **Variable validation**, only after recognition, for OpenRouter `total_count`:
   - type `int` or `Decimal`, never `bool`; finite; integral; ≥ 0. Otherwise `SNAPSHOT_INCONSISTENT(TOTAL_COUNT_INVALID)`.
   - equal to the retained row count. Otherwise `SNAPSHOT_INCONSISTENT(TOTAL_COUNT_MISMATCH)`.

   CI bindings have no variable keys.
3. **Row checks** (AM‑11, policy checks) run only after steps 1 and 2 pass.
4. **Absence.** Only a recognized, snapshot-consistent snapshot with zero rows, or one in which the offer's `offering_id` does not appear, establishes `ABSENT_FROM_LATEST`. An unsupported or snapshot-inconsistent snapshot, empty or not, establishes **no** absence. The offer becomes `LATEST_SNAPSHOT_UNSUPPORTED` or `LATEST_SNAPSHOT_INCONSISTENT`, and its open occurrence closes with `UNINTERPRETABLE`, not `ABSENT`. A recognized CI v2 snapshot containing the offering ID gives `LATEST_EVIDENCE_ONLY`.

Tests: IS‑3 (split), IS‑7, IS‑9 to IS‑11. Units: U3.

### AM‑15 — Temporal edges and status reasons · Type S

**This amendment supersedes Revision 2 §C.7 (`CurrentStatus`), §F.1 and §F.2 where inconsistent, and §F.4.**

1. **§F.1 clarified.** Freshness uses evidence age plus only the caps prescribed for `CURATED_TIME_BOUND`, `FREE_TIER_LIMITS` and `CURATED_STABLE`. `LIVE_FEED` has **no** cap from commercial expiry.
2. **Start-only restriction** (start `KNOWN`, end field not declared): `NOT_YET_APPLICABLE` if `now < start`, otherwise `APPLICABLE`. It has no expiry, never emits `TIME_LIMITED` or `TEMPORARY_FREE`, and emits `TIME_WINDOW_RESTRICTED` when it is a FREE group.
3. **End-only** behaves as in Revision 2. **Both fields absent** = `NO_DECLARED_RESTRICTION`. Any declared boundary that is `NULL`, `MALFORMED`, `UNKNOWN` or `CONFLICT`, or any `end ≤ start`, gives `RESTRICTION_UNRESOLVED`. It is never ignored or treated as satisfied.
4. **Per-group applicability.** Applicability is evaluated per price group. Each classification, qualification and `FreeAssessment` uses **its own supporting group**. An expired free group alongside an active paid group is not currently FREE.
5. **Assessment facts** are reported separately and all are kept:
   - `presence`: `PRESENT`, `ABSENT_FROM_LATEST`, `LATEST_ROW_INCONSISTENT`, `LATEST_SNAPSHOT_INCONSISTENT`, `LATEST_SNAPSHOT_UNSUPPORTED` or `LATEST_EVIDENCE_ONLY`;
   - `freshness`;
   - per-group applicability.

   Stale and expired coexist as distinct facts.
6. **Summary `CurrentStatus`.** `CURRENT` iff presence is `PRESENT`, freshness is `FRESH`, and at least one group is `APPLICABLE` or `NO_DECLARED_RESTRICTION`. Otherwise `NOT_CURRENT(primary_reason, all_reasons)`, where `primary_reason` is the first match in this order:
   1. `LATEST_SNAPSHOT_UNSUPPORTED`
   2. `LATEST_SNAPSHOT_INCONSISTENT`
   3. `LATEST_ROW_INCONSISTENT`
   4. `LATEST_EVIDENCE_ONLY`
   5. `ABSENT_FROM_LATEST`
   6. `UNVERIFIABLE`
   7. `STALE`
   8. `RESTRICTION_UNRESOLVED`
   9. `EXPIRED`
   10. `NOT_YET_APPLICABLE`
   11. `SCHEDULE_UNEVALUATED`

   The group-applicability reasons (8–11) are counted only when no group is applicable, and each is taken across all groups.

Tests: TP‑1 (amended), TP‑8 to TP‑11, FD‑11 to FD‑14. Units: U2.

### AM‑16 — FREE and `/deals` · Type S

**This amendment supersedes Revision 2 §E.7 (FREE clause).** These are domain semantics only; the Step 2 presentation is not frozen.

| FREE assurance | Deal-candidate effect (requires `current_discoverable = true`) |
|---|---|
| VERIFIED | May contribute deal reason `FREE_VERIFIED` |
| QUALIFIED | May contribute `FREE_QUALIFIED`, carrying every warning and restriction |
| POSSIBLE | Contributes **no** deal reason. Discovery under `/free` only. |

FREE never produces a savings percentage and never becomes a `SavingsQualification`. One entry per `offer_id` is kept, as in Revision 2.

Tests: FD‑17. Units: U6.

### AM‑17 — Editorial corrections · Type T

1. Every "HC‑1 pending", "disabled until ruling" and CI v1 "never FREE" statement in Revision 2 (§C.9, §E.1, §E.6, §G.1, §G.2, §J‑1, U4, FR‑*, the handoff) is superseded.
2. **GB‑3 clarified:** no *production* module outside `offers/` imports `offers`. Tests may.
3. **Baseline wording:** the Python 3.10 diagnostic run is unresolved historical evidence. It is neither diagnosed nor passing. U0 must produce fresh evidence and diagnose every failure.

---

## 3. Reconciled test matrix

**Unchanged and authoritative** (Revision 2 §I):
- GB‑1, GB‑2, GB‑4
- OI‑1 to OI‑6, OI‑8 to OI‑11
- HI‑1 to HI‑6
- IS‑2, IS‑4 to IS‑6
- CS‑1, CS‑2, CS‑5 to CS‑11
- TP‑2 to TP‑7
- PF‑1 to PF‑7
- PR‑4 to PR‑7
- the 297-test regression requirement

**Removed:** FR‑1 to FR‑4 (replaced by FD‑*).

**Changed**

| ID | Revised expectation |
|---|---|
| GB‑3 | Production modules outside `offers/` do not import it; tests may |
| OI‑7 | Two discriminators with different `[kind, value]` give different IDs |
| CS‑3 | Fictional policy with an explicit reviewed `CHARGE_POSSIBLE` rule for a request fee: component comparison OK with assumption noted; savings `INSUFFICIENT_EVIDENCE` |
| CS‑4 | Any uninterpreted field, scalar or structured, defaults to `TERMS_POSSIBLE` → component `INSUFFICIENT_EVIDENCE(TERMS_UNRESOLVED)` |
| IS‑1 | Row whose column disagrees with type-preserving re-derivation → `ROW_INCONSISTENT`; other rows interpreted |
| IS‑3 | Split: `total_count` of `true`, `"5"`, `1.5` or `-1` → `SNAPSHOT_INCONSISTENT(TOTAL_COUNT_INVALID)`; valid count ≠ rows → `TOTAL_COUNT_MISMATCH` |
| IS‑7 | Unknown metadata key → `UNSUPPORTED_SOURCE(KEY_SET_MISMATCH)` |
| TP‑1 | Adds: declared-but-null boundary → `RESTRICTION_UNRESOLVED` |
| PR‑1 | `example/model`: `NORMAL_LIST`; all extra keys `TERMS_POSSIBLE`; no FREE (no zero basis). `example/model:free` → FREE + VERIFIED, scope "OpenRouter catalog pricing record", `USAGE_LIMIT_UNKNOWN` |
| PR‑2 | CI events equal the deduplicated legacy prefix events (AM‑12); mapping to occurrences verified |
| PR‑3 | A `QUOTE_UNSUPPORTED` or v2 frame closes the occurrence (`UNINTERPRETABLE`) and creates no new occurrence |
| PR‑8 | CI v1 zero base input and output → FREE + POSSIBLE + `PARTIAL_ZERO_RATE_ONLY` (+ `ADDITIONAL_CHARGES_UNRESOLVED` for `discount_percent` etc.); accepted wording code present |

**Added — identity**

| ID | Case | Expected |
|---|---|---|
| OI‑12 | Same discriminator, different `rule_id` | Same `offer_id` |
| OI‑13 | Policy emits a discriminator without an emission contract, or outside its mapping | `RegistryError` / propagated error |
| OI‑14 | Production manifest | Zero emission contracts (static) |

**Added — digests**

| ID | Case | Expected |
|---|---|---|
| PF‑8 | `0` vs `"0"` in `source_record` | Different `input_digest` and `content_digest` |
| PF‑9 | `false` vs `"false"` | Different |
| PF‑10 | `null` vs `""` | Different |
| PF‑11 | `{}` vs `[]` | Different |
| PF‑12 | A requested provider with empty history | Present as `[name, []]`; adding it changes `input_digest` |
| PF‑13 | Provider request order permuted | Identical `input_digest` |
| PF‑14 | Same commercial state at two times or snapshots | Equal `content_digest`. A changed declared validity boundary → different |
| PF‑15 | Same inputs, different `now` | Equal `semantic_digest`, different `evaluation_digest` |

**Added — savings**

| ID | Case | Expected |
|---|---|---|
| CS‑3b | Scalar uninterpreted field, no explicit rule | `TERMS_POSSIBLE` |
| CS‑12 | Input equal, output unknown | `INSUFFICIENT_EVIDENCE` |
| CS‑13 | Input higher, output unknown | `NOT_QUALIFIED(KNOWN_HIGHER_COMPONENT)`; wording limited to dominance |
| CS‑14 | Offer-wide unresolved fee outside the selected group | In scope → `INSUFFICIENT_EVIDENCE` |
| CS‑15 | Both groups `NOT_REPRESENTED` on a required attribute | `INSUFFICIENT_EVIDENCE(APPLICABILITY_UNRESOLVED)` |
| CS‑16 | Contradictory condition, or a difference outside the supported scenario | Not `QUALIFIED_WITH_CONDITIONS` |
| CS‑17 | Several problems at once | Summary per precedence; every reason kept |

**Added — inconsistency and OpenRouter**

| ID | Case | Expected |
|---|---|---|
| IS‑8 | Valid snapshot with `total_count = 3` | Each one-row reconstruction is not compared with 3 |
| IS‑9 | Unsupported empty snapshot is the latest | `LATEST_SNAPSHOT_UNSUPPORTED`, not `ABSENT_FROM_LATEST` |
| IS‑10 | Recognized, consistent empty snapshot (`total_count = 0`) | `ABSENT_FROM_LATEST` |
| IS‑11 | Recognized empty snapshot with `total_count = 3` | `SNAPSHOT_INCONSISTENT`; no valid absence |
| IS‑12 | Retained `Decimal('0')` vs parser `int 0` | Equal (number category) |
| IS‑13 | Unsupported pricing number vs string with the same text | `ROW_INCONSISTENT` |

**Added — CI v1**

| ID | Case | Expected |
|---|---|---|
| PR‑9 | `available_until: null` | `RESTRICTION_UNRESOLVED`; assessment kept; not current-discoverable |
| PR‑10 | `available_until` absent | `NO_DECLARED_RESTRICTION` |
| PR‑11 | Decrease followed by unchanged frames | One `ObservedPriceChange`; `legacy_active_through_snapshot_id` advances |
| PR‑12 | Then increase, mixed, reset or absence | `legacy_cleared_by` set, matching legacy |
| PR‑13 | Empty and v2 frames | Passed to the legacy analysis unfiltered; barriers present |
| PR‑14 | Stale or expired at `now` | Legacy-derived events unchanged |
| PR‑15 | Legacy `Presence.CURRENT` vs `CurrentStatus` | Not asserted equal |

**Added — temporal**

| ID | Case | Expected |
|---|---|---|
| TP‑8 | Start-only, at `start − 1µs` and at `start` | `NOT_YET_APPLICABLE`, `APPLICABLE`; no `TIME_LIMITED` |
| TP‑9 | Expired group plus active group | Offer `CURRENT` via the active group; claims on the expired group not current |
| TP‑10 | Stale and expired together | Both reasons kept |
| TP‑11 | Summary priority | Deterministic `primary_reason` per AM‑15 §6 |

**Added — HC‑1 / FREE (replaces FR‑*)**

| ID | Case | Expected |
|---|---|---|
| FD‑1 | Whole `BillingScope` `KNOWN(0)`, nothing unresolved | FREE + VERIFIED |
| FD‑2 | Plus a known allowance | FREE + QUALIFIED + `USAGE_LIMITED(metric, qty, period)` |
| FD‑3 | Complete monetary zero, limit unknown | FREE + VERIFIED + `USAGE_LIMIT_UNKNOWN` |
| FD‑4 | Overage charges unknown | Not VERIFIED (QUALIFIED with known allowance; POSSIBLE without) |
| FD‑5 | Zero primary rates plus an uninterpreted pricing field | FREE + POSSIBLE + `ADDITIONAL_CHARGES_UNRESOLVED(field paths)` |
| FD‑6 | Primary scope `NULL`, `MISSING`, `MALFORMED`, or an uninterpreted `"0"` | No FREE |
| FD‑7 | Only a non-primary zero (e.g. cached input) | No FREE |
| FD‑8 | Input 0, output unknown | No FREE; `ZeroRateFact(zero={INPUT}, unresolved={OUTPUT})` |
| FD‑9 | Zero tokens plus a known established request fee | FREE + POSSIBLE + `KNOWN_ADDITIONAL_CHARGE(REQUEST, amount)`; amount disclosed |
| FD‑10 | CI v1 zero base input and output | FREE + POSSIBLE + `PARTIAL_ZERO_RATE_ONLY`; accepted wording code |
| FD‑11 | Temporary free at `end − 1µs` and at `end` | Discoverable, then not; `TEMPORARY_FREE`; no inferred paid price |
| FD‑12 | Future bounded free window | Assessment exists; not current-discoverable |
| FD‑13 | Expired free group plus active paid group | Not currently FREE |
| FD‑14 | Recurring free schedule | `SCHEDULE_EVALUATION_DEFERRED`; not current-discoverable |
| FD‑15 | Eligibility restricted | FREE + QUALIFIED + `ELIGIBILITY_RESTRICTED`; no user-qualifies field |
| FD‑16 | FREE added to or removed from an occurrence | `offer_id` unchanged |
| FD‑17 | `/deals` | VERIFIED → `FREE_VERIFIED`; QUALIFIED → `FREE_QUALIFIED` with warnings; POSSIBLE → no deal reason |
| FD‑18 | Stale evidence | Assessment kept; `current_discoverable = false` |
| FD‑19 | No established monetary zero in a group | No `ZeroRateFact` (no vacuous truth) |
| FD‑20 | CI v2 | No `FreeAssessment` |

---

## 4. Implementation-sequence amendments

Revision 2 U0–U8 stand except as follows.

| Unit | Change |
|---|---|
| **U0** | Unchanged scope. Completion now requires **fresh** evidence on Python ≥ 3.11 with every failure diagnosed (AM‑17). |
| **U1** | **Owns** the AM‑8 tagged canonical encoder, the `content_digest` projection and the identity hash (kind and value only, AM‑7 §1). Adds PF‑8 to PF‑11, PF‑14 and OI‑12. |
| **U2** | **Owns** start-only handling, per-group applicability, the separate assessment facts and the summary priority (AM‑15). Adds TP‑8 to TP‑11. |
| **U3** | **Owns** recognition-then-validation precedence and the absence rules (AM‑14); **enforcement of discriminator emission contracts** in the registry (AM‑7 §2, §7); and the `input_digest`, `semantic_digest` and `evaluation_digest` projections. Adds IS‑3, IS‑7, IS‑9 to IS‑11, OI‑13, OI‑14, PF‑12, PF‑13, PF‑15. |
| **U4** | Type-preserving re-derivation (AM‑11). The OpenRouter policy declares `primary_scope`, `BillingScope` (`$.pricing`, complete), all-`TERMS_POSSIBLE` handling, no `CHARGE_POSSIBLE` rules, and its applicability requirements and scenarios. **No HC‑1 dependency remains.** Adds IS‑1, IS‑8, IS‑12, IS‑13 and the amended PR‑1 policy-level parts. |
| **U5** | **Owns the CI legacy event projection** in a new file, `offers/changes_ci_v1.py` (AM‑12). It also covers the `available_until` rules (AM‑13) and the CI policy declarations (`primary_scope`, `BillingScope` incomplete, partial-rate scope code). Adds PR‑2, PR‑3 and PR‑9 to PR‑15. |
| **U6** | **Owns `FreeAssessment`, the HC‑1 decision procedure and all warning rules** in a new file, `offers/free.py` (AM‑1 to AM‑6). It also covers `ZeroRateFact` (AM‑4), the savings clarifications (AM‑10) and the FREE/deals mapping (AM‑16). Adds FD‑1 to FD‑20, CS‑3, CS‑3b, CS‑4, CS‑12 to CS‑17 and PR‑8. Prerequisites: U4 and U5. |
| **U7** | Unchanged: end-to-end acceptance against the reconciled matrix, plus the regression suite. |
| **U8** | Unchanged. Docs must describe FREE assurance levels and warnings accurately. |

Review levels are unchanged. The new files `free.py` and `changes_ci_v1.py` sit inside the accepted `offers/` package; no new architecture unit is introduced.

---

## 5. Supersession table

| Revision 2 location | Amendment | Revised normative rule |
|---|---|---|
| §C.0 `field_class` assignment | AM‑9 | `TERMS_POSSIBLE` by default; `CHARGE_POSSIBLE` only through an explicit reviewed rule; never decided by shape |
| §C.4 `ProductDiscriminator` and `offer_id` formula | AM‑7 | Hash `[kind, value]` only; `rule_id` in provenance; static emission contract; zero production emitters |
| §C.6 `zero_declared_rates` field | AM‑4 | Replaced by `ZeroRateFact`: nonempty, scoped, records unresolved dimensions |
| §C.6 `content_digest` definition | AM‑8 §3 | Explicit commercial projection with tagged types |
| §C.7 `CurrentStatus` | AM‑15 §5–6 | Separate facts plus a deterministic summary priority; applicability per group |
| §C.9 FREE "pending" and attribute | AM‑1, AM‑4 | FREE is active; `FreeAssessment` added; `ZeroRateFact` |
| §C.15 digests | AM‑8 §2, §4–5 | `input`, `semantic` and `evaluation` digests defined |
| §D.3 matching rules | AM‑14 §1–2 | Recognition, then variable validation |
| §D.4 row 3 example | AM‑14 §2 | Invalid `total_count` → `SNAPSHOT_INCONSISTENT`; unknown key → `UNSUPPORTED_SOURCE` |
| §D.5 step 4 (absence) | AM‑14 §4 | Only recognized, consistent snapshots establish absence |
| §D.7 canonical encoding | AM‑8 §1 | Tagged, type-preserving `mpw-canon-v1` |
| §D.8 fingerprint | AM‑8 §2 | Provider envelope, ordering and empty histories specified |
| §E.1 FREE row | AM‑1, AM‑3 | Discovery classification with assurance |
| §E.2 applicability phrase | AM‑10 §1 | Policy-declared requirements; `NOT_REPRESENTED` never satisfies one |
| §E.3 outcome and "nothing lower" rules | AM‑10 §2–5 | Complete-scope procedure (a)–(f); supported scenarios; offer-wide scope |
| §E.6 (entire) | AM‑1 to AM‑3, AM‑5 | Deterministic HC‑1 procedure with structured warnings |
| §E.7 FREE clause | AM‑16 | VERIFIED and QUALIFIED may be deal reasons; POSSIBLE is not |
| §E.8 for CI v1 | AM‑12 | Legacy reducer is the sole monetary authority |
| §F.1 | AM‑15 §1 | Category caps only; `LIVE_FEED` uncapped |
| §F.2 | AM‑15 §2–4 | Start-only defined; malformed or null declared bounds unresolved; per-group evaluation |
| §F.4 | AM‑15 §6 | Summary `CURRENT` requires an applicable group |
| §G.1 consistency check | AM‑11 | Type-preserving one-row re-derivation; separate snapshot checks |
| §G.1 shape and `discount` rule | AM‑9 | All extra keys `TERMS_POSSIBLE` |
| §G.2 change compatibility | AM‑12 | Prefix legacy projection; dedup by snapshot pair |
| §G.2 `available_until` | AM‑13 | Null → `RESTRICTION_UNRESOLVED` |
| §G.2 "never FREE" | AM‑6 | FREE + POSSIBLE + `PARTIAL_ZERO_RATE_ONLY` |
| §I FR‑1 to FR‑4, PR‑2, PR‑3, PR‑8, CS‑3, CS‑4, IS‑1, IS‑3, IS‑7, TP‑1, PR‑1, OI‑7, GB‑3 | §3 | Replaced or amended as listed |
| §J‑1, §J‑4, §J‑6 | AM‑1, AM‑13, AM‑9 | Resolved |
| §K U0, U1–U6 | §4 | Ownership and criteria updated |
| §B.1 baseline caveat | AM‑17 §3 | Historical, undiagnosed evidence; U0 must diagnose |
| Revision 2 handoff ("HC‑1 pending") | AM‑17 §1 | Superseded |

---

## 6. Astra amendment closure table

| Astra required amendment (§11) | Addressed where | Status |
|---|---|---|
| 1. HC‑1 FREE discovery semantics | AM‑1 to AM‑6, AM‑16; FD‑1 to FD‑20 | RESOLVED |
| 2. Type-preserving canonical encoding and digest projections | AM‑8; PF‑8 to PF‑15 | RESOLVED |
| 3. ProductDiscriminator identity narrowing | AM‑7; OI‑7, OI‑12 to OI‑14 | RESOLVED |
| 4. Conservative uninterpreted-field assignment | AM‑9; CS‑3, CS‑3b, CS‑4, PR‑1 | RESOLVED |
| 5. Savings clarifications | AM‑10; CS‑12 to CS‑17 | RESOLVED |
| 6. Narrow OpenRouter re-derivation | AM‑11; IS‑1, IS‑8, IS‑12, IS‑13 | RESOLVED |
| 7. CI legacy monetary-transition authority | AM‑12; PR‑2, PR‑3, PR‑11 to PR‑15 | RESOLVED |
| 8. Temporal and status edges, including CI null expiry | AM‑13, AM‑15; TP‑8 to TP‑11, PR‑9, PR‑10 | RESOLVED |
| 9. Registry and failure precedence | AM‑14; IS‑3, IS‑7, IS‑9 to IS‑11 | RESOLVED |
| 10. Acceptance-matrix reconciliation | §3, §4 | RESOLVED |
| Editorial (Astra §11, non-blocking) | AM‑17, AM‑15 §1, §5 | RESOLVED |

---

## 7. Self-audit

| # | Statement | True? | Where |
|---|---|---|---|
| 1 | HC‑1 is no longer described as pending | Yes | §1, AM‑1 §5, AM‑17 §1 |
| 2 | `/free` supports VERIFIED, QUALIFIED and POSSIBLE | Yes | AM‑1 §4 |
| 3 | Warning-bearing free offers stay discoverable where affirmative evidence exists | Yes | AM‑3 step 4, FD‑5 |
| 4 | Unknown values cannot manufacture FREE | Yes | AM‑3 step 1, FD‑6 |
| 5 | Known charges are disclosed as known | Yes | AM‑2, FD‑9 |
| 6 | CI v1 zero base rates can appear as FREE + POSSIBLE with warning | Yes | AM‑6, FD‑10 (current discoverability per AM‑13) |
| 7 | CI v2 stays evidence-only | Yes | AM‑6 §4, FD‑20 |
| 8 | FREE assurance does not change offer identity | Yes | AM‑1 §2, FD‑16 |
| 9 | Discriminator `rule_id` does not affect identity | Yes | AM‑7 §1, OI‑12 |
| 10 | No broad `market_scope` equivalent has reappeared | Yes | AM‑7 §3–4 |
| 11 | Canonical digests keep JSON value types | Yes | AM‑8 §1 |
| 12 | OpenRouter re-derivation keeps retained value types | Yes | AM‑11 §1, §5 |
| 13 | CI legacy monetary transitions have one authority | Yes | AM‑12 §1 |
| 14 | `available_until: null` is not treated as unrestricted | Yes | AM‑13 |
| 15 | Unknown or uninterpreted pricing defaults conservatively | Yes | AM‑9 |
| 16 | Component comparison stays separate from overall savings | Yes | Revision 2 §E.2 vs AM‑10 |
| 17 | FREE + POSSIBLE never becomes an unqualified savings claim | Yes | AM‑16 |
| 18 | Production curated execution stays deferred | Yes | Revision 2 §C.16, unchanged |
| 19 | Recurrence execution stays deferred | Yes | AM‑5 §3 |
| 20 | Cross-provider equivalence stays in Gate 1c | Yes | Revision 2 §C.3/§E.5, unchanged |
| 21 | No storage, schema or acquisition change is required | Yes | §1; all amendments are inside `offers/` |
| 22 | No implementation has occurred | Yes | Read-only; nothing modified or run |

---

## 8. Focused Astra confirmation handoff

## Astra Confirmation Handoff

- **The Revision 2 architecture is unchanged.** Every amendment sits inside the accepted `offers/` package. None needs a schema, storage, contract, acquisition, scanner or legacy-module change, so no scope-stop condition applies.
- **The Human Chair approved Revision 2 subject to Astra's amendments.** This addendum, read with Revision 2, is the proposed authoritative specification. The supersession table (§5) shows which Revision 2 statements no longer control.
- **HC‑1 is binding and incorporated:**
  - FREE is one discovery classification on the existing offer, with VERIFIED, QUALIFIED and POSSIBLE assurance and structured warnings (AM‑1 to AM‑5);
  - CI v1 zero base rates appear as FREE + POSSIBLE (AM‑6);
  - VERIFIED and QUALIFIED may be deal reasons; POSSIBLE is not (AM‑16).
- **All ten required amendments are resolved normatively** (§6).
- **Points for the brief confirmation pass:**
  - **(a)** The AM‑3 step 3 undermining test. It requires policy-established evidence, so default `TERMS_POSSIBLE` fields limit completeness but do not block discovery. This is my reading of HC‑1's "undermine vs. completeness" distinction.
  - **(b)** AM‑10 §1's consequence: production comparisons and qualifications are all `INSUFFICIENT_EVIDENCE` until a reviewed policy version establishes source applicability.
  - **(c)** AM‑13's consequence: CI v1 FREE + POSSIBLE assessments are never currently discoverable when the retained rows carry `available_until: null`. This is a disclosed consequence, not a contradiction. Changing it would take a separate, evidence-backed decision on null semantics.

  None of these needs a Chair decision for Gate 1b.
- **Implementation remains unauthorized.** U0 (fresh, fully diagnosed baseline on Python ≥ 3.11) and a separate Human Chair implementation authorization remain prerequisites.

Next governance step: **Claude normative addendum → Astra brief confirmation → U0 baseline verification → Human Chair implementation authorization decision.** No implementation is authorized by this assignment.
