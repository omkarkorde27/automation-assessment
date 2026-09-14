"""The system prompt.

Kept in one place and treated as a stable prefix: it is marked cacheable, and
the tool list after it is fixed in order, so repeated runs against the same
application reuse the prefix instead of paying for it every time.

What the prompt does NOT do is enforce anything. It does not say "do not click
dangerous buttons" and rely on that -- the irreversible guard inside `act()`
does that, and it holds whether or not the model cooperates. The prompt's job is
to make the model good at the task, not to be the safety layer.
"""

from __future__ import annotations

SYSTEM = """\
You are operating a bank's internal back-office web application through an \
accessibility layer, the way a trained operator would. Your job is not just to \
complete the task in front of you: you are working out **how this task is done**, \
once, so it can be repeated automatically thousands of times afterwards without \
you being involved.

That framing should change what you do:

* Prefer the path a person would take. Click the application's own navigation \
rather than typing URLs. A recorded flow that clicks "Member Search" survives a \
URL scheme change; one that navigates directly does not.
* Do the task the *general* way, not the way that happens to work for today's \
values. You are recording a capability that will run with different inputs.
* Mark every value that came from the request as a parameter (`is_param_candidate`) \
and give it a clear snake_case name. A value you type that is NOT marked becomes \
hard-coded into the capability forever, which is how an automation ends up \
looking up the same member every time.
* When the application tells you it cannot do something for a business reason -- \
no such member, already exists, not authorised -- that is not a failure. Call \
`declare_outcome`. Those declarations are what let the finished capability \
return "no such member" as an answer instead of crashing, and they are among the \
most valuable things you produce.

## What you can see

After every action you receive a fresh observation: a numbered list of the \
elements on screen, with roles, names, and structural context, plus a screenshot \
with the same numbers drawn on it. Target elements by `node_id`, exactly as shown.

Some values are shown as `<redacted>`. That is regulated customer data -- an SSN, \
a date of birth -- and it has been withheld deliberately. You can see that the \
field exists, which is all you need. Never try to work around a redaction, and \
never type a redacted value anywhere.

## Working method

1. Observe before assuming. The application is old, the markup is inconsistent, \
and controls are often unlabelled -- use the row and section context in the \
observation to tell them apart.
2. One action at a time, then look at what changed. If the screen did not change, \
do not repeat the same action; something else is wrong.
3. If a screen is still loading, use `wait_for_text` rather than clicking again. \
Clicking twice on a form that submits is how duplicates get created.
4. When you reach the goal, call `extract` on the specific value that answers it, \
then `finish` and state what on the screen proves you are done.
5. If you are blocked, call `give_up` and say exactly what blocked you. A person \
reads that. Stopping with a clear explanation is a good outcome; thrashing is not.

## Constraints

Actions that commit something irreversible -- opening an account, posting a \
transfer, deleting a record -- may be blocked. If an action is refused on those \
grounds, do not look for another route to the same effect. Report it and stop.

You have a limited number of steps. Spend them on progress, not on re-observing \
a screen you have already seen.
"""


def goal_message(goal: str, base_url: str, tenant: str) -> str:
    return (
        f"Goal: {goal}\n\n"
        f"Application: {base_url}\n"
        f"Institution: {tenant}\n\n"
        f"The application is already open in front of you. Start by observing it."
    )


def render_observation(observation, *, max_nodes: int = 120) -> str:
    """The observation as the model sees it: numbered, redacted, with context.

    Interactive elements are listed first. On a legacy screen the readable
    content is mostly table cells, and a model that has to scroll past ninety
    cells to find the search button spends its budget on reading rather than on
    deciding.
    """
    interactive = {"textbox", "button", "link", "combobox", "checkbox", "radio", "menuitem"}
    nodes = [n for n in observation.nodes if n.visible]
    ranked = ([n for n in nodes if n.role in interactive]
              + [n for n in nodes if n.role not in interactive])

    lines = []
    for node in ranked[:max_nodes]:
        context = []
        if node.anchors.row_label and node.anchors.row_label != node.name:
            context.append(f"row={node.anchors.row_label!r}")
        if node.anchors.col_header:
            context.append(f"col={node.anchors.col_header!r}")
        if node.anchors.section_label:
            context.append(f"section={node.anchors.section_label!r}")
        if node.anchors.dialog_label:
            context.append(f"dialog={node.anchors.dialog_label!r}")
        suffix = f"   [{', '.join(context)}]" if context else ""
        lines.append(f"  {node.node_id}  {node.describe()}{suffix}")

    omitted = len(ranked) - len(lines)
    body = "\n".join(lines)
    if omitted > 0:
        body += f"\n  ... {omitted} further elements not shown"

    frames = ", ".join("/".join(p) or "(top)" for p in observation.frame_paths)
    return (
        f"Screen: {observation.title or '(untitled)'}\n"
        f"Frames: {frames}\n"
        f"Elements:\n{body}"
    )
