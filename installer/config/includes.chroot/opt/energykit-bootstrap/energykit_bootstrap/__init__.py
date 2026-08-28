from __future__ import annotations

import asyncio, json, logging, os
from pathlib import Path
import aiohttp
from homeassistant.core import HomeAssistant

_LOGGER=logging.getLogger(__name__)
DOMAIN="energykit_bootstrap"
DONE=Path("/config/.energykit_bootstrap_done")
STATUS=Path("/config/energykit_bootstrap_status.json")
SLUG="local_energykit"
SUPERVISOR="http://supervisor"

def _write_status(**data):
    try: STATUS.write_text(json.dumps(data,indent=2,ensure_ascii=False)+"\n")
    except Exception: pass

async def _request(session,token,method,path,**kwargs):
    headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"}
    async with session.request(method,SUPERVISOR+path,headers=headers,**kwargs) as res:
        body=await res.text()
        if res.status>=400: raise RuntimeError(f"{method} {path}: HTTP {res.status}: {body[:500]}")
        if not body: return {}
        try:
            data=json.loads(body); return data.get("data",data)
        except Exception: return {"raw":body}

async def _installed_info(session,token):
    # /addons/{slug}/info is NOT an installation check: v1 falls back to Store info.
    data=await _request(session,token,"GET","/addons")
    apps=data.get("addons",data) if isinstance(data,dict) else data
    if not isinstance(apps,list): return None
    return next((a for a in apps if str(a.get("slug"))==SLUG),None)

async def _uninstall_if_present(session,token):
    app=await _installed_info(session,token)
    if not app: return False
    _write_status(ok=False,stage="repairing-installed-state",installed_state=app.get("state"),installed_version=app.get("version"))
    last=None
    for path in (f"/apps/{SLUG}/uninstall",f"/addons/{SLUG}/uninstall"):
        try:
            await _request(session,token,"POST",path,json={"remove_config":False}); return True
        except Exception as exc: last=exc
    raise RuntimeError(f"Alt-State konnte nicht entfernt werden: {last}")

async def _bootstrap(hass:HomeAssistant):
    if DONE.exists(): return
    token=os.environ.get("SUPERVISOR_TOKEN","")
    if not token:
        _write_status(ok=False,stage="token",error="Home Assistant Core hat keinen SUPERVISOR_TOKEN"); return

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
        last=""
        for attempt in range(1,31):
            try:
                _write_status(ok=False,stage="store-reload",attempt=attempt)
                try: await _request(session,token,"POST","/store/reload",json={})
                except Exception:
                    try: await _request(session,token,"POST","/addons/reload",json={})
                    except Exception: pass

                repaired=await _uninstall_if_present(session,token)
                if repaired:
                    await asyncio.sleep(3)
                    try: await _request(session,token,"POST","/store/reload",json={})
                    except Exception: pass

                _write_status(ok=False,stage="installing",attempt=attempt)
                install_error=None
                for path in (f"/store/apps/{SLUG}/install",f"/store/addons/{SLUG}/install",f"/addons/{SLUG}/install"):
                    try:
                        await _request(session,token,"POST",path,json={"background":False})
                        install_error=None; break
                    except Exception as exc: install_error=exc
                if install_error: raise install_error

                installed=None
                for _ in range(30):
                    installed=await _installed_info(session,token)
                    if installed: break
                    await asyncio.sleep(2)
                if not installed: raise RuntimeError("EnergyKit fehlt nach Installation in /addons")

                _write_status(ok=False,stage="starting",attempt=attempt,installed_version=installed.get("version"))
                await _request(session,token,"POST",f"/addons/{SLUG}/start",json={})

                started=None
                for _ in range(45):
                    current=await _installed_info(session,token)
                    if current and str(current.get("state","")).lower()=="started":
                        started=current; break
                    await asyncio.sleep(2)
                if not started: raise RuntimeError("Supervisor meldet EnergyKit nicht als started")

                DONE.write_text("installed-by-supervisor\n")
                _write_status(ok=True,stage="installed-and-started",attempt=attempt,installed_version=started.get("version"),supervisor_state=started.get("state"),repaired_stale_state=repaired)
                return
            except Exception as exc:
                last=str(exc); _write_status(ok=False,stage="retry",attempt=attempt,error=last)
                await asyncio.sleep(min(5+attempt,20))
        _write_status(ok=False,stage="failed",error=last)

async def async_setup(hass:HomeAssistant,config:dict)->bool:
    hass.async_create_task(_bootstrap(hass),"energykit_first_boot")
    return True
