from __future__ import annotations

import asyncio
import html
import json
import os
import re
import secrets
import shutil
import socket
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import websockets
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

APP_VERSION = "0.6.0"
SUPERVISOR = "http://supervisor"
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
DATA = Path("/config")
HA = Path("/homeassistant")
ADDON_CONFIGS = Path("/addon_configs")
STATE_FILE = DATA / "state.json"
MAPPING_FILE = HA / "energykit_mapping.json"
REPORT_FILE = DATA / "last_report.json"

app = FastAPI(title="EnergyKit", version=APP_VERSION)

DEFAULT_STATE: dict[str, Any] = {
    "setup_complete": False,
    "simulation": True,
    "service_user_id": None,
    "service_username": "energykit-service",
    "service_password": None,
    "customer": {"name": "", "installation_id": "", "location": "", "installer": ""},
    "components": {},
    "energy": {"vendor": None, "host": None, "port": 502, "modbus_id": 1, "deye_profile": "deye-hybrid-3p", "deye_batterytype": "lv", "deye_firmware1098": False, "configured": False},
    "mapping": {},
    "wallbox": {"vendor": "none", "model": "", "host": "", "port": 502, "entity": "", "device_id": "", "modbus_id": 1, "evcc_template": "", "control_path": "native", "max_current": 16, "phases": 3},
    "heatpump": {"mode": "none", "vendor": "", "model": "", "host": "", "port": 502, "entity": "", "switch": "", "switch_b": "", "device_id": "", "modbus_id": 1, "evcc_template": "", "control_path": "native", "min_power": 1200, "max_power": 6000},
    "evcc": {"installed": False, "slug": None, "configured": False, "config_path": None},
    "dashboard": {"installed": False},
    "last_backup": None,
    "last_checks": [],
    "restart_required": False,
}


def merge_defaults(value: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    for k, default in defaults.items():
        if k not in value:
            value[k] = json.loads(json.dumps(default))
        elif isinstance(default, dict) and isinstance(value.get(k), dict):
            merge_defaults(value[k], default)
    return value


def load_state() -> dict[str, Any]:
    DATA.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        save_state(json.loads(json.dumps(DEFAULT_STATE)))
    try:
        value = json.loads(STATE_FILE.read_text())
    except Exception:
        value = json.loads(json.dumps(DEFAULT_STATE))
    return merge_defaults(value, DEFAULT_STATE)


def save_state(state: dict[str, Any]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    os.chmod(tmp, 0o600)
    tmp.replace(STATE_FILE)


def require_supervisor_token() -> str:
    if not TOKEN:
        raise HTTPException(
            503,
            "EnergyKit wurde ohne Supervisor-Token gestartet. "
            "Der Supervisor-App-Lifecycle wurde nicht vollständig ausgeführt. "
            "Bitte EnergyKit bzw. die Appliance mit dem aktuellen Build neu starten."
        )
    return TOKEN


def sup_headers() -> dict[str, str]:
    token = require_supervisor_token()
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def sup(method: str, path: str, **kwargs):
    async with httpx.AsyncClient(timeout=90) as client:
        res = await client.request(method, SUPERVISOR + path, headers=sup_headers(), **kwargs)
        if res.status_code >= 400:
            raise HTTPException(res.status_code, f"Supervisor {path}: {res.text[:400]}")
        if not res.content:
            return {}
        data = res.json()
        return data.get("data", data)


async def core_rest(method: str, path: str, **kwargs):
    return await sup(method, "/core/api" + path, **kwargs)


async def core_ws(commands: list[dict[str, Any]]) -> list[Any]:
    token = require_supervisor_token()
    uri = "ws://supervisor/core/websocket"
    out: list[Any] = []
    async with websockets.connect(uri, open_timeout=15) as ws:
        hello = json.loads(await ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"Home Assistant WebSocket nicht bereit: {hello}")
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth = json.loads(await ws.recv())
        if auth.get("type") != "auth_ok":
            raise RuntimeError(
                f"Home Assistant WebSocket Auth fehlgeschlagen: "
                f"{auth.get('message') or auth.get('type')}"
            )
        for idx, cmd in enumerate(commands, 1):
            await ws.send(json.dumps({"id": idx, **cmd}))
            while True:
                result = json.loads(await ws.recv())
                if result.get("id") == idx:
                    break
            if not result.get("success"):
                raise RuntimeError(f"WS {cmd.get('type')}: {result.get('error')}")
            out.append(result.get("result"))
    return out


def ingress_user(request: Request) -> str | None:
    return request.headers.get("X-Remote-User-Id")


def ingress_admin(request: Request) -> bool:
    return request.headers.get("X-Remote-User-Is-Admin", "false").lower() == "true"


def ensure_access(request: Request) -> dict[str, Any]:
    state = load_state()
    uid = ingress_user(request)
    if not uid:
        raise HTTPException(403, "EnergyKit ist nur über Home Assistant Ingress erreichbar")
    if state["setup_complete"]:
        if uid != state.get("service_user_id"):
            raise HTTPException(403, "EnergyKit ist nach der Übergabe ausschließlich für den Service-Benutzer freigegeben")
    # The EnergyKit panel is configured with panel_admin: true. During setup,
    # any authenticated ingress request reaching the app is therefore an HA admin.
    return state


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def download(url: str, target: Path):
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        async with client.stream("GET", url, headers={"User-Agent": "EnergyKit"}) as res:
            res.raise_for_status()
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as fp:
                async for chunk in res.aiter_bytes():
                    fp.write(chunk)


async def github_latest(repo: str):
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        res = await client.get(
            f"https://api.github.com/repos/{repo}/releases/latest",
            headers={"User-Agent": "EnergyKit", "Accept": "application/vnd.github+json"},
        )
        res.raise_for_status()
        return res.json()


def safe_extract(z: zipfile.ZipFile, target: Path):
    base = target.resolve()
    for item in z.infolist():
        dest = (target / item.filename).resolve()
        if not str(dest).startswith(str(base)):
            raise RuntimeError("Unsicheres ZIP")
    z.extractall(target)


def copy_bridge() -> str:
    source = Path("/app/bundled/energykit_bridge")
    target = HA / "custom_components/energykit_bridge"
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    return f"bundled-{APP_VERSION}"


async def install_component_impl(name: str, state: dict[str, Any]):
    if name == "bridge":
        version = copy_bridge()
        state["components"][name] = {"version": version, "installed_at": now_iso()}
        state["restart_required"] = True
        save_state(state)
        return {"version": version, "restart_required": True}

    with tempfile.TemporaryDirectory() as td0:
        td = Path(td0)
        zpath, ex = td / "pkg.zip", td / "ex"
        ex.mkdir()
        if name == "mushroom":
            rel = await github_latest("piitaya/lovelace-mushroom")
            asset = next((a for a in rel.get("assets", []) if a["name"] == "mushroom.js"), None)
            if not asset:
                raise HTTPException(500, "mushroom.js im Release nicht gefunden")
            target = HA / "www/mushroom.js"
            await download(asset["browser_download_url"], target)
            version = rel["tag_name"]
        elif name == "sigenergy":
            rel = await github_latest("TypQxQ/Sigenergy-Local-Modbus")
            await download(rel["zipball_url"], zpath)
            with zipfile.ZipFile(zpath) as z:
                safe_extract(z, ex)
            src = next(ex.rglob("custom_components/sigen"), None)
            if not src:
                raise HTTPException(500, "custom_components/sigen nicht gefunden")
            target = HA / "custom_components/sigen"
            if target.exists():
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, target)
            version = rel["tag_name"]
        elif name == "deye":
            await download("https://github.com/Developer089/deye-modbus-ha/archive/refs/heads/main.zip", zpath)
            with zipfile.ZipFile(zpath) as z:
                safe_extract(z, ex)
            src = next(ex.rglob("custom_components/deye_modbus"), None)
            if not src:
                raise HTTPException(500, "custom_components/deye_modbus nicht gefunden")
            target = HA / "custom_components/deye_modbus"
            if target.exists():
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, target)
            version = "main"
        elif name == "visionos":
            rel = await github_latest("Nezz/homeassistant-visionos-theme")
            await download(rel["zipball_url"], zpath)
            with zipfile.ZipFile(zpath) as z:
                safe_extract(z, ex)
            target = HA / "themes/energykit-visionos"
            if target.exists():
                shutil.rmtree(target)
            target.mkdir(parents=True, exist_ok=True)
            copied = 0
            for f in ex.rglob("*.yaml"):
                if "themes" in [p.lower() for p in f.parts] or "vision" in f.name.lower() or "liquid" in f.name.lower():
                    shutil.copy2(f, target / f.name)
                    copied += 1
            if not copied:
                raise HTTPException(500, "Keine VisionOS-Theme-YAML im aktuellen Release gefunden")
            version = rel["tag_name"]
        else:
            raise HTTPException(404, "Unbekannte Komponente")
    state["components"][name] = {"version": version, "installed_at": now_iso()}
    if name in ("sigenergy", "deye", "bridge"):
        state["restart_required"] = True
    save_state(state)
    return {"version": version, "restart_required": bool(state.get("restart_required"))}


async def all_states() -> list[dict[str, Any]]:
    if load_state()["simulation"]:
        return [
            {"entity_id": "sensor.sigen_pv_power", "state": "6240", "attributes": {"friendly_name": "Sigenergy PV Power", "unit_of_measurement": "W", "device_class": "power"}},
            {"entity_id": "sensor.sigen_load_power", "state": "2130", "attributes": {"friendly_name": "Sigenergy Load Power", "unit_of_measurement": "W", "device_class": "power"}},
            {"entity_id": "sensor.sigen_grid_power", "state": "-1080", "attributes": {"friendly_name": "Sigenergy Grid Power", "unit_of_measurement": "W", "device_class": "power"}},
            {"entity_id": "sensor.sigen_battery_power", "state": "-3030", "attributes": {"friendly_name": "Sigenergy Battery Power", "unit_of_measurement": "W", "device_class": "power"}},
            {"entity_id": "sensor.sigen_battery_soc", "state": "74", "attributes": {"friendly_name": "Sigenergy Battery State of Charge", "unit_of_measurement": "%", "device_class": "battery"}},
            {"entity_id": "switch.sg_ready", "state": "off", "attributes": {"friendly_name": "SG Ready"}},
            {"entity_id": "switch.wallbox_enable", "state": "on", "attributes": {"friendly_name": "Wallbox Enable"}},
        ]
    return await core_rest("GET", "/states")


def score_entity(e: dict[str, Any], key: str, vendor: str | None) -> int:
    eid = e.get("entity_id", "").lower()
    a = e.get("attributes", {})
    text = f"{eid} {a.get('friendly_name','')}".lower()
    score = 0
    if vendor and vendor.lower() in text:
        score += 6
    rules = {
        "pv": ["pv", "solar", "photovolta"],
        "house": ["load", "house", "home", "verbrauch"],
        "grid": ["grid", "meter", "netz"],
        "battery_power": ["battery power", "battery_power", "ess power", "akku leistung", "storage power"],
        "battery_soc": ["soc", "state of charge", "battery level", "battery_soc"],
    }
    for token in rules[key]:
        if token in text:
            score += 5
    unit = str(a.get("unit_of_measurement", ""))
    if key == "battery_soc" and unit == "%":
        score += 4
    if key != "battery_soc" and unit.lower() in ("w", "kw"):
        score += 2
    if key == "battery_soc" and a.get("device_class") == "battery":
        score += 3
    return score


