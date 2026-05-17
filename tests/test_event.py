from __future__ import annotations

from homeassistant.helpers.dispatcher import async_dispatcher_send
from ttlock_ble import LockEvent, LogEntry

from custom_components.ttlock_ble.connection import event_signal, log_signal
from custom_components.ttlock_ble.event import (
    EVENT_TYPE_FAILED,
    EVENT_TYPE_SUCCESS,
)


def _operation_state(hass):
    """Return the operation event entity state."""
    return next(s for s in hass.states.async_all("event") if "operation" in s.entity_id)


def _log_state(hass):
    """Return the log event entity state."""
    return next(s for s in hass.states.async_all("event") if "log" in s.entity_id)


async def test_event_entity_created_for_each_key(hass, setup_integration) -> None:
    states = hass.states.async_all("event")
    assert len(states) == 2


async def test_event_success_fires_success_type(
    hass,
    setup_integration,
    sample_virtual_key,
) -> None:
    pushed = LockEvent(cmd_echo=0x47, status=1, data=b"\xaa\xbb")
    async_dispatcher_send(
        hass,
        event_signal(sample_virtual_key.lockMac),
        pushed,
    )
    await hass.async_block_till_done()
    state = _operation_state(hass)
    assert state.attributes["event_type"] == EVENT_TYPE_SUCCESS
    assert state.attributes["cmd_echo"] == 0x47
    assert state.attributes["data"] == "aabb"


async def test_event_failure_fires_failed_type(
    hass,
    setup_integration,
    sample_virtual_key,
) -> None:
    pushed = LockEvent(cmd_echo=0x47, status=0, data=b"")
    async_dispatcher_send(
        hass,
        event_signal(sample_virtual_key.lockMac),
        pushed,
    )
    await hass.async_block_till_done()
    state = _operation_state(hass)
    assert state.attributes["event_type"] == EVENT_TYPE_FAILED


async def test_event_has_unique_id(
    hass,
    setup_integration,
    sample_virtual_key,
) -> None:
    from homeassistant.helpers import entity_registry as er

    state = _operation_state(hass)
    registry = er.async_get(hass)
    entry = registry.async_get(state.entity_id)
    assert entry is not None
    assert entry.unique_id == f"{sample_virtual_key.lockMac}_operation"


async def test_event_surfaces_decoded_state_push(
    hass,
    setup_integration,
    sample_virtual_key,
) -> None:
    """A 3-byte heartbeat push lands as attributes on the event entity."""
    pushed = LockEvent.from_payload(0x14, 1, bytes.fromhex("2c0102"))
    async_dispatcher_send(
        hass,
        event_signal(sample_virtual_key.lockMac),
        pushed,
    )
    await hass.async_block_till_done()
    attrs = _operation_state(hass).attributes
    assert attrs["battery"] == 0x2C
    assert attrs["lock_state"] == "unlocked"
    assert "uid" not in attrs
    assert "record_id" not in attrs


async def test_event_surfaces_decoded_log_push(
    hass,
    setup_integration,
    sample_virtual_key,
) -> None:
    """A 15-byte log-entry push surfaces uid + record_id + timestamp."""
    payload = bytes.fromhex("2c000000006a0224a31a050b0f3008")
    pushed = LockEvent.from_payload(0x14, 1, payload)
    async_dispatcher_send(
        hass,
        event_signal(sample_virtual_key.lockMac),
        pushed,
    )
    await hass.async_block_till_done()
    attrs = _operation_state(hass).attributes
    assert attrs["battery"] == 0x2C
    assert attrs["uid"] == 0
    assert attrs["record_id"] == 0x6A0224A3
    assert attrs["timestamp"] == "2026-05-11 15:48:08"
    assert "lock_state" not in attrs


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
