#!/usr/bin/env python3
"""Monitor de novedades de My Hero Ultra Rumble -> avisos en Discord.

Fuentes:
  - ultrarumble.com (inicio): parches, gachas, eventos y personajes nuevos
  - ultrarumble.com/notices: avisos del juego
  - feeds RSS/Atom opcionales (por ejemplo, las noticias de Steam)

Uso:
  python monitor.py          # revisa y avisa si hay algo nuevo
  python monitor.py --test   # manda un mensaje de prueba a Discord

Variable de entorno necesaria: DISCORD_WEBHOOK_URL
Sin ella, el script funciona en "modo prueba" e imprime en pantalla.
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

BASE = "https://ultrarumble.com"
STATE_FILE = Path(__file__).with_name("state.json")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
MAX_SEEN = 3000

# Feeds extra opcionales. Ejemplo para Steam (necesitas el APPID del juego):
#   "https://store.steampowered.com/feeds/news/app/APPID/"
EXTRA_FEEDS = [
]

UA = "Mozilla/5.0 (compatible; mhur-monitor/1.0)"

COLORS = {
    "patch": 0xE74C3C,
    "gasha": 0xF1C40F,
    "event": 0x3498DB,
    "character": 0x2ECC71,
    "notice": 0x9B59B6,
    "feed": 0x95A5A6,
}
LABELS = {
    "patch": "Parche",
    "gasha": "Gacha",
    "event": "Evento",
    "character": "Personaje",
    "notice": "Aviso",
    "feed": "Noticia",
}


# ---------- utilidades ----------

def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def to_text(fragment):
    fragment = re.sub(r"(?is)<(script|style).*?</\1>", " ", fragment)
    fragment = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>|</h\d>", "\n", fragment)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    text = html.unescape(fragment)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*", "\n", text)
    return text.strip()


def one_line(text, limit=150):
    return re.sub(r"\s+", " ", text).strip()[:limit]


# ---------- fuentes ----------

LINK_RE = re.compile(
    r"""<a\b[^>]*href=["'](?:https://ultrarumble\.com)?(/(?:patch|gasha|event|character)/\d+)"""
    r"""(?:#[^"']*)?["'][^>]*>(.*?)</a>""",
    re.I | re.S,
)


def home_items():
    page = fetch(BASE + "/")
    items = {}
    for m in LINK_RE.finditer(page):
        path, inner = m.group(1), m.group(2)
        kind = path.split("/")[1]
        key = f"{kind}:{path}"
        title = one_line(to_text(inner))
        if key not in items:
            items[key] = {"kind": kind, "title": title or path, "url": BASE + path}
        elif title and items[key]["title"] == path:
            items[key]["title"] = title
    return items


DATE_RE = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")


def notice_items():
    page = fetch(BASE + "/notices")
    items = {}
    for part in re.split(r"(?i)(?=<h3\b)", page)[1:]:
        m = re.match(r"(?is)<h3\b[^>]*>(.*?)</h3>(.*)", part)
        if not m:
            continue
        title = one_line(to_text(m.group(1)), 200)
        body = to_text(m.group(2))
        dates = DATE_RE.findall(body)
        start = dates[0] if dates else ""
        if start:
            body = body[: body.find(start)].strip()
        if not title:
            continue
        key = f"notice:{title}|{start}"
        period = f"\n\n{dates[0]} -> {dates[1]} (JST)" if len(dates) >= 2 else ""
        items[key] = {
            "kind": "notice",
            "title": title,
            "url": BASE + "/notices",
            "body": body[:1700] + period,
        }
    return items


def feed_items(url):
    root = ET.fromstring(fetch(url))
    items = {}
    for node in root.iter():
        if node.tag.split("}")[-1] not in ("item", "entry"):
            continue
        title = link = guid = ""
        for c in node:
            t = c.tag.split("}")[-1]
            if t == "title":
                title = (c.text or "").strip()
            elif t == "link":
                link = (c.text or c.attrib.get("href", "")).strip()
            elif t in ("guid", "id"):
                guid = (c.text or "").strip()
        key = "feed:" + (guid or link or title)
        items[key] = {"kind": "feed", "title": title, "url": link}
    return items


