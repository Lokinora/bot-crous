#!/usr/bin/env python3
"""
Alerte Discord dès qu'un logement CROUS apparaît à Luminy (Marseille).

Fonctionnement :
  1. Se connecte à trouverunlogement.lescrous.fr avec ton compte
     MesServices (si MSE_EMAIL / MSE_PASSWORD sont fournis) : connecté,
     le site montre environ 10 fois plus de logements que déconnecté.
     La session est gardée (chiffrée) d'un passage à l'autre pour ne pas
     se reconnecter toutes les 5 minutes.
  2. Parcourt toute l'offre France et garde les logements de Luminy :
     "Luminy" ou "13288" dans le nom/adresse, ou position GPS sur le campus.
  3. Envoie un message Discord pour chaque logement qui n'était pas
     visible au passage précédent (y compris après un désistement).
  Sans identifiants, ou si la connexion échoue, le bot se rabat sur
  l'offre publique (et te prévient une fois sur Discord).

Utilisation :
  python crous_luminy_bot.py --test   # message de test sur Discord
  python crous_luminy_bot.py --once   # une vérification (GitHub Actions)
  python crous_luminy_bot.py          # boucle (PC)

Variables d'environnement :
  DISCORD_WEBHOOK_URL  (obligatoire) webhook du salon Discord
  MSE_EMAIL            identifiant MesServices (email)
  MSE_PASSWORD         mot de passe MesServices
  DISCORD_PING         "@everyone" (défaut) ou "<@TON_ID>"
  INTERVAL_SECONDS     délai entre vérifications en mode boucle (180)
  CROUS_TOOL_ID        année sur le site (47 = 2026-2027)
  STATE_FILE           annonces déjà vues (seen.json)
  SESSION_FILE         session chiffrée (session.bin)
"""

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = os.getenv("CROUS_BASE", "https://trouverunlogement.lescrous.fr").rstrip("/")
TOOL_ID = int(os.getenv("CROUS_TOOL_ID", "47"))
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
PING = os.getenv("DISCORD_PING", "").strip() or "@everyone"
STATE_FILE = Path(os.getenv("STATE_FILE", "seen.json"))
SESSION_FILE = Path(os.getenv("SESSION_FILE", "session.bin"))
INTERVAL = int(os.getenv("INTERVAL_SECONDS", "180"))
MSE_EMAIL = os.getenv("MSE_EMAIL", "").strip()
MSE_PASSWORD = os.getenv("MSE_PASSWORD", "")

# Zone du campus de Luminy (13009 Marseille)
BOUNDS = {"north": 43.245, "south": 43.220, "west": 5.415, "east": 5.460}
# Un logement est retenu si son nom ou son adresse contient un de ces mots
KEYWORDS = ["luminy", "13288"]  # 13288 = code postal cedex du campus

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"}


class LoginError(Exception):
    pass


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def in_bounds(lat, lon):
    return (BOUNDS["south"] <= lat <= BOUNDS["north"]
            and BOUNDS["west"] <= lon <= BOUNDS["east"])


def _is_luminy(lat, lon, texte):
    if lat is not None and lon is not None:
        try:
            if in_bounds(float(lat), float(lon)):
                return True
        except (TypeError, ValueError):
            pass
    return any(k in texte.lower() for k in KEYWORDS)


# --------------------------------------------------------------------------
# Lecture des pages de résultats (HTML)
# --------------------------------------------------------------------------
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


def count_from_text(texte):
    """ "330 logements trouvés" -> 330 ; "Aucun logement" -> 0 ; sinon None."""
    texte = " ".join(texte.split())
    m = re.search(r"(?<![\d\u2011-])(\d{1,3}(?:[\s\u202f\u00a0]\d{3})+|\d+)"
                  r"\s+logements?\s+trouv", texte, re.I)
    if m:
        return int(re.sub(r"\D", "", m.group(1)))
    if re.search(r"aucun logement", texte, re.I):
        return 0
    return None


def parse_total(html):
    soup = BeautifulSoup(html, "html.parser")
    for h in soup.find_all(["h1", "h2", "h3", "p", "span"]):
        n = count_from_text(h.get_text(" "))
        if n is not None:
            return n
    return count_from_text(soup.get_text(" "))


