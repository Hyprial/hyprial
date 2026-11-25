# Decision notes (phase B, S10; Allen 2026-11-03 "squire is an ordinary agent"):
# - An agent template is data: a packaged <name>/template.json (the agent
#   name, the capability facts stamped on the agent record and the cell params
#   seeded into its agent cell) plus its skill text.  user is the packaged
#   template; its role is the template's cell params, not code.
# - 2026-11-20 (S2b, remove-squire-setup-surface): the squire template and the
#   setup flow that lived beside the catalog (setup.py, home_layout.py,
#   store_lock.py) are deleted; only the packaged-data catalog remains.
"""Agent templates: packaged template data."""

from hyprial.daemon.impl.agent_templates.catalog import (
    TEMPLATE_CAPABILITY,
    AgentTemplate,
    AgentTemplateError,
    load_template,
    template_cell_params,
    template_name_of,
    template_names,
)

__all__ = [
    "TEMPLATE_CAPABILITY",
    "AgentTemplate",
    "AgentTemplateError",
    "load_template",
    "template_cell_params",
    "template_name_of",
    "template_names",
]
