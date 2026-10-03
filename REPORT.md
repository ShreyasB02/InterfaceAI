# Design Report

## 1. Architecture

```
            goal + params                         params
                 |                                   |
   target app <- agent/ (discovery, LLM)      replay/ (no LLM) -> target app
                 |                                   ^
                 +--> artifacts/ (schema, store, review gate) --+
                 |                                   |
                 +---- guardrails/  and  escalation/ +   (shared by both)
```

Two execution paths that share no code except what sits between them.
`agent/` turns a goal into a recorded flow. `replay/` executes a recorded
flow and imports nothing from `agent/`; there is no model client it could
call. The **artifact** is the only thing that crosses, and it is data, not a
code path, so the three can evolve separately. `guardrails/` and
`escalation/` are used by both sides, which is what guarantees a policy
can't be enforced on one and forgotten on the other.

Decisions and their trade-offs:

- **Single process, synchronous, flat files.** Artifacts are small JSON
  documents a reviewer should be able to read and diff, so the store is a
  directory. No queue, service split or database: the brief counts that
  against a submission. The seams where they would go exist (the
  repository module, `ControlTransport`, the provider router).
- **Playwright, driven by the harness, not a vendor computer-use SDK.** The
  model chooses from eight generic tools; the harness performs the action
  and records it. That keeps recording exact (I record what was done, not
  what the model said it did) and keeps the model swappable.
- **Perception is a DOM-derived element list plus a screenshot each turn.**
  The list makes acting precise and cheap; the screenshot covers what
  markup doesn't say. This is the choice most tied to the web surface (§4).
- **Provider-agnostic model access** (`agent/llm/`): Gemini natively, and
  one adapter for any OpenAI-compatible endpoint, with backoff and ordered
  failover over one neutral message history. Discovery is the only part
  with an external dependency, and a single overloaded provider shouldn't
  be able to stall it.
- **Login is infrastructure, not a recorded step.** Both paths sign in
  before anything is observed. An artifact assumes an authenticated
  session, the way an API client attaches auth before a call, which keeps
  it portable across tenants with different login flows.

## 2. Artifact schema

`CapabilityArtifact` (`artifacts/schema/artifact.py`) is a contract an agent
can call and a person can review, not a transcript:

- **`input_schema` / `output_schema`**: named, typed, described. Inputs are
  validated before a browser opens; a run that doesn't produce every
  declared output, coercible to its type, is a failure, not a success with
  a gap.
- **`steps`**: ordered, typed actions. Each has an `intent` (what, in plain
  language) separate from its `target` (how), so the flow can be read
  without parsing selectors. Values reference an input by name
  (`value_param`) instead of being templated into strings.
- **`target: LocatorSpec`**: a ranked chain of strategies, each with its own
  written `reasoning`, not one selector. Order of preference: the form
  field's `name` attribute (the app's own submission contract, so it
  survives re-skinning), accessible role and name, visible text, and for
  reading values a label-relative XPath ("the cell in the row labelled
  Savings") that survives row reordering.
- **`checkpoint`**: the success condition, asserted, never assumed.
- **`known_outcomes` / `recoverable_patterns`**: the error taxonomy as
  declared, versioned data on the artifact, each with a detection locator
  and the step it follows. A reviewer or calling agent can see which
  non-success results the capability recognises. The alternative, if/else
  in the engine, would hide the contract in code.
- **`risk_level` / `requires_confirmation` per step**, because a capability
  is usually safe reads followed by one irreversible write; an
  artifact-level flag would over-block the reads or under-protect the
  write. **`origin`** records whether the model or a human performed the
  step during discovery (§5).
- **`provenance`**: goal, run id, provider, model, a pointer to the
  evidence, and how many times a human intervened. The transcript itself
  stays out.
- **`status` and `review`**: discovery emits a `draft`. The replay engine
  refuses a draft unattended. Approval records who, when, and a hash of
  everything replay executes, so editing an approved artifact voids the
  approval. `base_url` is outside the hash: one reviewed flow is dispatched
  to many tenants' hosts.

**Versioning.** One immutable file per semver; the store refuses to
overwrite. A re-discovery is the next major, an authoring pass the next
minor, each starting as a draft. An unpinned unattended call resolves to
the latest *approved* version, never a newer draft.

What I'd change: the discovered checkpoint is only a URL pattern (plus the
output check above). A step-level postcondition on each mutating step would
catch a click that silently did nothing, earlier.

