"""Sensor platform for ttlock_ble — battery level, last contact and clock drift."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.bluetooth import (
    MONOTONIC_TIME,
    async_last_service_info,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    UnitOfTime,
)
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from ttlock_ble.constants import LogOperate

from .connection import credentials_count_signal, event_signal, log_signal
from .entity import TtlockBleEntity
from .event import PASSCODE_RECORD_TYPES, _record_type_name

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from ttlock_ble import LockEvent, LogEntry, VirtualKey

    from .connection import TtlockBleConnection
    from .coordinator import TtlockBleDataUpdateCoordinator
    from .data import TtlockBleConfigEntry

# How often the recorded advertisement time is re-read. Costs nothing on
# the wire: it only looks at what the bluetooth manager already holds.
LAST_SEEN_REFRESH_INTERVAL = timedelta(seconds=30)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: TtlockBleConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create sensors for every `VirtualKey`."""
    data = entry.runtime_data
    sensors: list[SensorEntity] = []

    for key in data.virtual_keys:
        conn = data.connections[key.lockMac]
        sensors.extend([
            TtlockBleBatterySensor(data.coordinator, key),
            TtlockBleLastSeenSensor(data.coordinator, key),
            TtlockBleClockDriftSensor(data.coordinator, key),
            TtlockBleLastUnlockMethodSensor(data.coordinator, key, conn),
            TtlockBleCredentialsCountSensor(data.coordinator, key, conn, "passcodes"),
            TtlockBleCredentialsCountSensor(data.coordinator, key, conn, "cards"),
            TtlockBleCredentialsCountSensor(data.coordinator, key, conn, "fingerprints"),
        ])

    async_add_entities(sensors)


class TtlockBleBatterySensor(TtlockBleEntity, SensorEntity):
    """Battery level reported by the lock — refreshed on poll and on every push."""

    _attr_translation_key = "battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: TtlockBleDataUpdateCoordinator,
        key: VirtualKey,
    ) -> None:
        """Bind the sensor to its key + coordinator."""
        super().__init__(coordinator, key)
        self._attr_native_value: int | None = None
        self._sync_from_coordinator()

    @property
    def unique_id(self) -> str:
        """Return a stable unique id for this entity."""
        return f"{self._key.lockMac}_battery"

    async def async_added_to_hass(self) -> None:
        """Subscribe to push-event notifications for the lock's MAC."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                event_signal(self._key.lockMac),
                self._on_lock_event,
            ),
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Adopt the coordinator's freshest battery reading, if any."""
        self._sync_from_coordinator()
        super()._handle_coordinator_update()

    @callback
    def _on_lock_event(self, event: LockEvent) -> None:
        """Adopt the battery byte the lock embedded in its push payload."""
        if event.battery is None:
            return
        self._attr_native_value = event.battery
        self.async_write_ha_state()

    def _sync_from_coordinator(self) -> None:
        """Copy `battery_level` from the coordinator snapshot, if known."""
        state = self._lock_state
        if state is None:
            return
        battery = state.get("battery_level")
        if battery is None:
            return
        self._attr_native_value = battery


