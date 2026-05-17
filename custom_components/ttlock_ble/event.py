"""
Event platform for ttlock_ble.

Surfaces two kinds of events per lock:

1. **Operation (push):** Unsolicited BLE push notifications from the
   lock (keypad, fingerprint, IC card, mechanical key, official app).
2. **Log (pull):** Historical operation records read from the lock's
   on-device storage every time the integration connects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.event import EventEntity
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from ttlock_ble import LogOperate

from .connection import event_signal, log_signal
from .entity import TtlockBleEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from ttlock_ble import LockEvent, LogEntry, VirtualKey

    from .coordinator import TtlockBleDataUpdateCoordinator
    from .data import TtlockBleConfigEntry


EVENT_TYPE_SUCCESS = "operation_success"
EVENT_TYPE_FAILED = "operation_failed"
LOCK_EVENT_STATUS_SUCCESS = 1

LOG_EVENT_TYPES: list[str] = [
    "unlock",
    "lock",
    "unlock_failed",
    "password_change",
    "other",
]


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: TtlockBleConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create operation-event and log-event entities per `VirtualKey`."""
    data = entry.runtime_data
    entities: list[EventEntity] = []
    for key in data.virtual_keys:
        entities.append(TtlockBleOperationEvent(data.coordinator, key))
        entities.append(TtlockBleLogEvent(data.coordinator, key))
    async_add_entities(entities)


class TtlockBleOperationEvent(TtlockBleEntity, EventEntity):
    """Fires when the lock pushes an unsolicited operation notification."""

    _attr_translation_key = "operation"

    def __init__(
        self,
        coordinator: TtlockBleDataUpdateCoordinator,
        key: VirtualKey,
    ) -> None:
        """Bind the entity to its key + coordinator."""
        super().__init__(coordinator, key)
        self._attr_unique_id = f"{key.lockMac}_operation"
        self._attr_event_types = [EVENT_TYPE_SUCCESS, EVENT_TYPE_FAILED]

    async def async_added_to_hass(self) -> None:
        """Subscribe to the dispatcher signal driven by the coordinator's poll."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                event_signal(self._key.lockMac),
                self._on_lock_event,
            ),
        )

    @callback
    def _on_lock_event(self, event: LockEvent) -> None:
        """
        Translate a `LockEvent` push into an HA event fire.

        Fields the SDK decoded (battery, lock_state, uid, record_id,
        timestamp) become event attributes; consumers can drive
        automations off them directly. Raw `cmd_echo` and `data` hex
        stay attached so an automation can still inspect any opcode the
        SDK doesn't yet recognise.
        """
        event_type = (
            EVENT_TYPE_SUCCESS
            if event.status == LOCK_EVENT_STATUS_SUCCESS
            else EVENT_TYPE_FAILED
        )
        attributes: dict[str, object] = {
            "cmd_echo": event.cmd_echo,
            "data": event.data.hex(),
        }
        if event.battery is not None:
            attributes["battery"] = event.battery
        if event.lock_state is not None:
            attributes["lock_state"] = "unlocked" if event.lock_state == 1 else "locked"
        if event.uid is not None:
            attributes["uid"] = event.uid
        if event.record_id is not None:
            attributes["record_id"] = event.record_id
        if event.timestamp is not None:
            attributes["timestamp"] = event.timestamp
        self._trigger_event(event_type, attributes)
        self.async_write_ha_state()


def _classify_record(record_type: int) -> str:
    """Map a LogOperate record type to an HA event type."""
    unlock_types = {
        LogOperate.MOBILE_UNLOCK,
        LogOperate.KEYBOARD_PASSWORD_UNLOCK,
        LogOperate.IC_UNLOCK_SUCCEED,
        LogOperate.FR_UNLOCK_SUCCEED,
        LogOperate.BONG_UNLOCK,
        LogOperate.GATEWAY_UNLOCK,
        LogOperate.WIRELESS_KEY_FOB,
        LogOperate.WIRELESS_KEY_PAD,
        LogOperate.REMOTE_CONTROL_KEY,
    }
    lock_types = {
        LogOperate.BLE_LOCK,
        LogOperate.PASSCODE_LOCK,
        LogOperate.IC_LOCK,
        LogOperate.FR_LOCK,
    }
    fail_types = {
        LogOperate.ERROR_PASSWORD_UNLOCK,
        LogOperate.FR_UNLOCK_FAILED,
        LogOperate.APP_UNLOCK_FAILED_LOCK_REVERSE,
        LogOperate.PASSCODE_UNLOCK_FAILED_LOCK_REVERSE,
        LogOperate.IC_UNLOCK_FAILED_LOCK_REVERSE,
        LogOperate.FR_UNLOCK_FAILED_LOCK_REVERSE,
        LogOperate.PASSCODE_EXPIRED,
        LogOperate.PASSCODE_IN_BLACK_LIST,
    }
    password_types = {
        LogOperate.KEYBOARD_MODIFY_PASSWORD,
        LogOperate.KEYBOARD_REMOVE_SINGLE_PASSWORD,
        LogOperate.KEYBOARD_REMOVE_ALL_PASSWORDS,
        LogOperate.KEYBOARD_PASSWORD_KICKED,
        LogOperate.USE_DELETE_CODE,
        LogOperate.ADD_IC,
        LogOperate.CLEAR_IC,
        LogOperate.DELETE_IC_SUCCEED,
        LogOperate.ADD_FR,
        LogOperate.DELETE_FR_SUCCEED,
    }
    if record_type in unlock_types:
        return "unlock"
    if record_type in lock_types:
        return "lock"
    if record_type in fail_types:
        return "unlock_failed"
    if record_type in password_types:
        return "password_change"
    return "other"


def _record_type_name(record_type: int) -> str:
    """Return a human-friendly name for the record type."""
    try:
        return LogOperate(record_type).name.lower()
    except ValueError:
        return str(record_type)


class TtlockBleLogEvent(TtlockBleEntity, EventEntity):
    """Fires when a new operation log entry is retrieved from the lock."""

    _attr_translation_key = "log"

    def __init__(
        self,
        coordinator: TtlockBleDataUpdateCoordinator,
        key: VirtualKey,
    ) -> None:
        """Bind the entity to its key + coordinator."""
        super().__init__(coordinator, key)
        self._attr_unique_id = f"{key.lockMac}_log"
        self._attr_event_types = LOG_EVENT_TYPES

    async def async_added_to_hass(self) -> None:
        """Subscribe to the log dispatcher signal."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                log_signal(self._key.lockMac),
                self._on_log_entry,
            ),
        )

    @callback
    def _on_log_entry(self, entry: LogEntry) -> None:
        """Translate a LogEntry into an HA event fire."""
        event_type = _classify_record(entry.record_type)
        attributes: dict[str, object] = {
            "record_type": _record_type_name(entry.record_type),
            "timestamp": entry.operate_date,
            "battery": entry.lock_battery,
        }
        if entry.uid is not None:
            attributes["uid"] = entry.uid
        if entry.password is not None:
            attributes["credential"] = entry.password
        if entry.key_id is not None:
            attributes["key_id"] = entry.key_id
        if entry.accessory_battery is not None:
            attributes["accessory_battery"] = entry.accessory_battery
        self._trigger_event(event_type, attributes)
        self.async_write_ha_state()
