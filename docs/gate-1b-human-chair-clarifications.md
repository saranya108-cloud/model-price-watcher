# Gate 1b — Human Chair clarifications and approval provenance

**Project:** Model Price Watch
**Record date:** October 19, 2026
**Specification:** [Revision 2](gate-1b-revision-2.md) + [Normative Amendment 1](gate-1b-normative-amendment-1.md), subject to the final ruling below.
**Implementation authority:** NONE — separately gated.

## Final Human Chair ruling (verbatim)

The Human Chair supplied the following exact final ruling directly in the documentation-commit chat on October 19, 2026, identifying it as the authoritative text from the MPW conversation. The ruling's U0-only next step records the decision at that time; the subsequent U0 result is recorded below.

```text
HUMAN CHAIR — FINAL GATE 1b SPECIFICATION RULING
Project: Model Price Watch
Gate: 1b — Offers and Interpretation Policies
Date: October 19, 2026
The Human Chair accepts Gate 1b Revision 2 + Normative Amendment 1, incorporating Astra’s final two narrow clarifications.
1. Component-rate comparison clarification
MPW may compare a narrowly defined pricing component when the basis of that component comparison is established, even when unrelated service attributes such as execution mode or service tier are not represented.
Such a result supports only the narrow claim being compared, for example:
“Provider A has the lower published input-token rate.”
It does not establish overall service equivalence or overall economic savings.
Attributes required for the specific comparison or a broader savings claim must still be established. NOT_REPRESENTED values must never be treated as equivalent defaults.
2. FREE discovery — known-charge clarification
The HC-1 /free discovery policy is clarified as follows:
Mandatory known charge
If reliable evidence establishes that every use within the asserted free scope necessarily incurs a positive charge, that price group must not be classified or discovered as FREE.
MPW should retain and disclose its zero-priced component facts and the known mandatory charge.
Charge beyond a genuine free allowance or optional paid condition
If a positive charge applies only:
- after an established free allowance;
- outside an established free condition;
- or to an optional separately established paid feature or usage mode;
the offer may remain discoverable under /free with the applicable restrictions and known charges disclosed.
Unresolved charge applicability
If meaningful zero/base-free evidence exists but MPW cannot determine whether another known or potential charge necessarily applies, the offer may remain:
FREE + POSSIBLE
with an explicit warning describing the charge and unresolved applicability.
It must never be represented as VERIFIED free.
Final specification status
With these two clarifications incorporated:
Gate 1b architecture and specification are APPROVED.
No further architecture or specification review is required before U0 unless new evidence reveals a material defect.
The next authorized activity is:
U0 — baseline verification only
U0 must verify the existing published baseline on supported Python ≥ 3.11 and diagnose any failures.
Gate 1b implementation is not yet authorized.
After successful U0, the Human Chair will separately decide whether to authorize implementation.
```

## Approval provenance

In the same direct message, the Human Chair identified the explicit approval reply to that ruling as:

> I approve

The Human Chair also supplied this subsequent status statement verbatim:

> Separately, U0 subsequently passed 297/297 tests on Python 3.11.16, and was accepted as U0 BASELINE VERIFIED. Implementation still remained separately gated after that result.

The final ruling governs the comparison scope and known-charge FREE rules wherever Revision 2 or Amendment 1 retains earlier wording. This record preserves the authored documents and the final ruling together rather than silently rewriting the historical proposals.

## U0 baseline verification evidence (verbatim reported result)

Source: [MPW handoff](chatgpt-conversation://6ad656d6-a2e8-83e8-a1f1-e7b31dafdf41), user message/turn `340a23bd-519e-4b2e-bd6e-6ec9cb192fed`. The following is the historical verification report, reproduced verbatim; no new test run is claimed by this documentation record.

````text
MPW GATE 1b — U0 BASELINE VERIFICATION RESULT

## Repository identity

- Branch: `main`
- HEAD: `d311857d0ad8fbed49bafa9c5bd52ca1b0a1ea1e`
- Expected baseline: `d311857d0ad8fbed49bafa9c5bd52ca1b0a1ea1e`
- Match: YES
- Commit message: `refactor: add provider-neutral storage boundary`
- Tracked tree: Clean
- Staged state: Empty
- Untracked paths: Existing `scratch/` only
- Cached `origin/main`: `2fb4be618ed1898c4f044e9b54b482aaad05346c`; no fetch performed

## Environment

- Python executable: `/Users/aclab/.local/bin/python3.11`
- Python version: **3.11.16**
- Other installed versions found: 3.14.7 and unsupported 3.9.6; neither used for testing
- Test command, run from the repository root:
```
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/aclab/.local/bin/python3.11 -m unittest discover -s tests -v

```

## Test result

- Tests run: **297**
- Passed: **297**
- Failed: **0**
- Errors: **0**
- Skipped: **0**
- Exit status: **0**
- Duration: 3.160 seconds

## Failure diagnosis

None — baseline suite passed.

## Post-test integrity

Pre-test and post-test inventories matched across all 618 repository entries, including `.git` and `scratch/`, using file hashes and modification metadata.

- HEAD unchanged: Confirmed
- Tracked tree unchanged: Confirmed; clean
- Staged state unchanged: Confirmed; empty, with index unchanged
- `scratch/` untouched: Confirmed
- No unauthorized persistent changes: Confirmed; no repository paths created, removed, or modified

## U0 disposition

**U0 BASELINE VERIFIED**

## Governance note

> This U0 verification does not authorize Gate 1b implementation, commits, pushes, or repository modifications.

**RECOMMENDATION TO HUMAN CHAIR: Accept U0 as verified and require separate explicit authorization before Gate 1b implementation begins.**
```
  Worked for 1m 19s • 1:00 PM

```
````

## Specification-source provenance

- Revision 2: [Review MPW Gate 1b specification](codex://threads/01a15581-585e-76b0-97b7-e9ea1b2c891e), user message `01a1558a-3a4d-7593-a97d-73c4f3653697`. Full authored specification; no summary substituted.
- Amendment 1: [Confirm Gate 1b amendments](codex://threads/01a155b7-b49e-7cd3-91e0-bbcba1428b0e), user message `01a155b8-1558-7dd3-8966-c34357d8ed47`. Full authored amendment; only the conversational prefix is excluded.
- Astra's confirmation in that second chat returned `CONFIRMED WITH MINOR AMENDMENTS` and identified the two final clarifications. It was a review recommendation; the directly supplied Human Chair ruling above establishes the final approval.

## Separate implementation-authorization boundary

**Gate 1b Revision 2 + Normative Amendment 1, incorporating the final Human Chair clarifications, are approved. U0 is baseline verified. Gate 1b implementation remains separately gated and is not authorized by this record, the U0 result, or this documentation-only commit.**

The current authorization covers exactly these three new documentation files and one local documentation-only commit. It authorizes no implementation and no push.