class TtlockBleClockDriftSensor(TtlockBleEntity, SensorEntity):
    """
    How far the lock's own clock was off local time when last compared.

    The lock stamps every operation-log record from a clock it keeps
    itself, with no NTP and no offset attached, and Home Assistant reads
    those stamps as local time. A drifted clock therefore files a whole
    day of door events at the wrong hour, and nothing else on the device
    would show it.

    Positive means the lock runs ahead. The value is what was measured
    before any correction, so it stays a reading of how far the clock
    wanders rather than resetting to zero every time one is written
    back. It reports `unknown` until a session opened for something else
    has carried a comparison — nothing here connects for it.
    """

    _attr_translation_key = "clock_drift"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_suggested_display_precision = 1

    @property
    def unique_id(self) -> str:
        """Return a stable unique id for this entity."""
        return f"{self._key.lockMac}_clock_drift"

    @property
    def native_value(self) -> float | None:
        """Return the drift of the last comparison, if one has run."""
        sync = self.coordinator.async_clock_sync(self._key.lockMac)
        return None if sync is None else sync["drift_seconds"]

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Carry when the comparison ran, so a stale reading reads as stale."""
        sync = self.coordinator.async_clock_sync(self._key.lockMac)
        return None if sync is None else {"checked_at": sync["checked_at"]}


class TtlockBleLastSeenSensor(TtlockBleEntity, SensorEntity):
    """
    When Home Assistant last received an advertisement from the lock.

    A lock that is working normally is silent most of the time: it holds
    no connection, and its own advertisements are the only sign it is
    still there. This reports the freshness of that sign, which is what
    separates "idle" from "out of range" — a distinction the connection
    sensor cannot make, because the session is down in both cases.

    The value is read from the bluetooth manager's own history rather
    than from the advertisement callback, because that callback is not
    told about an advertisement whose payload matches the previous one.
    An idle lock repeats the same bytes for as long as nothing about it
    changes, so a sensor driven by the callback stops moving while the
    lock is in perfect health — which is precisely backwards.

    Nothing announces those recorded-but-undispatched advertisements, so
    the value is re-read on a timer. The timer is this entity's own and
    not `should_poll`: on a `CoordinatorEntity` polling means
    `async_request_refresh`, which opens a BLE session to the lock. That
    turned a passive read of local memory into a connection every
    refresh interval, against a lock whose polling is deliberately
    spaced by `scan_interval`.
    """

    _attr_translation_key = "last_seen"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:bluetooth-audio"

    def __init__(
        self,
        coordinator: TtlockBleDataUpdateCoordinator,
        key: VirtualKey,
    ) -> None:
        """Bind the sensor to its key + coordinator."""
        super().__init__(coordinator, key)
        self._reported_at: float | None = None
        self._attr_native_value: datetime | None = None

    async def async_added_to_hass(self) -> None:
        """Re-read the history on a timer of its own."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_reread_history,
                LAST_SEEN_REFRESH_INTERVAL,
            ),
        )

    @callback
    def _async_reread_history(self, _now: datetime) -> None:
        """Publish whatever the bluetooth manager has recorded since."""
        self.async_write_ha_state()

    @property
    def unique_id(self) -> str:
        """Return a stable unique id for this entity."""
        return f"{self._key.lockMac}_last_seen"

    @property
    def native_value(self) -> datetime | None:
        """
        Return when the last advertisement arrived, as a wall-clock time.

        The stored reception time is monotonic, so it is converted on
        read and then cached against it: recomputing on every poll would
        walk the timestamp by a fraction of a second each time and write
        a new state for an advertisement that never changed.
        """
        service_info = async_last_service_info(
            self.hass,
            self._key.lockMac,
            connectable=False,
        )
        if service_info is None:
            return self._attr_native_value
        if service_info.time != self._reported_at:
            self._reported_at = service_info.time
            age = MONOTONIC_TIME() - service_info.time
            self._attr_native_value = dt_util.utcnow() - timedelta(seconds=age)
        return self._attr_native_value


def format_unlock_method(entry: LogEntry) -> str:
    """Format a LogEntry into a human-friendly unlock method string."""
    rtype = entry.record_type
    cred = entry.password

    if rtype in (
        LogOperate.FR_UNLOCK_SUCCEED,
        LogOperate.DOUBLE_CHECK_FINGER_PRINT_UNLOCK,
    ):
        return f"Fingerprint ({cred})" if cred else "Fingerprint"

    if rtype in (
        LogOperate.IC_UNLOCK_SUCCEED,
        LogOperate.DOUBLE_CHECK_CARD_UNLOCK,
    ):
        return f"RFID Card ({cred})" if cred else "RFID Card"

    if rtype in (
        LogOperate.KEYBOARD_PASSWORD_UNLOCK,
        LogOperate.DOUBLE_CHECK_PASSCODE_UNLOCK,
        LogOperate.ADMIN_CODE_UNLOCK,
    ):
        return "Passcode"

    if rtype in (
        LogOperate.MOBILE_UNLOCK,
        LogOperate.SERVER_UNLOCK,
        LogOperate.GATEWAY_UNLOCK,
        LogOperate.APP_AUTH_KEY_UNLOCK_SUCCESS,
        LogOperate.GATEWAY_AUTH_KEY_UNLOCK_SUCCESS,
    ):
        return f"Mobile App (Key ID: {entry.key_id})" if entry.key_id else "Mobile App"

    if rtype in (
        LogOperate.WIRELESS_KEY_FOB,
        LogOperate.REMOTE_CONTROL_KEY,
        LogOperate.DOUBLE_CHECK_KEY_FOB_UNLOCK,
    ):
        return f"Key Fob ({cred})" if cred else "Key Fob"

    if rtype in (
        LogOperate.FACE_3D_UNLOCK_SUCCESS,
        LogOperate.DOUBLE_CHECK_FACE_UNLOCK,
    ):
        return f"Face ({cred})" if cred else "Face"

    if rtype == LogOperate.AUTO_LOCK:
        return "Auto-Lock"

    if rtype == LogOperate.OPERATE_KEY_UNLOCK:
        return "Mechanical Key"

    if rtype == LogOperate.DOOR_SENSOR_UNLOCK:
        return "Door Sensor Unlock"

    if rtype == LogOperate.QR_CODE_UNLOCK_SUCCESS:
        return "QR Code"

    try:
        name = LogOperate(rtype).name.replace("_", " ").title()
        return f"{name} ({cred})" if cred else name
    except ValueError:
        return f"Method {rtype}"


