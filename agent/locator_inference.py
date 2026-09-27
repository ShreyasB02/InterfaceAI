"""
Turns a raw enumerated DOM element (what discovery sees live) into the
ranked locator strategies that get recorded onto an artifact Step for
replay. This is the one place robustness reasoning is written down, so a
human reviewer can audit the *policy*, not just individual selectors.

Priority order and why:
  1. CSS by `name` attribute — tied to the app's actual form-submission
     contract, so it survives a visual re-skin even with no id/test-id.
  2. Accessible role + visible name — what a user actually reads; survives
     DOM restructuring that a positional selector wouldn't.
  3. Plain text match — last semantic fallback.
  4. Tag+type — genuine last resort, only when nothing else resolved.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from artifacts.schema import LocatorMethod, LocatorStrategy


@dataclass
class ElementMeta:
    index: int
    tag: str
    type: str
    name: str
    value: str
    text: str
    label: Optional[str]

    def describe(self) -> str:
        if self.text:
            return f"'{self.text}' {self.tag}"
        if self.label:
            return f"the '{self.label}' field"
        if self.name:
            return f"field '{self.name}'"
        return f"{self.tag}[{self.index}]"


def _infer_role(el: ElementMeta) -> tuple[Optional[str], Optional[str]]:
    if el.tag == "button":
        return "button", el.text or el.value
    if el.tag == "input" and el.type in ("submit", "button"):
        return "button", el.value or el.text
    if el.tag == "a":
        return "link", el.text
    if el.tag in ("input", "textarea") and el.type not in ("submit", "button", "hidden"):
        return "textbox", None
    if el.tag == "select":
        return "combobox", None
    return None, None


def derive_locator_strategies(el: ElementMeta) -> list[LocatorStrategy]:
    strategies: list[LocatorStrategy] = []

    if el.name:
        strategies.append(LocatorStrategy(
            method=LocatorMethod.CSS,
            value=f'{el.tag}[name="{el.name}"]',
            reasoning=(
                f"The '{el.name}' name attribute is what the server actually reads "
                "on form submit, so it's tied to the app's real contract and "
                "survives a visual re-skin even with no id or test-id attributes."
            ),
        ))

    role, role_name = _infer_role(el)
    if role == "button" and role_name:
        strategies.append(LocatorStrategy(
            method=LocatorMethod.ROLE, value="button", role_name=role_name,
            reasoning=(
                f"Accessible role+name ('button', \"{role_name}\") targets what a "
                "user actually reads, which survives markup restructuring better "
                "than a positional selector."
            ),
        ))
    elif role == "link" and role_name:
        strategies.append(LocatorStrategy(
            method=LocatorMethod.ROLE, value="link", role_name=role_name,
            reasoning="Accessible role+name for a link; stable across markup changes.",
        ))

    if el.text and not any(s.method == LocatorMethod.ROLE for s in strategies):
        strategies.append(LocatorStrategy(
            method=LocatorMethod.TEXT, value=el.text,
            reasoning="Fallback: plain visible-text match, used only because no "
            "name attribute or accessible role/name resolved for this element.",
        ))

    if not strategies:
        strategies.append(LocatorStrategy(
            method=LocatorMethod.CSS, value=f'{el.tag}[type="{el.type}"]',
            reasoning="Last-resort tag+type selector — no name, text, or role was "
            "available to identify this element more precisely. Fragile if the "
            "page ever gains a second element of the same tag/type.",
        ))

    return strategies
