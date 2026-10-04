# Development Backlog

This is the permanent repository record for consciously deferred work, known
limitations, defensive hardening, dependency decisions, and architectural items
that must not depend on conversational memory.

If an issue is consciously deferred, it must be entered here before development
moves on. Completed items remain recorded as RESOLVED, with the resolving version
and commit when known.

Each item records ID, Status, Category, Origin, Description, Why deferred, Revisit
trigger, and Blocking. Allowed statuses are OPEN, PLANNED, BLOCKED, and RESOLVED.
Allowed categories are DEFECT, HARDENING, ARCHITECTURE, DEPENDENCY, and DATA_QUALITY.

## DB-001 — Legacy two-source reconciliation candidate selection

- **ID:** DB-001
- **Status:** OPEN
- **Category:** DEFECT
- **Origin:** v0.3B/v0.3C reconciliation
- **Description:** The legacy two-source `reconcile()` path may select the first
  exact canonical team-name match before enforcing the 24-hour candidate window,
  and only then calculate kickoff difference. The newer v0.3C graph reconciliation
  and v0.4B verification logic must not inherit this behavior.
- **Why deferred:** The currently used newer matching path already applies the
  appropriate candidate-window logic. Repairing legacy behavior was outside the
  scope of v0.4B/v0.5A.
- **Revisit trigger:** Before reusing, extending, exposing, or depending on the
  legacy two-source `reconcile()` path.
- **Blocking:** Not blocking v0.5B.

## DB-002 — v0.4B SFI competition mapping is ID + exact-name dependent

- **ID:** DB-002
- **Status:** OPEN
- **Category:** HARDENING
- **Origin:** v0.4B
- **Description:** `mapped_discovery` currently requires both the registered Soccer
  Football Info competition ID and the exact registered competition name. The SFI
  competition ID is expected to be the stronger identity, so requiring the name
  as well is intentionally conservative but brittle if the provider renames the
  competition while keeping the stable ID.
- **Why deferred:** Bundesliga mapping currently works and broader competition
  onboarding was outside v0.4B scope.
- **Revisit trigger:** Before adding additional competitions or when evidence
  shows SFI competition names can change independently of IDs.
- **Blocking:** Not blocking v0.5B.

## DB-003 — Verify observations lie inside declared verifier report windows

- **ID:** DB-003
- **Status:** OPEN
- **Category:** HARDENING
- **Origin:** v0.4B
- **Description:** v0.4B validates verifier report coverage/window metadata but
  does not independently verify that every observation contained in a verifier
  report actually lies within that declared report window. Current in-project
  collectors already guarantee this.
- **Why deferred:** Duplicating collector guarantees inside the handoff layer was
  unnecessary for the initial controlled pipeline.
- **Revisit trigger:** Before accepting verifier reports from external/untrusted
  generators or before the verifier ecosystem expands substantially.
- **Blocking:** Not blocking v0.5B.

## DB-004 — Permanent cross-provider fixture identity

- **ID:** DB-004
- **Status:** OPEN
- **Category:** ARCHITECTURE
- **Origin:** v0.4B
- **Description:** Candidate IDs are currently discovery-centered, e.g.
  `sfi:<fixture_id>`. A permanent provider-independent fixture/entity identity
  system was explicitly deferred.
- **Why deferred:** SFI is currently the discovery anchor and stable discovery IDs
  are sufficient for the present handoff.
- **Revisit trigger:** Before adding a second primary discovery provider,
  persistent historical candidate tracking across providers, or long-lived
  cross-provider entity relationships.
- **Blocking:** Not blocking v0.5B.

## DB-005 — API-Football current-season access

- **ID:** DB-005
- **Status:** BLOCKED
- **Category:** DEPENDENCY
- **Origin:** v0.3C
- **Description:** The API-Football integration works technically, but the current
  free plan does not provide access to the 2026 season. API-Football therefore
  remains an optional verifier rather than a required source.
- **Why deferred:** We deliberately chose not to purchase/upgrade the plan while
  other free and independent sources cover current development needs.
- **Revisit trigger:** If additional verifier coverage materially improves the
  system, if current sources prove insufficient, or before production requirements
  make a third authoritative verifier desirable.
- **Blocking:** Not blocking v0.5B. Do not purchase or upgrade automatically.

## DB-006 — Soccer Football Info future coverage is incomplete by competition

- **ID:** DB-006
- **Status:** OPEN
- **Category:** DATA_QUALITY
- **Origin:** v0.4A
- **Description:** Soccer Football Info successfully provides worldwide near-term
  fixture discovery, but testing showed that upcoming Bundesliga fixtures can be
  absent from its future daily feed even though historical/current Bundesliga
  data exists. Therefore SFI is supplementary discovery evidence only. Absence
  from SFI must never be interpreted as cancellation or proof that a fixture does
  not exist.