def scan_html(fetch, max_pages=150):
    """Parcourt toutes les pages de résultats. fetch(page) -> html."""
    found, count, total = {}, 0, None
    for page in range(1, max_pages + 1):
        html = fetch(page)
        soup = BeautifulSoup(html, "html.parser")
        if page == 1:
            total = parse_total(html)
        links = soup.select('h3 a[href*="/accommodations/"]') \
            or soup.select('a[href*="/accommodations/"]')
        if not links:
            break
        count += len(links)
        for a in links:
            m = re.search(r"/accommodations/(\d+)", a.get("href", ""))
            if not m:
                continue
            acc_id = m.group(1)
            texte = " ".join(_card_for(a, acc_id).get_text(" ", strip=True).split())
            if _is_luminy(None, None, texte):
                found[acc_id] = {
                    "titre": a.get_text(strip=True) or "Logement CROUS",
                    "details": texte[:400],
                    "url": urljoin(BASE + "/", a["href"]),
                }
        if not soup.find("a", string=re.compile(r"Page suivante", re.I)):
            break
        time.sleep(0.5)
    return found, total if total is not None else count


def search_url(page):
    return f"{BASE}/tools/{TOOL_ID}/search?page={page}"


# --------------------------------------------------------------------------
# Offre publique (sans connexion)
# --------------------------------------------------------------------------
FRANCE = [{"lon": -9.9079, "lat": 51.7087}, {"lon": 14.3224, "lat": 40.5721}]


def _fmt_prix(v):
    if not isinstance(v, (int, float)) or v <= 0:
        return None
    v = v / 100 if v > 5000 else v  # loyers parfois donnés en centimes
    return f"{v:.2f} €".replace(".", ",")


def parse_items(items, found):
    """Ajoute à `found` les annonces de l'API situées à Luminy."""
    for it in items:
        res = it.get("residence") or {}
        loc = res.get("location") or {}
        titre = it.get("label") or res.get("label") or "Logement CROUS"
        adresse = res.get("address") or ""
        if not _is_luminy(loc.get("lat"), loc.get("lon"),
                          f"{titre} {res.get('label', '')} {adresse}"):
            continue
        details = [res.get("label", ""), adresse]
        area = it.get("area") or {}
        if area.get("min"):
            details.append(f"{area['min']} m²")
        for mode in it.get("occupationModes") or []:
            prix = _fmt_prix((mode.get("rent") or {}).get("min"))
            if prix:
                genre = {"alone": "Individuel", "couple": "Couple",
                         "house_sharing": "Colocation"}.get(mode.get("type"), "")
                details.append(f"{genre} : {prix}" if genre else prix)
        found[str(it["id"])] = {
            "titre": titre,
            "details": " · ".join(d for d in details if d),
            "url": f"{BASE}/tools/{TOOL_ID}/accommodations/{it['id']}",
        }


def search_api():
    found, total, page = {}, 0, 1
    while page <= 50:
        payload = {
            "idTool": TOOL_ID, "need_aggregation": False,
            "page": page, "pageSize": 100,
            "sector": None, "occupationModes": [], "residence": None,
            "precision": 4, "equipment": [], "adaptedPmr": False,
            "price": {"max": 10000000}, "area": {"min": 0},
            "location": FRANCE, "toolMechanism": "residual",
        }
        r = requests.post(f"{BASE}/api/fr/search/{TOOL_ID}", json=payload,
                          headers=HEADERS, timeout=25)
        r.raise_for_status()
        items = (r.json().get("results") or {}).get("items")
        if not isinstance(items, list):
            raise ValueError("format de réponse API inattendu")
        total += len(items)
        parse_items(items, found)
        if len(items) < 100:
            break
        page += 1
        time.sleep(1)
    return found, total


def search_public():
    try:
        found, total = search_api()
        if total > 0:
            log(f"Offre publique (API) : {total} logement(s), dont {len(found)} à Luminy")
            return found, total
    except Exception as e:  # noqa: BLE001
        log(f"API publique indisponible ({e})")

    def fetch(page):
        r = requests.get(search_url(page), headers=HEADERS, timeout=25)
        r.raise_for_status()
        return r.text

    found, total = scan_html(fetch)
    log(f"Offre publique (pages) : {total} logement(s), dont {len(found)} à Luminy")
    return found, total


