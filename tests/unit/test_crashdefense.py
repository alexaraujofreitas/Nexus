"""
tests/unit/test_crashdefense.py — CrashDefenseController tests (CD-001 to CD-008)

The controller is called with a pre-determined tier string (e.g. "DEFENSIVE").
The caller (CrashDetectionAgent) is responsible for mapping score → tier.
Tests inject tier + score directly via respond_to_tier().

Tier thresholds (documented in source):
  DEFENSIVE  score ≥ 5.0
  HIGH_ALERT score ≥ 7.0
  EMERGENCY  score ≥ 8.0
  SYSTEMIC   score ≥ 9.0

Each test creates a fresh CrashDefenseController() instance so state from
one test never bleeds into the next.
"""

from __future__ import annotations

import pytest

from core.risk.crash_defense_controller import CrashDefenseController
from core.event_bus import Topics


# ── shared fixture: fresh controller per test ─────────────────────────────────

@pytest.fixture
def controller(qt_app):
    """
    Fresh CrashDefenseController starting in NORMAL / non-defensive state.
    qt_app is required because respond_to_tier() publishes to the global
    EventBus (a QObject) which must have a running QApplication.
    """
    return CrashDefenseController()


# ══════════════════════════════════════════════════════════════════════════════
#  CD-001 — Score below all thresholds → NORMAL, no actions
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_cd001_normal_tier_no_actions(controller):
    """
    Calling respond_to_tier("NORMAL") must return an empty actions list
    and leave the controller in the NORMAL / non-defensive state.
    """
    actions = controller.respond_to_tier("NORMAL", score=4.9, components={})

    assert controller.current_tier   == "NORMAL"
    assert controller.is_defensive   is False
    assert controller.is_safe_mode   is False
    assert actions == [], f"Expected no actions, got: {actions}"


