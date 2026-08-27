# EnergyKit v0.4 Alpha Testplan

## Gate A: ISO / First Boot
- ISO boots in UEFI VM.
- HAOS image download succeeds and digest verification succeeds.
- Target disk is the VM disk, not installer media.
- `hassos-data` is found after flashing.
- `local_energykit` exists in Supervisor preseed metadata.
- HAOS boots after removing ISO.
- Home Assistant completes first start.
- EnergyKit image downloads and app starts automatically.

## Gate B: Setup / Auth
- Owner can open EnergyKit before setup completion.
- Non-admin should not see the panel because `panel_admin: true`.
- `energykit-service` can be created.
- Credentials download works.
- User ID is persisted.

## Gate C: Simulation
- Simulation discovery returns Sigenergy, Deye, go-e.
- Base components install.
- Auto mapping returns all five required sources.
- Saving mapping installs/restarts EnergyKit Bridge.
- `sensor.ek_pv_power`, `sensor.ek_house_power`, `sensor.ek_grid_power`, `sensor.ek_battery_power`, `sensor.ek_battery_soc` exist after restart.
- Optional SG-Ready and wallbox fields can be saved.
- evcc app installs.
- evcc config is written to public addon config directory.
- evcc starts with EnergyKit HA meters.
- Dashboard is created and Mushroom resource loads.
- End-to-end check reports all mandatory checks green.
- HTML and JSON reports download.
- Supervisor backup is created.

## Gate D: Handover
- Handover is blocked if a mandatory check is red.
- Successful handover clears the plaintext service password.
- Owner receives HTTP 403 inside EnergyKit after handover.
- `energykit-service` can open EnergyKit.
- Service mode shows components, diagnostics and recovery.
- Recovery mode reopens setup without deleting HA integrations.

## Gate E: Real Hardware later
- Sigenergy Config Flow works against a real SigenStor.
- Deye Config Flow works against supported logger / Modbus transport.
- Auto-mapping confidence is checked against actual entity names.
- Power units/signs are validated against evcc sign conventions.
- Real wallbox charger adapters replace the generic HA switch where appropriate.
