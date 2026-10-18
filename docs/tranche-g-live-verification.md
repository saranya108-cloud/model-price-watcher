# Cheaper Inference live-verification evidence harness

This contributor tool prepares evidence for a separately authorized verification
of the existing Cheaper Inference public Standard integration. It delegates to
the production scanner, parser, transport and storage implementation. Offline
tests demonstrate the harness; they do not establish live compatibility.

## Revision 3 offline evidence-only compatibility

`cheaper_inference.public.standard` names the acquisition view/stream, not a
numeric certification of every scope within the response. Retained v2 raw rows
may contain pricing scopes and service tiers beyond the nominal Standard view.
Revision 3 retains those scopes as evidence only, without interpreting scope
precedence or selecting a default, cheapest, first, Standard or ZDR price.

Provider dispatch requires an exact source contract and interpretation policy:

| Source contract | Interpretation policy | Behavior |
|---|---|---|
| `cheaper-inference-public-models-v1` | `ci-advertised-base-token-v1` | Preserved historical Tranche F interpretation |
| `cheaper-inference-public-models-v2` | `ci-advertised-evidence-only-v2` | Current evidence-only admission; numeric pricing prohibited |

The exported `SOURCE_METADATA` represents v2; `V1_SOURCE_METADATA` remains
explicitly available. Mixed metadata is rejected. Row shape never selects a
contract and there is no fallback. Historical v1 retains its route-based
rejection, eligibility rules, numeric semantics and canonical basis keys. In v1,
a non-null ZDR route rejects the catalog; a true ZDR flag with no route retains
an ambiguous evidence-only row. V2 instead admits any safely representable route
string (including `seller`, `rail` and unfamiliar future strings) as evidence
and applies a complete numeric-price prohibition to every row. Unfamiliar
threshold/condition and pricing/media-unit strings likewise remain exact evidence,
without equivalence mapping, conversion or conditional-price interpretation.

Every accepted v2 observation has `advertised_quote=None`, both legacy numeric
price fields unset, and the retained reason
`numeric_scope_semantics_unestablished` (also emitted as a parser diagnostic).
Full, partial and both-unknown quote objects are forbidden. The complete raw row,
including top-level pricing and pricing scopes, remains retained. Service tier,
ZDR state, default scope, effective/expiry metadata, rounding and rate derivation
do not establish numeric authority. V2 requires `endpoint` as a string and
`supported_endpoints` as an array of strings. They are never used for routing or
price selection. Wrong documented structural types still reject the catalog.
Unknown row and nested scope fields are retained. Scope containers receive
structural checks only; missing or unfamiliar semantic content and reordered
scope lists remain opaque evidence, with no closed semantic allowlist or numeric
interpretation. The quote prohibition uses `is not None`, so equality-overriding
quote subclasses cannot appear absent. Exact source/evidence types prevent
equality overrides from defeating metadata dispatch or evidence validation.

The SQLite schema (version 2) and query/detection implementation remain
unchanged. Storage write preparation was hardened (BL-1–BL-4) without any schema
change. Direct writers, stored readers and pure history APIs validate the
declared policy against retained evidence. Forged v2 quotes are rejected. Valid
v1/v2 history remains readable, but a transition involving v2 is non-comparable
with `QUOTE_UNSUPPORTED` and clears any prior decrease event. Current selection
shows the v2 row without borrowing a stale historical v1 numeric quote.

Direct writers of v2 snapshots must supply exact built-in types. `started_at`,
`completed_at` and every `observed_at` must be an exact `datetime` whose
`tzinfo` is exactly `datetime.timezone` (for example `timezone.utc`); `zoneinfo`
or other timezone implementations and `datetime` subclasses are rejected. The
provider and offering IDs must be exact `str`, the source an exact
`SourceMetadata`, and each observation an exact `CatalogObservation`. Evidence
may use only `dict`, `list`, `str`, `int`, `Decimal`, `bool` and `None`; `float`
is rejected. Anything else is rejected before a transaction begins.