@pytest.mark.unit
def test_cd001_normal_after_defensive_deactivates(controller):
    """
    Transitioning DEFENSIVE → NORMAL must deactivate defensive mode and
    include a 'deactivated' action in the returned list.
    """
    controller.respond_to_tier("DEFENSIVE", score=5.1, components={})
    assert controller.is_defensive is True   # sanity check

    actions = controller.respond_to_tier("NORMAL", score=4.0, components={})

    assert controller.is_defensive is False
    assert controller.current_tier == "NORMAL"
    assert any("deactivated" in a.lower() for a in actions), (
        f"Expected a 'deactivated' action, got: {actions}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  CD-002 — DEFENSIVE tier activated at score ≥ 5.0
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_cd002_defensive_tier_activates(controller, event_capture):
    """
    respond_to_tier("DEFENSIVE") must:
      - set current_tier to "DEFENSIVE"
      - set is_defensive to True
      - return actions that include halt, tighten, size-cap items
      - publish DEFENSIVE_MODE_ACTIVATED on the EventBus
    """
    capture = event_capture(Topics.DEFENSIVE_MODE_ACTIVATED)

    actions = controller.respond_to_tier("DEFENSIVE", score=5.1, components={})

    assert controller.current_tier == "DEFENSIVE"
    assert controller.is_defensive is True
    assert controller.is_safe_mode is False

    action_text = " ".join(actions).lower()
    assert "halt" in action_text or "longs" in action_text, (
        f"Expected halt-longs action, got: {actions}"
    )
    assert "tighten" in action_text or "stop" in action_text, (
        f"Expected tighten-stops action, got: {actions}"
    )
    assert "size" in action_text or "cap" in action_text, (
        f"Expected size-cap action, got: {actions}"
    )

    # EventBus must have published the activation event
    assert len(capture[Topics.DEFENSIVE_MODE_ACTIVATED]) >= 1
    event_data = capture[Topics.DEFENSIVE_MODE_ACTIVATED][0].data
    assert event_data["tier"]  == "DEFENSIVE"
    assert event_data["score"] == pytest.approx(5.1)


@pytest.mark.unit
def test_cd002_defensive_actions_logged(controller):
    """
    After a DEFENSIVE activation, get_actions_log() must contain one entry
    with the correct tier and score.
    """
    controller.respond_to_tier("DEFENSIVE", score=5.5, components={"price_velocity": 2.0})

    log = controller.get_actions_log()
    assert len(log) >= 1
    latest = log[-1]
    assert latest["tier"]  == "DEFENSIVE"
    assert latest["score"] == pytest.approx(5.5)
    assert isinstance(latest["actions"], list)
    assert len(latest["actions"]) > 0


# ══════════════════════════════════════════════════════════════════════════════
#  CD-003 — HIGH_ALERT tier
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_cd003_high_alert_tier_activates(controller, event_capture):
    """
    HIGH_ALERT must set current_tier, activate defensive mode, and publish
    RISK_LIMIT_HIT (tier2 action) in addition to DEFENSIVE_MODE_ACTIVATED.
    """
    capture = event_capture(
        Topics.DEFENSIVE_MODE_ACTIVATED,
        Topics.RISK_LIMIT_HIT,
    )

    actions = controller.respond_to_tier("HIGH_ALERT", score=7.1, components={})

    assert controller.current_tier == "HIGH_ALERT"
    assert controller.is_defensive is True

    action_text = " ".join(actions).lower()
    # Tier 1 actions present
    assert "halt" in action_text or "longs" in action_text
    # Tier 2 actions present
    assert "partial" in action_text or "50%" in action_text, (
        f"Expected partial-exit action, got: {actions}"
    )
    assert "trailing" in action_text, (
        f"Expected trailing-stop action, got: {actions}"
    )

    # Both events published
    assert len(capture[Topics.DEFENSIVE_MODE_ACTIVATED]) >= 1
    assert len(capture[Topics.RISK_LIMIT_HIT]) >= 1

    risk_event = capture[Topics.RISK_LIMIT_HIT][0].data
    assert risk_event["tier"] == "HIGH_ALERT"


@pytest.mark.unit
def test_cd003_high_alert_includes_tier1_actions(controller):
    """
    HIGH_ALERT must apply ALL tier-1 actions in addition to tier-2 actions
    (cumulative escalation, not replacement).
    """
    actions = controller.respond_to_tier("HIGH_ALERT", score=7.5, components={})

    action_text = " ".join(actions)
    # Tier-1 markers
    assert "stop" in action_text.lower() or "tighten" in action_text.lower()
    # Tier-2 markers
    assert "partial_exit" in action_text or "trailing_stop" in action_text


# ══════════════════════════════════════════════════════════════════════════════
#  CD-004 — EMERGENCY tier
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_cd004_emergency_tier_activates(controller, event_capture):
    """
    EMERGENCY must close all longs and activate read-only mode.
    RISK_LIMIT_HIT must be published at least once.
    """
    capture = event_capture(
        Topics.DEFENSIVE_MODE_ACTIVATED,
        Topics.RISK_LIMIT_HIT,
    )

    actions = controller.respond_to_tier("EMERGENCY", score=8.1, components={})

    assert controller.current_tier == "EMERGENCY"
    assert controller.is_defensive is True
    # Safe mode is SYSTEMIC-only — EMERGENCY does not set it
    assert controller.is_safe_mode is False

    action_text = " ".join(actions).lower()
    assert "close_all_longs" in action_text or "all long" in action_text, (
        f"Expected close-all-longs action, got: {actions}"
    )
    assert "read_only" in action_text or "read only" in action_text or "no new" in action_text, (
        f"Expected read-only-mode action, got: {actions}"
    )

    assert len(capture[Topics.DEFENSIVE_MODE_ACTIVATED]) >= 1
    assert len(capture[Topics.RISK_LIMIT_HIT]) >= 1


@pytest.mark.unit
def test_cd004_emergency_is_cumulative(controller):
    """
    EMERGENCY actions must be a superset of DEFENSIVE + HIGH_ALERT actions
    (tiers are additive, not mutually exclusive).
    """
    actions = controller.respond_to_tier("EMERGENCY", score=8.5, components={})
    action_text = " ".join(actions).lower()

    # Tier-1 (DEFENSIVE) marker
    assert "halt" in action_text or "stop" in action_text or "longs" in action_text
    # Tier-2 (HIGH_ALERT) marker
    assert "partial" in action_text or "trailing" in action_text or "auto_execute" in action_text
    # Tier-3 (EMERGENCY) marker
    assert "close_all_longs" in action_text or "all long" in action_text


# ══════════════════════════════════════════════════════════════════════════════
#  CD-005 — SYSTEMIC tier
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_cd005_systemic_tier_activates(controller, event_capture):
    """
    SYSTEMIC must set safe mode, close ALL positions (including shorts),
    and publish EMERGENCY_STOP on the EventBus.
    """
    capture = event_capture(
        Topics.DEFENSIVE_MODE_ACTIVATED,
        Topics.EMERGENCY_STOP,
    )

    actions = controller.respond_to_tier("SYSTEMIC", score=9.1, components={})

    assert controller.current_tier  == "SYSTEMIC"
    assert controller.is_defensive  is True
    assert controller.is_safe_mode  is True           # SYSTEMIC-only flag

    action_text = " ".join(actions).lower()
    assert "safe_mode" in action_text or "safe mode" in action_text, (
        f"Expected safe-mode action, got: {actions}"
    )
    assert "close_all_positions" in action_text or "all position" in action_text, (
        f"Expected close-all-positions action, got: {actions}"
    )

    # EMERGENCY_STOP must have been published
    assert len(capture[Topics.EMERGENCY_STOP]) >= 1
    es_data = capture[Topics.EMERGENCY_STOP][0].data
    assert es_data["tier"]   == "SYSTEMIC"
    assert "crash" in es_data["reason"].lower() or "systemic" in es_data["reason"].lower()


@pytest.mark.unit
def test_cd005_systemic_includes_all_lower_tier_actions(controller):
    """
    SYSTEMIC is the most severe tier; its actions must be a superset of
    all lower-tier actions.
    """
    actions = controller.respond_to_tier("SYSTEMIC", score=9.5, components={})
    action_text = " ".join(actions).lower()

    # Tier-1 marker
    assert "stop" in action_text or "halt" in action_text or "longs" in action_text
    # Tier-2 marker
    assert "partial" in action_text or "trailing" in action_text or "auto_execute" in action_text
    # Tier-3 marker
    assert "close_all_longs" in action_text or "all long" in action_text
    # Tier-4 marker
    assert "safe_mode" in action_text or "safe mode" in action_text


# ══════════════════════════════════════════════════════════════════════════════
#  CD-006 — Score recovery → tier downgrade to NORMAL
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_cd006_recovery_deactivates_defensive_mode(controller, event_capture):
    """
    After EMERGENCY tier is active, a return to NORMAL must:
      - set current_tier back to "NORMAL"
      - set is_defensive to False
      - set is_safe_mode to False
      - publish SYSTEM_ALERT (deactivation notification)
    """
    capture = event_capture(Topics.SYSTEM_ALERT)

    # Escalate to EMERGENCY
    controller.respond_to_tier("EMERGENCY", score=8.1, components={})
    assert controller.current_tier == "EMERGENCY"   # sanity check

    # Score recovers
    actions = controller.respond_to_tier("NORMAL", score=4.8, components={})

    assert controller.current_tier == "NORMAL"
    assert controller.is_defensive  is False
    assert controller.is_safe_mode  is False

    assert any("deactivated" in a.lower() for a in actions)

    # SYSTEM_ALERT published to inform subscribers that constraints lifted
    assert len(capture[Topics.SYSTEM_ALERT]) >= 1


@pytest.mark.unit
def test_cd006_systemic_recovery_clears_safe_mode(controller):
    """
    SYSTEMIC → NORMAL must clear the safe_mode flag.
    """
    controller.respond_to_tier("SYSTEMIC", score=9.2, components={})
    assert controller.is_safe_mode is True  # sanity

    controller.respond_to_tier("NORMAL", score=3.0, components={})

    assert controller.is_safe_mode  is False
    assert controller.is_defensive  is False
    assert controller.current_tier  == "NORMAL"


@pytest.mark.unit
def test_cd006_multiple_escalation_recovery_cycles(controller):
    """
    The controller must handle repeated escalation→recovery cycles without
    accumulating stale state.
    """
    for _ in range(3):
        controller.respond_to_tier("HIGH_ALERT", score=7.2, components={})
        assert controller.is_defensive is True

        controller.respond_to_tier("NORMAL", score=2.0, components={})
        assert controller.is_defensive is False
        assert controller.current_tier == "NORMAL"


# ══════════════════════════════════════════════════════════════════════════════
#  CD-007 — DEFENSIVE tier halts new BUY (halt_new_longs action present)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_cd007_defensive_includes_halt_new_longs_action(controller):
    """
    The DEFENSIVE tier must include a 'halt new longs' action in its
    returned list, signalling that the order router has been instructed
    to stop accepting new long entries.

    Even when the order router is unavailable (test environment), the
    controller adds a fallback 'attempted' action to the list.
    """
    actions = controller.respond_to_tier("DEFENSIVE", score=5.2, components={})

    action_text = " ".join(actions).lower()
    assert "halt" in action_text or "longs" in action_text, (
        f"Expected a halt-new-longs action; got: {actions}"
    )


@pytest.mark.unit
def test_cd007_normal_tier_does_not_halt_longs(controller):
    """
    NORMAL tier must return an empty actions list — there is no halt action
    when the market is healthy (controller was never in defensive mode).
    """
    actions = controller.respond_to_tier("NORMAL", score=1.0, components={})
    assert actions == []


# ══════════════════════════════════════════════════════════════════════════════
#  CD-008 — EMERGENCY marks positions for close (close_all_longs action)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_cd008_emergency_close_all_longs_action_present(controller):
    """
    EMERGENCY must include 'close_all_longs' in its actions, indicating
    the execution layer has been instructed to exit all long positions.
    Any pending BUY orders are also blocked by the read-only-mode action.
    """
    actions = controller.respond_to_tier("EMERGENCY", score=8.3, components={})

    action_text = " ".join(actions).lower()
    assert "close_all_longs" in action_text or "all long" in action_text, (
        f"Expected close_all_longs action; got: {actions}"
    )
    assert "read_only" in action_text or "no new" in action_text, (
        f"Expected read-only-mode action (blocks pending buys); got: {actions}"
    )


@pytest.mark.unit
def test_cd008_systemic_close_all_positions_including_shorts(controller):
    """
    SYSTEMIC must close ALL positions — not just longs — and the action
    list must reflect this (close_all_positions, not just close_all_longs).
    """
    actions = controller.respond_to_tier("SYSTEMIC", score=9.0, components={})

    action_text = " ".join(actions).lower()
    # close_all_positions is the tier-4 action (broader than close_all_longs)
    assert "close_all_positions" in action_text or "all position" in action_text, (
        f"Expected close_all_positions (shorts + longs); got: {actions}"
    )


@pytest.mark.unit
def test_cd008_emergency_stop_event_not_published_for_emergency_only_systemic(
    controller, event_capture
):
    """
    EMERGENCY_STOP must only be published for SYSTEMIC, not for EMERGENCY.
    This distinguishes 'close longs' from 'full capital lockdown'.
    """
    capture = event_capture(Topics.EMERGENCY_STOP)

    controller.respond_to_tier("EMERGENCY", score=8.2, components={})

    assert len(capture[Topics.EMERGENCY_STOP]) == 0, (
        "EMERGENCY_STOP should NOT be published for EMERGENCY tier — "
        "only for SYSTEMIC tier"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Bonus — Thread safety
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_thread_safety_concurrent_tier_updates(qt_app):
    """
    Concurrent calls to respond_to_tier() from multiple threads must not
    corrupt the controller's state (no AttributeError or torn reads).
    """
    import threading

    controller = CrashDefenseController()
    errors: list[Exception] = []

    def _escalate():
        try:
            controller.respond_to_tier("DEFENSIVE", score=5.0, components={})
        except Exception as exc:
            errors.append(exc)

    def _recover():
        try:
            controller.respond_to_tier("NORMAL", score=1.0, components={})
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=_escalate if i % 2 == 0 else _recover)
        for i in range(20)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert errors == [], f"Thread-safety errors: {errors}"
    # After all threads finish, tier must be a valid value
    assert controller.current_tier in ("NORMAL", "DEFENSIVE")