# --------------------------------------------------------------------------
# Offre connectée (navigateur piloté + compte MesServices)
# --------------------------------------------------------------------------
def _fernet():
    from cryptography.fernet import Fernet
    key = hashlib.sha256(f"crous-luminy:{MSE_EMAIL}:{MSE_PASSWORD}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def load_session():
    try:
        return json.loads(_fernet().decrypt(SESSION_FILE.read_bytes()))
    except Exception:  # noqa: BLE001  (absent, périmé ou autre mot de passe)
        return None


def save_session(state):
    SESSION_FILE.write_bytes(_fernet().encrypt(json.dumps(state).encode()))


def _describe(page):
    """Décrit la page sans rien d'identifiant (les logs GitHub sont publics)."""
    u = urlparse(page.url)
    champs, boutons = [], []
    try:
        for el in page.locator("input:visible").all()[:10]:
            champs.append(el.get_attribute("name") or el.get_attribute("type") or "?")
        for el in page.locator("button:visible, a.fr-btn:visible, "
                               "input[type=submit]:visible").all()[:10]:
            boutons.append((el.inner_text() or el.get_attribute("value") or "")
                           .strip()[:30])
    except Exception:  # noqa: BLE001
        pass
    return f"{u.netloc}{u.path} | champs={champs} | boutons={boutons}"


def _cases_a_cocher(page):
    """Coche seulement "se souvenir de moi" / "rester connecté".

    Une case inconnue peut être un piège anti-robot : on n'y touche pas.
    """
    for case in page.locator("input[type=checkbox]").all()[:5]:
        try:
            label = ""
            cid = case.get_attribute("id")
            if cid and page.locator(f"label[for='{cid}']").count():
                label = page.locator(f"label[for='{cid}']").first.inner_text()
            if not label:
                label = case.evaluate("e => (e.closest('label') || "
                                      "e.parentElement || {}).innerText || ''")
            label = " ".join(label.split())[:60]
            visible = case.is_visible()
            cocher = visible and bool(re.search(r"souvenir|rester|connect", label, re.I))
            log(f"Case à cocher : « {label} » visible={visible} cochée_par_le_bot={cocher}")
            if cocher and not case.is_checked():
                case.check()
        except Exception:  # noqa: BLE001
            pass


def _texte_page(page):
    """Début du texte affiché (page de connexion : aucune donnée perso)."""
    try:
        t = " ".join(page.locator("main, form, body").first.inner_text().split())
        return t[:300]
    except Exception:  # noqa: BLE001
        return ""


def _message_erreur(page):
    """Texte d'erreur affiché par MesServices (ex : "mot de passe incorrect")."""
    try:
        for el in page.locator(".error:visible, .alert:visible, .fr-alert:visible, "
                               ".fr-error-text:visible, [role=alert]:visible").all()[:3]:
            t = " ".join(el.inner_text().split())
            if t:
                return f"message du site : « {t[:150]} »"
    except Exception:  # noqa: BLE001
        pass
    return "pas de message d'erreur visible"


def _first_visible(page, selectors):
    for sel in selectors:
        loc = page.locator(sel)
        try:
            for i in range(min(loc.count(), 5)):
                if loc.nth(i).is_visible():
                    return loc.nth(i)
        except Exception:  # noqa: BLE001
            continue
    return None


def _settle(page):
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(800)


USER_FIELDS = ["input[name='login[login]']", "input[name='login[email]']",
               "input[name='login[username]']", "input[name=j_username]", "input[name=username]",
               "input[type=email]", "input#username", "input[name=login]",
               "input[name=email]", "input[autocomplete=username]"]
LOGIN_BUTTONS = [".loginapp-button",
                 "button:has-text('MesServices')", "a:has-text('MesServices')",
                 "button:has-text('Me connecter')", "a:has-text('Me connecter')",
                 "button:has-text('Se connecter')", "a:has-text('Se connecter')",
                 "button:has-text('Connexion')", "a:has-text('Connexion')",
                 "button:has-text('Continuer')"]
COOKIE_BUTTONS = ["button:has-text('Tout accepter')", "button:has-text('Accepter')",
                  "button:has-text('J’accepte')", "button:has-text(\"J'accepte\")"]


def _is_logged_in(ctx):
    """Connecté = le menu n'a plus de lien "Identification" vers MesServices.

    On ne regarde que les vrais liens <a> (pas le code JavaScript de la page,
    qui peut contenir "logout" même quand on n'est pas connecté).
    """
    html = ctx.request.get(search_url(1)).text()
    soup = BeautifulSoup(html, "html.parser")
    liens = [a.get("href", "").lower() for a in soup.find_all("a")]
    identification = any("/mse/discovery/connect" in h for h in liens)
    deconnexion = any(("logout" in h or "deconnexion" in h or "disconnect" in h)
                      for h in liens)
    log(f"Vérif connexion : lien Identification={identification}, "
        f"lien Déconnexion={deconnexion}")
    return deconnexion or not identification


def browser_login(ctx):
    page = ctx.new_page()
    page.goto(f"{BASE}/mse/discovery/connect", wait_until="domcontentloaded")
    home = urlparse(BASE).netloc
    tentatives = 0
    for _ in range(15):
        _settle(page)
        u = urlparse(page.url)
        if u.netloc == home and not u.path.startswith("/mse/"):
            break  # revenu sur le site du CROUS
        btn = _first_visible(page, COOKIE_BUTTONS)
        if btn:
            btn.click()
            _settle(page)
        pwd = _first_visible(page, ["input[type=password]"])
        if pwd:
            tentatives += 1
            if tentatives > 1:  # déjà soumis une fois : refusé
                raise LoginError("toujours sur le formulaire après envoi. "
                                 + _message_erreur(page) + " | " + _describe(page)
                                 + " | texte : " + _texte_page(page))
            user = _first_visible(page, USER_FIELDS)
            if not user:  # dernier recours : le champ texte du même formulaire
                user = _first_visible(page, [
                    "form:has(input[type=password]) input[type=text]",
                    "form:has(input[type=password]) input[type=email]"])
            log(f"Formulaire MesServices : champ identifiant trouvé={bool(user)}")
            if user:
                user.fill(MSE_EMAIL)
            pwd.fill(MSE_PASSWORD)
            _cases_a_cocher(page)
            bouton = _first_visible(page, [
                "button:has-text(\"S'identifier\")", "button[type=submit]",
                "input[type=submit]", "button:has-text('Connexion')"])
            envois = []
            page.on("response", lambda r: envois.append(r.status)
                    if r.request.method == "POST" else None)
            url_avant = page.url
            try:
                with page.expect_navigation(timeout=30000):
                    if bouton:
                        bouton.click()
                    else:
                        pwd.press("Enter")
            except Exception:  # noqa: BLE001  (pas de changement de page)
                pass
            # Laisse le temps aux redirections (MesServices -> CROUS)
            for _ in range(20):
                if page.url != url_avant or not _first_visible(
                        page, ["input[type=password]"]):
                    break
                page.wait_for_timeout(1000)
            _settle(page)
            log(f"Formulaire envoyé : réponses POST={envois[:3]}, "
                f"page={urlparse(page.url).netloc}{urlparse(page.url).path}")
            continue
        user = _first_visible(page, USER_FIELDS)
        if user:  # formulaire en deux temps : email puis mot de passe
            user.fill(MSE_EMAIL)
            user.press("Enter")
            continue
        btn = _first_visible(page, LOGIN_BUTTONS)
        if btn:
            btn.click()
            continue
        raise LoginError("page de connexion inattendue : " + _describe(page))
    else:
        raise LoginError("trop d'étapes : " + _describe(page))

    # Règlement de la recherche à valider (une fois par session)
    try:
        page.goto(f"{BASE}/tools/{TOOL_ID}/rules", wait_until="domcontentloaded")
        _settle(page)
        btn = _first_visible(page, ["button[name=searchSubmit]",
                                    "button:has-text('Passer à la recherche')"])
        if btn:
            btn.click()
            _settle(page)
    except Exception as e:  # noqa: BLE001
        log(f"Règlement non validé ({e.__class__.__name__}), on continue")
    page.close()


def _rendered_total(page):
    """Nombre affiché à l'écran ("330 logements trouvés"), après JavaScript."""
    for sel in ["h2:has-text('trouv')", "h1:has-text('trouv')",
                "*:has-text('logements trouv')", "*:has-text('Aucun logement')"]:
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 5)):
                n = count_from_text(loc.nth(i).inner_text())
                if n is not None:
                    return n
        except Exception:  # noqa: BLE001
            continue
    return count_from_text(page.content())


