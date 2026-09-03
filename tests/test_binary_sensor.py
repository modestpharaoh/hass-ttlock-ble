from __future__ import annotations

from homeassistant.helpers.dispatcher import async_dispatcher_send

from custom_components.hass_ttlock_ble.connection import connection_signal


def _conn_state(hass):
    return next(
        s
        for s in hass.states.async_all("binary_sensor")
        if s.entity_id.endswith("_connection")
    )


async def test_connection_binary_sensor_created_for_each_key(
    hass,
    setup_integration,
) -> None:
    assert len(hass.states.async_all("binary_sensor")) >= 1
    assert _conn_state(hass) is not None


async def test_connection_binary_sensor_reflects_initial_state(
    hass,
    setup_integration,
) -> None:
    state = _conn_state(hass)
    assert state.state == "on"


async def test_connection_binary_sensor_device_class(
    hass,
    setup_integration,
) -> None:
    state = _conn_state(hass)
    assert state.attributes["device_class"] == "connectivity"


async def test_connection_binary_sensor_has_unique_id(
    hass,
    setup_integration,
    sample_virtual_key,
) -> None:
    from homeassistant.helpers import entity_registry as er

    state = _conn_state(hass)
    registry = er.async_get(hass)
    entry = registry.async_get(state.entity_id)
    assert entry is not None
    assert entry.unique_id == f"{sample_virtual_key.lockMac}_connection"


async def test_connection_binary_sensor_is_diagnostic(
    hass,
    setup_integration,
) -> None:
    from homeassistant.helpers import entity_registry as er

    state = _conn_state(hass)
    registry = er.async_get(hass)
    entry = registry.async_get(state.entity_id)
    assert entry is not None
    assert entry.entity_category == er.EntityCategory.DIAGNOSTIC


async def test_connection_binary_sensor_icon_reflects_state(
    hass,
    setup_integration,
    sample_virtual_key,
    mock_ttlock_connection,
) -> None:
    """Icon switches between bluetooth-connect and bluetooth-off."""
    state = _conn_state(hass)
    assert state.attributes["icon"] == "mdi:bluetooth-connect"
    mock_ttlock_connection.is_connected = False
    async_dispatcher_send(
        hass,
        connection_signal(sample_virtual_key.lockMac),
        False,  # noqa: FBT003
    )
    await hass.async_block_till_done()
    assert hass.states.get(state.entity_id).attributes["icon"] == "mdi:bluetooth-off"


async def test_connection_binary_sensor_updates_on_signal(
    hass,
    setup_integration,
    sample_virtual_key,
    mock_ttlock_connection,
) -> None:
    """A dispatcher signal pushes the freshest connection state without polling."""
    state = _conn_state(hass)
    assert state.state == "on"
    mock_ttlock_connection.is_connected = False
    async_dispatcher_send(
        hass,
        connection_signal(sample_virtual_key.lockMac),
        False,  # noqa: FBT003
    )
    await hass.async_block_till_done()
    assert hass.states.get(state.entity_id).state == "off"
