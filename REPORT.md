# Design Report

## 1. Architecture

Four things that don't know about each other's internals, connected by two
contracts:

```
target_app  <--browser-->  agent (discovery)  --artifact-->  artifacts/store
                                                                    |
target_app  <--browser-->  replay (executor)  <-------------------+
                                 |
                                 v
                          escalation (control channel, CDP handoff)
```

- **`agent/`** only knows how to turn a goal into a recorded trace of
  actions. It never knows the artifact will be replayed without it.
- **`replay/`** only knows how to execute a `CapabilityArtifact`. It has
  never heard of an LLM and can't call one even if it wanted to — there
  is no model client anywhere in `replay/`.
- The **artifact** (`artifacts/schema/`) is the only thing that crosses that
  boundary, and it's a plain, versioned, serializable Pydantic model, not a
  shared code path. This is the actual point of the exercise: the model
  discovers, the artifact is the reusable capability, replay is how an AI
  agent invokes it in production, and those three things should be able to
  evolve independently.
- **`guardrails/`** and **`escalation/`** are used by both discovery and
  replay (allowlist enforcement, redaction, the human-handoff channel) —
  cross-cutting, not owned by either side.

**Single process, synchronous, flat-file storage.** No queue, no service
boundary, no database. Artifacts are small JSON documents a human should be
able to `cat` and review; a directory of them (`artifacts/store/*.json`,
one immutable file per version, `<name>.v<semver>.json`) gives versioning and diffability for
free. The brief is explicit that scaling infrastructure isn't the point of
this exercise and that over-building it is a negative signal — everything
here is designed so that a queue, a service split, or a real DB could be
dropped in later without touching the artifact schema or the replay
contract, but none of it is built preemptively.

**Session/auth is bootstrapped outside the artifact.** Both discovery and
replay log in with a hardcoded operator credential *before* any recorded
step runs, and login is never part of the recorded trace. A capability
artifact assumes an authenticated context, the same way a production API
client attaches auth before calling an endpoint — it's what keeps an
artifact portable across tenants that might use entirely different login
flows (see §4).

**Discovery's perception layer is deliberately separate from replay's.**
`agent/browser_surface.py` enumerates live DOM elements and injects a
transient `data-cua-idx` attribute so the LLM can act by index *this
session only* — that attribute is never recorded. What gets recorded is
`agent/locator_inference.py`'s derivation of ranked, semantic locator
strategies (CSS-by-name, role+accessible-name, text, XPath-by-label) from
each element's real attributes. `replay/locator_resolver.py` re-resolves
those strategies against a fresh page with none of discovery's
scaffolding. Neither side depends on the other's mechanism — only on the
`LocatorSpec` in between.

## 2. Artifact schema

This was the piece I spent the most deliberate design time on, because the
brief calls it out as the focal point and because a thin schema here would
have quietly pushed all the hard decisions into replay-engine code where a
reviewer couldn't see them.

A `CapabilityArtifact` (`artifacts/schema/artifact.py`) has:

- **`steps`**: ordered, typed actions (`navigate` / `fill` / `click` /
  `wait_for` / `extract` / `handle_dialog`). Each step's `intent` is a
  plain-language description of *what*, kept separate from `target` (*how*)
  — a reviewer can read the capability's behavior without parsing
  selectors.