def search_logged_in():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(user_agent=UA, locale="fr-FR",
                                      timezone_id="Europe/Paris",
                                      storage_state=load_session())
            if _is_logged_in(ctx):
                log("Session précédente toujours valide")
            else:
                log("Connexion à MesServices…")
                browser_login(ctx)
                if not _is_logged_in(ctx):
                    raise LoginError("connexion terminée mais le site ne "
                                     "reconnaît pas la session")
                log("Connecté")
            save_session(ctx.storage_state())

            # Ouvre la page de recherche comme dans un vrai navigateur et
            # note la requête que le site envoie à son API.
            captured = []

            def on_request(req):
                if "/api/fr/search/" in req.url and req.method == "POST":
                    captured.append(req)

            page = ctx.new_page()
            page.on("request", on_request)
            page.goto(search_url(1), wait_until="domcontentloaded")
            _settle(page)
            page.wait_for_timeout(2000)
            affiche = _rendered_total(page)
            log(f"La page de recherche (connecté) affiche {affiche} logement(s)")

            found, recus = {}, 0
            if captured:
                req = captured[-1]
                payload = json.loads(req.post_data or "{}")
                headers = {k: v for k, v in req.headers.items()
                           if k.lower() in ("content-type", "accept",
                                            "x-requested-with", "x-csrf-token")}
                page_size = int(payload.get("pageSize") or 24)
                payload["pageSize"] = max(page_size, 100)
                for n in range(1, 101):
                    payload["page"] = n
                    r = ctx.request.post(req.url, data=json.dumps(payload),
                                         headers=headers or
                                         {"content-type": "application/json"})
                    if not r.ok:
                        raise RuntimeError(f"API connectée : HTTP {r.status}")
                    results = r.json().get("results") or {}
                    items = results.get("items") or []
                    recus += len(items)
                    parse_items(items, found)
                    total_api = (results.get("total") or {}).get("value")
                    if not items or (total_api and recus >= total_api):
                        break
                    time.sleep(0.5)
                log(f"API (connecté) : {recus} logement(s) récupéré(s)")
            else:
                log("Aucune requête API vue : lecture des pages affichées")

                def fetch(n):
                    page.goto(search_url(n), wait_until="domcontentloaded")
                    _settle(page)
                    return page.content()

                found, recus = scan_html(fetch)

            total = max(affiche or 0, recus)
            log(f"Offre connectée : {total} logement(s), dont {len(found)} à Luminy")
            return found, total
        finally:
            browser.close()


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------
def discord_send(content, embed=None):
    if not WEBHOOK:
        sys.exit("DISCORD_WEBHOOK_URL n'est pas défini.")
    body = {"content": content,
            "allowed_mentions": {"parse": ["everyone", "users", "roles"]}}
    if embed:
        body["embeds"] = [embed]
    for _ in range(3):
        r = requests.post(WEBHOOK, json=body, timeout=15)
        if r.status_code == 429:
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
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


