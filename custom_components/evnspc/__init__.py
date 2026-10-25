"""Tích hợp EVNSPC (Điện lực miền Nam) cho Home Assistant."""
from __future__ import annotations

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import EvnSpcClient
from .const import CONF_COOKIES
from .coordinator import EvnSpcCoordinator

PLATFORMS = ["sensor"]

type EvnSpcConfigEntry = ConfigEntry[EvnSpcCoordinator]


def build_client(hass: HomeAssistant) -> EvnSpcClient:
    """Mỗi entry/flow dùng một session riêng (cookie jar riêng)."""
    session = async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar())
    return EvnSpcClient(session)


async def async_setup_entry(hass: HomeAssistant, entry: EvnSpcConfigEntry) -> bool:
    client = build_client(hass)
    client.import_cookies(entry.data.get(CONF_COOKIES, {}))

    coordinator = EvnSpcCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: EvnSpcConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: EvnSpcConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
