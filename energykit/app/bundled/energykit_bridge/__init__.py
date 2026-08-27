from __future__ import annotations

import asyncio
import json
from pathlib import Path
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

PLATFORMS = ["sensor"]
MAP = Path("/config/energykit_mapping.json")

async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await _setup_heatpump_control(hass)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

async def _setup_heatpump_control(hass: HomeAssistant):
    """Small SG-Ready controller using grid export as the signal.

    It is intentionally conservative: only the SG-Ready switch mode is automated.
    Other heat-pump modes remain vendor-specific and are configured separately.
    """
    try:
        data=json.loads(MAP.read_text())
    except Exception:
        return
    hp=data.get("heatpump") or {}
    if hp.get("mode") != "sg-ready" or not hp.get("switch"):
        return
    grid_src=(data.get("sources") or {}).get("grid")
    if not grid_src:
        return
    switch=hp["switch"]
    on_w=float(hp.get("power_threshold",2500))
    off_w=float(hp.get("off_threshold",800))
    delay=max(1,int(hp.get("delay_min",5)))*60
    task=None

    async def set_switch(turn_on: bool):
        domain, entity = switch.split('.',1)
        await hass.services.async_call(domain, 'turn_on' if turn_on else 'turn_off', {"entity_id": switch}, blocking=False)

    def export_w():
        st=hass.states.get(grid_src)
        if st is None:return None
        try:v=float(st.state)
        except Exception:return None
        if str(st.attributes.get('unit_of_measurement','')).lower()=='kw':v*=1000
        # EnergyKit/evcc sign convention: negative grid power means export.
        return max(0.0,-v)

    @callback
    def changed(event):
        nonlocal task
        value=export_w()
        if value is None:return
        desired = True if value >= on_w else False if value <= off_w else None
        if desired is None:return
        current=hass.states.get(switch)
        is_on=current is not None and current.state=='on'
        if desired==is_on:return
        if task and not task.done():task.cancel()
        async def delayed():
            try:
                await asyncio.sleep(delay)
                value2=export_w()
                if value2 is None:return
                if desired and value2>=on_w:await set_switch(True)
                if (not desired) and value2<=off_w:await set_switch(False)
            except asyncio.CancelledError:
                return
        task=hass.async_create_task(delayed())

    async_track_state_change_event(hass,[grid_src],changed)