def enrich(item):
    """Para elementos nuevos, abre su página y saca un título limpio (y el texto del parche)."""
    if item["kind"] not in ("patch", "gasha", "event", "character"):
        return
    try:
        page = fetch(item["url"])
        t = re.search(r"(?is)<title>(.*?)</title>", page)
        if t:
            title = one_line(to_text(t.group(1)), 200)
            title = re.sub(r"\s*[-|]\s*My Hero Ultra Rumble.*$", "", title).strip()
            if title:
                item["title"] = title
        if item["kind"] == "patch":
            h = re.search(r"(?is)<h1\b.*", page)
            item["body"] = to_text(h.group(0) if h else page)[:1800]
    except Exception as e:  # noqa: BLE001
        print(f"[aviso] no se pudo abrir {item['url']}: {e}", file=sys.stderr)


# ---------- Discord ----------

def post_discord(payload):
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "DiscordBot (mhur-monitor, 1.0)"}
    for attempt in range(3):
        req = urllib.request.Request(WEBHOOK, data=data, headers=headers)
        try:
            urllib.request.urlopen(req, timeout=30).read()
            return
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                try:
                    wait = float(json.loads(e.read()).get("retry_after", 2))
                except Exception:  # noqa: BLE001
                    wait = 2
                time.sleep(wait + 0.5)
                continue
            raise


def make_embed(item):
    embed = {
        "title": f"{LABELS[item['kind']]}: {item['title']}"[:256],
        "color": COLORS[item["kind"]],
    }
    if item.get("url"):
        embed["url"] = item["url"]
    if item.get("body"):
        embed["description"] = item["body"][:1900]
    return embed


def send(items):
    embeds = [make_embed(i) for i in items]
    if not WEBHOOK:
        for e in embeds:
            print("[modo prueba]", json.dumps(e, ensure_ascii=False))
        return
    for i in range(0, len(embeds), 10):
        post_discord({"embeds": embeds[i : i + 10]})
        time.sleep(1)


def send_text(text):
    if not WEBHOOK:
        print("[modo prueba]", text)
        return
    post_discord({"content": text})


# ---------- estado ----------

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return None


def save_state(seen):
    STATE_FILE.write_text(
        json.dumps({"seen": seen[-MAX_SEEN:]}, ensure_ascii=False, indent=1), encoding="utf-8"
    )


# ---------- principal ----------

def main():
    if "--test" in sys.argv:
        send_text("✅ Prueba: el monitor de My Hero Ultra Rumble está conectado a este canal.")
        return

    sources = [("inicio", home_items), ("avisos", notice_items)]
    sources += [(f"feed {u}", (lambda u=u: feed_items(u))) for u in EXTRA_FEEDS]

    current, ok = {}, 0
    for name, fn in sources:
        try:
            current.update(fn())
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"[error] fuente '{name}': {e}", file=sys.stderr)
    if ok == 0:
        sys.exit("Ninguna fuente respondió.")

    state = load_state()
    if state is None:
        # Primera ejecución: guardar lo que ya existe sin avisar de todo.
        save_state(list(current))
        send_text(f"✅ Monitor de My Hero Ultra Rumble activo. Vigilando {len(current)} elementos; "
                  "a partir de ahora solo te aviso de lo nuevo.")
        print(f"Línea base guardada: {len(current)} elementos.")
        return

    seen = state.get("seen", [])
    seen_set = set(seen)
    new = [(k, v) for k, v in current.items() if k not in seen_set]
    if not new:
        print("Sin novedades.")
        return

    for _, item in new:
        enrich(item)
    send([item for _, item in new])  # si falla, no se guarda y se reintenta después
    save_state(seen + [k for k, _ in new])
    print(f"Avisadas {len(new)} novedades.")


if __name__ == "__main__":
    main()
