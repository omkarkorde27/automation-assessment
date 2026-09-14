"""The agent-facing capability interface.

A recorded artifact becomes a typed tool a production agent can be handed, and a
tool call the agent makes comes back here to be turned into a replay. This is the
last mile of the through-line: the model discovers, the artifact freezes it, and
`catalog` is how an agent *invokes* it without a model anywhere in the decision
loop.

Nothing here imports `anthropic`, and the import-graph test now covers this
package for the same reason it covers `escalation`: dispatch is exactly where a
"just ask the model which capability they meant" helper would look reasonable.
"""

from .tools import (
    CatalogEntry, ToolCatalog, ToolNotFound, ToolNotInvocable,
    build_catalog, result_payload, tool_name_for, tool_result_block,
)

__all__ = [
    "CatalogEntry", "ToolCatalog", "ToolNotFound", "ToolNotInvocable",
    "build_catalog", "result_payload", "tool_name_for", "tool_result_block",
]