The byte-identical offline fixture
`tests/fixtures/cheaper_inference_public_catalog_g1b_live_01.json` comes from the
retained G1b Attempt 01 `rejected-body.bin`: 732,879 bytes, SHA-256
`eacd19c8b34bb51928180ceb7c2831315f70de9515e4d6ca9900c71b9e77e0f1`.
That original attempt remains HTTP 200, `catalog_rejected`, exit 3, one GET,
no snapshot and no retry. Offline replay admits 97 evidence-only rows under v2;
fake-connection runner findings are test expectations, not live A/B/C outcomes.
The original G1b package and pre-adaptation archive remain historical evidence.

Under evidence-only v2, a successful compatible runner execution can mechanically
produce A=true, B=false, C=true under the existing A/B/C definitions. C is
policy-determined because v2 prohibits quotes; it is not new evidence that the
provider lacks numeric prices. B is unreachable under this policy. Consequently,
a v2 compatibility run cannot satisfy the G1 A+B gate. This is a policy
clarification, not a new live result, and it does not authorize Attempt 02.

**No Attempt 02 is justified by this adaptation.** No live execution is part of
Revision 3 implementation. A future numeric-price policy requires new semantic
evidence plus separate architecture, review and authorization. Tranche G remains
incomplete; Outcome B is not established and numeric Standard pricing has not
been restored. The operator instructions below describe the preserved runner;
they grant no execution authority.

## Describe the executable sources

From the repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
python3 tools/verify_cheaper_inference_live.py --describe-sources
```

This prints a versioned JSON source inventory to stdout. It makes no network
request, creates no directory or database, and invokes no Git command. The runner
also disables bytecode writing itself before importing project modules, so this
mode does not depend solely on the environment variable to prevent caches.

The intended checkout comes from the runner's own location. After eager imports
cover success, failure and verification paths, every loaded module named
`model_price_watcher` or starting with `model_price_watcher.` participates.
Each must have an inspectable `.py` source under that checkout's
`src/model_price_watcher` tree. The runner records its own source too. Every entry
contains the module name (null for the runner), absolute path, byte count and
SHA-256. Standard-library modules are excluded. Inherited imports can include
the OpenRouter implementation; no OpenRouter request is made.

Compare the `sources` array with the independently reviewed candidate inventory
before authorizing execution. Generated recording timestamps are not source
identity. Keep the checkout and runtime quiescent. The runner does not ingest an
approval manifest, inspect `.git`, or establish approval on its own.

## Execute one separately authorized attempt

There are exactly two operator modes: source description and live execution.
Source description cannot be combined with live options. There is no
reconciliation command, URL option, timeout override or database override.

**Every live attempt requires separate explicit authorization naming the exact
reviewed candidate, command and new run directory.** This includes conditional
retention of a bounded parser-rejection body. A failed or limited attempt never
authorizes another request.

After that authorization only:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
python3 tools/verify_cheaper_inference_live.py \
  --run-dir ABSOLUTE_NEW_DIRECTORY \
  --execute-live
```

Replace the placeholder with the authorized absolute path. Its parent must
already exist; the final directory must not exist, including as a dangling
symlink. It must be outside the checkout, including paths that resolve into the
checkout through symlinked ancestors. Creation is exclusive. Existing attempts
are never reused, overwritten or automatically deleted.

The scanner's fixed request is:

- `GET https://api.cheaperinference.com/public/models`
- `Accept: application/json`
- `Accept-Encoding: identity`
- Socket timeout argument: `30.0` seconds.
- Maximum complete response body: `16,777,216` bytes (16 MiB).

The production transport may read one extra byte to detect overflow. The budget
is at most one application-level GET after preflight. There is no HEAD,
connectivity probe, authentication probe, retry, redirect follow, pagination or
schema/documentation lookup. No credentials, cookies, provider account or custom
TLS context is used. TLS verification follows standard `HTTPSConnection`
behavior. No inference request is made.

