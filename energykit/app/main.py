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

APP_VERSION = "0.4.1"
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
    "energy": {"vendor": None, "host": None, "port": 502, "configured": False},
    "mapping": {},
    "wallbox": {"vendor": "none", "host": "", "entity": "", "max_current": 16, "phases": 3},
    "heatpump": {"mode": "none", "switch": "", "power_threshold": 2500, "off_threshold": 800, "delay_min": 5},
    "evcc": {"installed": False, "slug": None, "configured": False, "config_path": None},
    "dashboard": {"installed": False},
    "last_backup": None,
    "last_checks": [],
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


def sup_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


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
    uri = "ws://supervisor/core/websocket"
    out: list[Any] = []
    async with websockets.connect(uri, open_timeout=15) as ws:
        hello = json.loads(await ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError("Home Assistant WebSocket nicht bereit")
        await ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
        auth = json.loads(await ws.recv())
        if auth.get("type") != "auth_ok":
            raise RuntimeError("Home Assistant WebSocket Auth fehlgeschlagen")
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
        save_state(state)
        return {"version": version}

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
            await download("https://github.com/Nezz/homeassistant-visionos-theme/archive/refs/heads/main.zip", zpath)
            with zipfile.ZipFile(zpath) as z:
                safe_extract(z, ex)
            target = HA / "themes/energykit-visionos"
            target.mkdir(parents=True, exist_ok=True)
            copied = 0
            for f in ex.rglob("*.yaml"):
                if "theme" in str(f.parent).lower() or "vision" in f.name.lower() or "ios" in f.name.lower():
                    shutil.copy2(f, target / f.name)
                    copied += 1
            if not copied:
                raise HTTPException(500, "Keine Theme-YAML gefunden")
            version = "main"
        else:
            raise HTTPException(404, "Unbekannte Komponente")
    state["components"][name] = {"version": version, "installed_at": now_iso()}
    save_state(state)
    return {"version": version}


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


def evcc_yaml(state: dict[str, Any]) -> str:
    wall = state["wallbox"]
    lines = [
        "network:",
        "  schema: http",
        "  host: 0.0.0.0",
        "  port: 7070",
        "interval: 30s",
        "meters:",
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
        "site:",
        "  title: EnergyKit",
        "  meters:",
        "    grid: grid",
        "    pv: [pv]",
        "    battery: [battery]",
    ]
    if wall.get("entity"):
        lines += [
            "chargers:",
            "  - name: wallbox",
            "    type: template",
            "    template: homeassistant-switch",
            "    uri: http://homeassistant.local:8123",
            f"    switch: {wall['entity']}",
            "loadpoints:",
            "  - title: Wallbox",
            "    charger: wallbox",
            "    mode: pv",
            f"    phases: {int(wall.get('phases') or 3)}",
            f"    mincurrent: 6",
            f"    maxcurrent: {int(wall.get('max_current') or 16)}",
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
    add("Wärmepumpe konfiguriert", state["heatpump"].get("mode") in {"none", "sg-ready", "modbus", "integration"})
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
:root{--bg:#f4f6f8;--panel:#fff;--text:#17222c;--muted:#6b7782;--line:#e3e8ec;--blue:#2678f3;--blue2:#eaf2ff;--green:#188b58;--green2:#e8f7ef;--warn:#a86a00;--warn2:#fff5d8;--red:#b42318;--red2:#fff0ee}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}.top{height:64px;background:#ffffffee;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 26px;position:sticky;top:0;z-index:5;backdrop-filter:blur(16px)}.brand{display:flex;gap:10px;align-items:center;font-weight:800}.logo{width:31px;height:31px;border-radius:9px;background:var(--blue);color:#fff;display:grid;place-items:center}.tag,.badge{padding:6px 10px;background:#eef1f4;border-radius:999px;color:#61707b;font-size:12px;font-weight:700}.badge.ok{background:var(--green2);color:var(--green)}.badge.bad{background:var(--red2);color:var(--red)}.layout{display:grid;grid-template-columns:235px 1fr;min-height:calc(100vh - 64px)}aside{background:#fff;border-right:1px solid var(--line);padding:24px 16px}.nav{display:block;padding:10px 12px;border-radius:11px;color:#63717c;margin:3px 0}.nav.active{background:var(--blue2);color:var(--blue);font-weight:750}main{padding:38px}section{max-width:980px;margin:auto}h1{font-size:32px;margin:0 0 8px}h2{font-size:20px;margin:28px 0 12px}h3{margin:0}p{color:var(--muted)}.lead{font-size:16px;margin:0 0 24px}.card{background:#fff;border:1px solid var(--line);border-radius:17px;padding:20px;margin:14px 0;box-shadow:0 12px 38px #1d2b3a0c}.row,.head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}.head{margin-bottom:12px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.span2{grid-column:1/-1}label{font-weight:700;color:#46535e}input,select{width:100%;margin-top:6px;padding:11px 12px;border:1px solid #d4dce2;border-radius:10px;background:#fff}.btn{border:0;border-radius:10px;padding:10px 14px;background:var(--blue);color:#fff;font-weight:750;cursor:pointer}.btn.ghost{background:#fff;color:#384650;border:1px solid var(--line)}.btn.warn{background:#9a6400}.actions{display:flex;gap:9px;justify-content:flex-end;margin-top:18px;flex-wrap:wrap}.actions.left{justify-content:flex-start}.comp,.statusrow{display:flex;align-items:center;justify-content:space-between;padding:12px 0;border-top:1px solid var(--line);gap:10px}.comp:first-child,.statusrow:first-child{border-top:0}.note{background:#eef5ff;border:1px solid #d7e6fb;border-radius:13px;padding:14px 16px;color:#425365;margin:14px 0}.warnbox{background:var(--warn2);border:1px solid #f0dda5;border-radius:13px;padding:14px 16px}.success{background:var(--green2);color:var(--green);padding:13px 15px;border-radius:12px;font-weight:700}.metrics{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}.metric{background:#fff;border:1px solid var(--line);border-radius:13px;padding:14px}.metric span{display:block;color:var(--muted);font-size:12px;margin-bottom:6px}.devicegrid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.device{background:#fff;border:1px solid var(--line);border-radius:15px;padding:16px}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.hidden{display:none}.flowfield{margin:10px 0}.deny{max-width:500px;margin:100px auto;background:#fff;border:1px solid var(--line);border-radius:20px;padding:30px;text-align:center}.progress{height:8px;background:#e8edf1;border-radius:99px;overflow:hidden}.progress>i{display:block;height:100%;background:var(--blue)}@media(max-width:800px){.layout{grid-template-columns:1fr}aside{display:none}main{padding:22px 14px}.grid,.devicegrid{grid-template-columns:1fr}.span2{grid-column:auto}.metrics{grid-template-columns:1fr 1fr}}
"""

JS = r"""
async function api(url,opt={}){const r=await fetch(url,opt);let d={};try{d=await r.json()}catch(e){}if(!r.ok)throw new Error(d.detail||d.message||'Fehler');return d}
function fd(data){const f=new FormData();Object.entries(data).forEach(([k,v])=>f.append(k,v));return f}
async function setMode(v){await api('api/mode',{method:'POST',body:fd({simulation:v})});location.reload()}
async function sys(){const d=await api('api/system');document.querySelector('#system').innerHTML=Object.entries(d).map(([k,v])=>`<div class="statusrow"><span>${k}</span><b>${typeof v==='object'?JSON.stringify(v):v}</b></div>`).join('')}
async function saveCustomer(){await api('api/customer',{method:'POST',body:fd({name:byId('cust_name').value,installation_id:byId('cust_id').value,location:byId('cust_location').value,installer:byId('cust_installer').value})});alert('Anlagendaten gespeichert')}
async function createService(){if(!confirm('Service-Benutzer in Home Assistant anlegen?'))return;const d=await api('api/service-user',{method:'POST'});byId('serviceResult').innerHTML=`<div class="success">Service-Benutzer angelegt</div><p>Benutzer: <b>${d.username}</b></p><p>Passwort: <b class="mono">${d.password}</b></p><button class="btn ghost" onclick='downloadText("energykit-service.txt",${JSON.stringify('EnergyKit Service\nBenutzer: ')}+${JSON.stringify(d.username)}+${JSON.stringify('\nPasswort: ')}+${JSON.stringify(d.password)})'>Zugangsdaten herunterladen</button>`}
function downloadText(name,text){const b=new Blob([text],{type:'text/plain'}),a=document.createElement('a');a.href=URL.createObjectURL(b);a.download=name;a.click();URL.revokeObjectURL(a.href)}
function byId(x){return document.getElementById(x)}
async function discover(){const box=byId('devices');box.innerHTML='Suche…';const d=await api('api/discover');box.innerHTML=d.devices.map(x=>`<div class="device"><span class="badge">${d.mode}</span><h3>${x.vendor}</h3><p>${x.model}</p><small>${x.host} · ${(x.ports||[]).join(', ')}</small><div class="actions left"><button class="btn ghost" onclick="useDevice('${x.vendor}','${x.host}',${x.port||x.ports?.[0]||502})">Verwenden</button></div></div>`).join('')||'<div class="note">Keine Geräte gefunden.</div>'}
function useDevice(v,h,p){byId('vendor').value=v.toLowerCase().includes('deye')?'deye':'sigenergy';byId('host').value=h;byId('port').value=p}
async function saveDevice(){await api('api/device',{method:'POST',body:fd({vendor:byId('vendor').value,host:byId('host').value,port:byId('port').value})});alert('Energiesystem gespeichert')}
async function installComp(n){const d=await api(`api/components/${n}`,{method:'POST'});alert(`${n}: ${d.version||'installiert'}`);location.reload()}
async function installBase(){for(const n of ['mushroom','visionos','bridge']){await installCompQuiet(n)}alert('Basis-Komponenten installiert');location.reload()}
async function installCompQuiet(n){return api(`api/components/${n}`,{method:'POST'})}
async function restartCore(){await api('api/core/restart',{method:'POST'});alert('Home Assistant wird neu gestartet')}
async function startFlow(domain){const d=await api(`api/flow/start/${domain}`,{method:'POST'});renderFlow(d)}
function renderFlow(d){const box=byId('flow');if(d.type==='create_entry'){box.innerHTML='<div class="success">Integration eingerichtet</div>';return}let html=`<h3>${d.step_id||'Konfiguration'}</h3>`;(d.data_schema||[]).forEach(f=>{const name=f.name,sel=f.selector||{};let input=`<input id="flow_${name}" ${f.required?'required':''} value="${f.default??''}">`;if(sel.select&&sel.select.options){input=`<select id="flow_${name}">${sel.select.options.map(o=>`<option value="${typeof o==='object'?o.value:o}">${typeof o==='object'?(o.label||o.value):o}</option>`).join('')}</select>`}else if(sel.number){input=`<input type="number" id="flow_${name}" value="${f.default??''}">`}else if(sel.boolean){input=`<select id="flow_${name}"><option value="true">Ja</option><option value="false">Nein</option></select>`}html+=`<div class="flowfield"><label>${name}${input}</label></div>`});html+=`<button class="btn" onclick="submitFlow('${d.flow_id}')">Weiter</button>`;box.innerHTML=html;box.dataset.fields=JSON.stringify((d.data_schema||[]).map(x=>x.name))}
async function submitFlow(id){const box=byId('flow'),names=JSON.parse(box.dataset.fields||'[]'),data={};names.forEach(n=>{let v=byId('flow_'+n).value;if(v==='true')v=true;if(v==='false')v=false;if(/^\d+(\.\d+)?$/.test(String(v)))v=Number(v);data[n]=v});renderFlow(await api(`api/flow/${id}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}))}
async function loadEntities(){const d=await api('api/entities');const opts='<option value="">-- auswählen --</option>'+d.entities.map(e=>`<option value="${e.entity_id}">${e.entity_id}${e.name?' · '+e.name:''}</option>`).join('');['pv','house','grid','battery_power','battery_soc'].forEach(k=>byId('map_'+k).innerHTML=opts)}
async function autoMap(){await loadEntities();const d=await api('api/mapping/auto');Object.entries(d.mapping).forEach(([k,v])=>{if(byId('map_'+k))byId('map_'+k).value=v.entity_id||''});byId('mapHint').textContent=Object.values(d.mapping).every(v=>v.confident)?'Automatische Zuordnung eindeutig.':'Einige Werte sind nicht eindeutig. Bitte kontrollieren.'}
async function saveMapping(){const data={};['pv','house','grid','battery_power','battery_soc'].forEach(k=>data[k]=byId('map_'+k).value);await api('api/mapping',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});alert('Mapping gespeichert. Home Assistant startet neu.')}
async function saveWallbox(){await api('api/wallbox',{method:'POST',body:fd({vendor:byId('wb_vendor').value,host:byId('wb_host').value,entity:byId('wb_entity').value,max_current:byId('wb_current').value,phases:byId('wb_phases').value})});alert('Wallbox gespeichert')}
async function saveHeatpump(){await api('api/heatpump',{method:'POST',body:fd({mode:byId('hp_mode').value,switch_entity:byId('hp_switch').value,power_threshold:byId('hp_on').value,off_threshold:byId('hp_off').value,delay_min:byId('hp_delay').value})});alert('Wärmepumpe gespeichert')}
async function installEvcc(){const d=await api('api/evcc/install',{method:'POST'});alert(d.message||'evcc installiert');location.reload()}
async function configureEvcc(){const d=await api('api/evcc/configure',{method:'POST'});alert('evcc.yaml erzeugt: '+d.path);location.reload()}
async function makeDashboard(){await api('api/dashboard',{method:'POST'});alert('EnergyKit Dashboard angelegt');location.reload()}
async function runChecks(){const d=await api('api/checks',{method:'POST'});byId('checks').innerHTML=d.checks.map(x=>`<div class="statusrow"><span>${x.name}<small>${x.detail||''}</small></span><span class="badge ${x.ok?'ok':'bad'}">${x.ok?'OK':'Fehler'}</span></div>`).join('');byId('checkSummary').innerHTML=`<div class="progress"><i style="width:${d.percent}%"></i></div><p>${d.ok}/${d.total} Prüfungen bestanden (${d.percent} %)</p>`}
async function backup(){const d=await api('api/backup',{method:'POST'});alert('Backup erstellt: '+(d.slug||d.job_id||'OK'))}
async function finish(){if(!confirm('Service-Zugangsdaten gespeichert und Abschlussprüfung durchgeführt?'))return;await api('api/finish',{method:'POST'});location.reload()}
async function updateAll(){const d=await api('api/update-all',{method:'POST'});alert('Komponenten-Update abgeschlossen');location.reload()}
async function resetSetup(){if(!confirm('EnergyKit wieder in den Inbetriebnahmemodus versetzen?'))return;await api('api/recovery/reset',{method:'POST'});location.reload()}
"""


def h(v: Any) -> str:
    return html.escape(str(v if v is not None else ""))


def select_option(value: str, current: str, label: str | None = None) -> str:
    return f"<option value='{h(value)}' {'selected' if value==current else ''}>{h(label or value)}</option>"


def page(state: dict[str, Any]) -> str:
    mode = "Simulation" if state["simulation"] else "Echtbetrieb"
    nav = ["Übersicht", "Anlage", "Energiesystem", "Komponenten", "Energie", "Laden", "Wärmepumpe", "Übergabe"] if not state["setup_complete"] else ["Übersicht", "Geräte", "Updates", "Diagnose", "Backups", "Recovery"]
    c = state["customer"]
    energy = state["energy"]
    wb = state["wallbox"]
    hp = state["heatpump"]
    content = f"<h1>{'Inbetriebnahme' if not state['setup_complete'] else 'EnergyKit Service'}</h1><p class='lead'>{h(c.get('name') or 'Neue EnergyKit-Anlage')} · {mode}</p>"

    if not state["setup_complete"]:
        content += f"""
<h2>1 · System & Betriebsart</h2><div class='card row'><div><h3>Modus</h3><p>Simulation erlaubt den kompletten Ablauf ohne Hardware.</p></div><div><button class='btn {'ghost' if not state['simulation'] else ''}' onclick='setMode(true)'>Simulation</button> <button class='btn {'ghost' if state['simulation'] else ''}' onclick='setMode(false)'>Echtbetrieb</button></div></div><div class='card'><div class='head'><h3>Systemstatus</h3><button class='btn ghost' onclick='sys()'>Prüfen</button></div><div id='system'></div></div>
<h2>2 · Anlage & Service</h2><div class='card grid'><label>Kunde<input id='cust_name' value='{h(c.get('name'))}'></label><label>Anlagen-ID<input id='cust_id' value='{h(c.get('installation_id'))}' placeholder='EK-2026-0001'></label><label>Standort<input id='cust_location' value='{h(c.get('location'))}'></label><label>Installateur<input id='cust_installer' value='{h(c.get('installer'))}'></label><div class='span2 actions'><button class='btn' onclick='saveCustomer()'>Anlagendaten speichern</button></div></div>
<div class='card'><div class='head'><div><h3>Service-Benutzer</h3><p>Nach der Übergabe ist nur dessen HA-User-ID für EnergyKit freigeschaltet.</p></div><span class='badge {'ok' if state.get('service_user_id') else ''}'>{'vorhanden' if state.get('service_user_id') else 'offen'}</span></div><div id='serviceResult'>"""
        if state.get("service_user_id"):
            content += f"<div class='success'>✓ {h(state['service_username'])} · <span class='mono'>{h(state['service_user_id'])}</span></div>"
            if state.get("service_password"):
                content += f"<p>Einmaliges Passwort: <b class='mono'>{h(state['service_password'])}</b></p><div class='actions left'><button class='btn ghost' onclick='downloadText(\"energykit-service.txt\",\"EnergyKit Service\\nBenutzer: {h(state['service_username'])}\\nPasswort: {h(state['service_password'])}\")'>Zugangsdaten herunterladen</button></div>"
        else:
            content += "<button class='btn' onclick='createService()'>Service-Benutzer anlegen</button>"
        content += f"""</div></div>
<h2>3 · Energiesystem</h2><div class='actions left'><button class='btn' onclick='discover()'>Geräte suchen</button></div><div id='devices' class='devicegrid'></div>
<div class='card grid'><label>Hersteller<select id='vendor'>{select_option('sigenergy',energy.get('vendor'),'Sigenergy')}{select_option('deye',energy.get('vendor'),'Deye')}</select></label><label>IP<input id='host' value='{h(energy.get('host'))}'></label><label>Port<input id='port' type='number' value='{h(energy.get('port') or 502)}'></label><div class='span2 actions'><button class='btn' onclick='saveDevice()'>Energiesystem speichern</button></div></div>
<h2>4 · Komponenten ohne HACS</h2><div class='card'><div class='actions left'><button class='btn' onclick='installBase()'>Basis automatisch installieren</button></div>
"""
        for name, label in [("mushroom","Mushroom Cards"),("visionos","VisionOS Theme"),("sigenergy","Sigenergy Integration"),("deye","Deye Integration"),("bridge","EnergyKit Bridge")]:
            ver = state["components"].get(name, {}).get("version", "nicht installiert")
            content += f"<div class='comp'><div><b>{label}</b><small>{h(ver)}</small></div><button class='btn ghost' onclick=\"installComp('{name}')\">Installieren / Update</button></div>"
        content += "</div><div class='actions left'><button class='btn ghost' onclick='restartCore()'>Home Assistant neu starten</button></div>"
        content += """
<h2>5 · Hersteller-Integration</h2><div class='card'><div class='actions left'><button class='btn ghost' onclick="startFlow('sigen')">Sigenergy Config-Flow</button><button class='btn ghost' onclick="startFlow('deye_modbus')">Deye Config-Flow</button></div><div id='flow' class='note'>Im Simulationsmodus ist dieser Schritt optional. Im Echtbetrieb rendert EnergyKit den originalen HA Config-Flow dynamisch.</div></div>
<h2>6 · EnergyKit Messwerte</h2><div class='card'><div class='actions left'><button class='btn ghost' onclick='autoMap()'>Automatisch zuordnen</button><button class='btn ghost' onclick='loadEntities()'>Entities neu laden</button></div><p id='mapHint' class='muted'></p><div class='grid'>
<label>PV<select id='map_pv'></select></label><label>Haus<select id='map_house'></select></label><label>Netz<select id='map_grid'></select></label><label>Batterieleistung<select id='map_battery_power'></select></label><label>Batterie SoC<select id='map_battery_soc'></select></label><div class='span2 actions'><button class='btn' onclick='saveMapping()'>Mapping übernehmen</button></div></div></div>
"""
        content += f"""
<h2>7 · Wallbox & evcc</h2><div class='card grid'><label>Wallbox<select id='wb_vendor'>{select_option('none',wb.get('vendor'),'Keine')}{select_option('go-e',wb.get('vendor'),'go-e')}{select_option('sigenergy',wb.get('vendor'),'Sigenergy')}{select_option('keba',wb.get('vendor'),'KEBA')}{select_option('easee',wb.get('vendor'),'Easee')}{select_option('ha-switch',wb.get('vendor'),'Home-Assistant Switch')}</select></label><label>IP / Host<input id='wb_host' value='{h(wb.get('host'))}'></label><label>Schalt-Entity<input id='wb_entity' value='{h(wb.get('entity'))}' placeholder='switch.wallbox_enable'></label><label>Maximalstrom<input id='wb_current' type='number' value='{h(wb.get('max_current'))}'></label><label>Phasen<select id='wb_phases'>{select_option('1',str(wb.get('phases')),'1-phasig')}{select_option('3',str(wb.get('phases')),'3-phasig')}</select></label><div class='span2 actions'><button class='btn' onclick='saveWallbox()'>Wallbox speichern</button></div></div>
<div class='card row'><div><h3>evcc</h3><p>Installiert: {h(state['evcc'].get('installed'))} · Konfiguriert: {h(state['evcc'].get('configured'))}</p></div><div><button class='btn ghost' onclick='installEvcc()'>Installieren</button> <button class='btn' onclick='configureEvcc()'>Konfiguration erzeugen</button></div></div>
<h2>8 · Wärmepumpe</h2><div class='card grid'><label>Anbindung<select id='hp_mode'>{select_option('none',hp.get('mode'),'Keine')}{select_option('sg-ready',hp.get('mode'),'SG-Ready')}{select_option('modbus',hp.get('mode'),'Modbus TCP')}{select_option('integration',hp.get('mode'),'HA-Integration')}</select></label><label>Switch / SG-Ready Entity<input id='hp_switch' value='{h(hp.get('switch'))}' placeholder='switch.sg_ready'></label><label>Aktiv ab PV-Überschuss (W)<input id='hp_on' type='number' value='{h(hp.get('power_threshold'))}'></label><label>Aus unter (W)<input id='hp_off' type='number' value='{h(hp.get('off_threshold'))}'></label><label>Verzögerung (min)<input id='hp_delay' type='number' value='{h(hp.get('delay_min'))}'></label><div class='span2 actions'><button class='btn' onclick='saveHeatpump()'>Wärmepumpe speichern</button></div></div>
<h2>9 · Oberfläche</h2><div class='card row'><div><h3>EnergyKit Dashboard</h3><p>Mushroom, normalisierte sensor.ek_* und Service-/Kundenansicht.</p></div><button class='btn' onclick='makeDashboard()'>{'Neu erzeugen' if state['dashboard'].get('installed') else 'Dashboard erzeugen'}</button></div>
<h2>10 · Abschlussprüfung & Übergabe</h2><div class='card'><div class='head'><div><h3>End-to-End Test</h3><p>Im Simulationsmodus kann der komplette Zyklus ohne Hardware bestanden werden.</p></div><button class='btn' onclick='runChecks()'>Prüfung starten</button></div><div id='checkSummary'></div><div id='checks'></div></div>
<div class='actions left'><a class='btn ghost' href='api/report.json'>Bericht JSON</a><a class='btn ghost' href='api/report.html'>Übergabebericht HTML</a><button class='btn ghost' onclick='backup()'>Abschluss-Backup</button></div>
<div class='warnbox'>Vor Übergabe das Service-Passwort speichern. Nach Abschluss wird das Klartextpasswort aus EnergyKit gelöscht und der Owner aus der App ausgesperrt.</div><div class='actions'><button class='btn' onclick='finish()'>Anlage übergeben</button></div>
"""
    else:
        checks_ok = sum(1 for x in state.get("last_checks", []) if x.get("ok"))
        checks_total = len(state.get("last_checks", []))
        content += f"""
<div class='metrics'><div class='metric'><span>EnergyKit</span><b>v{APP_VERSION}</b></div><div class='metric'><span>Modus</span><b>{mode}</b></div><div class='metric'><span>Energiesystem</span><b>{h(state['energy'].get('vendor') or '—')}</b></div><div class='metric'><span>evcc</span><b>{'bereit' if state['evcc'].get('configured') else 'prüfen'}</b></div><div class='metric'><span>Letzter Test</span><b>{checks_ok}/{checks_total}</b></div></div>
<h2>Systemstatus</h2><div class='card'><div class='head'><h3>Diagnose</h3><button class='btn ghost' onclick='sys()'>Aktualisieren</button></div><div id='system'></div></div>
<h2>Komponenten & Updates</h2><div class='card'>"""
        for name in ("mushroom","visionos","sigenergy","deye","bridge"):
            content += f"<div class='comp'><div><b>{h(name)}</b><small>{h(state['components'].get(name,{}).get('version','nicht installiert'))}</small></div><button class='btn ghost' onclick=\"installComp('{name}')\">Update</button></div>"
        content += "</div><div class='actions left'><button class='btn ghost' onclick='backup()'>Backup erstellen</button><button class='btn' onclick='updateAll()'>Alle Komponenten aktualisieren</button></div>"
        content += """
<h2>Service & Recovery</h2><div class='card'><div class='comp'><span>Übergabebericht</span><span><a class='btn ghost' href='api/report.html'>HTML</a> <a class='btn ghost' href='api/report.json'>JSON</a></span></div><div class='comp'><span>Letztes Supervisor-Backup</span><b>""" + h(state.get("last_backup") or "—") + """</b></div><div class='comp'><span>Inbetriebnahme erneut öffnen</span><button class='btn warn' onclick='resetSetup()'>Recovery-Modus</button></div></div>
<div class='note'>Recovery setzt nur EnergyKit in den Setup-Modus zurück. Es löscht weder Home Assistant noch Geräteintegrationen.</div>
"""

    return f"""<!doctype html><html lang='de'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>EnergyKit</title><style>{CSS}</style></head><body><div class='top'><div class='brand'><span class='logo'>E</span>EnergyKit</div><div><span class='tag'>v{APP_VERSION}</span> <span class='tag'>{mode}</span></div></div><div class='layout'><aside><b>ENERGYKIT</b>{''.join(f"<span class='nav {'active' if i==0 else ''}'>{n}</span>" for i,n in enumerate(nav))}</aside><main><section>{content}</section></main></div><script>{JS}</script></body></html>"""


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
        return {"username": state["service_username"], "password": state.get("service_password"), "user_id": state["service_user_id"]}
    password = secrets.token_urlsafe(22)
    created = (await core_ws([{
        "type": "config/auth/create",
        "name": "EnergyKit Service",
        "group_ids": ["system-admin"],
        "local_only": False,
    }]))[0]
    user_id = created.get("user", created).get("id")
    await core_ws([{
        "type": "config/auth_provider/homeassistant/create",
        "user_id": user_id,
        "username": state["service_username"],
        "password": password,
    }])
    state["service_user_id"] = user_id
    state["service_password"] = password
    save_state(state)
    return {"username": state["service_username"], "password": password, "user_id": user_id}


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


@app.post("/api/device")
async def save_device(request: Request, vendor: str = Form(...), host: str = Form(...), port: int = Form(502)):
    state = ensure_access(request)
    state["energy"] = {"vendor": vendor, "host": host.strip(), "port": port, "configured": state["simulation"]}
    save_state(state)
    return {"ok": True}


@app.post("/api/components/{name}")
async def install_component(request: Request, name: str):
    state = ensure_access(request)
    return await install_component_impl(name, state)


@app.post("/api/core/restart")
async def restart_core(request: Request):
    ensure_access(request)
    await sup("POST", "/core/restart", json={})
    return {"ok": True}


@app.post("/api/flow/start/{domain}")
async def flow_start(request: Request, domain: str):
    state = ensure_access(request)
    if state["simulation"]:
        return {"type": "create_entry", "title": f"Simulation {domain}", "result": {}}
    return await core_rest("POST", "/config/config_entries/flow", json={"handler": domain, "show_advanced_options": True})


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
async def wallbox(request: Request, vendor: str = Form(...), host: str = Form(""), entity: str = Form(""), max_current: int = Form(16), phases: int = Form(3)):
    state = ensure_access(request)
    state["wallbox"] = {"vendor": vendor, "host": host.strip(), "entity": entity.strip(), "max_current": max_current, "phases": phases}
    if state["simulation"] and vendor != "none" and not state["wallbox"]["entity"]:
        state["wallbox"]["entity"] = "switch.wallbox_enable"
    save_state(state)
    return {"ok": True}


@app.post("/api/heatpump")
async def heatpump(request: Request, mode: str = Form(...), switch_entity: str = Form(""), power_threshold: int = Form(2500), off_threshold: int = Form(800), delay_min: int = Form(5)):
    state = ensure_access(request)
    state["heatpump"] = {"mode": mode, "switch": switch_entity.strip(), "power_threshold": power_threshold, "off_threshold": off_threshold, "delay_min": delay_min}
    if state["simulation"] and mode == "sg-ready" and not state["heatpump"]["switch"]:
        state["heatpump"]["switch"] = "switch.sg_ready"
    save_state(state)
    if state.get("mapping"):
        write_bridge_mapping(state)
        if "bridge" in state.get("components", {}):
            try:
                await sup("POST", "/core/restart", json={})
            except Exception:
                pass
    return {"ok": True}


@app.post("/api/evcc/install")
async def evcc_install(request: Request):
    state = ensure_access(request)
    repo = "https://github.com/evcc-io/hassio-addon"
    try:
        await sup("POST", "/store/repositories", json={"repository": repo})
    except HTTPException as exc:
        if exc.status_code not in (400, 409):
            raise
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
    {"title": "Energie", "path": "energie", "icon": "mdi:home-lightning-bolt", "cards": [
        {"type": "custom:mushroom-title-card", "title": "EnergyKit", "subtitle": "Energieübersicht"},
        {"type": "grid", "columns": 2, "square": False, "cards": [
            {"type": "custom:mushroom-entity-card", "entity": "sensor.ek_pv_power", "name": "PV", "icon": "mdi:solar-power"},
            {"type": "custom:mushroom-entity-card", "entity": "sensor.ek_house_power", "name": "Haus", "icon": "mdi:home-lightning-bolt"},
            {"type": "custom:mushroom-entity-card", "entity": "sensor.ek_grid_power", "name": "Netz", "icon": "mdi:transmission-tower"},
            {"type": "custom:mushroom-entity-card", "entity": "sensor.ek_battery_soc", "name": "Batterie", "icon": "mdi:battery"},
        ]},
        {"type": "history-graph", "title": "Leistung", "hours_to_show": 24, "entities": ["sensor.ek_pv_power", "sensor.ek_house_power", "sensor.ek_grid_power", "sensor.ek_battery_power"]},
    ]},
    {"title": "Steuerung", "path": "steuerung", "icon": "mdi:tune", "cards": [
        {"type": "custom:mushroom-title-card", "title": "Steuerung", "subtitle": "Wallbox und Wärmepumpe"},
    ]},
]}


@app.post("/api/dashboard")
async def dashboard(request: Request):
    state = ensure_access(request)
    (DATA / "dashboard.json").write_text(json.dumps(DASHBOARD, indent=2))
    try:
        resources = (await core_ws([{"type": "lovelace/resources"}]))[0] or []
    except Exception:
        resources = []
    if not any(r.get("url") == "/local/mushroom.js" for r in resources):
        try:
            await core_ws([{"type": "lovelace/resources/create", "res_type": "module", "url": "/local/mushroom.js"}])
        except Exception:
            pass
    try:
        await core_ws([{"type": "lovelace/dashboards/create", "url_path": "energykit", "title": "EnergyKit", "icon": "mdi:home-lightning-bolt", "show_in_sidebar": True, "require_admin": False}])
    except Exception:
        pass
    await core_ws([{"type": "lovelace/config/save", "url_path": "energykit", "config": DASHBOARD}])
    state["dashboard"]["installed"] = True
    save_state(state)
    return {"ok": True}


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
