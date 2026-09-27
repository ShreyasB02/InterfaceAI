"""
A hand-built artifact used only to unit-test the schema and the replay
engine in isolation from the discovery agent. This is NOT the artifact
that ships as /evidence/ — that one has to come from a real LLM-driven
discovery run (see /agent). Keeping this out of artifacts/store/ avoids
any confusion between the two.
"""
from datetime import datetime, timezone

from artifacts.schema import (
    ActionType,
    CapabilityArtifact,
    Checkpoint,
    CheckpointMethod,
    DiscoveryProvenance,
    InputParam,
    LocatorMethod,
    LocatorSpec,
    LocatorStrategy,
    OutcomeMarker,
    OutputField,
    ParamType,
    RecoverablePattern,
    RecoveryAction,
    RiskLevel,
    Step,
    TargetSurface,
)


def build_lookup_member_balance_fixture(base_url: str = "http://127.0.0.1:5055") -> CapabilityArtifact:
    return CapabilityArtifact(
        artifact_id="fixture-lookup-member-balance",
        name="lookup_member_balance",
        version="1.0.0",
        description=(
            "Looks up a member by ID in the servicer console and returns their "
            "current savings balance. Assumes an authenticated session already "
            "exists (session/auth is handled outside the artifact — see REPORT.md)."
        ),
        provenance=DiscoveryProvenance(
            goal="Look up member {member_id} and read their current savings balance.",
            discovery_run_id="fixture-not-a-real-run",
            model_provider="none",
            model_name="hand-authored-for-schema-testing",
            recorded_at=datetime.now(timezone.utc),
            evidence_path="tests/fixtures/",
        ),
        target=TargetSurface(
            surface_type="legacy_web",
            base_url=base_url,
            entry_path="/members/search",
            vendor_product="ACME Core Servicer Terminal",
        ),
        input_schema=[
            InputParam(
                name="member_id", type=ParamType.STRING, required=True,
                description="The member ID to look up.", example="10001",
                validation_pattern=r"^\d{5}$",
            )
        ],
        output_schema=[
            OutputField(name="member_name", type=ParamType.STRING, description="Member's full name."),
            OutputField(name="savings_balance", type=ParamType.NUMBER, description="Current savings balance in USD."),
        ],
        steps=[
            Step(
                step_id="s1", intent="Go to the member search page.",
                action=ActionType.NAVIGATE, value_literal="/members/search",
            ),
            Step(
                step_id="s2", intent="Enter the member ID into the search field.",
                action=ActionType.FILL, value_param="member_id",
                target=LocatorSpec(strategies=[
                    LocatorStrategy(
                        method=LocatorMethod.CSS, value="input[name='member_id']",
                        reasoning="The form field's name attribute is what the server actually "
                        "reads on submit, so it survives visual re-skins even with no id/test-id.",
                    ),
                    LocatorStrategy(
                        method=LocatorMethod.ROLE, value="textbox", role_name=None,
                        reasoning="Fallback: only textbox on the search page. Fragile if a second "
                        "field is ever added — kept as last resort only.",
                    ),
                ]),
            ),
            Step(
                step_id="s3", intent="Submit the search.",
                action=ActionType.CLICK,
                target=LocatorSpec(strategies=[
                    LocatorStrategy(
                        method=LocatorMethod.ROLE, value="button", role_name="Search",
                        reasoning="Accessible role+name targeting survives markup changes better "
                        "than position-based CSS on a page with no test IDs.",
                    ),
                    LocatorStrategy(
                        method=LocatorMethod.TEXT, value="Search",
                        reasoning="Fallback: plain visible-text match.",
                    ),
                ]),
            ),
            Step(
                step_id="s4", intent="Open the found member's detail page.",
                action=ActionType.CLICK,
                target=LocatorSpec(strategies=[
                    LocatorStrategy(
                        method=LocatorMethod.ROLE, value="button", role_name="View Member",
                        reasoning="Stable button label on the search-result row.",
                    ),
                ]),
            ),
            Step(
                step_id="s5", intent="Read the member's name.",
                action=ActionType.EXTRACT, output_name="member_name",
                target=LocatorSpec(strategies=[
                    LocatorStrategy(
                        method=LocatorMethod.XPATH,
                        value="//tr[td[normalize-space()='Name']]/td[2]",
                        reasoning="Legacy table layout with no semantic label: locate the value "
                        "cell by its adjacent label cell's text rather than position, so row "
                        "reordering doesn't break it.",
                    ),
                ]),
            ),
            Step(
                step_id="s6", intent="Read the savings account balance.",
                action=ActionType.EXTRACT, output_name="savings_balance",
                target=LocatorSpec(strategies=[
                    LocatorStrategy(
                        method=LocatorMethod.XPATH,
                        value="//tr[td[1][normalize-space()='Savings']]/td[3]",
                        reasoning="Same label-relative pattern applied to the accounts table: "
                        "find the row whose type cell says 'Savings', read its balance cell.",
                    ),
                ]),
            ),
        ],
        checkpoint=Checkpoint(
            description="Member detail page loaded for the requested member.",
            method=CheckpointMethod.URL_MATCHES,
            value="/members/{member_id}",
        ),
        known_outcomes=[
            OutcomeMarker(
                code="member_not_found",
                message="No member exists with the given ID.",
                after_step="s3",
                detection=LocatorSpec(strategies=[
                    LocatorStrategy(
                        method=LocatorMethod.TEXT, value="No member found matching ID",
                        reasoning="Server renders this exact banner text on a failed search; "
                        "checked before assuming step s4's target will exist.",
                    ),
                ]),
            ),
        ],
        recoverable_patterns=[
            RecoverablePattern(
                condition="One-time session-expired interstitial on member detail load.",
                after_step="s4",
                detection=LocatorSpec(strategies=[
                    LocatorStrategy(
                        method=LocatorMethod.TEXT, value="Your session has expired",
                        reasoning="Exact interstitial banner text.",
                    ),
                ]),
                recovery_action=RecoveryAction.RELOAD_AND_RETRY,
                max_attempts=1,
            ),
        ],
        default_risk_level=RiskLevel.SAFE,
    )

