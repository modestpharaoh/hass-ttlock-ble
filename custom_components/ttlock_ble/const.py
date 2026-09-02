"""Constants for ttlock_ble."""

from __future__ import annotations

from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

DOMAIN = "ttlock_ble"
MANUFACTURER = "TTLock"
ATTRIBUTION = "Data provided by the TTLock cloud and on-lock BLE"

CONF_PERMANENT_CONNECTION = "permanent_connection"

CLOUD_ERR_NEW_DEVICE_LOGIN = -1014
HTTP_STATUS_UNAUTHORIZED = frozenset({401, 403})

SERVICE_GET_PASSAGE_MODE = "get_passage_mode"
SERVICE_SET_PASSAGE_MODE = "set_passage_mode"
SERVICE_DELETE_PASSAGE_MODE = "delete_passage_mode"
SERVICE_CLEAR_PASSAGE_MODE = "clear_passage_mode"
