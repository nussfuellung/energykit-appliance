from homeassistant import config_entries

class EnergyKitBridgeFlow(config_entries.ConfigFlow, domain="energykit_bridge"):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id("energykit_bridge")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="EnergyKit Bridge", data={})
