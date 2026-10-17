"""Explicit upper composition callbacks for DaemonApplication."""

from .routines import routine_runtime_factory, routine_template_renderer, routine_text_loader
from .usage import usage_factory
from .quota import quota_evaluator_factory, quota_hook_consumer_factory
from .hooks import hook_consumers, reply_reminder_consumer_factory


def daemon_dependencies():
    """Constructor callbacks; usage_factory belongs only to from_environment."""
    return {
        "quota_evaluator_factory": quota_evaluator_factory,
        "quota_hook_consumer_factory": quota_hook_consumer_factory,
        "reply_reminder_consumer_factory": reply_reminder_consumer_factory,
        "hook_consumers": hook_consumers,
        "routine_runtime_factory": routine_runtime_factory,
        "routine_text_loader": routine_text_loader,
        "routine_template_renderer": routine_template_renderer,
    }


__all__ = ["daemon_dependencies", "usage_factory"]
