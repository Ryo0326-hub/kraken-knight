"""Shared, versioned risk-policy identifiers."""

from __future__ import annotations

from enum import StrEnum


class DrawdownPolicyMode(StrEnum):
    """Supported high-water drawdown responses.

    ``PERSISTENT`` preserves the sealed V1 behavior. ``DISABLED`` is the
    production V3 policy selected after the separately labelled V2
    counterfactual. ``COOLDOWN_REARM`` remains a research-only comparator.
    """

    PERSISTENT = "persistent"
    DISABLED = "disabled"
    COOLDOWN_REARM = "cooldown_rearm"