def auto_mapping(states: list[dict[str, Any]], vendor: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    used: set[str] = set()
    for key in ("pv", "house", "grid", "battery_power", "battery_soc"):
        ranked = sorted(((score_entity(e, key, vendor), e) for e in states), key=lambda x: x[0], reverse=True)
        ranked = [(s, e) for s, e in ranked if s > 0 and e.get("entity_id") not in used]
        if ranked:
            best, ent = ranked[0]
            second = ranked[1][0] if len(ranked) > 1 else 0
            out[key] = {"entity_id": ent["entity_id"], "score": best, "confident": best >= 8 and best - second >= 2}
            used.add(ent["entity_id"])
        else:
            out[key] = {"entity_id": "", "score": 0, "confident": False}
    return out




def write_bridge_mapping(state: dict[str, Any]) -> None:
    MAPPING_FILE.write_text(json.dumps({
        "simulation": state["simulation"],
        "sources": state.get("mapping", {}),
        "heatpump": state.get("heatpump", {}),
    }, indent=2, ensure_ascii=False))

async def ensure_bridge_entry():
    try:
        result = await core_rest("POST", "/config/config_entries/flow", json={"handler": "energykit_bridge", "show_advanced_options": False})
        if result.get("flow_id") and result.get("type") == "form":
            await core_rest("POST", f"/config/config_entries/flow/{result['flow_id']}", json={})
    except Exception:
        pass


async def scan_live() -> list[dict[str, Any]]:
    # Outbound LAN connections work from the app container. We cap discovery to the local /24.
    local_ip = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 53))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    if not local_ip or not re.match(r"^\d+\.\d+\.\d+\.\d+$", local_ip):
        return []
    prefix = ".".join(local_ip.split(".")[:3])
    sem = asyncio.Semaphore(70)
    results: list[dict[str, Any]] = []

    async def probe(host: str):
        ports: list[int] = []
        async with sem:
            for port in (502, 8899, 80, 443, 7070, 8123):
                try:
                    _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=.16)
                    ports.append(port)
                    w.close()
                    try:
                        await w.wait_closed()
                    except Exception:
                        pass
                except Exception:
                    pass
        if ports:
            if 502 in ports:
                vendor, model = "Modbus TCP", "Energiegerät"
            elif 8899 in ports:
                vendor, model = "Deye/SolarMAN", "Logger"
            elif 7070 in ports:
                vendor, model = "evcc", "Service"
            elif 8123 in ports:
                vendor, model = "Home Assistant", "Service"
            else:
                vendor, model = "Netzwerkgerät", "HTTP(S)"
            results.append({"vendor": vendor, "model": model, "host": host, "ports": ports, "port": 502 if 502 in ports else ports[0]})

    await asyncio.gather(*(probe(f"{prefix}.{i}") for i in range(1, 255) if f"{prefix}.{i}" != local_ip))
    return sorted(results, key=lambda d: tuple(map(int, d["host"].split("."))))


def _consumer_match(kind: str, text: str) -> int:
    s = text.lower()
    if kind == "wallbox":
        keys = {
            "sigenergy": 9, "sigen": 9, "ac charger": 10, "ev charger": 9, "wallbox": 9,
            "charger": 6, "go-e": 9, "go e": 8, "easee": 9, "zaptec": 9, "alfen": 9,
            "openwb": 9, "wall connector": 8, "charging": 4, "laden": 4,
        }
    else:
        keys = {
            "wärmepumpe": 10, "waermepumpe": 10, "heat pump": 10, "heatpump": 10,
            "vaillant": 8, "arotherm": 9, "viessmann": 8, "stiebel": 8, "nibe": 8,
            "wolf": 7, "panasonic": 7, "daikin": 7, "buderus": 7, "bosch": 6,
            "therma": 7, "ecodan": 8, "alpha innotec": 8, "samsung ehs": 8, "sg ready": 7,
        }
    return max((score for key, score in keys.items() if key in s), default=0)


async def discover_ha_consumers(kind: str) -> list[dict[str, Any]]:
    """Find wallboxes/heat pumps already known by Home Assistant.

    Device and entity registries give us manufacturer/model context, while the
    current states tell us which control entities are actually available.
    """
    if kind not in {"wallbox", "heatpump"}:
        raise HTTPException(400, "Unbekannter Gerätetyp")
    try:
        devices, entities = await core_ws([
            {"type": "config/device_registry/list"},
            {"type": "config/entity_registry/list"},
        ])
        states = await all_states()
    except Exception:
        return []

    devmap = {d.get("id"): d for d in (devices or [])}
    statemap = {s.get("entity_id"): s for s in states}
    grouped: dict[str, dict[str, Any]] = {}

    for ent in entities or []:
        eid = ent.get("entity_id") or ""
        dev = devmap.get(ent.get("device_id")) or {}
        st = statemap.get(eid) or {}
        attrs = st.get("attributes") or {}
        text = " ".join(str(x or "") for x in [
            eid, ent.get("original_name"), ent.get("name"), attrs.get("friendly_name"),
            dev.get("name"), dev.get("name_by_user"), dev.get("manufacturer"), dev.get("model"),
        ])
        score = _consumer_match(kind, text)
        if not score:
            continue
        did = ent.get("device_id") or eid
        item = grouped.setdefault(did, {
            "source": "homeassistant", "kind": kind, "device_id": ent.get("device_id") or "",
            "vendor": dev.get("manufacturer") or ("Sigenergy" if "sigen" in text.lower() else "Home Assistant"),
            "model": dev.get("model") or dev.get("name_by_user") or dev.get("name") or attrs.get("friendly_name") or eid,
            "host": "", "entities": [], "score": 0,
        })
        item["score"] = max(item["score"], score)
        item["entities"].append(eid)

    out=[]
    for item in grouped.values():
        candidates=item["entities"]
        preferred=[]
        if kind == "wallbox":
            for eid in candidates:
                low=eid.lower()
                domain=eid.split('.',1)[0]
                p=(8 if domain in {"switch","button","number","select"} else 0) + _consumer_match(kind, low)
                if any(k in low for k in ("start_charging","stop_charging","charging","charger","wallbox")): p += 5
                preferred.append((p,eid))
        else:
            for eid in candidates:
                low=eid.lower(); domain=eid.split('.',1)[0]
                p=(8 if domain in {"switch","climate","number","select"} else 0) + _consumer_match(kind, low)
                if any(k in low for k in ("sg_ready","sgready","heat","waerme","warm")): p += 5
                preferred.append((p,eid))
        preferred.sort(reverse=True)
        item["entity"] = preferred[0][1] if preferred else ""
        item["entities"] = [x[1] for x in preferred[:12]]
        # Sigenergy AC charger deserves a first-class label.
        blob=(str(item.get("vendor"))+" "+str(item.get("model"))+" "+" ".join(item["entities"])).lower()
        if kind == "wallbox" and ("sigen" in blob or "sigenergy" in blob) and ("charger" in blob or "charging" in blob):
            item["vendor"]="Sigenergy"
            item["model"] = item.get("model") or "Sigen AC Charger"
            item["sigenergy_ac_charger"] = True
        out.append(item)
    return sorted(out, key=lambda x: (-int(x.get("score",0)), str(x.get("model",""))))


async def create_backup(name: str):
    data = await sup("POST", "/backups/new/partial", json={
        "name": name,
        "homeassistant": True,
        "addons": ["local_energykit"],
        "compressed": True,
        "background": False,
    })
    st = load_state()
    st["last_backup"] = data.get("slug") or data.get("backup_slug") or data.get("job_id")
    save_state(st)
    return data


async def find_evcc_slug() -> str | None:
    addons = await sup("GET", "/addons")
    items = addons.get("addons", addons if isinstance(addons, list) else [])
    for a in items:
        if str(a.get("name", "")).strip().lower() == "evcc" and "nightly" not in str(a.get("name", "")).lower():
            return a.get("slug")
    return None


def _evcc_native_wallbox(wall: dict[str, Any]) -> list[str]:
    """Build an evcc charger block. Native device drivers always win over HA entities."""
    vendor = str(wall.get("vendor") or "").lower()
    template = str(wall.get("evcc_template") or "").strip()
    if not template:
        template = {
            "sigenergy": "sigenergy",
            "sigenergy-evdc": "sigenergy-evdc",
            "go-e": "go-e-v3",
            "openwb": "openwb",
        }.get(vendor, "")
    if template and wall.get("host"):
        lines = ["  - name: wallbox", "    type: template", f"    template: {template}"]
        if template in {"sigenergy", "sigenergy-evdc"}:
            lines += ["    modbus: tcpip", f"    id: {int(wall.get('modbus_id') or 1)}"]
        lines += [f"    host: {wall['host']}"]
        if template in {"sigenergy", "sigenergy-evdc"} or int(wall.get("port") or 0) not in (0, 80):
            lines += [f"    port: {int(wall.get('port') or 502)}"]
        return lines
    # Last-resort compatibility path only for unsupported hardware.
    if wall.get("control_path") == "homeassistant" and wall.get("entity"):
        return [
            "  - name: wallbox", "    type: template", "    template: homeassistant-switch",
            "    uri: http://homeassistant.local:8123", f"    switch: {wall['entity']}",
        ]
    return []


def _evcc_heatpump(hp: dict[str, Any]) -> list[str]:
    """Build an evcc heating charger. EnergyKit never performs runtime SG-Ready control."""
    mode = str(hp.get("mode") or "none")
    template = str(hp.get("evcc_template") or "").strip()
    if mode == "none":
        return []
    if template and hp.get("host"):
        lines = ["  - name: heatpump", "    type: template", f"    template: {template}"]
        # Most native heat-pump templates in evcc use Modbus TCP.
        lines += ["    modbus: tcpip", f"    id: {int(hp.get('modbus_id') or 1)}", f"    host: {hp['host']}", f"    port: {int(hp.get('port') or 502)}"]
        return lines
    # Generic SG Ready relay support. This is intentionally an evcc device,
    # not an EnergyKit/Home Assistant automation.
    if mode == "sg-ready" and hp.get("switch"):
        # evcc owns the SG-Ready state machine. Home Assistant is only used as
        # a relay transport when the physical dry contact is exposed there.
        lines = [
            "  - name: heatpump", "    type: sgready-relay",
            "    boost:", "      type: template", "      template: homeassistant-switch",
            "      uri: http://homeassistant.local:8123", f"      switch: {hp['switch']}",
        ]
        if hp.get("switch_b"):
            lines += [
                "    dim:", "      type: template", "      template: homeassistant-switch",
                "      uri: http://homeassistant.local:8123", f"      switch: {hp['switch_b']}",
            ]
        return lines
    # HA is a compatibility fallback when evcc has no native driver for the unit.
    if hp.get("control_path") == "homeassistant" and hp.get("switch"):
        return [
            "  - name: heatpump", "    type: template", "    template: homeassistant-switch",
            "    uri: http://homeassistant.local:8123", f"    switch: {hp['switch']}",
            "    integrateddevice: true", "    heating: true",
        ]
    return []



def _evcc_energy_meters(energy: dict[str, Any]) -> list[str]:
    """Build the preferred meter path for evcc.

    Supported plants are read directly by evcc. HA normalized sensors are only
    a compatibility fallback for hardware without a native/direct evcc driver.
    """
    vendor = str(energy.get("vendor") or "").strip().lower()
    host = str(energy.get("host") or "").strip()
    port = int(energy.get("port") or 502)
    modbus_id = int(energy.get("modbus_id") or 1)

    if vendor == "sigenergy" and host:
        lines = []
        for name, usage in (("grid", "grid"), ("pv", "pv"), ("battery", "battery")):
            lines += [
                f"  - name: {name}",
                "    type: template",
                "    template: sigenergy",
                f"    usage: {usage}",
                f"    host: {host}",
                f"    port: {port}",
                f"    id: {modbus_id}",
            ]
        return lines

    if vendor == "deye" and host:
        profile = str(energy.get("deye_profile") or "deye-hybrid-3p")
        if profile not in {"deye-hybrid-3p", "deye-storage", "deye-mi"}:
            profile = "deye-hybrid-3p"

        # Micro inverters expose PV production only. Grid/battery therefore
        # remain on EnergyKit's HA fallback unless separate meters are added.
        if profile == "deye-mi":
            return [
                "  - name: grid",
                "    type: template",
                "    template: homeassistant",
                "    usage: grid",
                "    uri: http://homeassistant.local:8123",
                "    power: sensor.ek_grid_power",
                "  - name: pv",
                "    type: template",
                "    template: deye-mi",
                "    usage: pv",
                "    modbus: tcpip",
                f"    id: {modbus_id}",
                f"    host: {host}",
                f"    port: {port}",
                "  - name: battery",
                "    type: template",
                "    template: homeassistant",
                "    usage: battery",
                "    uri: http://homeassistant.local:8123",
                "    power: sensor.ek_battery_power",
                "    soc: sensor.ek_battery_soc",
            ]

        lines = []
        for name, usage in (("grid", "grid"), ("pv", "pv"), ("battery", "battery")):
            lines += [
                f"  - name: {name}",
                "    type: template",
                f"    template: {profile}",
                f"    usage: {usage}",
                "    modbus: tcpip",
                f"    id: {modbus_id}",
                f"    host: {host}",
                f"    port: {port}",
            ]
            if profile == "deye-hybrid-3p":
                lines += [f"    batterytype: {str(energy.get('deye_batterytype') or 'lv')}"]
                if bool(energy.get("deye_firmware1098")):
                    lines += ["    firmware1098: true"]
        return lines

    return [
        "  - name: grid",
        "    type: template",
        "    template: homeassistant",
        "    usage: grid",
        "    uri: http://homeassistant.local:8123",
        "    power: sensor.ek_grid_power",
        "  - name: pv",
        "    type: template",
        "    template: homeassistant",
        "    usage: pv",
        "    uri: http://homeassistant.local:8123",
        "    power: sensor.ek_pv_power",
        "  - name: battery",
        "    type: template",
        "    template: homeassistant",
        "    usage: battery",
        "    uri: http://homeassistant.local:8123",
        "    power: sensor.ek_battery_power",
        "    soc: sensor.ek_battery_soc",
    ]


