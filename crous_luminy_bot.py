#!/usr/bin/env python3
"""
Alerte Discord dès qu'un logement CROUS apparaît à Luminy (Marseille).

Fonctionnement :
  1. Interroge le moteur de recherche de trouverunlogement.lescrous.fr
     sur une zone autour du campus de Luminy (API JSON du site).
  2. Si l'API ne répond pas comme prévu, lit les pages de résultats HTML
     et garde les annonces dont le texte contient "Luminy".
  3. Envoie un message sur Discord (webhook) pour chaque logement qui
     n'était pas visible au passage précédent — y compris un logement
     qui disparaît puis réapparaît (désistement).

Utilisation :
  python crous_luminy_bot.py --test   # envoie un message de test sur Discord
  python crous_luminy_bot.py --once   # une seule vérification (GitHub Actions, cron)
  python crous_luminy_bot.py          # tourne en boucle (PC, Raspberry, VPS)

Variables d'environnement :
  DISCORD_WEBHOOK_URL  (obligatoire) URL du webhook du salon Discord
  DISCORD_PING         qui notifier : "@everyone" (défaut) ou "<@TON_ID>"
  INTERVAL_SECONDS     délai entre deux vérifications en mode boucle (défaut 180)
  CROUS_TOOL_ID        identifiant de l'année sur le site (47 = 2026-2027)
  STATE_FILE           fichier mémorisant les annonces déjà vues (seen.json)
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://trouverunlogement.lescrous.fr"
TOOL_ID = int(os.getenv("CROUS_TOOL_ID", "47"))
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
PING = os.getenv("DISCORD_PING", "").strip() or "@everyone"
STATE_FILE = Path(os.getenv("STATE_FILE", "seen.json"))
INTERVAL = int(os.getenv("INTERVAL_SECONDS", "180"))

# Zone autour du campus de Luminy (13009 Marseille)
BOUNDS = {"north": 43.245, "south": 43.220, "west": 5.415, "east": 5.460}
# Mots-clés utilisés pour le filtrage texte (secours HTML)
KEYWORDS = ["luminy"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) alerte-logement-luminy",
    "Accept-Language": "fr-FR,fr;q=0.9",
}


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------
# Recherche
# --------------------------------------------------------------------------
def in_bounds(lat, lon):
    return (BOUNDS["south"] <= lat <= BOUNDS["north"]
            and BOUNDS["west"] <= lon <= BOUNDS["east"])


def search_api():
    """Recherche par zone géographique via l'API JSON utilisée par le site."""
    payload = {
        "idTool": TOOL_ID,
        "need_aggregation": False,
        "page": 1,
        "pageSize": 100,
        "sector": None,
        "occupationModes": [],
        "residence": None,
        "precision": 5,
        "equipment": [],
        "price": {"max": 10000000},
        "area": {"min": 0},
        "location": [
            {"lon": BOUNDS["west"], "lat": BOUNDS["north"]},
            {"lon": BOUNDS["east"], "lat": BOUNDS["south"]},
        ],
    }
    r = requests.post(f"{BASE}/api/fr/search/{TOOL_ID}", json=payload,
                      headers=HEADERS, timeout=25)
    r.raise_for_status()
    items = (r.json().get("results") or {}).get("items")
    if not isinstance(items, list):
        raise ValueError("format de réponse API inattendu")
    log(f"API : {len(items)} résultat(s) renvoyé(s) pour la zone")

    found = {}
    for it in items:
        res = it.get("residence") or {}
        loc = res.get("location") or {}
        titre = it.get("label") or res.get("label") or "Logement CROUS"
        adresse = res.get("address") or ""
        texte = f"{titre} {res.get('label', '')} {adresse}".lower()

        # Garde-fou : on ne garde que ce qui est vraiment à Luminy
        if "lat" in loc and "lon" in loc:
            if not in_bounds(float(loc["lat"]), float(loc["lon"])):
                continue
        elif not any(k in texte for k in KEYWORDS):
            continue

        details = [res.get("label", ""), adresse]
        area = it.get("area") or {}
        if area.get("min"):
            details.append(f"{area['min']} m²" if area.get("min") == area.get("max")
                           else f"{area.get('min')}–{area.get('max')} m²")
        found[str(it["id"])] = {
            "titre": titre,
            "details": " · ".join(d for d in details if d),
            "url": f"{BASE}/tools/{TOOL_ID}/accommodations/{it['id']}",
        }
    return found


