from __future__ import annotations

import json
from pathlib import Path
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfPower, PERCENTAGE
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event

MAP = Path("/config/energykit_mapping.json")

SPECS = {
    "pv": ("EK PV Power", "pv_power", UnitOfPower.WATT, SensorDeviceClass.POWER),
    "house": ("EK House Power", "house_power", UnitOfPower.WATT, SensorDeviceClass.POWER),
    "grid": ("EK Grid Power", "grid_power", UnitOfPower.WATT, SensorDeviceClass.POWER),
    "battery_power": ("EK Battery Power", "battery_power", UnitOfPower.WATT, SensorDeviceClass.POWER),
    "battery_soc": ("EK Battery SoC", "battery_soc", PERCENTAGE, SensorDeviceClass.BATTERY),
}
SIM = {"pv": 6240, "house": 2130, "grid": -1080, "battery_power": -3030, "battery_soc": 74}

async def async_setup_entry(hass, entry, async_add_entities):
    try:
        data=json.loads(MAP.read_text())
    except Exception:
        data={"simulation": True, "sources": {}}
    async_add_entities([EnergyKitSensor(hass,key,data) for key in SPECS], True)

class EnergyKitSensor(SensorEntity):
    _attr_should_poll=False
    _attr_state_class=SensorStateClass.MEASUREMENT

    def __init__(self,hass,key,data):
        self.hass=hass; self.key=key; self.data=data
        name,obj,unit,device_class=SPECS[key]
        self._attr_name=name
        self._attr_unique_id=f"energykit_{obj}"
        self._attr_suggested_object_id=f"ek_{obj}"
        self._attr_native_unit_of_measurement=unit
        self._attr_device_class=device_class
        self._attr_native_value=None
        self._unsub=None

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        if self.data.get("simulation"):
            self._attr_native_value=SIM[self.key]
            self.async_write_ha_state(); return
        src=self.data.get("sources",{}).get(self.key)
        if not src: return
        self._update(src)
        @callback
        def changed(event):
            self._update(src); self.async_write_ha_state()
        self._unsub=async_track_state_change_event(self.hass,[src],changed)

    def _update(self,src):
        st=self.hass.states.get(src)
        if st is None: self._attr_native_value=None; return
        try:
            value=float(st.state)
        except (ValueError,TypeError):
            self._attr_native_value=None; return
        unit=str(st.attributes.get("unit_of_measurement", "")).lower()
        # Normalize every EnergyKit power entity to watts.
        if self.key != "battery_soc" and unit == "kw":
            value *= 1000
        self._attr_native_value=value

    async def async_will_remove_from_hass(self):
        if self._unsub:self._unsub()
