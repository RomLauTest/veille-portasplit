# Veille stock — Midea PortaSplit 12000 BTU

Outil Python qui surveille le retour en stock du climatiseur **Midea PortaSplit
12000 BTU réversible** (réf. `MMCS-12HRN8-QRD0`) chez plusieurs marchands, et qui
vous **notifie par push (ntfy), email ou console** dès qu'un site repasse en stock.

Il ne notifie **que sur la transition** rupture/incertain → en stock, donc pas de
spam si un site reste dispo plusieurs passages.

---

## 1. Installation

Prérequis : Python 3.8+.

```bash
# dans le dossier du projet
pip install -r requirements.txt
```

(Optionnel) Pour le mode navigateur headless `--playwright` (utile sur Amazon) :

```bash
python -m playwright install chromium
```

---

## 2. Configuration

Tout se règle en haut de [`monitor_stock.py`](monitor_stock.py), dans le bloc
`CONFIG`.

### a) Notifications ntfy (recommandé — push gratuit sur téléphone)

1. Installez l'application **ntfy** (Android / iOS) ou utilisez https://ntfy.sh.
2. Choisissez un **topic secret** et difficile à deviner (c'est votre seule
   protection : quiconque connaît le topic reçoit/peut écrire dessus). Ex.
   `portasplit-r7h3-k9x2`.
3. Dans l'appli, **abonnez-vous** à ce topic.
4. Reportez le même topic dans la config :

```python
"ntfy": {
    "enabled": True,
    "server": "https://ntfy.sh",
    "topic": "portasplit-r7h3-k9x2",   # <-- votre topic secret
    "priority": "high",
},
```

Testez tout de suite :

```bash
python monitor_stock.py --notify-all
```

Vous devez recevoir un push pour chaque site déjà en stock (le titre est en ASCII,
le corps en UTF-8, et un appui sur la notification ouvre la page produit).

### b) Email SMTP (optionnel)

Passez `"enabled": True` dans la section `email` et renseignez votre serveur SMTP.
Pour Gmail, créez un **mot de passe d'application** (les mots de passe normaux sont
refusés par SMTP).

### c) Console (repli)

Toujours active : si aucun push/email ne fonctionne, l'alerte est écrite dans les
logs (`veille.log`).

### d) Sites surveillés

La liste `SITES` contient les 10 marchands surveillés :

| Marchand | Fiable | Moteur | Remarque |
|----------|:------:|--------|----------|
| Optimea (neuf + seconde vie) | oui | requests | répond souvent `503` / maintenance |
| ManoMano | oui | requests | JSON-LD ; anti-bot intermittent (`403`) |
| GroupSumi | oui | requests | JSON-LD |
| Boulanger | oui | requests | JSON-LD |
| Castorama | oui | **playwright** | JSON-LD trompeur → détection par bouton d'achat (voir §5) |
| Rakuten | non | requests | marketplace, mots-clés seulement |
| Leroy Merlin | non | requests | DataDome : bloqué (`403`) |
| Bricoman | non | requests | stock magasin (`InStoreOnly`) |
| Amazon | non | playwright | anti-bot |

Les sites dont le stock dépend d'un magasin physique ou qui ont un anti-bot
agressif sont marqués `"fiable": False` : ils sont quand même vérifiés, avec un
**avertissement** dans le log, mais le résultat est à prendre avec prudence.

> **Sites volontairement non inclus** (anti-bot bloquant en `requests` *et* en
> headless) : **Fnac** et **Darty** renvoient `403`. Vous pouvez tenter de les
> ajouter avec `"moteur": "playwright"`, mais comme Leroy Merlin ils risquent le
> captcha. Le comparateur **idealo**
> (`idealo.fr/prix/206299262/midea-mmcs-12hrn8-qrd0.html`) est une bonne page à
> consulter **à la main** : il agrège les prix de tous les marchands.

> Cas particulier « stock magasin » : certains sites (Bricoman, Leroy Merlin)
> annoncent `InStoreOnly`. Le script le considère comme « en_stock » mais la
> notification précise **« disponible en magasin uniquement »** — utile si vous
> pouvez aller retirer en magasin, mais ce n'est pas forcément commandable en
> ligne.

### e) Prix