def _card_for(link, acc_id):
    """Remonte jusqu'au plus grand bloc HTML qui ne concerne que cette annonce."""
    card = link
    for parent in link.parents:
        ids = {m.group(1) for a in parent.select('a[href*="/accommodations/"]')
               if (m := re.search(r"/accommodations/(\d+)", a.get("href", "")))}
        if ids != {acc_id}:
            break
        card = parent
    return card


def search_html(max_pages=60):
    """Secours : parcourt les pages de résultats et filtre par mot-clé."""
    found = {}
    for page in range(1, max_pages + 1):
        r = requests.get(f"{BASE}/tools/{TOOL_ID}/search", params={"page": page},
                         headers=HEADERS, timeout=25)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        links = soup.select('h3 a[href*="/accommodations/"]') \
            or soup.select('a[href*="/accommodations/"]')
        if not links:
            break
        for a in links:
            m = re.search(r"/accommodations/(\d+)", a.get("href", ""))
            if not m:
                continue
            acc_id = m.group(1)
            texte = " ".join(_card_for(a, acc_id).get_text(" ", strip=True).split())
            if any(k in texte.lower() for k in KEYWORDS):
                found[acc_id] = {
                    "titre": a.get_text(strip=True) or "Logement CROUS",
                    "details": texte[:400],
                    "url": urljoin(BASE, a["href"]),
                }
        if not soup.find("a", string=re.compile(r"Page suivante", re.I)):
            break
        time.sleep(1)  # on reste poli avec le site
    return found


def search():
    try:
        return search_api()
    except Exception as e:  # noqa: BLE001
        log(f"API indisponible ({e}), passage à la lecture HTML")
        return search_html()


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------
def discord_send(content, embed=None):
    if not WEBHOOK:
        sys.exit("DISCORD_WEBHOOK_URL n'est pas défini.")
    body = {
        "content": content,
        "allowed_mentions": {"parse": ["everyone", "users", "roles"]},
    }
    if embed:
        body["embeds"] = [embed]
    for _ in range(3):
        r = requests.post(WEBHOOK, json=body, timeout=15)
        if r.status_code == 429:  # limite de débit Discord
            time.sleep(float(r.json().get("retry_after", 2)))
            continue
        r.raise_for_status()
        return
    log("Discord : message non envoyé après 3 tentatives")


def notify(listing):
    discord_send(
        f"{PING} 🏠 **Logement CROUS dispo à Luminy !** Fonce le demander 👇",
        {
            "title": listing["titre"][:256],
            "url": listing["url"],
            "description": listing["details"][:1500],
            "color": 0x2ECC71,
            "footer": {"text": "trouverunlogement.lescrous.fr"},
            "timestamp": datetime.utcnow().isoformat() + "Z",
        },
    )


# --------------------------------------------------------------------------
# État (annonces visibles au passage précédent)
# --------------------------------------------------------------------------
def load_state():
    try:
        return set(json.loads(STATE_FILE.read_text()).get("visible", []))
    except (FileNotFoundError, ValueError):
        return set()


def save_state(ids):
    STATE_FILE.write_text(json.dumps({"visible": sorted(ids),
                                      "updated": datetime.now().isoformat()}))


def check_once():
    previous = load_state()
    try:
        current = search()
    except Exception as e:  # noqa: BLE001
        log(f"Erreur pendant la recherche : {e}")
        return None  # on ne touche pas à l'état pour ne pas rater d'annonce
    new_ids = [i for i in current if i not in previous]
    log(f"{len(current)} logement(s) visible(s) à Luminy, {len(new_ids)} nouveau(x)")
    for i in new_ids:
        notify(current[i])
        time.sleep(1)
    save_state(current.keys())
    return len(current)


def main():
    p = argparse.ArgumentParser(description="Alerte Discord logements CROUS Luminy")
    p.add_argument("--once", action="store_true", help="une seule vérification")
    p.add_argument("--test", action="store_true", help="envoie un message de test")
    args = p.parse_args()

    if args.test:
        discord_send(f"{PING} ✅ Le bot d'alerte CROUS Luminy est bien branché.")
        log("Message de test envoyé")
        return
    if args.once:
        count = check_once()
        # Lancement manuel depuis GitHub ("Run workflow") : bilan sur Discord
        if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
            if count is None:
                bilan = "⚠️ Vérification manuelle : erreur pendant la recherche, regarde les logs GitHub."
            else:
                bilan = (f"✅ Bot CROUS Luminy opérationnel — {count} logement(s) "
                         "actuellement en ligne à Luminy.")
            discord_send(bilan)
            log("Bilan envoyé sur Discord")
        return
    log(f"Surveillance lancée (toutes les {INTERVAL} s). Ctrl+C pour arrêter.")
    while True:
        check_once()
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