# --------------------------------------------------------------------------
# État
# --------------------------------------------------------------------------
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state):
    state["updated"] = datetime.now().isoformat()
    STATE_FILE.write_text(json.dumps(state))


LOGIN_RETRY_SECONDS = 3600  # après un échec, on ne réessaie qu'1 fois par heure


def search(state):
    """Renvoie (logements_luminy, total_france, mode)."""
    manual = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"
    recent_fail = time.time() - state.get("login_fail_at", 0) < LOGIN_RETRY_SECONDS
    if MSE_EMAIL and MSE_PASSWORD and recent_fail and not manual:
        log("Connexion en pause après un échec récent (protège ton compte)")
    elif MSE_EMAIL and MSE_PASSWORD:
        try:
            found, total = search_logged_in()
            if state.get("login_ok") is False:
                discord_send("✅ Connexion au CROUS rétablie : le bot voit de "
                             "nouveau toute l'offre.")
            state["login_ok"] = True
            state.pop("login_fail_at", None)
            return found, total, "connecté"
        except Exception as e:  # noqa: BLE001
            log(f"Échec de la connexion : {e}")
            if state.get("login_ok") is not False:
                discord_send("⚠️ Le bot n'arrive pas à se connecter au CROUS : il ne "
                             "voit plus que l'offre publique (≈10 fois moins de "
                             "logements). Regarde les logs GitHub.")
            state["login_ok"] = False
            state["login_fail_at"] = time.time()
    found, total = search_public()
    return found, total, "public"


def check_once():
    """Renvoie (nb_luminy, nb_france, mode), ou None si tout a échoué."""
    state = load_state()
    previous = set(state.get("visible", []))
    try:
        current, total, mode = search(state)
    except Exception as e:  # noqa: BLE001
        log(f"Erreur pendant la recherche : {e}")
        save_state(state)
        return None
    new_ids = [i for i in current if i not in previous]
    log(f"{len(current)} logement(s) visible(s) à Luminy, {len(new_ids)} nouveau(x)")
    for i in new_ids:
        notify(current[i])
        time.sleep(1)
    state["visible"] = sorted(current)
    save_state(state)
    return len(current), total, mode


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
        if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
            if result is None:
                bilan = "⚠️ Vérification manuelle : erreur, regarde les logs GitHub."
            else:
                luminy, france, mode = result
                bilan = (f"✅ Bot CROUS Luminy opérationnel (mode {mode}) — le site "
                         f"affiche {france} logement(s) en France, dont {luminy} "
                         "à Luminy.")
            discord_send(bilan)
            log("Bilan envoyé sur Discord")
        if result is None:
            sys.exit(1)
        return
    log(f"Surveillance lancée (toutes les {INTERVAL} s). Ctrl+C pour arrêter.")
    while True:
        check_once()
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
