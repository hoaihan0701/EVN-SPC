"""Sensor EVNSPC."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import EvnSpcConfigEntry
from .const import DOMAIN
from .coordinator import EvnSpcCoordinator, EvnSpcData


@dataclass(frozen=True, kw_only=True)
class EvnSpcSensorDescription(SensorEntityDescription):
    value_fn: Callable[[EvnSpcData], float | None]


SENSORS: tuple[EvnSpcSensorDescription, ...] = (
    EvnSpcSensorDescription(
        key="daily_kwh",
        translation_key="daily_kwh",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=2,
        value_fn=lambda d: d.latest.kwh if d.latest else None,
    ),
    EvnSpcSensorDescription(
        key="meter_index",
        translation_key="meter_index",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda d: d.latest.index if d.latest else None,
    ),
    EvnSpcSensorDescription(
        key="month_kwh",
        translation_key="month_kwh",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=2,
        value_fn=lambda d: d.month_total(),
    ),
    EvnSpcSensorDescription(
        key="week_kwh",
        translation_key="week_kwh",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=2,
        value_fn=lambda d: d.total_last_days(7),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EvnSpcConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(EvnSpcSensor(coordinator, d) for d in SENSORS)


class EvnSpcSensor(CoordinatorEntity[EvnSpcCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: EvnSpcSensorDescription

    def __init__(
        self, coordinator: EvnSpcCoordinator, description: EvnSpcSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        code = coordinator.customer_code
        self._attr_unique_id = f"{code}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, code)},
            name=f"EVNSPC {code}",
            manufacturer="EVNSPC",
            model="Đo ghi xa (cskh.evnspc.vn)",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://cskh.evnspc.vn",
        )

    @property
    def native_value(self) -> float | None:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict | None:
        data = self.coordinator.data
        if not data or not data.latest:
            return None
        attrs: dict = {"reading_date": data.latest.day.isoformat()}
        if self.entity_description.key == "daily_kwh":
            attrs["history"] = {r.day.isoformat(): r.kwh for r in data.readings}
        return attrs