class TtlockBleLastUnlockMethodSensor(TtlockBleEntity, RestoreEntity, SensorEntity):
    """Sensor reporting how the lock was last opened or operated."""

    _attr_translation_key = "last_unlock_method"
    _attr_icon = "mdi:account-lock-open-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: TtlockBleDataUpdateCoordinator,
        key: VirtualKey,
        connection: TtlockBleConnection,
    ) -> None:
        """Bind sensor to coordinator, key, and connection."""
        super().__init__(coordinator, key)
        self._connection = connection
        self._attr_native_value: str | None = None
        self._attr_extra_state_attributes: dict[str, Any] = {}

    @property
    def unique_id(self) -> str:
        """Return a stable unique id for this entity."""
        return f"{self._key.lockMac}_last_unlock_method"

    async def async_added_to_hass(self) -> None:
        """Subscribe to log notifications and restore prior state."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            if last_state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                self._attr_native_value = last_state.state
                self._attr_extra_state_attributes = dict(last_state.attributes)

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                log_signal(self._key.lockMac),
                self._on_log_entry,
            ),
        )

    @callback
    def _on_log_entry(self, entry: LogEntry) -> None:
        """Update sensor value and attributes from a new LogEntry."""
        self._attr_native_value = format_unlock_method(entry)
        attrs: dict[str, Any] = {
            "record_type": _record_type_name(entry.record_type),
            "record_number": entry.record_number,
        }
        if entry.operate_date is not None:
            attrs["operate_date"] = entry.operate_date.isoformat()
        if entry.uid is not None:
            attrs["uid"] = entry.uid
        if entry.password is not None and entry.record_type not in PASSCODE_RECORD_TYPES:
            attrs["credential"] = entry.password
        if entry.key_id is not None:
            attrs["key_id"] = entry.key_id
        if entry.lock_battery is not None:
            attrs["lock_battery"] = entry.lock_battery
        self._attr_extra_state_attributes = attrs
        self.async_write_ha_state()


CREDENTIAL_ICONS: dict[str, str] = {
    "passcodes": "mdi:form-textbox-password",
    "cards": "mdi:smart-card-outline",
    "fingerprints": "mdi:fingerprint",
}


class TtlockBleCredentialsCountSensor(TtlockBleEntity, RestoreEntity, SensorEntity):
    """Sensor reporting the number of enrolled credentials on the lock."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: TtlockBleDataUpdateCoordinator,
        key: VirtualKey,
        connection: TtlockBleConnection,
        cred_type: str,
    ) -> None:
        """Bind sensor to coordinator, key, connection, and credential type."""
        super().__init__(coordinator, key)
        self._connection = connection
        self._cred_type = cred_type
        self._attr_translation_key = f"{cred_type}_count"
        self._attr_icon = CREDENTIAL_ICONS.get(cred_type, "mdi:key")

    @property
    def unique_id(self) -> str:
        """Return a stable unique id."""
        return f"{self._key.lockMac}_{self._cred_type}_count"

    @property
    def native_value(self) -> int | None:
        """Return current enrolled count."""
        return self._connection.get_credential_count(self._cred_type)

    async def async_added_to_hass(self) -> None:
        """Subscribe to credential count updates and restore prior count."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            if last_state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                try:
                    count = int(last_state.state)
                    self._connection.set_credential_count(self._cred_type, count)
                except (ValueError, TypeError):
                    pass

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                credentials_count_signal(self._key.lockMac),
                self._on_count_update,
            )
        )

    @callback
    def _on_count_update(self, cred_type: str, count: int) -> None:
        """Update sensor state when its credential count changes."""
        if cred_type == self._cred_type:
            self.async_write_ha_state()