## 3. Determinism & error handling

**Determinism.** No model call. Each step resolves its locator chain in
order and takes the first strategy matching *exactly one* visible element;
an ambiguous match counts as a miss, because acting on "one of several" is
the blind proceeding the brief warns about. Every wait is bounded. The run
ends by asserting the checkpoint and the outputs.

**The result contract** is an enum the caller switches on, never a message
to parse:

| Outcome | Meaning | Caller should |
|---|---|---|
| `success` | checkpoint held, all outputs returned | use the outputs |
| `business_outcome` | a declared outcome matched ("no such member") | treat as an answer |
| `failure` | couldn't complete; carries step, expected, observed | investigate |
| `input_error` | params don't satisfy the schema; no browser opened | fix the call |
| `refused` | artifact not approved for this kind of run | get it reviewed |
| `escalated` | waiting on a human who didn't respond in time | follow up |
| `interrupted` | an operator stopped the run | not retry blindly |

**Runtime conditions.** After each step the page is probed (a short
timeout, so the happy path isn't slowed) against the artifact's rules:

- *Business outcome*: stop and return its code. Not logged as an error. I
  class a validation rejection (deposit below minimum) here too: it is a
  deterministic answer to the caller's input.
- *Recoverable*: apply the declared action (`reload_and_retry`,
  `retry_step`, `dismiss_and_continue`) within a retry budget, then carry
  on. It never becomes a result class; it is listed in `recovered_steps`
  so a "successful" run still shows it wasn't smooth.
- *Hard failure*: anything else. A locator that never resolved, a timeout,
  a failed load, the app being down, an exhausted retry budget, a policy
  violation. Returned as `failure` with the step, what was expected, what
  was observed, a screenshot and a redacted DOM snapshot. No exception
  reaches the caller as a traceback.

Recoverable conditions that only a person can clear go to §5.

**Drift**, secondary per the brief: each step logs which strategy index
resolved, and `locator_fallbacks` reports steps that fell past their
primary. A tenant that starts reporting fallbacks is degrading before it
breaks.

**Limit.** A successful run never sees "no such member", so known outcomes
and recoverable patterns come from a separate authoring pass
(`agent/augment_artifact.py`), not from discovery.

## 4. Heterogeneity & multi-tenant

Design only, as the brief asks.

**Surface abstraction.** The seam is the artifact vocabulary. A `Step` says
"click the control identified by this ranked spec" and names no browser
API. Moving to another surface means a new perceive/act implementation
(today `agent/browser_surface.py` and `replay/locator_resolver.py`) and new
locator methods, not a new schema or a new executor loop:

- *Legacy web with framesets*: frame-aware resolution in the resolver, and
  a `frame` field on the locator strategy.
- *Desktop*: the same contract over the OS accessibility tree (UIA / AX),
  with `role`+name carrying over unchanged, plus a last-resort
  `screenshot_region` method (bounding box and OCR anchor).

Honest gap: the executor still calls Playwright directly for navigation,
dialogs and reload. Those dozen calls need to move behind a `Surface`
interface before a second surface is real. And discovery's perception leans
on the DOM; a surface with no usable markup needs the accessibility tree or
coordinates as the primary path.

**Multi-tenant reuse.** An artifact is recorded once against a vendor
product (`target.vendor_product`), looked up by `(vendor_product, name)`,
and dispatched to the calling tenant's `base_url`. The approval hash
excludes `base_url` for this reason. A re-skinned tenant mostly doesn't
break it, because locators prefer field names and accessible names over
anything visual.

Where a tenant truly differs (a relabelled field, an extra confirmation), a
thin **override** keyed by `(vendor_product, tenant_id, base_version)`
declares only the differing locator strategies, steps or outcome rules, and
is merged onto the approved base at load. It is reviewed like any artifact.
The ranked `LocatorSpec` is the insertion point: an override usually just
prepends a strategy.

**Drift per tenant and version**: aggregate `locator_fallbacks` and failure
rates by `(tenant, artifact version)`. Rising fallbacks flag that tenant's
override for re-review without disturbing the others; a new vendor version
gets a new base recording, with the old one kept for tenants still on it.

## 5. Escalation & handoff

**Four ways a run reaches a human**, through one mechanism:

| Trigger | Where | Detected by |
|---|---|---|
| The model can't safely proceed | discovery | the model calls `request_human` |
| The model is stuck without saying so | discovery | the harness: the same call 3 times running, or 3 failed calls in a row |
| A step needs a person: an irreversible action, or a value only a human supplied | both | the model's declaration in discovery; the artifact's flag in replay |
| A step failed and can't recover | replay | opt-in `--escalate-on-failure`; off by default so an unattended call fails promptly |

An operator can also take over uninvited, cancel a pending request, or stop
the run, at any step boundary. A dead end no human could fix ("no such
member") does not page one.

**The request** carries capability, goal, step, reason, current URL, a
screenshot, and the endpoint to attach to.

**Control transfer.** Both paths launch the browser with a debugging port
open, so the session a human attaches to is the one automation was driving.
From the request until `wait_for_resume()` returns, the human owns the
session and automation makes no page call. Ownership is one field on the
`ControlTransport` (`automation` / `paused_for_human` / `human_active` /
`resume_requested`) that both sides read. The operator attaches from a
separate process over CDP.

**What the human did is observed, not self-reported**
(`escalation/recorder.py`): a listener in the page reports their clicks and
field edits; navigations and dialogs come from page events. In replay this
lands in the result. In discovery it also becomes artifact steps marked
`origin: "human"`, so a flow that needed a person still records end to end
and replays. A value they typed is bound to an input if it matches one;
otherwise it is never written down and that step is handed to a human at
replay.

**Handing back.** The operator states whether they completed the paused
step or want automation to run it. The run then verifies the checkpoint as
usual. A failed step is escalated once.

**Mocked:** the console is a bare page, and a person drives the browser
through the headed window or `chrome://inspect`, not an embedded view. A
scripted operator stands in for a person's decisions in the evidence; the
attach, transfer, observation and resume are the real mechanism.
**Limits:** the recorder misses keyboard-only navigation; the transport is
file-backed, so console and run share a filesystem.

## 6. Safety

**Policy** (`guardrails/allowlist.py`) has four configurable axes: allowed
domains, allowed routes, allowed action types, and risky routes. Empty or
unparseable input blocks.

**Enforced before the fact.** Action type and navigation target are checked
before acting. A click's destination can't be known in advance, so every
request the session makes is hooked (`guardrails/network.py`) and one that
leaves the allowlist is aborted before it is sent; the test clicks "Log
Off" under a policy without `/logout` and shows the session still signed
in. The landed URL is checked afterwards to cover redirects.

**Risky actions require a human**, not a block (the capability exists to do
them) and not a flag afterwards (too late). Two signals classify one: the
app raised a confirmation dialog, or the action made a non-GET request to a
risky route. In discovery a declared irreversible click waits for approval,
and an undeclared one is stopped on the wire. In replay a flagged step
escalates, or runs under `--auto-approve` only if the artifact is approved;
an unflagged step is held to safe requests whatever the artifact claims.

**Data.** Logs are redacted whole and recursively: sensitive key names,
value shapes (SSN, card, account number, amount), and values the run itself
read off a record. Artifact text written by the model is generalized to
placeholders before saving. Sensitive-shaped values are tokenized before
they reach the model and restored only at the browser. Credentials come
from one seam and never enter an artifact, log, or prompt. A capability's
declared outputs are deliberately returned unredacted: they are the
deliverable.

**Limits.** Redaction is by pattern and provenance, not a PII classifier.
Screenshots are not pixel-masked (those in `/evidence/` show only fabricated
data). The policy knows routes and verbs, not business meaning.

## 7. Cuts

- **Multi-tenant dispatch and overrides**: designed (§4), not built.
  Proving it needed a fabricated second tenant; the time went to the replay
  contract and the handoff.
- **A second surface**, and the `Surface` interface extraction it needs.
- **A real operator console**: out of scope per the brief; a bare page over
  the real mechanism.
- **Discovered error handling**: known outcomes come from an authoring
  pass written per capability. Next step: a declarative profile per vendor
  app, applied to every artifact recorded on it.
- **Step-level postconditions** and a richer discovered checkpoint (§2).
- **Drift aggregation**: the per-run signal exists, the trend doesn't.
- **Scale infrastructure**: queueing, a database, a networked control
  transport.
- **Stretch goals**: the approval gate is built. Code generation,
  stability scoring and LLM-assisted single-step recovery are not.

Next, in order: tenant overrides, the per-vendor outcome profile, then an
accessibility-tree perception path to make "no clean DOM" concrete.