- **Why deferred:** This is a provider coverage characteristic rather than a
  collector defect.
- **Revisit trigger:** If SFI feed behavior changes, a better worldwide discovery
  source becomes available, or multiple discovery feeds are introduced.
- **Blocking:** Not blocking v0.5B.

## DB-007 — SFI timestamp semantics

- **ID:** DB-007
- **Status:** OPEN
- **Category:** DATA_QUALITY
- **Origin:** v0.4A
- **Description:** Future SFI match timestamps are currently treated as UTC based
  on observed behavior and the collector's documented assumption.
- **Why deferred:** No stronger provider timezone metadata was available during
  v0.4A work.
- **Revisit trigger:** If official SFI documentation clarifies timestamp semantics,
  observed fixtures show timezone drift, or another independent source
  demonstrates inconsistency.
- **Blocking:** Not blocking v0.5B.

## DB-008 — Evaluate acquisition engines before choosing one

- **ID:** DB-008
- **Status:** PLANNED
- **Category:** DEPENDENCY
- **Origin:** v0.5A → v0.5B
- **Description:** Direct HTTP, Scrapy, Crawl4AI and Firecrawl remain candidate
  acquisition methods. No general crawler has yet been selected as the permanent
  solution. The benchmark must evaluate representative football sources against
  the criteria below.
- **Why deferred:** v0.5A deliberately defined the evidence contract before
  introducing acquisition technology.
- **Revisit trigger:** Immediately in v0.5B.
- **Blocking:** This IS the next development task and must be completed before
  choosing or installing a permanent crawler stack.

Benchmark criteria:

- Successful content retrieval
- Article/link discovery
- JavaScript requirements
- Publication-time preservation
- Useful text extraction
- Provenance preservation
- Stability
- CPU/RAM usage
- Latency
- Maintenance burden
- Operational cost

## DB-009 — Verify source domains/URLs during onboarding

- **ID:** DB-009
- **Status:** PLANNED
- **Category:** DATA_QUALITY
- **Origin:** v0.5A
- **Description:** The initial evidence source registry contains Wettbasis,
  Wettfreunde, LeagueLane and Gooners Guide. It intentionally does not yet contain
  domains or URLs. Exact source domains/entry points must be verified before
  acquisition adapters are configured.
- **Why deferred:** v0.5A was offline and intentionally did not perform web
  acquisition.
- **Revisit trigger:** During v0.5B source onboarding.
- **Blocking:** Blocks live acquisition for each individual source until that
  source is verified.

## DB-010 — Onboard official, club, local-media and specialist sources

- **ID:** DB-010
- **Status:** PLANNED
- **Category:** ARCHITECTURE
- **Origin:** v0.5A
- **Description:** The source registry currently contains only four TIPSTER
  sources. Future gathering agents require verified sources in the other
  supported classes: OFFICIAL, PRIMARY_MEDIA, REPUTABLE_MEDIA, SPECIALIST and
  OTHER.
- **Why deferred:** Those sources should be added only as they are actually
  verified and onboarded, rather than inventing a large speculative registry.
- **Revisit trigger:** When implementing the first Team Availability,
  Lineup/Rotation, Schedule/Motivation and Local/Official News gathering agents.
- **Blocking:** Not blocking the acquisition benchmark itself.

## Resolved items

### DB-R001 — Source registry ID syntax hardening

- **ID:** DB-R001
- **Status:** RESOLVED
- **Category:** HARDENING
- **Origin:** v0.5A
- **Resolved in:** v0.5A, commit `0e61a62`
- **Description:** Initial source-ID validation allowed any nonempty sequence of
  lowercase letters, numbers and hyphens, including leading/trailing/consecutive
  hyphens.
- **Why deferred:** The initial four registered source IDs were already valid;
  hardening was deferred until before broader source onboarding.
- **Revisit trigger:** Before broader source onboarding; completed in v0.5A.
- **Blocking:** Resolved; no remaining blocker from this item.
- **Resolution:** Validation was tightened to `[a-z0-9]+(?:-[a-z0-9]+)*`.
  Regression tests reject leading, trailing and consecutive hyphens.

This resolved item remains in the backlog as an audit example of the new
deferred-work discipline.

## Project rules

1. New deferred items must be recorded here before moving to the next development
   step.
2. Every item must have a concrete revisit trigger.
3. “Remember later” is not an acceptable state.
4. Resolved items remain recorded with version/commit information.
5. Backlog entries do not authorize implementation automatically.
6. Dependency purchases, infrastructure expansion, or external-service upgrades
   still require explicit approval.
7. This backlog is not the product roadmap. It records deferred work, limitations
   and decisions that could otherwise be forgotten.