Execution is **bounded in request count and response size, not wall-clock time,
and operator-supervised**. The timeout applies to socket operations; it is not
a 30-second overall deadline. DNS, incremental delivery, parsing and persistence
can extend elapsed time. Interrupting a run does not establish rollback.

## Disposable database and verification

Each attempt initializes a new `catalog.sqlite` through the existing storage
initializer, verifies schema v2 and zero snapshots/observations, then calls the
unchanged production scanner once. It does not open or copy user history and
never requests migration. “Disposable” describes its isolated purpose; the
database remains evidence until separately authorized cleanup.

Before a request, an invalid clock or a clock earlier than an unexpectedly
stored successful completion causes an internal/chronology refusal (exit 7),
without database-corruption wording. This harness requires an empty database;
it does not provide a history-scanning operator mode.

After normal scanner return, the write connection closes. Fresh readback checks
the final database component with `lstat`, requires a regular non-symlink file,
and refuses any `-journal`, `-wal` or `-shm` sibling, including dangling symlinks.
It reads the 100-byte SQLite header before connecting and requires the SQLite
signature and rollback-journal mode bytes 18/19 equal to 1. It uses `Path.as_uri()`
plus `?mode=ro`, foreign-key enforcement, query-only enforcement and a read
transaction. There is no `immutable=1`, recovery, checkpoint, schema change or
repair. Symlinked ancestors are allowed. This boundary assumes a trusted,
quiescent filesystem, not hostile concurrent path replacement.

Physical integrity, exact schema and foreign keys are checked using existing
storage behavior (including sanctioned `storage._verify_schema`). Existing
readers and advertised queries validate the full relevant Standard stream.
The exact captured body is reparsed in memory with the recorded observation time
and same source metadata. Every accepted observation must agree with persistence,
including exact identity, Decimal values, quote basis and retained evidence.
Reparse makes no request and writes no snapshot.

The detailed query sample uses exact-ID ordering: up to two complete quotes,
one partial quote and one evidence-only row, when available. A deterministic
sentinel is checked for exact absence and adjusted if its ID is present.
Sampling is verification, not ranking, recommendation, alias matching or
normalization. All accepted rows still receive full round-trip verification.

## Evidence package

| File | Meaning |
|---|---|
| `run.json` | Runtime, invocation, limits, pre/post source inventories, clocks, request count, HTTP evidence, phase and persistence status |
| `catalog.sqlite` | Newly initialized schema-v2 database; accepted row evidence |
| `catalog-summary.json` | Four observation categories, parser diagnostics where available, structural field/type inventory |
| `selected-results.json` | Deterministic sample, absent sentinel and exact-ID report |
| `verification.json` | Integrity, round-trip, source drift and provisional A/B/C conditions |
| `manifest.sha256` | SHA-256 of completed package artifacts, excluding the manifest itself |
| `rejected-body.bin` | Conditional bounded body rejected at the parser boundary |

JSON artifacts carry format version 1, UTC recording timestamps, exact Decimal
strings and explicit nulls for unavailable evidence. The four exclusive
categories are complete quotes, partial quotes, wholly unknown quotes, and
evidence-only observations. Their sum equals accepted observation count.
Numeric-component observations are complete plus partial; complete numeric
quotes count only the first category. Rejected rows are not accepted observations.

Only Content-Type, Content-Encoding, Content-Length and Transfer-Encoding header
occurrences are recorded at the connection seam, preserving order and duplicates.
The recording wrapper does not consume the body. Status, body byte count and
body SHA-256 describe a complete bounded production transport return. On failure
before that return, body count and digest remain null, never invented zeroes.
Header-recording failure leaves scanner semantics intact and marks evidence
incomplete.

Wall start/end, request start/end and monotonic durations are distinct from
scanner observation/completion timestamps and the post-return timestamp.
`snapshot.completed_at` precedes persistence and **is not commit time**.

