from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import aiohttp
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
DOMAIN = "energykit_bootstrap"
DONE = Path("/config/.energykit_bootstrap_done")
STATUS = Path("/config/energykit_bootstrap_status.json")
SLUG = "local_energykit"
SUPERVISOR = "http://supervisor"


def _write_status(**data):
    try:
        STATUS.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    except Exception:
        pass


async def _request(session, token, method, path, **kwargs):
    headers={"Authorization": f"Bearer {token}", "Content-Type":"application/json"}
    async with session.request(method, SUPERVISOR + path, headers=headers, **kwargs) as res:
        text=await res.text()
        if res.status >= 400:
            raise RuntimeError(f"{method} {path}: HTTP {res.status}: {text[:500]}")
        if not text:
            return {}
        try:
            data=json.loads(text)
            return data.get("data", data)
        except Exception:
            return {"raw":text}


async def _installed(session, token):
    for path in (f"/addons/{SLUG}/info", f"/apps/{SLUG}/info"):
        try:
            await _request(session, token, "GET", path)
            return True
        except Exception:
            pass
    return False


async def _bootstrap(hass: HomeAssistant):
    if DONE.exists():
        return
    token=os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        _write_status(ok=False, stage="token", error="Home Assistant Core hat keinen SUPERVISOR_TOKEN")
        _LOGGER.error("EnergyKit Bootstrap: SUPERVISOR_TOKEN fehlt in Home Assistant Core")
        return

    timeout=aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # Supervisor/store can need a little time after Core is already up.
        last_error=""
        for attempt in range(1, 31):
            try:
                if await _installed(session, token):
                    DONE.write_text("already-installed\n")
                    _write_status(ok=True, stage="already-installed", attempt=attempt)
                    return
                try:
                    await _request(session, token, "POST", "/store/reload", json={})
                except Exception:
                    # Current v1 API keeps the reload compatibility alias.
                    try:
                        await _request(session, token, "POST", "/addons/reload", json={})
                    except Exception:
                        pass

                # Prefer the current apps endpoint, then the long-lived v1 alias.
                install_error=None
                for path in (f"/store/apps/{SLUG}/install", f"/store/addons/{SLUG}/install"):
                    try:
                        await _request(session, token, "POST", path, json={"background":False})
                        install_error=None
                        break
                    except Exception as exc:
                        install_error=exc
                if install_error:
                    raise install_error

                # Installation and start are intentionally separate. This forces
                # Supervisor to create and inject the app's own supervisor token.
                for path in (f"/addons/{SLUG}/start", f"/apps/{SLUG}/start"):
                    try:
                        await _request(session, token, "POST", path, json={})
                        break
                    except Exception as exc:
                        last_error=str(exc)

                DONE.write_text("installed-by-supervisor\n")
                _write_status(ok=True, stage="installed-and-started", attempt=attempt)
                _LOGGER.info("EnergyKit Bootstrap: %s regulär über Supervisor installiert", SLUG)
                return
            except Exception as exc:
                last_error=str(exc)
                _write_status(ok=False, stage="retry", attempt=attempt, error=last_error)
                await asyncio.sleep(min(5 + attempt, 20))

        _write_status(ok=False, stage="failed", error=last_error)
        _LOGGER.error("EnergyKit Bootstrap endgültig fehlgeschlagen: %s", last_error)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.async_create_task(_bootstrap(hass), "energykit_first_boot")
    return True