- **`target: LocatorSpec`**: not one selector — a *ranked fallback chain*,
  each strategy carrying its own `reasoning` string. Replay tries them in
  order and stops at the first that resolves to exactly one visible
  element. This is the single biggest lever for surviving the kind of
  small DOM variation the brief describes (§1's "stable UIs, but no test
  IDs"), and it's also the seam a per-tenant override slots into (§4).
- **`input_schema` / `output_schema`**: typed, named, described — what a
  calling agent must supply and will get back. Dynamic step values
  reference a param by name (`value_param`) rather than being
  string-templated, so this typed contract is the single source of truth,
  not a side effect of how the steps happen to be written.
- **`checkpoint`**: an explicit, asserted condition (URL pattern / text /
  element present) that has to be true before a run counts as successful —
  never "we clicked and assumed it worked."
- **`known_outcomes` / `recoverable_patterns`**: this is the part I added
  beyond the minimum. Instead of burying "no such member" or "dismiss this
  known interstitial" as if/else logic inside the replay engine, they're
  declared, versioned data on the artifact itself — checked after a named
  step, with their own detection locator. A human reviewer (or the calling
  agent) can read the artifact and see exactly which non-success outcomes
  this capability recognizes and what it does about them, which is
  precisely the "clear contract, not just a step list" the brief asks for.
- **`risk_level` / `requires_confirmation`** live on each *step*, not just
  the artifact, because one capability is usually mostly-safe reads
  followed by exactly one irreversible write — an artifact-level flag would
  either over-block the reads or under-protect the write. During
  discovery, a click that actually triggered a native confirmation dialog
  is automatically recorded as `irreversible` + `requires_confirmation` —
  risk classification comes from what happened, not a guess applied after
  the fact.
- **`provenance`**: goal, discovery run id, model, timestamp, and a
  *pointer* to the raw transcript under `/evidence/` — never the transcript
  itself. The artifact is decoupled from how it was discovered, on purpose.
- **`status: draft | approved | rejected`** plus a **`review`** record
  (who, when, notes, and a hash of the content they reviewed). Discovery
  only ever emits a `draft`. The replay engine itself refuses to run a
  draft unattended, returning `REFUSED` before a browser opens; a person
  can run one attended to validate it, and approves it with
  `python -m artifacts.review approve`. The approval is bound to a sha256
  over everything replay executes (steps, locators, I/O contract,
  checkpoint, outcome and recovery rules), so editing an approved artifact
  voids its approval. `base_url` is deliberately outside the hash: the same
  reviewed flow is dispatched to different tenants' hosts (§4).

Every version is its own immutable file (`<name>.v<semver>.json`); the
store refuses to overwrite one. A re-discovery records the next major
version, the outcome/recovery authoring pass records the next minor, and
each new version starts as an unreviewed `draft`. `ls artifacts/store/` is
the full history, and an unpinned unattended replay resolves to the latest
*approved* version, never to a newer draft.

## 3. Determinism & error handling

Replay (`replay/executor.py`) never calls a model. Determinism comes from
three things together: the ranked locator resolution above, an explicit
checkpoint assertion at the end, and — the part I think matters most —
keeping the three outcome classes structurally distinct rather than
collapsing them into a boolean:

- **`SUCCESS`** — checkpoint verified, typed `outputs` populated.
- **`BUSINESS_OUTCOME`** — a `known_outcomes` marker matched. Data the
  caller needs ("no such member," "action not permitted"), not a failure.
  I chose to classify a validation rejection (deposit below minimum) as a
  business outcome too, not a failure — it's a deterministic, well-
  understood response to specific caller input, not a sign anything broke.
  A different reasonable design would force the caller to pre-validate and
  treat it as a failure; I preferred surfacing it as structured data
  because it's directly actionable by an agent that can just ask for a
  corrected amount.
- **`FAILURE`** — a locator never resolved, a recoverable pattern exhausted
  its retry budget, the checkpoint didn't hold after all steps ran, or an
  allowlist violation. Always carries `step_id` / `expected` / `observed`
  from the actual attempt, not a generic exception string.

**Recoverable conditions never reach the caller as a fourth case.** A
`recoverable_pattern` (e.g. the seeded one-time session-expired
interstitial) is detected with a short probe (~400ms — this matters:
checking for an error condition after *every* step with a multi-second
timeout would make the happy path slow) and handled via one of three
recovery actions declared on the pattern: `reload_and_retry` (re-request
the current page — right for a transient bad response), `retry_step`
(re-run the step that produced this state — right for a flaky click),
or `dismiss_and_continue` (the condition doesn't actually block
progress). What replay did about it is logged into `recovered_steps` on
the result, so a "successful" run's evidence still shows it wasn't
perfectly smooth.

**Known-outcome and recoverable-pattern checks are artifact data, checked
against `after_step`** — so the same declared contract that a reviewer
reads is exactly what replay evaluates; there's no second, hidden copy of
this logic in code.

## 4. Heterogeneity & multi-tenant

Not built — this is the design-only part the brief asks for.

**Surface abstraction.** The seam is exactly the discovery/replay split in
§1: `LocatorSpec` and `Step` don't know anything about Playwright. A
desktop surface would implement the same `observe()`/act contract against
the OS accessibility tree instead of the DOM (Windows UIA / macOS AX APIs,
which Playwright-equivalent tools like `pywinauto` or `atomac` expose) and
add `LocatorMethod.AX_PATH` alongside `css`/`role`/`text`/`xpath`. A legacy
web app with framesets needs frame-aware resolution in
`locator_resolver.py`, not a schema change. Everything above the browser
surface — the artifact, the replay executor's control flow, the outcome
taxonomy — is already surface-agnostic; I bottlenecked all surface-specific
code into `agent/browser_surface.py` and `replay/locator_resolver.py`
deliberately so this swap is contained to those two files.

**Multi-tenant reuse.** `TargetSurface.vendor_product` identifies the
underlying vendor product an artifact was recorded against, independent of
`base_url` — the idea being an artifact is looked up by
`(vendor_product, capability_name)` and dispatched against whichever
tenant's `base_url` is calling, not re-recorded per tenant. Two tenants on
the same vendor product with a re-skinned theme mostly don't break anything
here, *because* locator strategies prefer name attributes and accessible
role+name over anything visual. Where a tenant genuinely differs (a
relabeled field, an extra required column), the fix is a **per-tenant
override layer**: a small artifact patch keyed by tenant id that
adds/replaces specific `LocatorStrategy` entries or `known_outcomes`
markers without touching step semantics — the ranked-list structure of
`LocatorSpec` was designed with exactly this insertion point in mind, I
just didn't build the override-resolution code itself.

**Drift detection.** The signal is in the result contract: every step logs
which strategy index in its `LocatorSpec` resolved, and
`ReplayResult.locator_fallbacks` lists each step that had to fall back past
its primary strategy. A tenant whose index-0 strategy stops resolving and
keeps falling back to index 2 is showing drift *before* it becomes an
outright failure. What isn't built is the aggregation: trending that field
per tenant and flagging the artifact for re-review.

## 5. Escalation & handoff

**Four ways a run reaches a human**, all through one mechanism:

| Trigger | Where | Detected by |
|---|---|---|
| The model can't safely proceed (a decision it isn't authorised to make, a control it can't find) | discovery | the model calls `request_human(reason)` |
| The model is stuck without saying so | discovery | the harness: the same tool call 3 times running, or 3 failed calls in a row |
| A step needs a person: an irreversible click, or a value only a human supplied | replay | the artifact (`requires_confirmation`), unless the approved artifact is run with `--auto-approve` (refused for a draft, §2) |
| A step failed and replay can't recover | replay | opt-in, `--escalate-on-failure`. Off by default: an unattended production call should return `FAILURE` promptly, not block on a person |

An operator can also take the wheel uninvited (`takeover`), give up on a
pending request (`cancel`), or stop the run (`interrupt`), at any step or
turn boundary. `finish_stuck` stays terminal: "no such member" is a dead
end a human can't fix either, so it doesn't page one.

**The intervention request** carries what the operator needs to act:
capability, goal, the step or turn, why it stopped, the current URL, a
screenshot, and the CDP endpoint to attach to.

**Control transfer.** Both discovery and replay launch Chromium with a
remote-debugging port open from the start, so the session a human attaches
to is the one automation was driving, not a fresh one. The run writes the
request to the `ControlTransport` and from that moment until
`wait_for_resume()` returns, the human owns the session: automation makes
no page call, it only idles. Who is in control is one field
(`status`: `automation` / `paused_for_human` / `human_active` /
`resume_requested`) that both sides and the console read. The operator,
or `escalation/simulated_operator.py` for repeatable evidence, attaches
with `connect_over_cdp` from a separate process and drives the same page.

**What the human did is observed, not self-reported**
(`escalation/recorder.py`). A script injected into every document reports
the operator's clicks and field edits through a Playwright binding;
navigations and dialogs come from page events. Recording is on only while
the human holds control. In replay the observed actions go into the result
(`escalation.human_actions`) and the log. In discovery they also become
artifact steps marked `origin: "human"`, with the same locator inference
the model's actions get, so a flow that needed a person is still recorded
end to end and replays. A value the human typed is bound to an input param
if it matches one; otherwise it is never written down, and that step is
flagged so replay hands it to a human rather than inventing a value.

**Handing back.** The operator says what happened to the paused step:
"I completed it" (automation continues from the next step) or "run it
yourself" (automation executes it). Unstated, the default follows the
kind of request. Either way the run then verifies the checkpoint and the
declared outputs like any other; it doesn't trust that the handoff worked.
A failed step is escalated once: if it still fails after the handoff, that
is a hard failure.

**What's mocked:** the operator console is a bare Flask page, and a real
person drives the page through `chrome://inspect` (or the headed window)
rather than an embedded live view, which the brief scopes out. The scripted
operator stands in for a person's decisions only. **Limits:** the recorder
sees clicks and field changes, not free keyboard navigation or drag
gestures; the file-backed transport assumes the operator's console shares
a filesystem with the run (an HTTP transport is one new `ControlTransport`
subclass); one fixed CDP port per run means one escalatable run per host
at a time.

## 6. Safety

- **Allowlist** (`guardrails/allowlist.py`): a domain allowlist checked
  after every navigation and every click that might have caused one, in
  both discovery and replay — the same code path, so there's no way for
  one side to enforce it and the other not to.
- **Risk handling**: safe/reversible steps execute normally; risky/
  irreversible steps escalate by default (§5). This is coarser than a
  full action-type policy (e.g. distinguishing "any POST" from "any GET"),
  but it's driven by what discovery actually observed happening, not a
  guess, and it's the one axis the brief calls out as needing conservative
  handling.
- **Redaction** (`guardrails/redaction.py`): applied at the log-writing
  boundary in both discovery and replay, not just at points I remembered
  were sensitive — password-typed fields and anything matching common
  secret/PII shapes (SSN, card-number patterns) are redacted before a line
  ever reaches disk. The one real credential in this system (the operator
  login) never even flows through the logged path — bootstrap login is
  driven directly, not through the same tool-call logging discovery's LLM
  actions go through.
- **Limits of this model**: redaction here is pattern-based, not a full
  PII classifier, and risk classification is single-signal (did a dialog
  fire). Both are honest v1s, not a claim of completeness — see §7.

## 7. Cuts

Deliberately not built, and why:

- **Multi-tenant dispatch / per-tenant override resolution** — designed
  (§4), not implemented. Building it against one tenant would have meant
  fabricating a second fake tenant to prove it, which felt like effort
  better spent on the replay engine's error taxonomy, which the brief
  weights higher.
- **A real operator co-browsing UI** — explicitly out of scope per the
  brief; a bare mock console stands in, with the real handoff mechanism
  underneath it.
- **Desktop/OS-automation surface** — designed as a `LocatorMethod`
  extension (§4), not built; no desktop app was in scope for one concrete
  surface.
- **A full action-type/route-based risk policy** — current classification
  is dialog-driven only. Next: classify by HTTP verb / route pattern too,
  so a POST to a mutating endpoint is flagged even if the discovery run
  never happened to trigger a confirmation dialog for it.
- **Escalation "decline" path** — resume currently always means "the human
  proceeded." A real decline (abort the run, or retry with different
  params) is the next thing I'd add to the control channel's state
  machine.
- **Confidence/approval scoring and canonicalized route patterns**
  (stretch goals) — skipped in favor of depth on the required core:
  schema, replay error handling, and a genuinely-reattaching escalation
  mechanism, per the brief's explicit preference for depth over breadth.

With more time, in order: the escalation decline path (cheapest, closes a
real gap), then per-tenant override resolution (highest leverage against
the brief's stated production reality of hundreds of tenants), then a
route-based risk policy.
