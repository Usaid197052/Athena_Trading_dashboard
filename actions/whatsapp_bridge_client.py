"""
Local WhatsApp Baileys bridge client for Athena.

Spawns whatsapp_bridge/server.js on demand and talks HTTP on 127.0.0.1:8765.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import requests

from actions import whatsapp_contacts_book as contacts_book

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = int(os.environ.get("WA_BRIDGE_PORT", "8765"))
BASE_URL = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}"
_START_TIMEOUT = 20.0
_HTTP_TIMEOUT = 8.0

_proc: subprocess.Popen | None = None
_proc_lock = threading.Lock()
_cache_lock = threading.Lock()
_deps_lock = threading.Lock()


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def bridge_dir() -> Path:
    return _base_dir() / "whatsapp_bridge"


def auth_dir() -> Path:
    d = _base_dir() / "memory" / "whatsapp_baileys"
    d.mkdir(parents=True, exist_ok=True)
    return d


def qr_path() -> Path:
    return auth_dir() / "qr.png"


def contacts_cache_path() -> Path:
    return _base_dir() / "memory" / "whatsapp_contacts.json"


def _norm_key(name: str) -> str:
    import re
    s = (name or "").lower().replace("\xa0", " ")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _book_label(jid: str, fallback: str = "", is_group: bool = False) -> str:
    fb = (fallback or "").strip()
    if is_group or str(jid or "").endswith("@g.us"):
        return fb
    try:
        name = contacts_book.display_for_jid(jid, fb)
        return (name or fb).strip()
    except Exception:
        return fb


def _learn_ids(jid: str = "", lid: str = "", pn: str = "", display: str = "") -> None:
    try:
        if lid or pn:
            contacts_book.remember_lid_mapping(lid=lid or jid, phone_jid=pn or jid)
    except Exception:
        pass
    label = display
    for candidate in (pn, jid, lid):
        if candidate and not str(candidate).endswith("@g.us"):
            label = _book_label(candidate, label)
            if label and label != display:
                break
    if jid and (label or display):
        cache_set(label or display, jid, label or display)


def cache_get(name: str) -> dict[str, str] | None:
    key = _norm_key(name)
    if not key:
        return None
    path = contacts_cache_path()
    if not path.exists():
        return None
    try:
        with _cache_lock:
            data = json.loads(path.read_text(encoding="utf-8"))
        row = data.get(key)
        if isinstance(row, dict) and row.get("jid"):
            jid = str(row["jid"])
            raw_name = str(row.get("name") or name)
            return {
                "jid": jid,
                "name": _book_label(jid, raw_name, jid.endswith("@g.us")),
            }
    except Exception:
        return None
    return None


def cache_set(name: str, jid: str, display: str = "") -> None:
    key = _norm_key(name)
    if not key or not jid:
        return
    is_group = str(jid).endswith("@g.us")
    label = _book_label(jid, display or name, is_group) or display or name
    path = contacts_cache_path()
    with _cache_lock:
        data: dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data[key] = {"jid": jid, "name": label, "ts": time.time()}
        dkey = _norm_key(label)
        if dkey and dkey != key:
            data[dkey] = data[key]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _http(method: str, path: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", _HTTP_TIMEOUT)
    url = f"{BASE_URL}{path}"
    return requests.request(method, url, **kwargs)


def ping() -> dict[str, Any] | None:
    try:
        r = _http("GET", "/status", timeout=2.0)
        if r.ok:
            return r.json()
    except Exception:
        return None
    return None


def status() -> dict[str, Any]:
    data = ping()
    if data:
        return data
    return {"ok": False, "state": "disconnected"}


def is_connected() -> bool:
    return str(status().get("state") or "") == "connected"


def _find_node() -> str | None:
    """Prefer a Node binary shipped next to the app, then PATH."""
    candidates = [
        _base_dir() / "tools" / "node" / "node.exe",
        _base_dir() / "tools" / "node" / "node",
        bridge_dir() / "node.exe",
    ]
    for p in candidates:
        if p.is_file():
            return str(p)
    return shutil.which("node")


def has_node() -> bool:
    return bool(_find_node())


def _find_npm() -> str | None:
    node = _find_node()
    if node:
        d = Path(node).parent
        for name in ("npm.cmd", "npm.exe", "npm"):
            p = d / name
            if p.is_file():
                return str(p)
    return shutil.which("npm")


def _bridge_deps_ok() -> bool:
    nm = bridge_dir() / "node_modules"
    return (nm / "express").exists() or (nm / "@whiskeysockets").exists()


def ensure_dependencies() -> str | None:
    """Install whatsapp_bridge npm packages if missing. Returns error or None."""
    bd = bridge_dir()
    if not (bd / "package.json").exists():
        return f"WhatsApp bridge missing at {bd}"
    if _bridge_deps_ok():
        return None
    with _deps_lock:
        if _bridge_deps_ok():
            return None
        npm = _find_npm()
        if not npm:
            return (
                "WhatsApp packages are not installed and npm was not found. "
                "Install Node.js 18+ or use a build that includes whatsapp_bridge/node_modules."
            )
        env = os.environ.copy()
        node = _find_node()
        if node:
            env["PATH"] = str(Path(node).parent) + os.pathsep + env.get("PATH", "")
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        try:
            r = subprocess.run(
                [npm, "install", "--omit=dev"],
                cwd=str(bd),
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
                creationflags=creationflags,
            )
        except Exception as e:
            return f"Could not install WhatsApp bridge packages: {e}"
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()[-400:]
            return f"npm install failed for WhatsApp bridge. {err}"
        if not _bridge_deps_ok():
            return "npm install finished but WhatsApp packages are still missing."
    return None


def open_qr() -> str:
    """Open the WhatsApp QR image in the default viewer. Returns path or empty."""
    p = qr_path()
    if not p.exists():
        return ""
    try:
        if sys.platform == "win32":
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except Exception:
        pass
    return str(p)


def _spawn_bridge() -> str | None:
    """Start node server.js. Returns error string or None."""
    global _proc
    bd = bridge_dir()
    server = bd / "server.js"
    if not server.exists():
        return f"WhatsApp bridge missing at {server}"
    dep_err = ensure_dependencies()
    if dep_err:
        return dep_err
    node = _find_node()
    if not node:
        return "Node.js is not installed or not on PATH. Install Node 18+ to use WhatsApp."
    env = os.environ.copy()
    env["AUTH_DIR"] = str(auth_dir())
    env["WA_BRIDGE_PORT"] = str(BRIDGE_PORT)
    node_dir = str(Path(node).parent)
    env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    try:
        _proc = subprocess.Popen(
            [node, str(server)],
            cwd=str(bd),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except FileNotFoundError:
        return "Node.js is not installed or not on PATH. Install Node 18+ to use WhatsApp."
    except Exception as e:
        return f"Could not start WhatsApp bridge: {e}"
    return None


def ensure_bridge(timeout: float = _START_TIMEOUT) -> tuple[bool, str]:
    """
    Ensure bridge is reachable. Spawns if needed.
    Returns (ok, message). ok means HTTP is up (any state including qr).
    """
    global _proc
    cur = ping()
    if cur:
        st = cur.get("state", "")
        if st == "qr":
            qp = cur.get("qrPath") or str(qr_path())
            return True, f"WhatsApp bridge waiting for QR scan: {qp}"
        if st == "connected":
            return True, "WhatsApp bridge connected."
        return True, f"WhatsApp bridge state={st}."

    with _proc_lock:
        cur = ping()
        if cur:
            return True, f"WhatsApp bridge state={cur.get('state')}."
        if _proc is not None and _proc.poll() is None:
            pass
        else:
            err = _spawn_bridge()
            if err:
                return False, err

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        cur = ping()
        if cur:
            st = cur.get("state", "")
            if st == "qr":
                qp = cur.get("qrPath") or str(qr_path())
                return True, f"Scan WhatsApp QR to link Athena: {qp}"
            return True, f"WhatsApp bridge state={st}."
        time.sleep(0.35)

    return False, "WhatsApp bridge did not start in time. Is Node installed?"


def stop_bridge() -> None:
    """Stop the local Node bridge so the next ensure_bridge() loads current server.js."""
    global _proc
    with _proc_lock:
        if _proc is not None and _proc.poll() is None:
            try:
                _proc.terminate()
            except Exception:
                pass
            try:
                _proc.wait(timeout=4)
            except Exception:
                try:
                    _proc.kill()
                except Exception:
                    pass
        _proc = None
    try:
        import psutil
        for c in psutil.net_connections(kind="inet"):
            try:
                if (
                    c.status == "LISTEN"
                    and c.laddr
                    and int(c.laddr.port) == BRIDGE_PORT
                    and c.pid
                ):
                    psutil.Process(int(c.pid)).terminate()
            except Exception:
                continue
    except Exception:
        pass
    time.sleep(0.45)


def request_new_qr(timeout: float = 28.0) -> tuple[bool, str]:
    """Force a fresh pairing QR (logs out the current session if any)."""
    ok, msg = ensure_bridge(timeout=min(12.0, timeout))
    if not ok:
        return False, msg

    def _call_pair() -> requests.Response:
        return _http("POST", "/pair", json={}, timeout=max(8.0, timeout))

    try:
        r = _call_pair()
    except Exception as e:
        return False, f"Could not request a new QR: {e}"

    if r.status_code == 404:
        # Old bridge process without /pair — recycle it so current server.js loads
        stop_bridge()
        ok, msg = ensure_bridge(timeout=min(12.0, timeout))
        if not ok:
            return False, msg
        try:
            r = _call_pair()
        except Exception as e:
            return False, f"Could not request a new QR: {e}"

    data = r.json() if r.content else {}
    if r.status_code >= 400 and not data.get("ok"):
        return False, str(data.get("error") or r.text or "pair failed")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = status()
        if str(st.get("state") or "") == "qr" and (st.get("qrPng") or qr_path().exists()):
            return True, "Scan the QR with WhatsApp on your phone."
        time.sleep(0.3)
    return False, "QR was not generated in time. Try Start / Refresh QR again."


def resolve(name: str, *, kind: str = "any") -> dict[str, Any]:
    """Resolve contact/group name → {ok, jid, name, isGroup}.

    Order: JID/phone → Contacts/ folder (people) → JSON cache → Baileys.
    """
    import re

    kind = (kind or "any").lower().strip()
    if kind not in ("any", "group", "contact"):
        kind = "any"
    raw = (name or "").strip()
    if not raw:
        return {"ok": False, "error": "name required"}

    if "@" in raw and (
        raw.endswith("@s.whatsapp.net")
        or raw.endswith("@g.us")
        or raw.endswith("@lid")
    ):
        is_group = raw.endswith("@g.us")
        return {
            "ok": True,
            "jid": raw,
            "name": _book_label(raw, raw.split("@")[0], is_group),
            "cached": False,
            "isGroup": is_group,
        }

    digits = re.sub(r"\D", "", raw)
    if (
        kind != "group"
        and 8 <= len(digits) <= 15
        and re.fullmatch(r"[\d\s+\-()]+", raw)
    ):
        norm = contacts_book.normalize_phone(raw)
        jid = contacts_book.phone_to_jid(norm) if norm else f"{digits}@s.whatsapp.net"
        display = _book_label(jid, norm or digits)
        cache_set(raw, jid, display)
        return {"ok": True, "jid": jid, "name": display, "cached": False, "isGroup": False}

    # Local phone book (VCF/CSV) — people only, not groups
    if kind != "group":
        try:
            hit = contacts_book.lookup(raw)
        except Exception:
            hit = None
        if hit:
            if not hit.get("ok"):
                return {"ok": False, "error": str(hit.get("error") or "Ambiguous contact name.")}
            jid = str(hit.get("jid") or "")
            display = _book_label(jid, str(hit.get("name") or raw))
            if jid:
                cache_set(raw, jid, display)
                return {
                    "ok": True,
                    "jid": jid,
                    "name": display,
                    "cached": False,
                    "isGroup": False,
                    "source": "contacts_book",
                }

        cached = cache_get(name)
        if cached:
            jid = str(cached.get("jid") or "")
            if kind == "contact" and jid.endswith("@g.us"):
                pass
            else:
                is_group = jid.endswith("@g.us")
                return {
                    "ok": True,
                    "jid": jid,
                    "name": _book_label(jid, str(cached.get("name") or name), is_group),
                    "cached": True,
                    "isGroup": is_group,
                }

    ok, msg = ensure_bridge()
    if not ok:
        return {"ok": False, "error": msg}
    st = status()
    if st.get("state") != "connected":
        qp = st.get("qrPath") or (str(qr_path()) if qr_path().exists() else "")
        return {
            "ok": False,
            "error": (
                f"WhatsApp not linked yet (state={st.get('state')}). "
                f"Open and scan: {qp or 'memory/whatsapp_baileys/qr.png'}"
            ),
            "state": st.get("state"),
        }

    try:
        r = _http("POST", "/resolve", json={"name": name, "kind": kind})
        data = r.json() if r.content else {}
        if not r.ok or not data.get("ok"):
            return {"ok": False, "error": data.get("error") or r.text or "resolve failed"}
        jid = str(data["jid"])
        is_group = bool(data.get("isGroup")) or jid.endswith("@g.us")
        display = _book_label(jid, str(data.get("name") or name), is_group)
        cache_set(name, jid, display)
        return {
            "ok": True,
            "jid": jid,
            "name": display,
            "cached": False,
            "isGroup": is_group,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def send(
    jid: str,
    text: str = "",
    *,
    media_path: str = "",
    caption: str = "",
    media_type: str = "",
    ptt: bool = False,
) -> dict[str, Any]:
    ok, msg = ensure_bridge()
    if not ok:
        return {"ok": False, "error": msg}
    if status().get("state") != "connected":
        st = status()
        qp = st.get("qrPath") or str(qr_path())
        return {
            "ok": False,
            "error": f"WhatsApp not connected. Scan QR: {qp}",
            "state": st.get("state"),
        }
    payload: dict[str, Any] = {"jid": jid, "text": text or ""}
    if media_path:
        payload["mediaPath"] = media_path
        payload["caption"] = caption or text or ""
        if media_type:
            payload["mediaType"] = media_type
        if ptt:
            payload["ptt"] = True
    timeout = 60.0 if media_path else 15.0
    try:
        r = _http("POST", "/send", json=payload, timeout=timeout)
        data = r.json() if r.content else {}
        if not r.ok or not data.get("ok"):
            return {"ok": False, "error": data.get("error") or r.text or "send failed"}
        return {"ok": True, "jid": jid}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def chat_messages(jid: str, limit: int = 15) -> dict[str, Any]:
    ok, msg = ensure_bridge()
    if not ok:
        return {"ok": False, "error": msg, "messages": []}
    if status().get("state") != "connected":
        st = status()
        return {
            "ok": False,
            "error": f"WhatsApp not connected (state={st.get('state')}).",
            "messages": [],
        }
    try:
        from urllib.parse import quote
        r = _http("GET", f"/messages?jid={quote(jid, safe='')}&limit={int(limit)}", timeout=20.0)
        data = r.json() if r.content else {}
        if not r.ok or not data.get("ok"):
            return {
                "ok": False,
                "error": data.get("error") or r.text or "messages failed",
                "messages": [],
            }
        rows = []
        for m in data.get("messages") or []:
            is_group = bool(m.get("isGroup"))
            sender = str(m.get("senderJid") or m.get("jid") or jid)
            name = "You" if m.get("fromMe") else _book_label(
                sender, str(m.get("name") or ""), is_group
            )
            rows.append({**m, "name": name})
        return {
            "ok": True,
            "jid": jid,
            "messages": rows,
            "limited": bool(data.get("limited")),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "messages": []}


def unread_chats() -> dict[str, Any]:
    ok, msg = ensure_bridge()
    if not ok:
        return {"ok": False, "error": msg, "chats": []}
    if status().get("state") != "connected":
        st = status()
        return {
            "ok": False,
            "error": f"WhatsApp not connected (state={st.get('state')}).",
            "chats": [],
        }
    try:
        r = _http("GET", "/chats?unread=1", timeout=8.0)
        data = r.json() if r.content else {}
        if not r.ok or not data.get("ok"):
            return {
                "ok": False,
                "error": data.get("error") or r.text or "chats failed",
                "chats": [],
            }
        rows = []
        for c in data.get("chats") or []:
            jid = str(c.get("jid") or "")
            is_group = bool(c.get("isGroup")) or jid.endswith("@g.us")
            rows.append({
                **c,
                "name": _book_label(jid, str(c.get("name") or ""), is_group),
            })
        return {"ok": True, "chats": rows}
    except Exception as e:
        return {"ok": False, "error": str(e), "chats": []}


def events_since(seq: int) -> dict[str, Any]:
    try:
        r = _http("GET", f"/events?since={int(seq)}", timeout=4.0)
        data = r.json() if r.content else {}
        if not r.ok:
            return {"ok": False, "events": [], "latest": seq, "reset": False}
        return {
            "ok": True,
            "events": data.get("events") or [],
            "latest": int(data.get("latest") or seq),
            "bootId": data.get("bootId") or "",
            "reset": bool(data.get("reset")),
        }
    except Exception:
        return {"ok": False, "events": [], "latest": seq, "reset": False}


def ack(ids: list[str]) -> None:
    if not ids:
        return
    try:
        _http("POST", "/ack", json={"ids": ids}, timeout=3.0)
    except Exception:
        pass
