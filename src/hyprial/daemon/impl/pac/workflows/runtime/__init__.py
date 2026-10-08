"""Graph workflow request projection and durable dispatch, owned by PAC.

No inbox polling or interpretation of replies. Flags are completion facts;
workflow rows bind a dispatch request, its fixed deadline and failure policy.
"""

from __future__ import annotations

from hyprial.daemon.impl.pac.workflows.runtime.core import _GraphWorkflowServiceCore
from hyprial.daemon.impl.pac.workflows.runtime.operations import _GraphWorkflowServiceOps
from hyprial.daemon.impl.pac.workflows.runtime.types import (
    NotificationReceiptAuthority,
    WorkflowSender,
    WorkflowServiceError,
    WorkflowTickAuthority,
    close_workflow,
)

__all__ = [
    "GraphWorkflowService",
    "WorkflowServiceError",
    "WorkflowSender",
    "NotificationReceiptAuthority",
    "WorkflowTickAuthority",
    "close_workflow",
]


class GraphWorkflowService(_GraphWorkflowServiceCore, _GraphWorkflowServiceOps):
    """Project workflow requests into PAC and drive their durable dispatch."""
