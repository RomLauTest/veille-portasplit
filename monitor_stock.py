#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monitor_stock.py
================

Surveillance automatique du retour en stock du climatiseur
Midea PortaSplit 12000 BTU reversible (ref MMCS-12HRN8-QRD0).

Principe
--------
- On verifie periodiquement une liste de pages produit.
- Pour chaque page on cherche des mots-cles dans le HTML pour decider d'un
  statut : "en_stock", "rupture" ou "incertain".
  Regle : en_stock si un signe "in" est present ET aucun signe "out".
- On memorise le dernier etat de chaque URL dans un fichier JSON et on ne
  notifie QUE lors d'une transition (rupture / incertain) -> en_stock, pour
  eviter le spam.

Modes
-----
- Par defaut : un seul passage puis arret (ideal cron / Tache planifiee).
- --loop : boucle continue, intervalle configurable (defaut 30 min, mini 30 min).

Notifications (voir la section CONFIG ci-dessous)
- ntfy (push telephone) par defaut.
- email SMTP en option.
- console en repli (toujours active en plus, et seule si rien d'autre marche).

Usage
-----
    python monitor_stock.py                 # un passage
    python monitor_stock.py --loop          # boucle, intervalle par defaut
    python monitor_stock.py --loop --interval 45   # boucle toutes les 45 min
    python monitor_stock.py --playwright    # rendu JS pour les sites anti-bot
    python monitor_stock.py --notify-all    # notifie aussi l'etat courant (test)
    python monitor_stock.py --once-verbose  # affiche le statut detaille de chaque site

Dependances : voir requirements.txt
"""

import argparse
import json
import logging
import os
import re
import smtplib
import sys
import time
import unicodedata
from datetime import datetime
from email.mime.text import MIMEText
from email.utils import formataddr

import requests

# Sur un poste d'entreprise, un proxy TLS (Zscaler, Netskope...) reemet les
# certificats avec un CA interne que le bundle de Python (certifi) ne connait
# pas -> erreur "CERTIFICATE_VERIFY_FAILED". truststore fait utiliser a Python
# le magasin de certificats du systeme (Windows/macOS), qui contient ce CA.
# Import optionnel : si absent, on continue normalement.
try:
    import truststore
    truststore.inject_into_ssl()
    _TRUSTSTORE_OK = True
except Exception:  # noqa: BLE001
    _TRUSTSTORE_OK = False

# ============================================================================
# CONFIG  --  a adapter (notifications, sites, seuils)
# ============================================================================

# ---- Notifications --------------------------------------------------------

NOTIFY = {
    # ntfy : push gratuit sur telephone. Installez l'appli ntfy, choisissez un
    # topic SECRET (difficile a deviner) et abonnez-vous dessus. Mettez le meme
    # topic ici. Voir README.
    "ntfy": {
        "enabled": True,
        "server": "https://ntfy.sh",
        "topic": "portasplit-midea-CHANGEZ-MOI-7h3k9",  # <-- CHANGEZ ce topic
        "priority": "high",   # min, low, default, high, max
    },

    # email SMTP : optionnel. Mettez enabled=True et renseignez vos identifiants.
    # Pour Gmail, utilisez un "mot de passe d'application" (pas votre mot de passe).
    "email": {
        "enabled": False,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "use_tls": True,
        "username": "vous@gmail.com",
        "password": "mot-de-passe-application",
        "from_addr": "vous@gmail.com",
        "from_name": "Veille PortaSplit",
        "to_addrs": ["vous@gmail.com"],
    },

    # console : repli. Toujours affiche dans les logs ; utile sans telephone.
    "console": {
        "enabled": True,
    },
}

# ---- Surcharge par variables d'environnement (pour GitHub Actions / serveur) -
# Permet de NE PAS ecrire le topic secret dans le code : on le fournit via un
# "secret" GitHub (ou une variable d'environnement sur un serveur). Si la
# variable existe, elle prend le pas sur la valeur ci-dessus.
#   NTFY_TOPIC     -> topic ntfy (le plus important)
#   NTFY_SERVER    -> serveur ntfy (defaut https://ntfy.sh)
#   NTFY_PRIORITY  -> priorite
# Email (optionnel) : EMAIL_ENABLED=1, SMTP_HOST, SMTP_PORT, SMTP_USER,
#   SMTP_PASSWORD, EMAIL_FROM, EMAIL_TO (destinataires separes par des virgules).
if os.environ.get("NTFY_TOPIC"):
    NOTIFY["ntfy"]["topic"] = os.environ["NTFY_TOPIC"]
if os.environ.get("NTFY_SERVER"):
    NOTIFY["ntfy"]["server"] = os.environ["NTFY_SERVER"]
if os.environ.get("NTFY_PRIORITY"):
    NOTIFY["ntfy"]["priority"] = os.environ["NTFY_PRIORITY"]

if os.environ.get("EMAIL_ENABLED") in ("1", "true", "True"):
    NOTIFY["email"].update({
        "enabled": True,
        "smtp_host": os.environ.get("SMTP_HOST", NOTIFY["email"]["smtp_host"]),
        "smtp_port": int(os.environ.get("SMTP_PORT", NOTIFY["email"]["smtp_port"])),
        "username": os.environ.get("SMTP_USER", ""),
        "password": os.environ.get("SMTP_PASSWORD", ""),
        "from_addr": os.environ.get("EMAIL_FROM", ""),
        "to_addrs": [a.strip() for a in os.environ.get("EMAIL_TO", "").split(",") if a.strip()],
    })

# ---- Prix de reference ----------------------------------------------------

PRIX_BASE_EUR = 999          # prix officiel de base
PRIX_ALERTE_EUR = 1200       # au-dessus : se mefier des revendeurs opportunistes

# ---- Reseau / robustesse --------------------------------------------------

HTTP_TIMEOUT = 20            # secondes
INTERVALLE_DEFAUT_MIN = 30   # minutes
INTERVALLE_MINIMUM_MIN = 30  # plancher de politesse vis-a-vis des sites

# User-Agent navigateur realiste + langue FR.
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

# Fichier d'etat (memoire entre deux passages).
ETAT_FICHIER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "etat_stock.json")

# ---- Detection du statut --------------------------------------------------
# Strategie en CASCADE (du plus fiable au plus bruite) :
#   1) JSON-LD schema.org "availability" : standard structure, presque infaillible
#      quand il est present (la plupart des e-commerce serieux le fournissent).
#   2) Repli mots-cles, mais RESTREINT a la zone du bouton d'achat principal, pour
#      eviter les faux signaux venant des avis clients, du cross-sell, du footer...
# Tout est compare en minuscules et sans accents (voir _normaliser()).

# -- 1) Valeurs schema.org/availability (sans le prefixe d'URL) --
# https://schema.org/ItemAvailability
DISPO_SCHEMA_EN_STOCK = {
    "instock", "onlineonly", "limitedavailability", "presale", "preorder",
    "backorder", "instoreonly",  # instoreonly = dispo, mais magasin uniquement
}
DISPO_SCHEMA_RUPTURE = {
    "outofstock", "soldout", "discontinued",
}
DISPO_SCHEMA_MAGASIN = {"instoreonly"}  # nuance signalee dans la notif

# -- 2) Mots-cles pour le repli (zone bouton d'achat) --
# "in" = disponibilite.
SIGNES_IN = [
    "ajouter au panier",
    "ajouter au paner",          # tolerance fautes de frappe vues sur certains sites
    "ajout au panier",
    "add to cart",
    "disponible immediatement",
]

# "out" FORTS : tournures qui n'apparaissent quasiment que sur le bloc d'achat
# d'un produit en rupture (jamais dans un avis client ou un menu). Decisifs.
SIGNES_OUT_FORTS = [
    "me prevenir",
    "prevenez-moi",
    "rupture de stock",
    "produit indisponible",
    "produit non disponible",
    "actuellement indisponible",
    "victime de son succes",
    "bientot de retour",
    "out of stock",
]

# "out" FAIBLES : mots trop generiques ("epuise", "indisponible", "plus
# disponible") qui se retrouvent dans les avis, le cross-sell, les CGV. On ne
# les considere QUE s'ils tombent dans la zone du bouton d'achat (voir analyse).
SIGNES_OUT_FAIBLES = [
    "rupture",
    "indisponible",
    "plus disponible",
    "epuise",
]

# Mots-cles servant a localiser la "zone d'achat" dans le HTML.
ANCRES_ZONE_ACHAT = [
    "ajouter au panier", "ajout au panier", "add to cart",
    "me prevenir", "prevenez-moi", "ajouter au devis",
]
# Demi-largeur (en caracteres) de la fenetre analysee autour de l'ancre.
ZONE_ACHAT_DEMI_LARGEUR = 600

# ---- Sites a surveiller ---------------------------------------------------
# Champs par site :
#   nom, url            : identite de la page produit.
#   fiable (bool)       : False = stock magasin ou anti-bot -> avertissement log,
#                         resultat a prendre avec prudence (ne plante jamais).
#   moteur (str)        : "requests" (defaut) ou "playwright".
#                         Le moteur "playwright" n'est REELLEMENT utilise que si
#                         le flag global --playwright est passe ; sinon on reste
#                         sur requests. Choix par site car Playwright n'est pas
#                         toujours meilleur : les sites proteges par DataDome
#                         (Leroy Merlin, Bricoman) BLOQUENT le navigateur headless
#                         (captcha) alors qu'une simple requete requests passe.

SITES = [
    {
        "nom": "Optimea (neuf)",
        "url": "https://www.optimea.fr/product/climatiseur-split-mobile-midea/",
        "fiable": True,
        # Repond parfois 503 / page "Maintenance" (Cloudflare). requests par defaut.
        "moteur": "requests",
    },
    {
        "nom": "Optimea (seconde vie)",
        "url": "https://www.optimea.fr/product/seconde-vie-climatiseur-split-mobile-midea-silencieux-reversible-sans-installation/",
        "fiable": True,
        "moteur": "requests",
    },
    {
        "nom": "ManoMano",
        "url": "https://www.manomano.fr/p/midea-climatiseur-split-mobile-reversible-froid-chaud-3500w12000btu-wifi-deshumidificateur-ventilateur-jusqua-40m2-kit-fenetre-inclus-83810402",
        "fiable": True,
        "moteur": "requests",
    },
    {
        "nom": "GroupSumi",
        "url": "https://groupsumi.fr/chauffage/climatisation/climatiseur-mobile/climatiseur-et-deshumidificateur-portable-4-en-1-midea-portasplit-3-5-kw-13907811",
        "fiable": True,
        "moteur": "requests",
    },
    {
        "nom": "Boulanger",
        "url": "https://www.boulanger.com/ref/1216685",
        "fiable": True,   # expose un JSON-LD availability propre -> fiable
        "moteur": "requests",
    },
    {
        "nom": "Castorama",
        "url": "https://www.castorama.fr/climatiseur-portasplit-midea-reversible-3500w/8431312260509_CAFR.prd",
        # ATTENTION : le JSON-LD de Castorama annonce "InStock" meme quand le
        # produit est en rupture (bouton grise, "stock : 0"). Son JSON-LD n'est
        # donc PAS fiable. Seul le rendu JS (etat reel du bouton d'achat) dit la
        # verite -> Playwright OBLIGATOIRE.
        "fiable": True,
        "moteur": "playwright",
        "requiert_playwright": True,
    },
    {
        "nom": "Rakuten (offre Boulanger)",
        "url": "https://fr.shopping.rakuten.com/offer/buy/13466164647/clim-reversible-optimea-mmcs-12hrn8-qrd0.html",
        "fiable": False,  # marketplace multi-vendeurs, pas de JSON-LD availability :
                          # detection par mots-cles uniquement, a recouper.
        "moteur": "requests",
    },
    {
        "nom": "Leroy Merlin",
        "url": "https://www.leroymerlin.fr/produits/climatiseur-split-mobile-reversible-portasplit-midea-par-optimea-93857579.html",
        "fiable": False,   # stock magasin + DataDome : bloque en requests (403) ET en headless (captcha)
        "moteur": "requests",
    },
    {
        "nom": "Bricoman / Tecnomat",
        "url": "https://www.bricoman.fr/produits/climatiseur-mobile-reversible-portasplit-midea-25088072.html",
        "fiable": False,   # stock magasin ; DataDome -> requests marche, Playwright NON
        "moteur": "requests",
    },
    {
        "nom": "Amazon.fr",
        "url": "https://www.amazon.fr/dp/B0CY2YW8BT",
        "fiable": False,   # anti-bot : Playwright aide reellement ici
        "moteur": "playwright",
    },
]

# ---- Signes de page de blocage (captcha / anti-bot / maintenance) ----------
# Si la page recuperee ressemble a un mur anti-bot plutot qu'a la fiche produit,
# on le signale explicitement (statut incertain + avertissement) au lieu de
# deduire un faux statut.
#
# ATTENTION : des mots comme "datadome" ou "captcha" sont presents dans le HTML
# des VRAIES fiches produit (le script anti-bot est embarque partout). On ne les
# utilise donc PAS comme signe. On se fie a deux indices fiables :
#   1) des PHRASES de challenge/maintenance sans ambiguite ;
#   2) la TAILLE : une page de challenge fait ~1-2 Ko, une fiche produit des
#      centaines de Ko.
SIGNES_BLOCAGE = [
    "verifiez que vous etes humain",
    "verifying you are human",
    "just a moment",            # Cloudflare
    "attention required",       # Cloudflare
    "enable javascript and cookies to continue",
    "site en maintenance",
    "page en maintenance",
]
# En dessous de cette taille, une page est suspecte (vraie fiche produit = gros HTML).
TAILLE_PAGE_SUSPECTE = 4000

# ============================================================================
# Logging horodaté
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "veille.log"),
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("veille")


# ============================================================================
# Utilitaires
# ============================================================================

def _normaliser(texte: str) -> str:
    """Minuscules + suppression des accents, pour une recherche robuste."""
    texte = texte.lower()
    texte = unicodedata.normalize("NFKD", texte)
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    # On compacte les espaces multiples pour fiabiliser les recherches de phrases.
    texte = re.sub(r"\s+", " ", texte)
    return texte


def _vers_ascii(texte: str) -> str:
    """En-tete ntfy (Title) doit etre en ASCII : on translittere proprement."""
    texte = unicodedata.normalize("NFKD", texte)
    return texte.encode("ascii", "ignore").decode("ascii")


# ============================================================================
# Detection du statut
# ============================================================================

_RE_AVAILABILITY = re.compile(
    r'availability"\s*:\s*"https?://(?:www\.)?schema\.org/(\w+)"'
)


def _statut_via_jsonld(texte: str):
    """
    Tente de decider via les valeurs schema.org/availability du JSON-LD.
    Renvoie (statut, details) ou None si aucune valeur exploitable.
    Plusieurs offres possibles : si AU MOINS une est disponible, le produit
    est considere disponible (variante achetable).
    """
    valeurs = [v.lower() for v in _RE_AVAILABILITY.findall(texte)]
    if not valeurs:
        return None

    dispo = [v for v in valeurs if v in DISPO_SCHEMA_EN_STOCK]
    rupture = [v for v in valeurs if v in DISPO_SCHEMA_RUPTURE]

    details = {"methode": "json-ld", "availability": valeurs}

    if dispo:
        magasin = all(v in DISPO_SCHEMA_MAGASIN for v in dispo)
        details["magasin_uniquement"] = magasin
        return "en_stock", details
    if rupture:
        details["magasin_uniquement"] = False
        return "rupture", details
    # Valeurs presentes mais non reconnues (preorder exotique, etc.) -> on
    # laisse le repli mots-cles trancher.
    return None


def _extraire_zones_achat(texte: str):
    """
    Renvoie la concatenation des fenetres de texte autour de chaque ancre
    "bloc d'achat". Si aucune ancre trouvee, renvoie une chaine vide.
    """
    morceaux = []
    for ancre in ANCRES_ZONE_ACHAT:
        for m in re.finditer(re.escape(ancre), texte):
            debut = max(0, m.start() - ZONE_ACHAT_DEMI_LARGEUR)
            fin = m.end() + ZONE_ACHAT_DEMI_LARGEUR
            morceaux.append(texte[debut:fin])
    return " ".join(morceaux)


def _statut_via_motscles(texte: str):
    """
    Repli quand le JSON-LD est absent/inexploitable.
    - Les signes "out" FORTS sont decisifs ou qu'ils soient.
    - Les signes "out" FAIBLES ne comptent que dans la zone du bouton d'achat.
    - Les signes "in" sont cherches dans la zone d'achat en priorite, sinon
      dans toute la page.
    """
    zone = _extraire_zones_achat(texte)

    out_forts = [s for s in SIGNES_OUT_FORTS if s in texte]
    out_faibles_zone = [s for s in SIGNES_OUT_FAIBLES if zone and s in zone]
    in_zone = [s for s in SIGNES_IN if zone and s in zone]
    in_page = [s for s in SIGNES_IN if s in texte]

    signes_in = in_zone or in_page
    signes_out = out_forts + out_faibles_zone

    details = {
        "methode": "mots-cles",
        "signes_in": signes_in,
        "signes_out": signes_out,
        "zone_achat_trouvee": bool(zone),
    }

    # Un "out" fort prime (c'est le signal de rupture le plus fiable).
    if out_forts:
        return "rupture", details
    # Sinon, un "in" net dans la zone d'achat sans "out faible local -> en stock.
    if signes_in and not out_faibles_zone:
        return "en_stock", details
    # Signaux contradictoires ou rien d'exploitable.
    if signes_out and not signes_in:
        return "rupture", details
    return "incertain", details


def _page_bloquee(html: str):
    """
    Detecte une page de blocage (captcha DataDome, challenge Cloudflare, page de
    maintenance...) plutot qu'une vraie fiche produit.
    Renvoie une courte raison (str) ou None.
    """
    texte = _normaliser(html)
    for signe in SIGNES_BLOCAGE:
        if signe in texte:
            return signe
    # Page anormalement courte = probablement un mur anti-bot.
    if len(html) < TAILLE_PAGE_SUSPECTE:
        return f"page tres courte ({len(html)} octets)"
    return None


def analyser_html(html: str):
    """
    Renvoie (statut, details) ou statut in {"en_stock", "rupture", "incertain"}.
    Cascade (du plus fiable au moins fiable) :
      0) etat REEL du bouton d'achat (marqueur ATB pose par Playwright) ;
      1) JSON-LD schema.org/availability ;
      2) repli mots-cles (zone d'achat).
    """
    texte = _normaliser(html)

    # 0) Marqueur d'etat du bouton d'achat (uniquement present via Playwright).
    #    Un bouton grise prime sur tout le reste : on ne peut PAS commander.
    if "atb:disabled" in texte:
        return "rupture", {"methode": "bouton-achat", "bouton": "disabled"}
    bouton_actif = "atb:enabled" in texte

    # 1) JSON-LD.
    resultat = _statut_via_jsonld(texte)
    if resultat is not None:
        # Si le bouton d'achat est cliquable, on fait confiance au JSON-LD tel
        # quel. Si le JSON-LD dit "en_stock" mais qu'aucun bouton actif n'a ete
        # rendu (cas Playwright), on a deja traite le DISABLED plus haut ; ici le
        # marqueur est ENABLED ou absent (mode requests) -> on garde le JSON-LD.
        return resultat

    # 2) Repli mots-cles. Un bouton actif rendu = signe "in" fort.
    statut, details = _statut_via_motscles(texte)
    if bouton_actif and statut == "incertain":
        details["bouton"] = "enabled"
        return "en_stock", details
    return statut, details


# ============================================================================
# Recuperation du HTML
# ============================================================================

def recuperer_html_requests(url: str) -> str:
    """Recupere le HTML via requests. Leve une exception en cas d'echec."""
    reponse = requests.get(url, headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT)
    reponse.raise_for_status()
    return reponse.text


def recuperer_html_playwright(url: str) -> str:
    """
    Recupere le HTML rendu par un navigateur headless (Playwright).
    Active uniquement avec le flag --playwright. Import paresseux pour ne pas
    imposer la dependance si on ne l'utilise pas.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        navigateur = p.chromium.launch(headless=True)
        page = navigateur.new_page(
            user_agent=HTTP_HEADERS["User-Agent"],
            locale="fr-FR",
            extra_http_headers={"Accept-Language": HTTP_HEADERS["Accept-Language"]},
        )
        try:
            page.goto(url, timeout=HTTP_TIMEOUT * 1000, wait_until="domcontentloaded")
            # Petit delai pour laisser le JS peupler le bouton panier / stock.
            page.wait_for_timeout(3500)
            html = page.content()
            # Etat REEL du bouton d'achat dans le DOM rendu : c'est le signal le
            # plus fiable (bien plus que le JSON-LD, qui peut etre trompeur :
            # certains sites affichent availability=InStock sur un produit en
            # rupture, ex. Castorama). On determine s'il existe un bouton
            # "ajouter au panier" CLIQUABLE (=ENABLED) ou seulement grise
            # (=DISABLED), et on l'injecte comme marqueur dans le HTML.
            etat_atb = page.evaluate(_JS_ETAT_BOUTON_ACHAT)
            html += f"\n<!--ATB:{etat_atb}-->\n"
        finally:
            navigateur.close()
    return html


# JS evalue dans la page rendue : renvoie ENABLED / DISABLED / NONE selon l'etat
# du (des) bouton(s) "ajouter au panier".
_JS_ETAT_BOUTON_ACHAT = """
() => {
  const ancres = ['ajouter au panier', 'ajout au panier', 'add to cart'];
  const els = Array.from(document.querySelectorAll(
    'button, a, input[type=submit], input[type=button], [role=button]'));
  let trouve = false, unEnabled = false;
  for (const el of els) {
    const txt = ((el.innerText || el.value || el.getAttribute('aria-label') || '')
                 + '').toLowerCase();
    if (ancres.some(a => txt.includes(a))) {
      trouve = true;
      const grise = el.disabled
        || el.getAttribute('aria-disabled') === 'true'
        || (el.className + '').toLowerCase().includes('disabled');
      if (!grise) unEnabled = true;
    }
  }
  if (!trouve) return 'NONE';
  return unEnabled ? 'ENABLED' : 'DISABLED';
}
"""


def verifier_site(site: dict, utiliser_playwright: bool):
    """
    Verifie un site. Renvoie (statut, details).
    statut peut valoir "erreur" si la recuperation echoue (un site en echec ne
    casse jamais les autres).
    """
    nom = site["nom"]
    url = site["url"]

    if not site.get("fiable", True):
        log.warning("[%s] site marque NON FIABLE (stock magasin ou anti-bot) "
                    "- resultat a prendre avec prudence.", nom)

    # Garde anti-faux-positif : certains sites (Castorama) exposent un JSON-LD
    # trompeur et ne sont fiables qu'avec le rendu JS. Sans --playwright, on
    # refuse de conclure plutot que de risquer un faux "en stock".
    if site.get("requiert_playwright") and not utiliser_playwright:
        log.warning("[%s] necessite --playwright (JSON-LD non fiable sans rendu JS) : "
                    "statut force a 'incertain' en mode requests.", nom)
        return "incertain", {"methode": "requiert-playwright"}

    # Choix du moteur : Playwright seulement si le site le demande ET si le flag
    # global --playwright est actif. Sinon requests.
    via_playwright = utiliser_playwright and site.get("moteur") == "playwright"
    try:
        if via_playwright:
            log.info("[%s] recuperation via Playwright (headless)...", nom)
            html = recuperer_html_playwright(url)
        else:
            html = recuperer_html_requests(url)
    except Exception as exc:  # noqa: BLE001 - on isole volontairement chaque site
        log.error("[%s] echec de recuperation : %s", nom, exc)
        return "erreur", {"erreur": str(exc)}

    # Detection d'une page de blocage (captcha / maintenance) avant analyse stock.
    raison_blocage = _page_bloquee(html)
    if raison_blocage:
        log.warning("[%s] page de blocage detectee (%s) : statut force a 'incertain'. "
                    "Verification manuelle conseillee.", nom, raison_blocage)
        return "incertain", {"methode": "blocage", "raison": raison_blocage}

    statut, details = analyser_html(html)
    methode = details.get("methode")
    if methode == "bouton-achat":
        log.info("[%s] statut=%s (bouton d'achat rendu : %s)",
                 nom, statut, details.get("bouton"))
    elif methode == "json-ld":
        suffixe = "magasin uniquement" if details.get("magasin_uniquement") else ""
        log.info("[%s] statut=%s (json-ld availability=%s) %s",
                 nom, statut, details.get("availability"), suffixe)
    else:
        log.info("[%s] statut=%s (mots-cles in=%s | out=%s | zone=%s | bouton=%s)",
                 nom, statut, details.get("signes_in"), details.get("signes_out"),
                 details.get("zone_achat_trouvee"), details.get("bouton"))
    return statut, details


# ============================================================================
# Persistance de l'etat
# ============================================================================

def charger_etat() -> dict:
    if not os.path.exists(ETAT_FICHIER):
        return {}
    try:
        with open(ETAT_FICHIER, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Impossible de lire %s (%s) : on repart d'un etat vide.",
                    ETAT_FICHIER, exc)
        return {}


def sauvegarder_etat(etat: dict) -> None:
    try:
        with open(ETAT_FICHIER, "w", encoding="utf-8") as f:
            json.dump(etat, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        log.error("Impossible d'ecrire %s : %s", ETAT_FICHIER, exc)


# ============================================================================
# Notifications
# ============================================================================

def _corps_notification(nom: str, url: str, magasin_uniquement: bool = False) -> str:
    if magasin_uniquement:
        entete = (f"{nom} : le PortaSplit Midea 12000 BTU est signale DISPONIBLE "
                  f"EN MAGASIN (retrait/stock magasin, pas forcement en ligne).")
    else:
        entete = (f"{nom} : le PortaSplit Midea 12000 BTU semble DE NOUVEAU EN STOCK.")
    return (
        f"{entete}\n\n"
        f"Prix de reference officiel : {PRIX_BASE_EUR} EUR.\n"
        f"Mefiez-vous au-dessus d'environ {PRIX_ALERTE_EUR} EUR "
        f"(revendeurs opportunistes pendant la canicule).\n\n"
        f"Verifiez vite : {url}"
    )


def notifier_ntfy(nom: str, url: str, corps: str) -> bool:
    cfg = NOTIFY["ntfy"]
    if not cfg.get("enabled"):
        return False
    endpoint = f"{cfg['server'].rstrip('/')}/{cfg['topic']}"
    # Title doit etre ASCII (en-tete HTTP) ; le corps reste en UTF-8.
    titre = _vers_ascii(f"STOCK: {nom} - PortaSplit Midea dispo")
    headers = {
        "Title": titre,
        "Priority": cfg.get("priority", "high"),
        "Tags": "rotating_light",
        "Click": url,
    }
    try:
        r = requests.post(
            endpoint,
            data=corps.encode("utf-8"),
            headers=headers,
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        log.info("[notif ntfy] envoyee sur le topic '%s'.", cfg["topic"])
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("[notif ntfy] echec : %s", exc)
        return False


def notifier_email(nom: str, url: str, corps: str) -> bool:
    cfg = NOTIFY["email"]
    if not cfg.get("enabled"):
        return False
    try:
        msg = MIMEText(corps, _charset="utf-8")
        msg["Subject"] = f"[Veille] {nom} : PortaSplit Midea de nouveau en stock"
        msg["From"] = formataddr((cfg.get("from_name", ""), cfg["from_addr"]))
        msg["To"] = ", ".join(cfg["to_addrs"])

        serveur = smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=HTTP_TIMEOUT)
        try:
            if cfg.get("use_tls", True):
                serveur.starttls()
            if cfg.get("username"):
                serveur.login(cfg["username"], cfg["password"])
            serveur.sendmail(cfg["from_addr"], cfg["to_addrs"], msg.as_string())
        finally:
            serveur.quit()
        log.info("[notif email] envoyee a %s.", ", ".join(cfg["to_addrs"]))
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("[notif email] echec : %s", exc)
        return False


def notifier_console(nom: str, url: str, corps: str) -> bool:
    if not NOTIFY["console"].get("enabled"):
        return False
    log.info("=" * 60)
    log.info("ALERTE STOCK -- %s", nom)
    for ligne in corps.splitlines():
        log.info("  %s", ligne)
    log.info("=" * 60)
    return True


def notifier(nom: str, url: str, magasin_uniquement: bool = False) -> None:
    """Envoie la notification via tous les canaux actives (console toujours en repli)."""
    corps = _corps_notification(nom, url, magasin_uniquement)
    canaux_ok = False
    canaux_ok |= notifier_ntfy(nom, url, corps)
    canaux_ok |= notifier_email(nom, url, corps)
    # Console : toujours, mais surtout indispensable si aucun autre canal n'a marche.
    notifier_console(nom, url, corps)
    if not canaux_ok:
        log.warning("Aucun canal push/email actif ou fonctionnel : "
                    "repli sur la console uniquement.")


# ============================================================================
# Passage de verification
# ============================================================================

def faire_un_passage(utiliser_playwright: bool, notifier_tout: bool = False,
                     verbeux: bool = False) -> None:
    """Verifie tous les sites, met a jour l'etat, notifie sur transition -> en_stock."""
    etat = charger_etat()
    horodatage = datetime.now().isoformat(timespec="seconds")

    for site in SITES:
        nom = site["nom"]
        url = site["url"]
        statut, details = verifier_site(site, utiliser_playwright)

        ancien = etat.get(url, {}).get("statut")

        # Transition qui declenche une notification :
        #   ancien dans {rupture, incertain, erreur, None} ET nouveau == en_stock.
        transition_vers_stock = (statut == "en_stock" and ancien != "en_stock")

        if statut == "en_stock" and (transition_vers_stock or notifier_tout):
            log.info("[%s] PASSAGE EN STOCK detecte (ancien=%s) -> notification.",
                     nom, ancien)
            notifier(nom, url, details.get("magasin_uniquement", False))

        # On n'ecrase pas un statut connu par une "erreur" passagere : on garde
        # l'ancien statut mais on note l'incident. Cela evite de re-notifier a
        # tort apres une simple erreur reseau.
        if statut == "erreur" and ancien is not None:
            etat[url].setdefault("erreurs", 0)
            etat[url]["erreurs"] += 1
            etat[url]["derniere_erreur"] = horodatage
            etat[url]["details_erreur"] = details.get("erreur")
        else:
            etat[url] = {
                "nom": nom,
                "statut": statut,
                "maj": horodatage,
                "methode": details.get("methode"),
                "availability": details.get("availability"),
                "magasin_uniquement": details.get("magasin_uniquement", False),
                "signes_in": details.get("signes_in", []),
                "signes_out": details.get("signes_out", []),
            }

        if verbeux:
            log.info("[%s] detail -> %s", nom, json.dumps(etat[url], ensure_ascii=False))

    sauvegarder_etat(etat)

    # Petit recap.
    en_stock = [e["nom"] for e in etat.values() if e.get("statut") == "en_stock"]
    if en_stock:
        log.info("Recap : EN STOCK -> %s", ", ".join(en_stock))
    else:
        log.info("Recap : aucun marchand en stock pour l'instant.")


# ============================================================================
# Point d'entree
# ============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Veille de retour en stock du climatiseur Midea PortaSplit 12000 BTU."
    )
    parser.add_argument("--loop", action="store_true",
                        help="boucle continue au lieu d'un seul passage.")
    parser.add_argument("--interval", type=int, default=INTERVALLE_DEFAUT_MIN,
                        help=f"intervalle en minutes pour --loop "
                             f"(defaut {INTERVALLE_DEFAUT_MIN}, mini {INTERVALLE_MINIMUM_MIN}).")
    parser.add_argument("--playwright", action="store_true",
                        help="utiliser un navigateur headless (Playwright) pour les "
                             "sites non fiables (JS / anti-bot).")
    parser.add_argument("--notify-all", action="store_true",
                        help="notifier tous les sites actuellement en stock "
                             "(utile pour tester les notifications).")
    parser.add_argument("--once-verbose", action="store_true",
                        help="afficher le detail JSON de chaque site.")
    args = parser.parse_args()

    # On force le plancher de politesse.
    intervalle = max(args.interval, INTERVALLE_MINIMUM_MIN)
    if args.interval < INTERVALLE_MINIMUM_MIN:
        log.warning("Intervalle demande %d min < minimum %d min : on force %d min.",
                    args.interval, INTERVALLE_MINIMUM_MIN, INTERVALLE_MINIMUM_MIN)

    if _TRUSTSTORE_OK:
        log.info("truststore actif : verification TLS via le magasin de certificats systeme.")
    else:
        log.info("truststore absent : verification TLS via certifi "
                 "(peut echouer derriere un proxy TLS d'entreprise -> pip install truststore).")

    if args.playwright:
        log.info("Mode Playwright active pour les sites non fiables.")

    if args.loop:
        log.info("Demarrage en mode boucle (intervalle %d min). Ctrl+C pour arreter.",
                 intervalle)
        try:
            while True:
                faire_un_passage(args.playwright, args.notify_all, args.once_verbose)
                log.info("Prochaine verification dans %d min.", intervalle)
                time.sleep(intervalle * 60)
        except KeyboardInterrupt:
            log.info("Arret demande par l'utilisateur. Au revoir.")
            return 0
    else:
        faire_un_passage(args.playwright, args.notify_all, args.once_verbose)

    return 0


if __name__ == "__main__":
    sys.exit(main())