def build_open_sub_account_fixture(base_url: str = "http://127.0.0.1:5055") -> CapabilityArtifact:
    """Covers the richer flow: multi-field form, a confirmation step with a
    native dialog, and (via known_outcomes) two named business outcomes.
    Used to unit-test escalation and business-outcome handling in the
    replay engine without needing a real discovery run for every scenario."""
    return CapabilityArtifact(
        artifact_id="fixture-open-sub-account",
        name="open_sub_account",
        version="1.0.0",
        description=(
            "Opens a new sub-savings account for a member and returns the new account "
            "number. Assumes an authenticated session already exists."
        ),
        provenance=DiscoveryProvenance(
            goal="Open a new sub-account for member {member_id} with nickname {nickname} "
                 "and an initial deposit of {initial_deposit}, and reach the confirmation screen.",
            discovery_run_id="fixture-not-a-real-run",
            model_provider="none", model_name="hand-authored-for-schema-testing",
            recorded_at=datetime.now(timezone.utc), evidence_path="tests/fixtures/",
        ),
        target=TargetSurface(
            surface_type="legacy_web", base_url=base_url, entry_path="/members/search",
            vendor_product="ACME Core Servicer Terminal",
        ),
        input_schema=[
            InputParam(name="member_id", type=ParamType.STRING, required=True,
                       description="The member ID to open a sub-account for.", example="10001"),
            InputParam(name="nickname", type=ParamType.STRING, required=True,
                       description="Nickname for the new sub-account.", example="Vacation Fund"),
            InputParam(name="initial_deposit", type=ParamType.NUMBER, required=True,
                       description="Initial deposit amount in USD.", example="100"),
        ],
        output_schema=[
            OutputField(name="new_account_number", type=ParamType.STRING,
                        description="The newly created sub-account's account number."),
        ],
        steps=[
            Step(step_id="s1", intent="Go to the member search page.",
                 action=ActionType.NAVIGATE, value_literal="/members/search"),
            Step(step_id="s2", intent="Enter the member ID.", action=ActionType.FILL,
                 value_param="member_id",
                 target=LocatorSpec(strategies=[LocatorStrategy(
                     method=LocatorMethod.CSS, value="input[name='member_id']",
                     reasoning="Form field name attribute; tied to the server's actual contract.")])),
            Step(step_id="s3", intent="Submit the search.", action=ActionType.CLICK,
                 target=LocatorSpec(strategies=[LocatorStrategy(
                     method=LocatorMethod.ROLE, value="button", role_name="Search",
                     reasoning="Accessible role+name, stable across markup changes.")])),
            Step(step_id="s4", intent="Open the member's detail page.", action=ActionType.CLICK,
                 target=LocatorSpec(strategies=[LocatorStrategy(
                     method=LocatorMethod.ROLE, value="button", role_name="View Member",
                     reasoning="Stable button label on the search-result row.")])),
            Step(step_id="s5", intent="Go to the open-sub-account form.", action=ActionType.CLICK,
                 target=LocatorSpec(strategies=[LocatorStrategy(
                     method=LocatorMethod.ROLE, value="button", role_name="Open Sub-Account",
                     reasoning="Stable button label on the member detail page.")])),
            Step(step_id="s6", intent="Enter the sub-account nickname.", action=ActionType.FILL,
                 value_param="nickname",
                 target=LocatorSpec(strategies=[LocatorStrategy(
                     method=LocatorMethod.CSS, value="input[name='nickname']",
                     reasoning="Form field name attribute.")])),
            Step(step_id="s7", intent="Enter the initial deposit amount.", action=ActionType.FILL,
                 value_param="initial_deposit",
                 target=LocatorSpec(strategies=[LocatorStrategy(
                     method=LocatorMethod.CSS, value="input[name='initial_deposit']",
                     reasoning="Form field name attribute.")])),
            Step(step_id="s8", intent="Continue to the confirmation screen.", action=ActionType.CLICK,
                 target=LocatorSpec(strategies=[LocatorStrategy(
                     method=LocatorMethod.ROLE, value="button", role_name="Continue",
                     reasoning="Stable button label.")])),
            Step(step_id="s9", intent="Confirm and open the account (irreversible).",
                 action=ActionType.CLICK, risk_level=RiskLevel.IRREVERSIBLE, requires_confirmation=True,
                 target=LocatorSpec(strategies=[LocatorStrategy(
                     method=LocatorMethod.ROLE, value="button", role_name="Confirm & Open Account",
                     reasoning="Stable button label; this action creates a real account and "
                     "cannot be undone by the agent itself.")])),
            Step(step_id="s10", intent="Accept the native confirmation dialog.",
                 action=ActionType.HANDLE_DIALOG, on_dialog="accept",
                 risk_level=RiskLevel.IRREVERSIBLE, requires_confirmation=True),
            Step(step_id="s11", intent="Read the newly created account number.",
                 action=ActionType.EXTRACT, output_name="new_account_number",
                 target=LocatorSpec(strategies=[LocatorStrategy(
                     method=LocatorMethod.XPATH,
                     value="//tr[td[normalize-space()='New Account Number']]/td[2]",
                     reasoning="Label-relative XPath: robust to row reordering.")])),
        ],
        checkpoint=Checkpoint(
            description="Success banner with the new account number is visible.",
            method=CheckpointMethod.TEXT_PRESENT, value="opened successfully",
        ),
        known_outcomes=[
            OutcomeMarker(
                code="permission_denied", message="Sub-account creation is blocked for this member (compliance hold).",
                after_step="s8",
                detection=LocatorSpec(strategies=[LocatorStrategy(
                    method=LocatorMethod.TEXT, value="Action not permitted",
                    reasoning="Exact banner text the app renders for a restricted member.")]),
            ),
            OutcomeMarker(
                code="invalid_deposit_amount", message="The initial deposit did not meet the app's minimum.",
                after_step="s8",
                detection=LocatorSpec(strategies=[LocatorStrategy(
                    method=LocatorMethod.TEXT, value="Initial deposit must be at least",
                    reasoning="Exact validation banner text.")]),
            ),
        ],
        default_risk_level=RiskLevel.SAFE,
    )