Le prix de référence officiel est **999 €**. Chaque notification rappelle de se
méfier au-dessus d'environ **1200 €** (revendeurs opportunistes pendant la canicule).
Réglable via `PRIX_BASE_EUR` / `PRIX_ALERTE_EUR`.

---

## 3. Utilisation

```bash
python monitor_stock.py                 # un seul passage puis arrêt (pour cron)
python monitor_stock.py --loop          # boucle, intervalle par défaut (30 min)
python monitor_stock.py --loop --interval 45   # boucle toutes les 45 min
python monitor_stock.py --playwright    # rendu JS pour les sites anti-bot
python monitor_stock.py --notify-all    # notifie l'état courant (test)
python monitor_stock.py --once-verbose  # détail JSON de chaque site
```

> L'intervalle minimum est **30 min** (politesse vis-à-vis des sites). Une valeur
> inférieure est automatiquement remontée à 30 min.

> **Conseil** : lancez de préférence avec `--playwright`. Sans ce flag, **Amazon**
> est analysé en mode dégradé et **Castorama** est ignoré (forcé à `incertain`,
> pour ne jamais produire de faux « en stock »). Avec `--playwright`, ces deux
> sites sont couverts correctement (détection par état réel du bouton d'achat).
> Prérequis : `python -m playwright install chromium`.

Fichiers générés dans le dossier :
- `etat_stock.json` — dernier statut connu de chaque URL (la « mémoire »).
- `veille.log` — journal horodaté.

---

## 4. Planification automatique

Le mode par défaut (un passage) est idéal pour un planificateur. Choisissez **soit**
le mode `--loop` qui tourne en continu, **soit** une tâche planifiée toutes les
30 min — pas les deux.

### Linux / macOS — crontab (toutes les 30 min)

```bash
crontab -e
```

Ajoutez (adaptez les chemins) :

```cron
*/30 * * * * cd /home/pi/recherche-clim && /usr/bin/python3 monitor_stock.py --playwright >> cron.log 2>&1
```

Sur Raspberry Pi, `/usr/bin/python3` est le chemin habituel (`which python3` pour
confirmer).

### Windows — Tâche planifiée

Option simple, en ligne de commande (PowerShell) :

```powershell
$action  = New-ScheduledTaskAction -Execute "python" `
  -Argument "monitor_stock.py --playwright" `
  -WorkingDirectory "C:\Users\romain.lautrec\Cowork\Perso\Recherche Clim"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
  -RepetitionInterval (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "Veille PortaSplit" `
  -Action $action -Trigger $trigger -Description "Stock Midea PortaSplit"
```

Ou via l'interface : **Planificateur de tâches → Créer une tâche de base →**
déclencheur « répéter toutes les 30 minutes », action « Démarrer un programme »,
programme `python`, argument `monitor_stock.py --playwright`, « Commencer dans » =
le dossier du projet.

### En continu (Raspberry Pi allumé en permanence)

```bash
python3 monitor_stock.py --loop
```

Pour le lancer au démarrage et le garder vivant, créez un service `systemd` qui
exécute cette commande (mode `--loop`).

---

## 4 bis. Hébergement gratuit sur GitHub Actions (sans laisser un PC allumé)

C'est la méthode recommandée : la veille tourne **toutes les 30 min sur
l'infrastructure GitHub**, gratuitement, sans aucun serveur ni PC allumé. Le
fichier [`.github/workflows/veille.yml`](.github/workflows/veille.yml) est déjà
fourni.

### Principe

- GitHub exécute le script toutes les 30 min (déclencheur `schedule`).
- Le topic ntfy est fourni via un **secret GitHub** (jamais écrit dans le code).
- À chaque exécution, `etat_stock.json` est **re-commité dans le dépôt** : c'est
  la « mémoire » qui évite de te re-notifier à chaque passage.
- Les notifications ntfy partent du serveur GitHub directement vers ton téléphone.

### Mise en place (≈ 10 min, une seule fois)

1. **Crée un compte GitHub** (si tu n'en as pas) sur https://github.com.
2. **Crée un nouveau dépôt** (bouton « New repository »). Choisis-le **public**
   → les minutes GitHub Actions sont alors **illimitées et gratuites**. (Aucun
   secret n'est exposé : le topic est dans un secret, et `etat_stock.json` ne
   contient que des statuts de stock, rien de sensible.)
3. **Envoie ces fichiers dans le dépôt.** Le plus simple sans connaître git :
   sur la page du dépôt, « Add file » → « Upload files », puis glisse
   `monitor_stock.py`, `requirements.txt`, `etat_stock.json`, le dossier
   `.github/` et ce `README.md`. Valide (« Commit changes »).
   *(Ou en ligne de commande : voir l'encadré plus bas.)*
4. **Ajoute ton topic ntfy en secret** : dans le dépôt, **Settings → Secrets and
   variables → Actions → New repository secret**.
   - Name : `NTFY_TOPIC`
   - Secret : ton topic secret (ex. `portasplit-r7h3-k9x2`)
   - (Optionnel : `NTFY_SERVER`, `NTFY_PRIORITY`.)
5. **Abonne-toi à ce topic** dans l'appli ntfy de ton téléphone (cf. §2-a).
6. **Active et teste** : onglet **Actions** → accepte d'activer les workflows si
   demandé → clique sur « Veille PortaSplit » → **Run workflow** pour un test
   immédiat. Regarde les logs du run ; tu dois voir le récap des 10 sites.

> Laisse le `topic` placeholder tel quel dans `monitor_stock.py` : en production
> c'est le secret `NTFY_TOPIC` qui le remplace automatiquement.

<details>
<summary>Variante git en ligne de commande</summary>

```bash
cd "Recherche Clim"
git init
git add monitor_stock.py requirements.txt README.md etat_stock.json .gitignore .github
git commit -m "Veille PortaSplit"
git branch -M main
git remote add origin https://github.com/<ton-compte>/<ton-depot>.git
git push -u origin main
```
</details>

### Bon à savoir

- **Horaire** : le cron GitHub est en UTC et peut être décalé de quelques minutes
  en période de charge. Sans importance pour une veille toutes les 30 min.
- **Inactivité** : GitHub **désactive les workflows planifiés après 60 jours sans
  activité dans le dépôt** (les commits automatiques du robot ne comptent pas).
  Si la veille doit durer plus longtemps, fais de temps en temps un petit commit
  manuel, ou relance via **Run workflow**, pour ré-armer la planification.
- **Suivi** : chaque exécution est visible dans l'onglet **Actions** (logs
  complets). L'historique d'état est visible dans les commits `maj etat stock`.
- **Coût** : 0 € sur dépôt public. Sur dépôt privé, le quota gratuit (2000
  min/mois) serait dépassé par des runs Playwright toutes les 30 min → préfère le
  public.

---

## 5. Comment fonctionne la détection

La détection se fait en **cascade**, du signal le plus fiable au plus bruité :

0. **État réel du bouton d'achat** (uniquement en mode `--playwright`). Après
   rendu de la page, le script regarde si le bouton « Ajouter au panier » est
   **cliquable** ou **grisé** (`disabled`). Un bouton grisé = **rupture**, et ce
   signal prime sur tout le reste. C'est le plus fiable car il reflète ce que
   voit réellement l'utilisateur.

   > Pourquoi c'est nécessaire : **Castorama** annonce `InStock` dans son JSON-LD
   > *même quand le produit est en rupture* (« Non disponible », « Stock : 0 »).
   > Se fier au JSON-LD donnait un **faux « en stock »**. Castorama est donc passé
   > en `moteur: playwright` et, par sécurité, renvoie `incertain` (jamais
   > « en stock ») si on l'interroge sans `--playwright`.

1. **JSON-LD `schema.org/availability`** (prioritaire en mode requests). La plupart des e-commerce
   sérieux exposent leur stock dans une balise structurée
   `"availability": "https://schema.org/..."`. C'est le signal le plus fiable :
   - `InStock`, `OnlineOnly`, `LimitedAvailability`, `PreOrder`, `BackOrder` → **en_stock**
   - `InStoreOnly` → **en_stock**, signalé « disponible en magasin uniquement »
   - `OutOfStock`, `SoldOut`, `Discontinued` → **rupture**
   - S'il y a plusieurs offres/variantes, une seule disponible suffit à conclure
     « en_stock ».

2. **Repli par mots-clés** (si pas de JSON-LD exploitable). Le HTML est mis en
   minuscules et sans accents, puis :
   - on cherche les signes « en stock » (`ajouter au panier`, …) en **priorité
     dans la zone du bouton d'achat** (fenêtre de texte autour du bouton), pas
     dans toute la page ;
   - les signes de rupture **forts** (`me prévenir`, `rupture de stock`,
     `actuellement indisponible`, …) sont décisifs où qu'ils soient ;
   - les signes **faibles et ambigus** (`épuisé`, `indisponible`, `plus
     disponible`) ne comptent que dans la zone du bouton d'achat — sinon ils
     polluent (ils apparaissent souvent dans les **avis clients** ou les produits
     recommandés).

Cette approche évite le piège de la v1 : un mot comme « plus disponible » dans un
avis client ne fait plus basculer à tort tout le site en « incertain ».

Les sites très chargés en JavaScript ou anti-bot (Optimea renvoie parfois `503`,
Leroy Merlin `403`) peuvent échouer en mode `requests` : utilisez `--playwright`.

### Note sur la vérification TLS (poste d'entreprise)

Si vous obtenez une erreur `CERTIFICATE_VERIFY_FAILED` / `unable to get local
issuer certificate`, c'est qu'un **proxy TLS d'entreprise** (Zscaler, Netskope…)
réémet les certificats avec une autorité interne inconnue de Python. Le paquet
[`truststore`](https://pypi.org/project/truststore/) (dans `requirements.txt`)
règle ça en faisant utiliser à Python le **magasin de certificats du système**.
Il est chargé automatiquement s'il est installé ; sinon le script bascule sur
`certifi` et vous prévient dans le log.

---

## 6. Mode `--playwright` : quand ça aide (et quand ça nuit)

Le flag `--playwright` lance un Chromium headless **seulement** pour les sites
dont le champ `"moteur"` vaut `"playwright"` dans la config (par défaut : Amazon
uniquement). Tous les autres restent en `requests`, c'est volontaire.

Pourquoi pas Playwright partout ? Parce qu'il n'est **pas** toujours meilleur.
Bilan mesuré sur ce produit :

| Site | `requests` | Playwright headless | Réglage retenu |
|------|-----------|---------------------|----------------|
| Amazon | ok | **mieux** (page rendue) | `moteur: playwright` |
| Bricoman | **ok** (JSON-LD `InStoreOnly`) | bloqué (captcha DataDome) | `moteur: requests` |
| Leroy Merlin | bloqué (403) | bloqué (captcha DataDome) | `moteur: requests` |
| Optimea | `503` / page maintenance | page maintenance | `moteur: requests` |

Les sites protégés par **DataDome** (Leroy Merlin, Bricoman) **détectent le
navigateur headless** et renvoient un captcha, alors qu'une simple requête
`requests` passe. Lancer Playwright dessus serait contre-productif.

Pour changer le moteur d'un site, éditez son champ `"moteur"` dans `SITES`
(`"requests"` ou `"playwright"`).

### Détection des pages de blocage

Quand un site renvoie une page de challenge (captcha) ou de maintenance au lieu
de la fiche produit, le script le repère (phrases de challenge **ou** page
anormalement courte < 4 Ko) et force le statut à **`incertain`** avec un
avertissement dans le log — plutôt que d'en déduire un faux « en stock » /
« rupture ».

---

## 7. Limites

- Détection par mots-clés (repli) : un changement de formulation d'un marchand
  peut fausser un statut. Ajustez `SIGNES_IN` / `SIGNES_OUT_FORTS` si besoin.
- Le signal le plus fiable reste le JSON-LD `schema.org/availability` ; tous les
  sites ne l'exposent pas.
- Les sites « non fiables » (Amazon, Leroy Merlin, Bricoman) reflètent souvent un
  stock magasin ou bloquent les robots : considérez-les comme indicatifs.
- Leroy Merlin reste inaccessible (DataDome bloque requests **et** headless) ;
  Optimea répond actuellement `503` / maintenance. Ces deux-là sont à vérifier
  manuellement tant que la situation dure.
- Restez correct : ne descendez pas sous 30 min d'intervalle.
