#!/usr/bin/env python3
"""
Alerte Discord dès qu'un logement CROUS apparaît à Luminy (Marseille).

Fonctionnement :
  1. Récupère TOUTE l'offre publique de trouverunlogement.lescrous.fr
     (API JSON du site) et garde les logements situés à Luminy : position
     GPS dans la zone du campus, ou "Luminy" dans le nom ou l'adresse.
  2. Si l'API ne répond pas comme prévu, lit toutes les pages de résultats
     HTML et garde les annonces dont le texte contient "Luminy".
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
# Mots-clés : un logement est retenu si son nom ou son adresse en contient un
KEYWORDS = ["luminy", "13288"]  # 13288 = code postal cedex du campus

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


# Emprise de toute la France (métropole + Corse), comme le site par défaut
FRANCE = [{"lon": -9.9079, "lat": 51.7087}, {"lon": 14.3224, "lat": 40.5721}]


def _is_luminy(lat, lon, texte):
    """Vrai si le logement est à Luminy (coordonnées GPS OU nom/adresse)."""
    if lat is not None and lon is not None:
        try:
            if in_bounds(float(lat), float(lon)):
                return True
        except (TypeError, ValueError):
            pass
    return any(k in texte.lower() for k in KEYWORDS)


def _fmt_prix(v):
    if not isinstance(v, (int, float)) or v <= 0:
        return None
    v = v / 100 if v > 5000 else v  # l'API donne parfois les loyers en centimes
    return f"{v:.2f} €".replace(".", ",")


def search_api():
    """Récupère TOUTE l'offre France via l'API du site, puis filtre Luminy ici.

    On ne demande pas à l'API de filtrer la zone : si le format de la zone
    était mal compris par le site, on raterait des logements sans le savoir.
    Renvoie (logements_luminy, nombre_total_en_france).
    """
    found, total, page = {}, 0, 1
    while page <= 50:
        payload = {
            "idTool": TOOL_ID, "need_aggregation": False,
            "page": page, "pageSize": 100,
            "sector": None, "occupationModes": [], "residence": None,
            "precision": 4, "equipment": [],
            "price": {"max": 10000000}, "area": {"min": 0},
            "location": FRANCE,
        }
        r = requests.post(f"{BASE}/api/fr/search/{TOOL_ID}", json=payload,
                          headers=HEADERS, timeout=25)
        r.raise_for_status()
        results = r.json().get("results") or {}
        items = results.get("items")
        if not isinstance(items, list):
            raise ValueError("format de réponse API inattendu")
        total += len(items)

        for it in items:
            res = it.get("residence") or {}
            loc = res.get("location") or {}
            titre = it.get("label") or res.get("label") or "Logement CROUS"
            adresse = res.get("address") or ""
            texte = f"{titre} {res.get('label', '')} {adresse}"
            if not _is_luminy(loc.get("lat"), loc.get("lon"), texte):
                continue

            details = [res.get("label", ""), adresse]
            area = it.get("area") or {}
            if area.get("min"):
                details.append(f"{area['min']} m²" if area.get("min") == area.get("max")
                               else f"{area.get('min')}–{area.get('max')} m²")
            for mode in it.get("occupationModes") or []:
                rent = mode.get("rent") or {}
                prix = _fmt_prix(rent.get("min"))
                if prix:
                    genre = {"alone": "Individuel", "couple": "Couple",
                             "house_sharing": "Colocation"}.get(mode.get("type"), "")
                    details.append(f"{genre} : {prix}" if genre else prix)
            found[str(it["id"])] = {
                "titre": titre,
                "details": " · ".join(d for d in details if d),
                "url": f"{BASE}/tools/{TOOL_ID}/accommodations/{it['id']}",
            }

        if len(items) < 100:
            break
        page += 1
        time.sleep(1)
    log(f"API : {total} logement(s) en France, dont {len(found)} à Luminy")
    return found, total


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


def search_html(max_pages=150):
    """Secours : parcourt toutes les pages de résultats et filtre par mot-clé."""
    found, total = {}, 0
    for page in range(1, max_pages + 1):
        r = requests.get(f"{BASE}/tools/{TOOL_ID}/search", params={"page": page},
                         headers=HEADERS, timeout=25)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        links = soup.select('h3 a[href*="/accommodations/"]') \
            or soup.select('a[href*="/accommodations/"]')
        if not links:
            break
        total += len(links)
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
    log(f"HTML : {total} logement(s) en France, dont {len(found)} à Luminy")
    return found, total


def search():
    try:
        found, total = search_api()
        if total > 0:
            return found, total
        log("API : 0 logement en France, vérification par la lecture HTML")
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
    """Renvoie (nb_luminy, nb_france), ou None si la recherche a échoué."""
    previous = load_state()
    try:
        current, total = search()
    except Exception as e:  # noqa: BLE001
        log(f"Erreur pendant la recherche : {e}")
        return None  # on ne touche pas à l'état pour ne pas rater d'annonce
    new_ids = [i for i in current if i not in previous]
    log(f"{len(current)} logement(s) visible(s) à Luminy, {len(new_ids)} nouveau(x)")
    for i in new_ids:
        notify(current[i])
        time.sleep(1)
    save_state(current.keys())
    return len(current), total


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
        result = check_once()
        # Lancement manuel depuis GitHub ("Run workflow") : bilan sur Discord
        if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
            if result is None:
                bilan = ("⚠️ Vérification manuelle : erreur pendant la recherche, "
                         "regarde les logs GitHub.")
            else:
                luminy, france = result
                bilan = (f"✅ Bot CROUS Luminy opérationnel — le site affiche {france} "
                         f"logement(s) en France, dont {luminy} à Luminy.")
            discord_send(bilan)
            log("Bilan envoyé sur Discord")
        if result is None:
            sys.exit(1)  # croix rouge sur GitHub + mail : on sait que ça a planté
        return
    log(f"Surveillance lancée (toutes les {INTERVAL} s). Ctrl+C pour arrêter.")
    while True:
        check_once()
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
