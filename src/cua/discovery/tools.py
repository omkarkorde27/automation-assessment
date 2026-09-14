"""The tools the discovery agent is given.

Two properties are load-bearing and both are enforced by the schema rather than
by asking nicely in the prompt:

* **Every tool takes a `reason`.** The brief asks for a structured log of what
  the agent did *and why*. Making `reason` a required parameter means the "why"
  cannot be absent, cannot drift out of sync with the action, and does not
  depend on the model volunteering commentary.
* **Targets are `node_id`s, never selectors.** The model picks a numbered node
  out of an observation it was shown. It has no way to express a CSS selector or
  an XPath, so locator robustness is a property of the recorder rather than of
  how good the model's selector-writing happens to be that day.

`strict` with `additionalProperties: false` on every schema, so a malformed call
is rejected by the API instead of arriving here as a surprise key.

Tool order is fixed: the list is part of the cached prompt prefix, and reordering
it would invalidate the cache on every run for no benefit.
"""

from __future__ import annotations

REASON = {
    "type": "string",
    "description": (
        "Why this is the right action right now, in one sentence. This is "
        "recorded as the step's intent and read by a human reviewing the "
        "capability later, so describe the business purpose ('enter the member "
        "id so the search can find them'), not the mechanics ('type in box 4')."
    ),
}

NODE_ID = {
    "type": "string",
    "description": "The node_id of an element from the most recent observation, exactly as shown.",
}


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


TOOLS: list[dict] = [
    _tool(
        "observe",
        "Look at the screen again. Returns every element currently visible, with "
        "its node_id. Use this when you are unsure what is on screen; every other "
        "tool already returns a fresh observation afterwards, so you rarely need it "
        "twice in a row.",
        {"reason": REASON},
        ["reason"],
    ),
    _tool(
        "click",
        "Click an element: a link, a button, a row.",
        {"node_id": NODE_ID, "reason": REASON},
        ["node_id", "reason"],
    ),
    _tool(
        "fill",
        "Type a value into a text field, replacing whatever is there.",
        {
            "node_id": NODE_ID,
            "text": {"type": "string", "description": "The value to type."},
            "is_param_candidate": {
                "type": "boolean",
                "description": (
                    "True if this value came from the goal you were given and would "
                    "differ on another run -- a member id, an amount, a product code. "
                    "False only for a value that is genuinely fixed for every run of "
                    "this capability. Getting this right is what makes the recorded "
                    "capability reusable instead of hard-coded to today's request."
                ),
            },
            "param_name": {
                "type": "string",
                "description": (
                    "If is_param_candidate is true, the name this input should have "
                    "in the capability's signature, in snake_case (member_id, "
                    "initial_deposit). Empty string otherwise."
                ),
            },
            "reason": REASON,
        },
        ["node_id", "text", "is_param_candidate", "param_name", "reason"],
    ),
    _tool(
        "select_option",
        "Choose a value in a dropdown.",
        {
            "node_id": NODE_ID,
            "value": {"type": "string", "description": "The option's code or visible label."},
            "is_param_candidate": {"type": "boolean",
                                   "description": "As for fill: does this vary per run?"},
            "param_name": {"type": "string", "description": "snake_case name, or empty string."},
            "reason": REASON,
        },
        ["node_id", "value", "is_param_candidate", "param_name", "reason"],
    ),
    _tool(
        "press_key",
        "Press a single key, such as Enter or Escape.",
        {"key": {"type": "string", "description": "Key name, e.g. 'Enter'."}, "reason": REASON},
        ["key", "reason"],
    ),
    _tool(
        "navigate",
        "Go directly to a URL within the application. Prefer clicking the "
        "application's own navigation: a recorded flow that clicks the menu keeps "
        "working when URLs change, and proves the path a person would take.",
        {"url": {"type": "string", "description": "Absolute URL inside the application."},
         "reason": REASON},
        ["url", "reason"],
    ),
    _tool(
        "scroll",
        "Scroll the page down (positive) or up (negative).",
        {"amount": {"type": "integer", "description": "Pixels; 400 is about one screenful."},
         "reason": REASON},
        ["amount", "reason"],
    ),
    _tool(
        "wait_for_text",
        "Wait until some text appears on screen. Use it when the application is "
        "still loading rather than clicking again.",
        {"text": {"type": "string", "description": "Text to wait for; matched case-insensitively."},
         "reason": REASON},
        ["text", "reason"],
    ),
    _tool(
        "extract",
        "Record an element's value as one of this capability's outputs. This is how "
        "the answer to the goal gets returned to a caller, so extract the specific "
        "value asked for, not the whole screen.",
        {
            "node_id": NODE_ID,
            "output_name": {
                "type": "string",
                "description": "snake_case name for this output, e.g. savings_balance.",
            },
            "reason": REASON,
        },
        ["node_id", "output_name", "reason"],
    ),
    _tool(
        "declare_outcome",
        "Record that this screen represents a legitimate business answer rather than "
        "a malfunction -- 'no such member', 'already exists', 'not permitted'. A "
        "capability that declares its outcomes returns them as typed results instead "
        "of looking broken. Declare one whenever you see the application tell you it "
        "cannot do something for a business reason.",
        {
            "code": {"type": "string",
                     "description": "SCREAMING_SNAKE_CASE code, e.g. MEMBER_NOT_FOUND."},
            "node_id": {"type": "string",
                        "description": "The node showing the message, or empty string."},
            "hint": {"type": "string",
                     "description": "What this outcome means, for the caller."},
            "reason": REASON,
        },
        ["code", "node_id", "hint", "reason"],
    ),
    _tool(
        "finish",
        "The goal is achieved and the screen in front of you proves it. Say what on "
        "this screen shows the task is complete -- that becomes the capability's "
        "success condition.",
        {"reason": REASON},
        ["reason"],
    ),
    _tool(
        "give_up",
        "Stop. The goal cannot be achieved from here. Say precisely what blocked you, "
        "because a person reads this to decide what to do next.",
        {"reason": REASON},
        ["reason"],
    ),
]

TOOL_NAMES = {t["name"] for t in TOOLS}
TERMINAL_TOOLS = {"finish", "give_up"}