def evcc_yaml(state: dict[str, Any]) -> str:
    wall = state["wallbox"]
    hp = state["heatpump"]
    lines = [
        "network:", "  schema: http", "  host: 0.0.0.0", "  port: 7070", "interval: 30s",
        "meters:",
    ]
    lines += _evcc_energy_meters(state["energy"])
    lines += [
        "site:", "  title: EnergyKit", "  meters:", "    grid: grid", "    pv: [pv]", "    battery: [battery]",
    ]
    chargers = _evcc_native_wallbox(wall) + _evcc_heatpump(hp)
    if chargers:
        lines += ["chargers:"] + chargers
    if _evcc_native_wallbox(wall):
        lines += [
            "loadpoints:", "  - title: Wallbox", "    charger: wallbox", "    mode: pv",
            f"    phases: {int(wall.get('phases') or 3)}", "    mincurrent: 6", f"    maxcurrent: {int(wall.get('max_current') or 16)}",
        ]
    if _evcc_heatpump(hp):
        if "loadpoints:" not in lines:
            lines += ["loadpoints:"]
        lines += [
            "  - title: Wärmepumpe", "    charger: heatpump", "    mode: pv",
            "    phases: 1", f"    mincurrent: {max(1, int((hp.get('min_power') or 1200) / 230))}",
            f"    maxcurrent: {max(1, int((hp.get('max_power') or 6000) / 230))}",
        ]
    return "\n".join(lines) + "\n"


def report_payload(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "energykit_version": APP_VERSION,
        "created_at": now_iso(),
        "customer": state["customer"],
        "mode": "simulation" if state["simulation"] else "live",
        "energy": state["energy"],
        "mapping": state["mapping"],
        "wallbox": state["wallbox"],
        "heatpump": state["heatpump"],
        "evcc": state["evcc"],
        "components": state["components"],
        "checks": state["last_checks"],
    }