Accepted bodies remain transient in memory; the complete accepted body is not
saved a second time. Raw accepted row evidence is stored in SQLite. Only
`catalog_rejected`, after accepted HTTP framing, media type, encoding/UTF-8 and
bounded complete body acquisition, permits `rejected-body.bin`. HTTP error pages,
unsupported media, incomplete fragments and overflow bodies are not retained.

Pre-request intent records deliberately leave final request count unavailable
and persistence unestablished. Process loss can therefore leave an incomplete
package without falsely claiming zero requests or absent persistence. Source
drift, artifact I/O failure or interruption prevents a clean package. Preserve
the attempt and return for review; do not repair or retry automatically.

## Persistence and read-only reconciliation

“No snapshot was stored” requires positively located pre-write failure or safe
independent proof of absence. A scanner result alone is insufficient to establish
persistence: normal success also requires writer closure, safe independent
readback, validated expected snapshot and complete round-trip agreement.

Possible write, commit, post-COMMIT, finalization or verification failure means
**“Persistence outcome not established.”** Exception type/text, missing return,
connection closure, attempted rollback and process exit do not prove rollback.
If later artifact/output failure follows completed independent verification,
the established persistence fact is retained even though the command fails.

Reconciliation is the importable function in the runner:

```python
reconcile_run(
    run_dir,
    expected_sources=reviewed_sources_array,
    expected_snapshot=None,
    expected_observation_sha256=None,
)
```

Load the runner explicitly by filesystem path with standard-library
`importlib.util.spec_from_file_location` / `module_from_spec` / `exec_module`;
disable bytecode writing before loading. `expected_sources` is the explicit
independently reviewed `sources` array. Optional expectations supplement an
incomplete run using independently established snapshot fields and the runner's
typed semantic observation digest. Otherwise the recorded expectations are used.
That digest compares semantics, not original HTTP bytes.

The function makes no request, uses safe read-only opening, changes no package
file, and returns structured findings for separate recording. It checks
source and artifact hashes, complete stored evidence, category/structural
summaries and selected-ID agreement. Validated zero snapshots/observations proves
absence. A single validated expected snapshot with matching semantic contents
proves persistence. Mismatch or unsafe sidecars leaves persistence unestablished.
Persistence and whole-package reconciliation are separate findings: a manifest
failure does not erase an already independently established persistence fact.
An incomplete package cannot support acceptance. The function never rewrites
`run.json`, manifests, evidence or the database, and never adds a CLI mode.

## Interpretation and next gates

The runner records provisional conditions; external review determines outcomes:

- **A:** acquisition and persistence integration verified, including an accepted
  nonempty catalog, exactly one snapshot, full round-trip and selected-ID
  agreement, and source/evidence reconciliation.
- **B:** at least one complete numeric advertised quote survives exact-ID and
  exact-value persistence, demonstrating the numeric path live.
- **C:** an accepted catalog contains no complete supported numeric quote.
  C may coexist with A; acquisition failure is not C.

Never relax eligibility to manufacture B. Source-file hashes identify recorded
file bytes at measured paths; they do not prove Git commit identity, arbitrary
runtime integrity, bytecode provenance or machine integrity. The external
candidate comparison supplies approval binding. Body hashes and source metadata
do not authenticate a provider. Parsing does not prove upstream completeness
beyond the received accepted response. Database evidence permits semantic replay,
not reconstruction of accepted HTTP wire bytes. Structural inventory is review
evidence, not a second schema interpreter. One live catalog cannot prove historical
decrease behavior, final charges, guaranteed savings or execution availability.

Offline validation:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
python3 -m unittest discover -s tests -p test_live_verification.py -v

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
python3 -m unittest discover -s tests -v
```

No migration, automatic publication, upload, commit, scheduling or cleanup is
part of this tool. Implementation review precedes any separately authorized live
attempt; evidence interpretation and subsequent product work have their own gates.
