from __future__ import annotations

from homeassistant.helpers.dispatcher import async_dispatcher_send
from ttlock_ble import LogEntry

from custom_components.ttlock_ble.connection import log_signal


def _log_state(hass):
    """Return the log event entity state."""
    return next(s for s in hass.states.async_all("event") if "log" in s.entity_id)


async def test_event_entity_created_for_each_key(hass, setup_integration) -> None:
    states = hass.states.async_all("event")
    assert len(states) == 1


async def test_log_event_fires_on_new_record(
    hass,
    setup_integration,
    sample_virtual_key,
) -> None:
    """Log entity fires when a LogEntry arrives via the log dispatcher signal."""
    entry = LogEntry(
        record_number=1,
        record_type=4,
        operate_date="2026-05-17 10:00:00",
        lock_battery=85,
        uid=1234,
        password="123456",
    )
    async_dispatcher_send(
        hass,
        log_signal(sample_virtual_key.lockMac),
        entry,
    )
    await hass.async_block_till_done()
    state = _log_state(hass)
    assert state.attributes["event_type"] == "unlock"
    assert state.attributes["record_type"] == "keyboard_password_unlock"
    assert state.attributes["timestamp"] == "2026-05-17 10:00:00"
    assert state.attributes["battery"] == 85
    assert state.attributes["uid"] == 1234
    assert state.attributes["credential"] == "123456"


async def test_log_event_has_unique_id(
    hass,
    setup_integration,
    sample_virtual_key,
) -> None:
    from homeassistant.helpers import entity_registry as er

    state = _log_state(hass)
    registry = er.async_get(hass)
    reg_entry = registry.async_get(state.entity_id)
    assert reg_entry is not None
    assert reg_entry.unique_id == f"{sample_virtual_key.lockMac}_log"