async def final_checks(state: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    def add(name: str, ok: bool, detail: str = ""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    try:
        info = await sup("GET", "/info")
        add("Supervisor erreichbar", True, str(info.get("supervisor") or "OK"))
    except Exception as exc:
        add("Supervisor erreichbar", False, str(exc))
    try:
        cfg = await core_rest("GET", "/config")
        add("Home Assistant erreichbar", True, str(cfg.get("version") or "OK"))
    except Exception as exc:
        add("Home Assistant erreichbar", False, str(exc))
    add("Service-Benutzer vorhanden", bool(state.get("service_user_id")), str(state.get("service_username")))
    add("Anlagendaten vollständig", bool(state["customer"].get("name") and state["customer"].get("installation_id")))
    add("Energiesystem gewählt", bool(state["energy"].get("vendor")))
    add("EnergyKit Bridge installiert", "bridge" in state["components"])
    needed = {"pv", "house", "grid", "battery_power", "battery_soc"}
    add("Messwert-Mapping vollständig", needed.issubset(set(k for k, v in state["mapping"].items() if v)))
    add("Dashboard erzeugt", bool(state["dashboard"].get("installed")))
    add("evcc installiert", bool(state["evcc"].get("installed")))
    add("evcc konfiguriert", bool(state["evcc"].get("configured")))
    add("Wärmepumpe konfiguriert", state["heatpump"].get("mode") in {"none", "native", "sg-ready", "integration"})
    add("Wallbox konfiguriert", state["wallbox"].get("vendor") == "none" or bool(state["wallbox"].get("entity") or state["wallbox"].get("host")))
    if state["simulation"]:
        add("Simulationsdaten plausibel", True, "PV 6.24 kW · SoC 74 %")
    else:
        try:
            states = await core_rest("GET", "/states")
            byid = {s["entity_id"]: s for s in states}
            good = 0
            for eid in ("sensor.ek_pv_power", "sensor.ek_house_power", "sensor.ek_grid_power", "sensor.ek_battery_power", "sensor.ek_battery_soc"):
                s = byid.get(eid)
                if s and s.get("state") not in {"unknown", "unavailable", None, ""}:
                    good += 1
            add("Live-EnergyKit-Sensoren", good == 5, f"{good}/5 verfügbar")
        except Exception as exc:
            add("Live-EnergyKit-Sensoren", False, str(exc))
    return checks


CSS = """
:root{
  --bg:#f7f8fa;--surface:#fff;--surface2:#f4f5f7;--text:#111827;--muted:#697386;
  --line:#e5e7eb;--blue:#006fff;--blue-hover:#0062e6;--blue-soft:#eaf3ff;
  --green:#0b8f55;--green-soft:#e9f8f0;--amber:#a86500;--amber-soft:#fff5df;
  --red:#c0392b;--red-soft:#fff0ee;--shadow:0 12px 36px rgba(17,24,39,.06)
}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%;background:var(--bg);color:var(--text);font:14px Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
button,input,select{font:inherit}
.shell{min-height:100vh;display:grid;grid-template-columns:244px 1fr}
.sidebar{position:sticky;top:0;height:100vh;background:#fff;border-right:1px solid var(--line);padding:22px 16px;display:flex;flex-direction:column}
.brand{height:44px;display:flex;align-items:center;gap:11px;padding:0 8px;font-size:15px;font-weight:700;letter-spacing:-.01em}
.mark{width:30px;height:30px;border-radius:8px;background:var(--blue);color:#fff;display:grid;place-items:center;font-weight:800;box-shadow:0 5px 14px rgba(0,111,255,.22)}
.steps{margin-top:24px;display:flex;flex-direction:column;gap:3px}
.stepnav{appearance:none;border:0;background:transparent;text-align:left;padding:10px 10px;border-radius:8px;color:#737d8c;cursor:pointer;display:flex;align-items:center;gap:10px}
.stepnav:hover{background:#f6f7f9}.stepnav.active{background:var(--blue-soft);color:#075fca;font-weight:650}
.stepnum{width:21px;height:21px;border:1px solid #d7dce2;border-radius:50%;display:grid;place-items:center;font-size:11px;background:#fff}
.stepnav.active .stepnum{background:var(--blue);border-color:var(--blue);color:#fff}
.sidefoot{margin-top:auto;padding:12px 9px;color:#8a93a0;font-size:12px}
.main{min-width:0}.topbar{height:62px;background:rgba(255,255,255,.92);backdrop-filter:blur(14px);border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 30px;position:sticky;top:0;z-index:20}
.topmeta{display:flex;gap:8px;align-items:center}.pill{font-size:12px;padding:5px 9px;border-radius:999px;background:#f1f3f5;color:#687381}
.content{max-width:920px;margin:0 auto;padding:48px 34px 110px}
.eyebrow{color:#7d8794;font-size:12px;font-weight:650;margin-bottom:10px}.title{font-size:30px;line-height:1.15;letter-spacing:-.035em;margin:0}.subtitle{font-size:15px;color:var(--muted);line-height:1.55;margin:10px 0 28px;max-width:700px}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);padding:22px;margin:14px 0}
.panel.flat{box-shadow:none}.panelhead{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;margin-bottom:16px}
.panel h3{font-size:15px;margin:0 0 5px}.panel p{margin:0;color:var(--muted);line-height:1.5}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}.span2{grid-column:1/-1}
label{display:block;color:#4d5968;font-size:12px;font-weight:650}
input,select{width:100%;margin-top:7px;height:40px;border:1px solid #d8dde4;border-radius:8px;background:#fff;padding:0 11px;color:#18212b;outline:none}
input:focus,select:focus{border-color:#80b8ff;box-shadow:0 0 0 3px rgba(0,111,255,.1)}
.actions{display:flex;gap:9px;align-items:center;justify-content:flex-end;margin-top:20px;flex-wrap:wrap}.actions.left{justify-content:flex-start}
.btn{height:38px;border:1px solid transparent;border-radius:8px;padding:0 14px;background:var(--blue);color:#fff;font-weight:650;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;gap:8px;text-decoration:none;transition:.15s}
.btn:hover{background:var(--blue-hover)}.btn.secondary{background:#fff;border-color:#dce1e7;color:#354151}.btn.secondary:hover{background:#f7f8fa}
.btn.danger{background:#fff;border-color:#f0c6c1;color:var(--red)}.btn:disabled{opacity:.55;cursor:not-allowed}
.btn.loading:before{content:"";width:13px;height:13px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.row{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:13px 0;border-top:1px solid #edf0f2}.row:first-child{border-top:0}.row small{display:block;color:#8a93a0;margin-top:3px}
.status{font-size:11px;font-weight:700;padding:5px 8px;border-radius:999px;background:#f1f3f5;color:#6f7986}.status.ok{background:var(--green-soft);color:var(--green)}.status.bad{background:var(--red-soft);color:var(--red)}.status.warn{background:var(--amber-soft);color:var(--amber)}
.callout{border-radius:9px;padding:13px 14px;background:#f3f7fc;color:#526174;border:1px solid #e0eaf5;line-height:1.45;margin:14px 0}.callout.warn{background:var(--amber-soft);border-color:#f3dfb4;color:#76501a}
.devicegrid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.device{border:1px solid var(--line);border-radius:10px;padding:16px;background:#fff}.device h3{margin:9px 0 4px}.device small{color:#7b8592}
.progress{height:6px;background:#edf0f3;border-radius:99px;overflow:hidden}.progress i{display:block;height:100%;background:var(--blue);transition:width .3s}
.step{display:none}.step.active{display:block}.hidden{display:none!important}
.footerbar{position:fixed;bottom:0;left:244px;right:0;height:72px;background:rgba(255,255,255,.94);backdrop-filter:blur(14px);border-top:1px solid var(--line);z-index:15}
.footerinner{height:100%;max-width:920px;margin:auto;padding:0 34px;display:flex;align-items:center;justify-content:space-between}
.toaststack{position:fixed;right:22px;top:78px;z-index:100;display:flex;flex-direction:column;gap:9px;width:min(360px,calc(100vw - 44px))}
.toast{background:#fff;border:1px solid var(--line);box-shadow:0 14px 40px rgba(0,0,0,.12);border-radius:10px;padding:13px 14px;display:flex;gap:10px;align-items:flex-start;animation:toastin .18s ease-out}.toast.ok{border-left:3px solid var(--green)}.toast.bad{border-left:3px solid var(--red)}
@keyframes toastin{from{opacity:0;transform:translateY(-6px)}}
.modalback{position:fixed;inset:0;background:rgba(15,23,42,.34);backdrop-filter:blur(3px);display:none;place-items:center;z-index:200;padding:20px}.modalback.open{display:grid}
.modal{width:min(470px,100%);background:#fff;border-radius:13px;box-shadow:0 28px 80px rgba(0,0,0,.24);padding:23px}.modal h3{margin:0 0 8px;font-size:17px}.modal p{color:var(--muted);line-height:1.5}
.codebox{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;background:#f6f7f9;border:1px solid var(--line);border-radius:8px;padding:12px;white-space:pre-wrap;word-break:break-word}
.flowfield{margin:12px 0}.checklist .row span:first-child{display:flex;flex-direction:column;gap:3px}
.deny{max-width:480px;margin:100px auto;background:#fff;border:1px solid var(--line);border-radius:14px;padding:30px;text-align:center}
@media(max-width:800px){.shell{grid-template-columns:1fr}.sidebar{display:none}.topbar{padding:0 16px}.content{padding:30px 16px 105px}.footerbar{left:0}.footerinner{padding:0 16px}.grid2,.devicegrid{grid-template-columns:1fr}.span2{grid-column:auto}}
"""

JS = r"""
let currentStep=Number(localStorage.getItem('ek_step')||0);
const STEPS=9;
function byId(id){return document.getElementById(id)}
function fd(data){const f=new FormData();Object.entries(data).forEach(([k,v])=>f.append(k,v));return f}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function api(url,opt={}){const r=await fetch(url,opt);let d={};try{d=await r.json()}catch(e){d={detail:await r.text().catch(()=> '')}}if(!r.ok)throw new Error(d.detail||d.message||`HTTP ${r.status}`);return d}
function toast(msg,type='ok',ms=4200){const el=document.createElement('div');el.className=`toast ${type}`;el.innerHTML=`<div>${type==='ok'?'✓':'!'}</div><div>${esc(msg)}</div>`;byId('toasts').appendChild(el);setTimeout(()=>el.remove(),ms)}
function setBusy(btn,busy,label){if(!btn)return;if(busy){btn.dataset.old=btn.textContent;btn.classList.add('loading');btn.disabled=true;if(label)btn.textContent=label}else{btn.classList.remove('loading');btn.disabled=false;btn.textContent=btn.dataset.old||btn.textContent}}
async function action(btn,fn,success){setBusy(btn,true);try{const d=await fn();if(success)toast(typeof success==='function'?success(d):success);return d}catch(e){toast(e.message,'bad',7000);throw e}finally{setBusy(btn,false)}}
function goStep(n){currentStep=Math.max(0,Math.min(STEPS-1,n));localStorage.setItem('ek_step',currentStep);document.querySelectorAll('.step').forEach((e,i)=>e.classList.toggle('active',i===currentStep));document.querySelectorAll('.stepnav').forEach((e,i)=>e.classList.toggle('active',i===currentStep));byId('backBtn').style.visibility=currentStep?'visible':'hidden';byId('nextBtn').textContent=currentStep===STEPS-1?'Fertig':'Weiter';byId('stepCounter').textContent=`Schritt ${currentStep+1} von ${STEPS}`;window.scrollTo({top:0,behavior:'smooth'})}
function nextStep(){if(currentStep===STEPS-1){runChecks(byId('nextBtn'));return}goStep(currentStep+1)}
function modal(title,body,ok='Bestätigen'){return new Promise(resolve=>{byId('modalTitle').textContent=title;byId('modalBody').innerHTML=body;byId('modalOk').textContent=ok;byId('modalBack').classList.add('open');window._modalResolve=resolve})}
function closeModal(v){byId('modalBack').classList.remove('open');if(window._modalResolve){window._modalResolve(v);window._modalResolve=null}}
function downloadText(name,text){const b=new Blob([text],{type:'text/plain'}),a=document.createElement('a');a.href=URL.createObjectURL(b);a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),500)}
async function setMode(v,btn){await action(btn,()=>api('api/mode',{method:'POST',body:fd({simulation:v})}),'Modus gespeichert');location.reload()}
async function sys(btn){const d=await action(btn,()=>api('api/system'),'Systemstatus aktualisiert');byId('system').innerHTML=Object.entries(d).map(([k,v])=>`<div class="row"><span>${esc(k)}</span><b>${esc(typeof v==='object'?JSON.stringify(v):v)}</b></div>`).join('')}
async function diagnostics(btn){const d=await action(btn,()=>api('api/diagnostics'),'Diagnose abgeschlossen');byId('diagnostics').textContent=JSON.stringify(d,null,2)}
async function saveCustomer(btn){await action(btn,()=>api('api/customer',{method:'POST',body:fd({name:byId('cust_name').value,installation_id:byId('cust_id').value,location:byId('cust_location').value,installer:byId('cust_installer').value})}),'Anlagendaten gespeichert')}
async function createService(btn){if(!await modal('Service-Zugang anlegen','EnergyKit legt einen separaten Home-Assistant-Administrator für Wartung und die spätere EnergyKit-App an.','Benutzer anlegen'))return;const d=await action(btn,()=>api('api/service-user',{method:'POST'}),'Service-Benutzer angelegt');byId('serviceResult').innerHTML=`<div class="callout"><b>Zugangsdaten jetzt sichern.</b><div class="codebox" style="margin-top:10px">Benutzer: ${esc(d.username)}\nPasswort: ${esc(d.password||'bereits erzeugt')}</div><div class="actions left"><button class="btn secondary" id="dlCred">Zugangsdaten herunterladen</button></div></div>`;byId('dlCred').onclick=()=>downloadText('energykit-service.txt',`EnergyKit Service\nBenutzer: ${d.username}\nPasswort: ${d.password||''}\n`)}
async function discover(btn){const box=byId('devices');box.innerHTML='<div class="callout">Suche im Netzwerk …</div>';const d=await action(btn,()=>api('api/discover'),'Gerätesuche abgeschlossen');box.innerHTML=d.devices.map(x=>`<div class="device"><span class="status">${esc(d.mode)}</span><h3>${esc(x.vendor)}</h3><p>${esc(x.model)}</p><small>${esc(x.host)} · ${esc((x.ports||[]).join(', '))}</small><div class="actions left"><button class="btn secondary" onclick="useDevice('${esc(x.vendor)}','${esc(x.host)}',${x.port||x.ports?.[0]||502})">Verwenden</button></div></div>`).join('')||'<div class="callout">Keine Geräte gefunden.</div>'}
function useDevice(v,h,p){byId('vendor').value=v.toLowerCase().includes('deye')?'deye':'sigenergy';byId('host').value=h;byId('port').value=p;toast('Gerät übernommen')}
async function saveDevice(btn){await action(btn,()=>api('api/device',{method:'POST',body:fd({
vendor:byId('vendor').value,
host:byId('host').value,
port:byId('port').value,
modbus_id:byId('energy_modbus_id').value,
deye_profile:byId('deye_profile').value,
deye_batterytype:byId('deye_batterytype').value,
deye_firmware1098:byId('deye_firmware1098').checked?'true':'false'
})}),'Energiesystem gespeichert')}
async function installComp(n,btn){
  const d=await action(btn,()=>api(`api/components/${n}`,{method:'POST'}),x=>`${n}: ${x.version||'installiert'}`);
  if(d.restart_required){
    byId('integrationRestartNotice')?.classList.remove('hidden');
    toast('Home Assistant Neustart erforderlich','bad',6500);
  }
  return d
}
async function installBase(btn){setBusy(btn,true,'Installiere …');try{for(const [i,n] of ['mushroom','visionos','bridge'].entries()){byId('baseProgress').style.width=`${i*33}%`;byId('baseStatus').textContent=`Installiere ${n} …`;await api(`api/components/${n}`,{method:'POST'});byId('baseProgress').style.width=`${(i+1)*33}%`}byId('baseProgress').style.width='100%';byId('baseStatus').textContent='Basis installiert. Home Assistant muss neu gestartet werden.';toast('Basis-Komponenten installiert')}catch(e){toast(e.message,'bad',7000)}finally{setBusy(btn,false)}}
async function restartCore(btn){if(!await modal('Home Assistant neu starten','Home Assistant Core wird neu gestartet. EnergyKit bleibt geöffnet, einige API-Aufrufe sind für kurze Zeit nicht verfügbar.','Neu starten'))return;await action(btn,()=>api('api/core/restart',{method:'POST'}),'Neustart ausgelöst');byId('restartState').textContent='Home Assistant startet neu …';for(let i=0;i<30;i++){await new Promise(r=>setTimeout(r,2000));try{await api('api/system');byId('restartState').textContent='Home Assistant ist wieder erreichbar.';toast('Home Assistant wieder online');return}catch(e){}}byId('restartState').textContent='Neustart läuft länger als erwartet.'}
async function startFlow(domain,btn){const d=await action(btn,()=>api(`api/flow/start/${domain}`,{method:'POST'}));renderFlow(d)}
function renderFlow(d){const box=byId('flow');if(d.type==='create_entry'){box.innerHTML='<div class="callout"><b>Integration eingerichtet.</b></div>';toast('Config Flow abgeschlossen');return}if(d.type==='abort'){box.innerHTML=`<div class="callout warn">Flow abgebrochen: ${esc(d.reason||'unbekannt')}</div>`;return}if(d.type!=='form'){box.innerHTML=`<div class="callout">Home Assistant Flow: ${esc(d.type||'unbekannt')}. Bitte ggf. in Home Assistant fortsetzen.</div>`;return}let html=`<div class="panel flat"><h3>${esc(d.step_id||'Konfiguration')}</h3>`;(d.data_schema||[]).forEach(f=>{const name=f.name,sel=f.selector||{};let input=`<input id="flow_${esc(name)}" ${f.required?'required':''} value="${esc(f.default??'')}">`;if(sel.select&&sel.select.options){input=`<select id="flow_${esc(name)}">${sel.select.options.map(o=>`<option value="${esc(typeof o==='object'?o.value:o)}">${esc(typeof o==='object'?(o.label||o.value):o)}</option>`).join('')}</select>`}else if(sel.number){input=`<input type="number" id="flow_${esc(name)}" value="${esc(f.default??'')}">`}else if(sel.boolean){input=`<select id="flow_${esc(name)}"><option value="true">Ja</option><option value="false">Nein</option></select>`}html+=`<div class="flowfield"><label>${esc(name)}${input}</label></div>`});html+=`<div class="actions"><button class="btn" onclick="submitFlow('${esc(d.flow_id)}',this)">Weiter</button></div></div>`;box.innerHTML=html;box.dataset.fields=JSON.stringify((d.data_schema||[]).map(x=>x.name))}
async function submitFlow(id,btn){const box=byId('flow'),names=JSON.parse(box.dataset.fields||'[]'),data={};names.forEach(n=>{let v=byId('flow_'+n).value;if(v==='true')v=true;else if(v==='false')v=false;else if(/^-?\d+(\.\d+)?$/.test(String(v)))v=Number(v);data[n]=v});const d=await action(btn,()=>api(`api/flow/${id}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}));renderFlow(d)}
async function loadEntities(){const d=await api('api/entities');const opts='<option value="">– auswählen –</option>'+d.entities.map(e=>`<option value="${esc(e.entity_id)}">${esc(e.entity_id)}${e.name?' · '+esc(e.name):''}</option>`).join('');['pv','house','grid','battery_power','battery_soc'].forEach(k=>byId('map_'+k).innerHTML=opts)}
async function autoMap(btn){await action(btn,async()=>{await loadEntities();const d=await api('api/mapping/auto');Object.entries(d.mapping).forEach(([k,v])=>{if(byId('map_'+k))byId('map_'+k).value=v.entity_id||''});byId('mapHint').textContent=Object.values(d.mapping).every(v=>v.confident)?'Automatische Zuordnung ist eindeutig.':'Einige Werte sind nicht eindeutig. Bitte kontrollieren.';return d},'Messwerte analysiert')}
async function saveMapping(btn){const data={};['pv','house','grid','battery_power','battery_soc'].forEach(k=>data[k]=byId('map_'+k).value);await action(btn,()=>api('api/mapping',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}),'Mapping gespeichert')}
async function discoverConsumer(kind,btn){
 const box=byId(kind==='wallbox'?'wb_devices':'hp_devices');box.innerHTML='<div class="callout">Suche Home Assistant und lokales Netzwerk …</div>';
 const d=await action(btn,()=>api(`api/discover/consumers?kind=${kind}`),'Gerätesuche abgeschlossen');
 box.innerHTML=d.devices.map((x,i)=>`<div class="device"><span class="status">${esc(x.source||d.mode)}</span><h3>${esc(x.vendor||'Gerät')}</h3><p>${esc(x.model||'')}</p><small>${esc(x.host||'Home Assistant')}${x.entity?' · '+esc(x.entity):''}${x.modbus_id?' · ID '+esc(x.modbus_id):''}</small><div class="actions left"><button class="btn secondary" onclick='useConsumer(${JSON.stringify(kind)},${JSON.stringify(x)})'>Übernehmen</button></div></div>`).join('')||'<div class="callout">Keine passenden Geräte gefunden. Manuelle Eingabe bleibt möglich.</div>'
}
function useConsumer(kind,x){
 if(kind==='wallbox'){
   const blob=((x.vendor||'')+' '+(x.model||'')+' '+(x.entity||'')).toLowerCase();
   byId('wb_vendor').value=(blob.includes('sigen')||blob.includes('sigenergy'))?'sigenergy':blob.includes('go-e')?'go-e':'homeassistant';
   byId('wb_host').value=x.host||byId('host')?.value||'';byId('wb_entity').value=x.entity||'';byId('wb_device_id').value=x.device_id||'';byId('wb_model').value=x.model||'';if(x.modbus_id)byId('wb_modbus_id').value=x.modbus_id;
   if(blob.includes('sigen')||blob.includes('sigenergy')){byId('wb_control').value='native';byId('wb_template').value=blob.includes('evdc')?'sigenergy-evdc':'sigenergy'}
 }else{
   byId('hp_mode').value=x.entity?.startsWith('switch.')?'sg-ready':'native';byId('hp_vendor').value=x.vendor||'';byId('hp_model').value=x.model||'';byId('hp_host').value=x.host||'';byId('hp_entity').value=x.entity||'';byId('hp_switch').value=x.entity?.startsWith('switch.')?x.entity:'';byId('hp_device_id').value=x.device_id||'';
 } toast('Gerät übernommen')
}
async function saveWallbox(btn){await action(btn,()=>api('api/wallbox',{method:'POST',body:fd({vendor:byId('wb_vendor').value,model:byId('wb_model').value,host:byId('wb_host').value,port:byId('wb_port').value,entity:byId('wb_entity').value,device_id:byId('wb_device_id').value,modbus_id:byId('wb_modbus_id').value,evcc_template:byId('wb_template').value,control_path:byId('wb_control').value,max_current:byId('wb_current').value,phases:byId('wb_phases').value})}),'Wallbox gespeichert')}
async function saveHeatpump(btn){await action(btn,()=>api('api/heatpump',{method:'POST',body:fd({mode:byId('hp_mode').value,vendor:byId('hp_vendor').value,model:byId('hp_model').value,host:byId('hp_host').value,port:byId('hp_port').value,entity:byId('hp_entity').value,device_id:byId('hp_device_id').value,switch_entity:byId('hp_switch').value,switch_b:byId('hp_switch_b').value,modbus_id:byId('hp_modbus_id').value,evcc_template:byId('hp_template').value,control_path:byId('hp_control').value,min_power:byId('hp_min_power').value,max_power:byId('hp_max_power').value})}),'Wärmepumpe gespeichert')}
async function installEvcc(btn){await action(btn,()=>api('api/evcc/install',{method:'POST'}),d=>d.message||'evcc installiert')}
async function configureEvcc(btn){const d=await action(btn,()=>api('api/evcc/configure',{method:'POST'}),'evcc-Konfiguration geschrieben');byId('evccPath').textContent=d.path||''}
async function makeDashboard(btn){await action(btn,()=>api('api/dashboard',{method:'POST'}),'EnergyKit Dashboard erzeugt')}
async function runChecks(btn){const d=await action(btn,()=>api('api/checks',{method:'POST'}),'Prüfung abgeschlossen');byId('checks').innerHTML=d.checks.map(x=>`<div class="row"><span>${esc(x.name)}<small>${esc(x.detail||'')}</small></span><span class="status ${x.ok?'ok':'bad'}">${x.ok?'OK':'Fehler'}</span></div>`).join('');byId('checkProgress').style.width=`${d.percent}%`;byId('checkSummary').textContent=`${d.ok}/${d.total} Prüfungen bestanden · ${d.percent}%`}
async function backup(btn){await action(btn,()=>api('api/backup',{method:'POST'}),'Backup erstellt')}
async function finish(btn){if(!await modal('Anlage übergeben','Nach der Übergabe ist EnergyKit nur noch mit dem Service-Benutzer erreichbar. Stelle sicher, dass die Zugangsdaten gespeichert wurden.','Anlage übergeben'))return;await action(btn,()=>api('api/finish',{method:'POST'}),'Anlage übergeben');location.reload()}
document.addEventListener('DOMContentLoaded',()=>{goStep(currentStep);loadEntities().catch(()=>{});sys().catch(()=>{})})
"""

def h(v: Any) -> str:
    return html.escape(str(v if v is not None else ""))

def select_option(value: str, current: str, label: str | None = None) -> str:
    return f"<option value='{h(value)}' {'selected' if value==current else ''}>{h(label or value)}</option>"

def page(state: dict[str, Any]) -> str:
    c=state["customer"]; e=state["energy"]; wb=state["wallbox"]; hp=state["heatpump"]
    mode="Simulation" if state["simulation"] else "Live"
    service_ready=bool(state.get("service_user_id"))
    comp=state.get("components",{})
    base_ready=all(x in comp for x in ("mushroom","visionos","bridge"))
    nav=["System","Anlage","Basis","Energiesystem","Messwerte","Verbraucher","evcc","Dashboard","Abschluss"]
    if state.get("setup_complete"):
        return f"""<!doctype html><html lang='de'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>EnergyKit</title><style>{CSS}</style></head><body>
<div class='shell'><aside class='sidebar'><div class='brand'><span class='mark'>E</span>EnergyKit</div><div class='steps'><button class='stepnav active'><span class='stepnum'>✓</span>Service</button></div><div class='sidefoot'>v{APP_VERSION}</div></aside>
<div class='main'><div class='topbar'><b>EnergyKit Service</b><div class='topmeta'><span class='pill'>{mode}</span><span class='pill'>v{APP_VERSION}</span></div></div><div class='content'><div class='eyebrow'>SERVICE MODE</div><h1 class='title'>Anlage verwalten</h1><p class='subtitle'>Diagnose, Komponentenpflege und Recovery für die übergebene EnergyKit-Anlage.</p>
<div class='panel'><div class='panelhead'><div><h3>Systemstatus</h3><p>Supervisor, Home Assistant und Appliance-Verbindungen.</p></div><button class='btn secondary' onclick='sys(this)'>Aktualisieren</button></div><div id='system'></div></div>
<div class='panel'><div class='panelhead'><div><h3>Diagnose</h3><p>Technische Details für Servicefälle.</p></div><button class='btn secondary' onclick='diagnostics(this)'>Diagnose laden</button></div><pre id='diagnostics' class='codebox'>Noch keine Diagnose geladen.</pre></div>
<div class='panel'><h3>Recovery</h3><p>Öffnet die Inbetriebnahme erneut, ohne Geräte oder Home Assistant zu löschen.</p><div class='actions left'><button class='btn secondary' onclick="api('api/recovery/reset',{{method:'POST'}}).then(()=>location.reload())">Inbetriebnahme öffnen</button></div></div>
</div></div></div><div id='toasts' class='toaststack'></div><div id='modalBack' class='modalback'><div class='modal'><h3 id='modalTitle'></h3><div id='modalBody'></div><div class='actions'><button class='btn secondary' onclick='closeModal(false)'>Abbrechen</button><button class='btn' id='modalOk' onclick='closeModal(true)'>Bestätigen</button></div></div></div><script>{JS}</script></body></html>"""

    step_buttons=''.join(f"<button class='stepnav' onclick='goStep({i})'><span class='stepnum'>{i+1}</span>{h(n)}</button>" for i,n in enumerate(nav))
    return f"""<!doctype html><html lang='de'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>EnergyKit Setup</title><style>{CSS}</style></head><body>
<div class='shell'>
<aside class='sidebar'><div class='brand'><span class='mark'>E</span>EnergyKit</div><div class='steps'>{step_buttons}</div><div class='sidefoot'>EnergyKit Appliance<br>v{APP_VERSION}</div></aside>
<div class='main'><div class='topbar'><div><b>Inbetriebnahme</b> <span class='pill' id='stepCounter'>Schritt 1 von 9</span></div><div class='topmeta'><span class='pill'>{mode}</span><span class='pill'>v{APP_VERSION}</span></div></div>
<div class='content'>

<section class='step'>
<div class='eyebrow'>ENERGYKIT SETUP</div><h1 class='title'>Willkommen</h1><p class='subtitle'>Wir führen dich durch die vollständige Inbetriebnahme. Jeder Schritt lässt sich prüfen, bevor es weitergeht.</p>
<div class='panel'><div class='panelhead'><div><h3>Betriebsmodus</h3><p>Simulation für VM-Tests oder Live für die echte Anlage.</p></div><span class='status {'ok' if state["simulation"] else 'warn'}'>{mode}</span></div>
<div class='actions left'><button class='btn {'secondary' if state["simulation"] else ''}' onclick='setMode(true,this)'>Simulation</button><button class='btn {'secondary' if not state["simulation"] else ''}' onclick='setMode(false,this)'>Live-Anlage</button></div></div>
<div class='panel'><div class='panelhead'><div><h3>Systemstatus</h3><p>Prüft Home Assistant OS, Supervisor und installierte Apps.</p></div><button class='btn secondary' onclick='sys(this)'>System prüfen</button></div><div id='system'></div></div>
<div class='panel'><div class='panelhead'><div><h3>Technische Diagnose</h3><p>Zeigt API- und WebSocket-Berechtigungen. Hilfreich, wenn eine Aktion nicht funktioniert.</p></div><button class='btn secondary' onclick='diagnostics(this)'>Diagnose</button></div><pre id='diagnostics' class='codebox'>Noch keine Diagnose geladen.</pre></div>
</section>

<section class='step'>
<div class='eyebrow'>ANLAGE</div><h1 class='title'>Anlagendaten & Service</h1><p class='subtitle'>Dokumentation und ein separater Service-Zugang für Wartung und die spätere EnergyKit-App.</p>
<div class='panel grid2'><label>Kunde<input id='cust_name' value='{h(c.get("name"))}'></label><label>Anlagen-ID<input id='cust_id' value='{h(c.get("installation_id"))}'></label><label>Standort<input id='cust_location' value='{h(c.get("location"))}'></label><label>Installateur<input id='cust_installer' value='{h(c.get("installer"))}'></label><div class='span2 actions'><button class='btn' onclick='saveCustomer(this)'>Anlagendaten speichern</button></div></div>
<div class='panel'><div class='panelhead'><div><h3>EnergyKit Service-Benutzer</h3><p>Eigener Home-Assistant-Administrator. Das Passwort wird zufällig erzeugt und nur während der Einrichtung angezeigt.</p></div><span class='status {'ok' if service_ready else ''}'>{'angelegt' if service_ready else 'offen'}</span></div><div id='serviceResult'></div><div class='actions left'><button class='btn' onclick='createService(this)' {'disabled' if service_ready else ''}>{'Service-Benutzer vorhanden' if service_ready else 'Service-Benutzer anlegen'}</button></div></div>
</section>

<section class='step'>
<div class='eyebrow'>BASIS</div><h1 class='title'>EnergyKit Basis installieren</h1><p class='subtitle'>Mushroom, VisionOS-Theme und EnergyKit Bridge werden ohne HACS direkt installiert.</p>
<div class='panel'><div class='panelhead'><div><h3>Basis-Komponenten</h3><p>Direkte, reproduzierbare Installation aus den jeweiligen Releases.</p></div><span class='status {'ok' if base_ready else ''}'>{'bereit' if base_ready else 'nicht vollständig'}</span></div>
<div class='row'><span>Mushroom<small>{h(comp.get("mushroom",{}).get("version","nicht installiert"))}</small></span></div>
<div class='row'><span>VisionOS Theme<small>{h(comp.get("visionos",{}).get("version","nicht installiert"))}</small></span></div>
<div class='row'><span>EnergyKit Bridge<small>{h(comp.get("bridge",{}).get("version","nicht installiert"))}</small></span></div>
<div class='progress' style='margin-top:16px'><i id='baseProgress' style='width:{100 if base_ready else 0}%'></i></div><p id='baseStatus' style='margin-top:9px'>{'Basis ist installiert.' if base_ready else 'Noch nicht installiert.'}</p>
<div class='actions left'><button class='btn' onclick='installBase(this)'>Basis automatisch installieren</button><button class='btn secondary' onclick='restartCore(this)'>Home Assistant neu starten</button></div><div id='restartState' class='callout'>Nach Installation oder Update einer Custom Integration ist ein Core-Neustart erforderlich.</div></div>
</section>

<section class='step'>
<div class='eyebrow'>ENERGIESYSTEM</div><h1 class='title'>Wechselrichter & Speicher</h1><p class='subtitle'>Gerät finden, Integration installieren und den echten Home-Assistant-Config-Flow durchlaufen.</p>
<div class='panel'><div class='panelhead'><div><h3>Geräteerkennung</h3><p>Im Simulationsmodus werden Testgeräte angeboten.</p></div><button class='btn secondary' onclick='discover(this)'>Geräte suchen</button></div><div id='devices' class='devicegrid'></div></div>
<div class='panel grid2'>
<label>Hersteller<select id='vendor'>{select_option("sigenergy",e.get("vendor"),"Sigenergy")}{select_option("deye",e.get("vendor"),"Deye")}</select></label>
<label>IP / Host<input id='host' value='{h(e.get("host"))}' placeholder='192.168.1.50'></label>
<label>Port<input id='port' type='number' value='{h(e.get("port") or 502)}'></label>
<label>Modbus ID<input id='energy_modbus_id' type='number' min='1' max='247' value='{h(e.get("modbus_id") or 1)}'></label>
<label>Deye Profil<select id='deye_profile'>
{select_option("deye-hybrid-3p",e.get("deye_profile"),"3-phasiger Hybrid / SUN-SG04 (empfohlen)")}
{select_option("deye-storage",e.get("deye_profile"),"Storage / Hybrid")}
{select_option("deye-mi",e.get("deye_profile"),"Micro Inverter")}
</select></label>
<label>Deye Batterie<select id='deye_batterytype'>
{select_option("lv",e.get("deye_batterytype"),"LV / Niedervolt")}
{select_option("hv",e.get("deye_batterytype"),"HV / Hochvolt")}
</select></label>
<label class='checkline'><input id='deye_firmware1098' type='checkbox' {"checked" if e.get("deye_firmware1098") else ""}> Deye HV Firmware 1098 oder neuer</label>
<div></div>
<div class='span2 callout'><b>Direkter evcc-Pfad:</b> Sigenergy sowie unterstützte Deye-Wechselrichter werden von evcc direkt per Modbus gelesen. Home-Assistant-Entities bleiben nur Dashboard- und Fallback-Daten.</div>
<div class='span2 actions'><button class='btn secondary' onclick='saveDevice(this)'>Gerät speichern</button><button class='btn secondary' onclick="installComp('sigenergy',this)">Sigenergy Integration installieren</button><button class='btn secondary' onclick="installComp('deye',this)">Deye Integration installieren</button></div>
</div>
<div id='integrationRestartNotice' class='callout warn {'hidden' if not state.get("restart_required") else ''}'><b>Neustart erforderlich.</b> Home Assistant muss die neu installierten Custom Integrations erst laden.<div class='actions left'><button class='btn' onclick='restartCore(this)'>Home Assistant jetzt neu starten</button></div></div>
<div class='panel'><div class='panelhead'><div><h3>Home Assistant Config Flow</h3><p>Sigenergy verwendet den Domain-Handler <code>sigen</code>, Deye <code>deye_modbus</code>.</p></div></div><div class='actions left'><button class='btn' onclick="startFlow('sigenergy',this)">Sigenergy konfigurieren</button><button class='btn secondary' onclick="startFlow('deye',this)">Deye konfigurieren</button></div><div id='flow'></div></div>
</section>

<section class='step'>
<div class='eyebrow'>MESSWERTE</div><h1 class='title'>Messwerte normalisieren</h1><p class='subtitle'>EnergyKit ordnet PV, Haus, Netz und Batterie auf stabile <code>sensor.ek_*</code>-Entitäten ab.</p>
<div class='panel grid2'>
<label>PV<select id='map_pv'></select></label><label>Haus<select id='map_house'></select></label><label>Netz<select id='map_grid'></select></label><label>Batterieleistung<select id='map_battery_power'></select></label><label>Batterie SoC<select id='map_battery_soc'></select></label><div></div>
<div class='span2 callout' id='mapHint'>Automatische Zuordnung starten oder Werte manuell auswählen.</div><div class='span2 actions'><button class='btn secondary' onclick='autoMap(this)'>Automatisch zuordnen</button><button class='btn' onclick='saveMapping(this)'>Mapping speichern</button></div></div>
</section>

<section class='step'>
<div class='eyebrow'>VERBRAUCHER</div><h1 class='title'>Wallbox & Wärmepumpe</h1><p class='subtitle'>EnergyKit findet Geräte und übersetzt sie in native evcc-Treiber. Home Assistant bleibt Visualisierung und Fallback, nicht der primäre Steuerpfad.</p>
<div class='panel'><div class='panelhead'><div><h3>Wallbox suchen</h3><p>Durchsucht Home Assistant und das lokale Netz. Treffer dienen der Identifikation; wenn evcc das Gerät nativ unterstützt, verbindet sich evcc direkt zur Hardware.</p></div><button class='btn secondary' onclick="discoverConsumer('wallbox',this)">Geräte suchen</button></div><div id='wb_devices' class='devicegrid'></div></div>
<div class='panel grid2'><label>Wallbox<select id='wb_vendor'>{select_option("none",wb.get("vendor"),"Keine")}{select_option("sigenergy",wb.get("vendor"),"Sigenergy EVAC")}{select_option("sigenergy-evdc",wb.get("vendor"),"Sigenergy EVDC")}{select_option("go-e",wb.get("vendor"),"go-e Gemini / HOME")}{select_option("openwb",wb.get("vendor"),"openWB")}{select_option("custom",wb.get("vendor"),"Anderer nativer evcc-Treiber")}{select_option("homeassistant",wb.get("vendor"),"Home Assistant Fallback")}</select></label><label>Modell<input id='wb_model' value='{h(wb.get("model"))}' placeholder='z. B. Sigen EVAC 11 kW'></label><label>Steuerpfad<select id='wb_control'>{select_option("native",wb.get("control_path"),"evcc direkt (empfohlen)")}{select_option("homeassistant",wb.get("control_path"),"Home Assistant Fallback")}</select></label><label>evcc Template<input id='wb_template' value='{h(wb.get("evcc_template"))}' placeholder='automatisch, z. B. sigenergy'></label><label>Host / Geräte-IP<input id='wb_host' value='{h(wb.get("host"))}' placeholder='192.168.1.50'></label><label>Port<input id='wb_port' type='number' value='{h(wb.get("port") or 502)}'></label><label>Modbus ID<input id='wb_modbus_id' type='number' min='1' max='247' value='{h(wb.get("modbus_id") or 1)}'></label><label>Phasen<input id='wb_phases' type='number' min='1' max='3' value='{h(wb.get("phases"))}'></label><label>Max. Strom (A)<input id='wb_current' type='number' min='6' value='{h(wb.get("max_current"))}'></label><label>HA Device-ID <small>(nur Erkennung)</small><input id='wb_device_id' value='{h(wb.get("device_id"))}'></label><label>HA Entity <small>(nur Fallback)</small><input id='wb_entity' value='{h(wb.get("entity"))}' placeholder='switch.wallbox'></label><div></div><div class='span2 callout'><b>Sigenergy:</b> EVAC nutzt in evcc das Template <code>sigenergy</code>, EVDC <code>sigenergy-evdc</code>. Beide werden direkt per Modbus TCP angesprochen. Bei EVDC ist die Modbus-ID des zugehörigen Hybrid-Wechselrichters maßgeblich.</div><div class='span2 actions'><button class='btn' onclick='saveWallbox(this)'>Wallbox speichern</button></div></div>
<div class='panel'><div class='panelhead'><div><h3>Wärmepumpe suchen</h3><p>Findet Climate-/SG-Ready-Geräte und Modbus-Kandidaten. Danach wählst du den nativen evcc-Treiber oder bewusst den HA-Fallback.</p></div><button class='btn secondary' onclick="discoverConsumer('heatpump',this)">Geräte suchen</button></div><div id='hp_devices' class='devicegrid'></div></div>
<div class='panel grid2'><label>Anbindung<select id='hp_mode'>{select_option("none",hp.get("mode"),"Keine")}{select_option("native",hp.get("mode"),"Nativer evcc Heizgeräte-Treiber")}{select_option("sg-ready",hp.get("mode"),"SG Ready über Relais")}{select_option("integration",hp.get("mode"),"Home Assistant Fallback")}</select></label><label>Hersteller<input id='hp_vendor' value='{h(hp.get("vendor"))}' placeholder='NIBE, Stiebel Eltron, Vaillant …'></label><label>Modell<input id='hp_model' value='{h(hp.get("model"))}'></label><label>Steuerpfad<select id='hp_control'>{select_option("native",hp.get("control_path"),"evcc direkt (empfohlen)")}{select_option("homeassistant",hp.get("control_path"),"Home Assistant Fallback")}</select></label><label>evcc Template<input id='hp_template' value='{h(hp.get("evcc_template"))}' placeholder='z. B. nibe-s-series, stiebel-lwa'></label><label>Host / IP<input id='hp_host' value='{h(hp.get("host"))}'></label><label>Port<input id='hp_port' type='number' value='{h(hp.get("port") or 502)}'></label><label>Modbus ID<input id='hp_modbus_id' type='number' min='1' max='247' value='{h(hp.get("modbus_id") or 1)}'></label><label>SG Ready Relais A<input id='hp_switch' value='{h(hp.get("switch"))}' placeholder='switch.sg_ready_a'></label><label>SG Ready Relais B <small>(optional)</small><input id='hp_switch_b' value='{h(hp.get("switch_b"))}' placeholder='switch.sg_ready_b'></label><label>HA Haupt-Entity <small>(nur Erkennung)</small><input id='hp_entity' value='{h(hp.get("entity"))}' placeholder='climate.waermepumpe'></label><label>HA Device-ID<input id='hp_device_id' value='{h(hp.get("device_id"))}'></label><label>Min. therm./el. Leistung (W)<input id='hp_min_power' type='number' value='{h(hp.get("min_power") or 1200)}'></label><label>Max. Leistung (W)<input id='hp_max_power' type='number' value='{h(hp.get("max_power") or 6000)}'></label><div class='span2 callout'><b>Klare Zuständigkeit:</b> EnergyKit richtet ein. evcc entscheidet im Betrieb über PV-Überschuss, Lade-/Heizleistung und SG Ready. EnergyKit Bridge schaltet keine SG-Ready-Relais mehr selbst.</div><div class='span2 actions'><button class='btn' onclick='saveHeatpump(this)'>Wärmepumpe speichern</button></div></div>
</section>

<section class='step'>
<div class='eyebrow'>EVCC</div><h1 class='title'>PV-Überschussladen</h1><p class='subtitle'>evcc wird als Home-Assistant-App installiert. Wallboxen und Wärmepumpen werden bevorzugt direkt über native evcc-Treiber angebunden; HA-Entities sind nur Fallback.</p>
<div class='panel'><div class='row'><span>evcc App<small>{'installiert · '+h(state["evcc"].get("slug")) if state["evcc"].get("installed") else 'nicht installiert'}</small></span><span class='status {'ok' if state["evcc"].get("installed") else ''}'>{'bereit' if state["evcc"].get("installed") else 'offen'}</span></div><div class='row'><span>Konfiguration<small id='evccPath'>{h(state["evcc"].get("config_path") or "")}</small></span><span class='status {'ok' if state["evcc"].get("configured") else ''}'>{'geschrieben' if state["evcc"].get("configured") else 'offen'}</span></div><div class='actions left'><button class='btn' onclick='installEvcc(this)'>evcc installieren</button><button class='btn secondary' onclick='configureEvcc(this)'>Konfiguration erzeugen</button></div></div>
</section>

<section class='step'>
<div class='eyebrow'>OBERFLÄCHE</div><h1 class='title'>EnergyKit Dashboard</h1><p class='subtitle'>Erzeugt ein kundentaugliches Lovelace-Dashboard ausschließlich mit Home-Assistant-Standardkarten und stabilen EnergyKit-Sensoren. Keine HACS-Frontend-Abhängigkeit.</p>
<div class='panel'><div class='panelhead'><div><h3>Dashboard</h3><p>URL-Pfad: <code>energykit-dashboard</code>. Vorhandene Installation wird aktualisiert.</p></div><span class='status {'ok' if state["dashboard"].get("installed") else ''}'>{'erstellt' if state["dashboard"].get("installed") else 'offen'}</span></div><div class='actions left'><button class='btn' onclick='makeDashboard(this)'>Dashboard erzeugen</button></div></div>
</section>

<section class='step'>
<div class='eyebrow'>ABSCHLUSS</div><h1 class='title'>Prüfen & übergeben</h1><p class='subtitle'>EnergyKit prüft die gesamte Kette. Erst danach wird die Anlage in den Service-Modus übergeben.</p>
<div class='panel'><div class='panelhead'><div><h3>End-to-End Prüfung</h3><p id='checkSummary'>Noch nicht ausgeführt.</p></div><button class='btn secondary' onclick='runChecks(this)'>Prüfung starten</button></div><div class='progress'><i id='checkProgress' style='width:0%'></i></div><div id='checks' class='checklist' style='margin-top:12px'></div></div>
<div class='panel'><h3>Übergabe</h3><p>Vorher Service-Zugangsdaten sichern und idealerweise ein Abschluss-Backup erstellen.</p><div class='actions left'><a class='btn secondary' href='api/report.html'>Übergabebericht</a><button class='btn secondary' onclick='backup(this)'>Backup erstellen</button><button class='btn' onclick='finish(this)'>Anlage übergeben</button></div></div>
</section>

</div>
<div class='footerbar'><div class='footerinner'><button id='backBtn' class='btn secondary' onclick='goStep(currentStep-1)'>Zurück</button><button id='nextBtn' class='btn' onclick='nextStep()'>Weiter</button></div></div>
</div></div>
<div id='toasts' class='toaststack'></div>
<div id='modalBack' class='modalback'><div class='modal'><h3 id='modalTitle'></h3><div id='modalBody'></div><div class='actions'><button class='btn secondary' onclick='closeModal(false)'>Abbrechen</button><button class='btn' id='modalOk' onclick='closeModal(true)'>Bestätigen</button></div></div></div>
<script>{JS}</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    try:
        state = ensure_access(request)
    except HTTPException as exc:
        if exc.status_code == 403:
            return HTMLResponse(f"<style>{CSS}</style><div class='deny'><div class='logo' style='margin:auto'>E</div><h1>Zugriff verweigert</h1><p>{h(exc.detail)}</p></div>", status_code=403)
        raise
    return HTMLResponse(page(state))


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz():
    return "ok"


@app.post("/api/mode")
async def set_mode(request: Request, simulation: bool = Form(...)):
    state = ensure_access(request)
    state["simulation"] = simulation
    save_state(state)
    return {"ok": True}


@app.get("/api/system")
async def system_info(request: Request):
    ensure_access(request)
    info = await sup("GET", "/info")
    addons = await sup("GET", "/addons")
    return {
        "Supervisor Token": "verfügbar" if TOKEN else "FEHLT",
        "HAOS": info.get("hassos"),
        "Home Assistant": info.get("homeassistant"),
        "Supervisor": info.get("supervisor"),
        "Architektur": info.get("arch"),
        "Unterstützt": info.get("supported"),
        "Installierte Apps": len(addons.get("addons", addons if isinstance(addons, list) else [])),
    }


@app.post("/api/customer")
async def save_customer(request: Request, name: str = Form(...), installation_id: str = Form(...), location: str = Form(""), installer: str = Form("")):
    state = ensure_access(request)
    state["customer"] = {"name": name.strip(), "installation_id": installation_id.strip(), "location": location.strip(), "installer": installer.strip()}
    save_state(state)
    return {"ok": True}


@app.post("/api/service-user")
async def create_service_user(request: Request):
    state = ensure_access(request)
    if state.get("service_user_id"):
        return {"username": state["service_username"], "password": state.get("service_password"), "user_id": state["service_user_id"], "existing": True}

    username = state["service_username"]
    password = secrets.token_urlsafe(24)
    user_id = None
    try:
        users = (await core_ws([{"type": "config/auth/list"}]))[0] or []
        existing = next((u for u in users if u.get("username") == username or u.get("name") == "EnergyKit Service"), None)
        if existing:
            raise HTTPException(409, f"Ein Home-Assistant-Benutzer für EnergyKit existiert bereits ({existing.get('username') or existing.get('name')}). Bitte diesen Benutzer entfernen oder Recovery verwenden.")

        created = (await core_ws([{
            "type": "config/auth/create",
            "name": "EnergyKit Service",
            "group_ids": ["system-admin"],
            "local_only": False,
        }]))[0]
        user_id = (created.get("user") or created).get("id")
        if not user_id:
            raise RuntimeError("Home Assistant hat keine User-ID zurückgegeben")

        await core_ws([{
            "type": "config/auth_provider/homeassistant/create",
            "user_id": user_id,
            "username": username,
            "password": password,
        }])
    except HTTPException:
        raise
    except Exception as exc:
        if user_id:
            try:
                await core_ws([{"type": "config/auth/delete", "user_id": user_id}])
            except Exception:
                pass
        raise HTTPException(500, f"Service-Benutzer konnte nicht angelegt werden: {exc}")

    state["service_user_id"] = user_id
    state["service_password"] = password
    save_state(state)
    return {"username": username, "password": password, "user_id": user_id, "existing": False}


@app.get("/api/discover")
async def discover(request: Request):
    state = ensure_access(request)
    if state["simulation"]:
        return {"mode": "simulation", "devices": [
            {"vendor": "Sigenergy", "model": "SigenStor EC 12.0", "host": "192.168.1.42", "ports": [502], "port": 502},
            {"vendor": "Deye", "model": "SUN-12K-SG04", "host": "192.168.1.61", "ports": [8899, 502], "port": 502},
            {"vendor": "go-e", "model": "Charger Gemini", "host": "192.168.1.54", "ports": [80], "port": 80},
        ]}
    return {"mode": "live", "devices": await scan_live()}


@app.get("/api/discover/consumers")
async def discover_consumers(request: Request, kind: str):
    state = ensure_access(request)
    if kind not in {"wallbox", "heatpump"}:
        raise HTTPException(400, "kind muss wallbox oder heatpump sein")
    if state["simulation"]:
        if kind == "wallbox":
            return {"mode": "simulation", "devices": [
                {"source":"homeassistant","kind":"wallbox","vendor":"Sigenergy","model":"Sigen AC Charger 11 kW","host":"192.168.1.42","entity":"button.sigen_ac_charger_start_charging","device_id":"sim-sigen-ac","modbus_id":1,"sigenergy_ac_charger":True},
                {"source":"network","kind":"wallbox","vendor":"go-e","model":"Charger Gemini","host":"192.168.1.54","entity":"switch.wallbox_enable"},
            ]}
        return {"mode": "simulation", "devices": [
            {"source":"homeassistant","kind":"heatpump","vendor":"Vaillant","model":"aroTHERM plus","host":"192.168.1.71","entity":"climate.waermepumpe","device_id":"sim-hp-1"},
            {"source":"homeassistant","kind":"heatpump","vendor":"SG-Ready","model":"Freigabekontakt","host":"","entity":"switch.sg_ready","device_id":"sim-hp-2"},
        ]}

    devices = await discover_ha_consumers(kind)
    network = await scan_live()
    for n in network:
        # Network-only candidates are deliberately labelled as candidates, not
        # guessed manufacturers. Port 502 is useful for both chargers and HPs.
        if 502 not in n.get("ports", []) and kind == "heatpump":
            continue
        if kind == "wallbox" and not any(p in n.get("ports", []) for p in (502,80,443)):
            continue
        devices.append({
            "source":"network", "kind":kind, "vendor":n.get("vendor","Netzwerkgerät"),
            "model": ("Modbus/LAN Kandidat" if 502 in n.get("ports",[]) else n.get("model","Netzwerkgerät")),
            "host":n.get("host",""), "entity":"", "ports":n.get("ports",[]),
        })
    return {"mode":"live", "devices":devices}


@app.post("/api/device")
async def save_device(
    request: Request,
    vendor: str = Form(...),
    host: str = Form(...),
    port: int = Form(502),
    modbus_id: int = Form(1),
    deye_profile: str = Form("deye-hybrid-3p"),
    deye_batterytype: str = Form("lv"),
    deye_firmware1098: str = Form("false"),
):
    state = ensure_access(request)
    allowed_profiles = {"deye-hybrid-3p", "deye-storage", "deye-mi"}
    state["energy"] = {
        "vendor": vendor,
        "host": host.strip(),
        "port": max(1, min(65535, port)),
        "modbus_id": max(1, min(247, modbus_id)),
        "deye_profile": deye_profile if deye_profile in allowed_profiles else "deye-hybrid-3p",
        "deye_batterytype": deye_batterytype if deye_batterytype in {"lv", "hv"} else "lv",
        "deye_firmware1098": str(deye_firmware1098).lower() in {"1", "true", "yes", "on"},
        "configured": state["simulation"],
    }
    save_state(state)
    return {"ok": True}


@app.post("/api/components/{name}")
async def install_component(request: Request, name: str):
    state = ensure_access(request)
    return await install_component_impl(name, state)


@app.post("/api/core/restart")
async def restart_core(request: Request):
    state = ensure_access(request)
    await sup("POST", "/core/restart", json={})
    # Wait for Core to disappear and return.
    await asyncio.sleep(3)
    last_error = None
    for _ in range(60):
        try:
            await core_rest("GET", "/config")
            state["restart_required"] = False
            save_state(state)
            return {"ok": True, "ready": True}
        except Exception as exc:
            last_error = str(exc)
            await asyncio.sleep(2)
    raise HTTPException(504, f"Home Assistant kam nach dem Neustart nicht rechtzeitig zurück: {last_error}")


@app.post("/api/flow/start/{domain}")
async def flow_start(request: Request, domain: str):
    state = ensure_access(request)
    domain_map = {
        "sigenergy": "sigen",
        "sigen": "sigen",
        "deye": "deye_modbus",
        "deye_modbus": "deye_modbus",
        "energykit_bridge": "energykit_bridge",
    }
    handler = domain_map.get(domain, domain)
    if state.get("restart_required") and not state["simulation"]:
        raise HTTPException(
            409,
            "Home Assistant muss nach der Installation der Custom Integration zuerst neu gestartet werden."
        )
    if state["simulation"]:
        return {"type": "create_entry", "title": f"Simulation {handler}", "result": {}, "handler": handler}
    try:
        result = await core_rest("POST", "/config/config_entries/flow", json={"handler": handler, "show_advanced_options": True})
        result["handler"] = handler
        return result
    except HTTPException as exc:
        raise HTTPException(exc.status_code, f"Config Flow '{handler}' konnte nicht gestartet werden. Ist die Integration installiert und Home Assistant danach neu gestartet worden? {exc.detail}")


@app.post("/api/flow/{flow_id}")
async def flow_submit(request: Request, flow_id: str):
    state = ensure_access(request)
    if state["simulation"]:
        return {"type": "create_entry", "title": "Simulation", "result": {}}
    data = await request.json()
    result = await core_rest("POST", f"/config/config_entries/flow/{flow_id}", json=data)
    if result.get("type") == "create_entry":
        state["energy"]["configured"] = True
        save_state(state)
    return result


@app.get("/api/entities")
async def entities(request: Request):
    ensure_access(request)
    states = await all_states()
    entities = []
    for s in states:
        eid = s.get("entity_id", "")
        if eid.startswith(("sensor.", "number.", "input_number.", "switch.")):
            entities.append({"entity_id": eid, "name": s.get("attributes", {}).get("friendly_name", "")})
    return {"entities": sorted(entities, key=lambda x: x["entity_id"])}


@app.get("/api/mapping/auto")
async def mapping_auto(request: Request):
    state = ensure_access(request)
    return {"mapping": auto_mapping(await all_states(), state["energy"].get("vendor"))}


@app.post("/api/mapping")
async def mapping(request: Request):
    state = ensure_access(request)
    data = await request.json()
    if not all(data.get(k) for k in ("pv", "house", "grid", "battery_power", "battery_soc")):
        raise HTTPException(400, "Alle fünf EnergyKit-Messwerte müssen zugeordnet sein")
    state["mapping"] = data
    save_state(state)
    write_bridge_mapping(state)
    copy_bridge()
    state["components"]["bridge"] = {"version": f"bundled-{APP_VERSION}", "installed_at": now_iso()}
    save_state(state)
    await sup("POST", "/core/restart", json={})
    for _ in range(50):
        await asyncio.sleep(2)
        try:
            await core_rest("GET", "/config")
            break
        except Exception:
            pass
    await ensure_bridge_entry()
    return {"ok": True}


@app.post("/api/wallbox")
async def wallbox(request: Request, vendor: str = Form(...), model: str = Form(""), host: str = Form(""), port: int = Form(502), entity: str = Form(""), device_id: str = Form(""), modbus_id: int = Form(1), evcc_template: str = Form(""), control_path: str = Form("native"), max_current: int = Form(16), phases: int = Form(3)):
    state = ensure_access(request)
    if vendor in {"sigenergy", "sigenergy-evdc"} and not host.strip() and not state["simulation"]:
        host = str(state.get("energy", {}).get("host") or "")
    template = evcc_template.strip()
    if not template:
        template = {"sigenergy":"sigenergy","sigenergy-evdc":"sigenergy-evdc","go-e":"go-e-v3","openwb":"openwb"}.get(vendor, "")
    state["wallbox"] = {"vendor": vendor, "model": model.strip(), "host": host.strip(), "port": max(1,min(65535,port)), "entity": entity.strip(), "device_id": device_id.strip(), "modbus_id": max(1,min(247,modbus_id)), "evcc_template": template, "control_path": control_path if control_path in {"native","homeassistant"} else "native", "max_current": max(6,max_current), "phases": max(1,min(3,phases))}
    save_state(state)
    return {"ok": True, "wallbox": state["wallbox"], "evcc_driver": template or ("homeassistant-switch" if control_path == "homeassistant" else None)}


@app.post("/api/heatpump")
async def heatpump(request: Request, mode: str = Form(...), vendor: str = Form(""), model: str = Form(""), host: str = Form(""), port: int = Form(502), entity: str = Form(""), device_id: str = Form(""), switch_entity: str = Form(""), switch_b: str = Form(""), modbus_id: int = Form(1), evcc_template: str = Form(""), control_path: str = Form("native"), min_power: int = Form(1200), max_power: int = Form(6000)):
    state = ensure_access(request)
    state["heatpump"] = {"mode": mode, "vendor": vendor.strip(), "model": model.strip(), "host": host.strip(), "port": max(1,min(65535,port)), "entity": entity.strip(), "device_id": device_id.strip(), "switch": switch_entity.strip(), "switch_b": switch_b.strip(), "modbus_id": max(1,min(247,modbus_id)), "evcc_template": evcc_template.strip(), "control_path": control_path if control_path in {"native","homeassistant"} else "native", "min_power": max(0,min_power), "max_power": max(max(0,min_power),max_power)}
    save_state(state)
    if state.get("mapping"):
        write_bridge_mapping(state)
    return {"ok": True, "heatpump": state["heatpump"], "runtime_controller": "evcc"}


@app.post("/api/evcc/install")
async def evcc_install(request: Request):
    state = ensure_access(request)
    repo = "https://github.com/evcc-io/hassio-addon"
    try:
        await sup("POST", "/store/repositories", json={"repository": repo})
    except HTTPException as exc:
        # Already-added repositories may answer 400/409.
        if exc.status_code not in (400, 409):
            raise HTTPException(exc.status_code, f"evcc Repository konnte nicht hinzugefügt werden: {exc.detail}")
    try:
        await sup("POST", "/store/reload", json={})
    except Exception:
        pass
    store = await sup("GET", "/store/addons")
    apps = store.get("addons", store if isinstance(store, list) else [])
    candidate = next((a for a in apps if str(a.get("name", "")).strip().lower() == "evcc" and "nightly" not in str(a.get("name", "")).lower()), None)
    if not candidate:
        raise HTTPException(404, "evcc App nach Repository-Reload nicht gefunden")
    slug = candidate.get("slug")
    try:
        await sup("POST", f"/store/addons/{slug}/install", json={"background": False})
    except HTTPException as exc:
        if exc.status_code not in (400, 409):
            raise
    state["evcc"].update({"installed": True, "slug": slug})
    save_state(state)
    return {"ok": True, "slug": slug, "message": "evcc installiert"}


@app.post("/api/evcc/configure")
async def evcc_configure(request: Request):
    state = ensure_access(request)
    if not state["evcc"].get("installed"):
        raise HTTPException(400, "Zuerst evcc installieren")
    if not state["mapping"]:
        raise HTTPException(400, "Zuerst EnergyKit Messwerte zuordnen")
    slug = state["evcc"].get("slug") or await find_evcc_slug()
    if not slug:
        raise HTTPException(404, "evcc Slug nicht gefunden")
    # all_addon_configs is mounted read/write at /addon_configs by EnergyKit.
    target_dir = ADDON_CONFIGS / slug
    if not target_dir.exists():
        # Repositories usually prefix public addon config dirs with a repository hash.
        matches = list(ADDON_CONFIGS.glob(f"*_{slug}")) + list(ADDON_CONFIGS.glob("*_evcc"))
        target_dir = matches[0] if matches else target_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "evcc.yaml"
    path.write_text(evcc_yaml(state))
    try:
        await sup("POST", f"/addons/{slug}/options", json={"options": {"config_file": "/config/evcc.yaml"}})
    except Exception:
        pass
    try:
        await sup("POST", f"/addons/{slug}/restart", json={})
    except Exception:
        try:
            await sup("POST", f"/addons/{slug}/start", json={})
        except Exception:
            pass
    state["evcc"].update({"configured": True, "config_path": str(path)})
    save_state(state)
    return {"ok": True, "path": str(path), "yaml": evcc_yaml(state)}


DASHBOARD = {"views": [
    {"title": "Übersicht", "path": "uebersicht", "icon": "mdi:view-dashboard", "cards": [
        {"type": "markdown", "content": "# EnergyKit\\n### Energie auf einen Blick\\nPV, Haus, Netz und Speicher in einer ruhigen Kundenansicht."},
        {"type": "grid", "columns": 4, "square": False, "cards": [
            {"type": "gauge", "entity": "sensor.ek_pv_power", "name": "PV-Leistung", "min": 0, "max": 15000, "needle": True},
            {"type": "gauge", "entity": "sensor.ek_house_power", "name": "Hausverbrauch", "min": 0, "max": 15000, "needle": True},
            {"type": "gauge", "entity": "sensor.ek_battery_soc", "name": "Speicher", "min": 0, "max": 100, "needle": True},
            {"type": "entity", "entity": "sensor.ek_grid_power", "name": "Netz", "icon": "mdi:transmission-tower"},
        ]},
        {"type": "entities", "title": "Aktueller Energiefluss", "show_header_toggle": False, "entities": [
            {"entity": "sensor.ek_pv_power", "name": "PV-Erzeugung", "icon": "mdi:solar-power"},
            {"entity": "sensor.ek_house_power", "name": "Hausverbrauch", "icon": "mdi:home-lightning-bolt"},
            {"entity": "sensor.ek_grid_power", "name": "Netzbezug / Einspeisung", "icon": "mdi:transmission-tower"},
            {"entity": "sensor.ek_battery_power", "name": "Batterieleistung", "icon": "mdi:battery-sync"},
            {"entity": "sensor.ek_battery_soc", "name": "Batterieladestand", "icon": "mdi:battery"},
        ]},
        {"type": "history-graph", "title": "Leistungsverlauf · 24 Stunden", "hours_to_show": 24, "entities": ["sensor.ek_pv_power", "sensor.ek_house_power", "sensor.ek_grid_power", "sensor.ek_battery_power"]},
        {"type": "markdown", "content": "**Hinweis**  \\nNegative Netzleistung bedeutet Einspeisung. Die eigentliche PV-Überschussregelung für Wallbox und Wärmepumpe übernimmt evcc."},
    ]},
    {"title": "Verbraucher", "path": "verbraucher", "icon": "mdi:ev-station", "cards": [
        {"type": "markdown", "content": "# Flexible Verbraucher\\nWallbox und Wärmepumpe werden von **evcc** optimiert. EnergyKit stellt die Geräte bereit und zeigt den Anlagenzustand."},
        {"type": "entities", "title": "Energiestatus", "entities": ["sensor.ek_pv_power", "sensor.ek_grid_power", "sensor.ek_battery_soc"]},
        {"type": "markdown", "content": "### Bedienung\\nLademodus, Ladepläne und Heizstrategien werden in evcc verwaltet. Dadurch gibt es nur **eine** Energiemanagement-Engine und keine konkurrierenden Home-Assistant-Automationen."},
    ]},
    {"title": "Anlage", "path": "anlage", "icon": "mdi:cog-outline", "cards": [
        {"type": "markdown", "content": "# Anlagenstatus\\nEnergyKit verwendet stabile `sensor.ek_*`-Entitäten. Herstellerwechsel oder Integrationsupdates verändern damit nicht automatisch das Kundendashboard."},
        {"type": "entities", "title": "EnergyKit Messpunkte", "entities": ["sensor.ek_pv_power", "sensor.ek_house_power", "sensor.ek_grid_power", "sensor.ek_battery_power", "sensor.ek_battery_soc"]},
    ]},
]}


@app.post("/api/dashboard")
async def dashboard(request: Request):
    state = ensure_access(request)
    if state.get("restart_required") and not state["simulation"]:
        raise HTTPException(409, "Home Assistant muss vor dem Dashboard zuerst neu gestartet werden")
    (DATA / "dashboard.json").write_text(json.dumps(DASHBOARD, indent=2))
    try:
        await core_ws([{"type": "lovelace/dashboards/create", "url_path": "energykit-dashboard", "title": "EnergyKit", "icon": "mdi:home-lightning-bolt", "show_in_sidebar": True, "require_admin": False}])
    except Exception:
        pass
    await core_ws([{"type": "lovelace/config/save", "url_path": "energykit-dashboard", "config": DASHBOARD}])
    state["dashboard"]["installed"] = True
    save_state(state)
    return {"ok": True}


@app.get("/api/diagnostics")
async def diagnostics(request: Request):
    state = ensure_access(request)
    result = {
        "supervisor_token": bool(TOKEN),
        "supervisor_token_source": ("SUPERVISOR_TOKEN" if os.environ.get("SUPERVISOR_TOKEN") else None),
        "homeassistant_config_mounted": HA.exists(),
        "addon_configs_mounted": ADDON_CONFIGS.exists(),
        "service_user": bool(state.get("service_user_id")),
        "simulation": state.get("simulation"),
        "components": state.get("components", {}),
    }
    bootstrap_status = HA / "energykit_bootstrap_status.json"
    if bootstrap_status.exists():
        try:
            result["first_boot_bootstrap"] = json.loads(bootstrap_status.read_text())
        except Exception as exc:
            result["first_boot_bootstrap"] = {"ok": False, "error": f"Statusdatei unlesbar: {exc}"}
    else:
        result["first_boot_bootstrap"] = {"ok": True, "stage": "status-file-not-present-or-cleaned"}
    try:
        info = await sup("GET", "/info")
        result["supervisor"] = {"ok": True, "version": info.get("supervisor"), "core": info.get("homeassistant"), "os": info.get("hassos"), "state": info.get("state")}
    except Exception as exc:
        result["supervisor"] = {"ok": False, "error": str(exc)}
    try:
        cfg = await core_rest("GET", "/config")
        result["core_api"] = {"ok": True, "version": cfg.get("version")}
    except Exception as exc:
        result["core_api"] = {"ok": False, "error": str(exc)}
    try:
        users = (await core_ws([{"type": "config/auth/list"}]))[0]
        result["admin_websocket"] = {"ok": True, "users": len(users or [])}
    except Exception as exc:
        result["admin_websocket"] = {"ok": False, "error": str(exc)}
    return result


@app.post("/api/checks")
async def checks(request: Request):
    state = ensure_access(request)
    result = await final_checks(state)
    state["last_checks"] = result
    save_state(state)
    REPORT_FILE.write_text(json.dumps(report_payload(state), indent=2, ensure_ascii=False))
    ok = sum(1 for x in result if x["ok"])
    total = len(result)
    return {"checks": result, "ok": ok, "total": total, "percent": int(ok * 100 / total) if total else 0}


@app.get("/api/report.json")
async def report_json(request: Request):
    state = ensure_access(request)
    payload = report_payload(state)
    return JSONResponse(payload, headers={"Content-Disposition": f"attachment; filename=energykit-{state['customer'].get('installation_id') or 'report'}.json"})


@app.get("/api/report.html")
async def report_html(request: Request):
    state = ensure_access(request)
    payload = report_payload(state)
    rows = "".join(f"<tr><td>{h(x['name'])}</td><td>{'✓' if x['ok'] else '✕'}</td><td>{h(x.get('detail',''))}</td></tr>" for x in payload["checks"])
    body = f"""<!doctype html><meta charset='utf-8'><title>EnergyKit Übergabebericht</title><style>body{{font:14px system-ui;margin:40px;max-width:900px}}table{{border-collapse:collapse;width:100%}}td,th{{padding:9px;border-bottom:1px solid #ddd;text-align:left}}code{{background:#eee;padding:2px 5px}}</style><h1>EnergyKit Übergabebericht</h1><p><b>Anlage:</b> {h(payload['customer'].get('installation_id'))}<br><b>Kunde:</b> {h(payload['customer'].get('name'))}<br><b>Modus:</b> {h(payload['mode'])}<br><b>EnergyKit:</b> {APP_VERSION}</p><h2>Komponenten</h2><pre>{h(json.dumps(payload['components'],indent=2,ensure_ascii=False))}</pre><h2>Prüfung</h2><table><tr><th>Prüfung</th><th>Status</th><th>Details</th></tr>{rows}</table>"""
    return HTMLResponse(body, headers={"Content-Disposition": f"attachment; filename=energykit-{state['customer'].get('installation_id') or 'report'}.html"})


@app.post("/api/backup")
async def backup(request: Request):
    ensure_access(request)
    return await create_backup("EnergyKit Inbetriebnahme")


@app.post("/api/finish")
async def finish(request: Request):
    state = ensure_access(request)
    if not state.get("service_user_id"):
        raise HTTPException(400, "Zuerst Service-Benutzer anlegen")
    checks = await final_checks(state)
    state["last_checks"] = checks
    if any(not x["ok"] for x in checks):
        bad = ", ".join(x["name"] for x in checks if not x["ok"])
        raise HTTPException(400, f"Übergabe gesperrt. Fehlgeschlagen: {bad}")
    try:
        await create_backup("EnergyKit Übergabe")
    except Exception:
        pass
    state = load_state()
    state["setup_complete"] = True
    state["service_password"] = None
    save_state(state)
    REPORT_FILE.write_text(json.dumps(report_payload(state), indent=2, ensure_ascii=False))
    return {"ok": True}


@app.post("/api/update-all")
async def update_all(request: Request):
    state = ensure_access(request)
    results: dict[str, Any] = {}
    try:
        results["backup"] = await create_backup("EnergyKit pre-update")
    except Exception as exc:
        raise HTTPException(500, f"Backup vor Update fehlgeschlagen: {exc}")
    for name in ("mushroom", "visionos", "sigenergy", "deye", "bridge"):
        try:
            results[name] = await install_component_impl(name, state)
        except Exception as exc:
            results[name] = {"error": str(exc)}
    return results


@app.post("/api/recovery/reset")
async def recovery_reset(request: Request):
    state = ensure_access(request)
    # preserve service identity, all hardware config and components; only reopen setup UI.
    state["setup_complete"] = False
    save_state(state)
    return {"ok": True}
