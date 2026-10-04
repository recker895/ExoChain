"""Execution tools require a durable approved run. No direct purchase shortcut."""

from services.approval_service import execute


def execute_procurement_order(store, run_id, actor):
    return execute(store, run_id, actor)
