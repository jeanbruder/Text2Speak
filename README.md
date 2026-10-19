# Text2Speak : TTS multi-pièces

Service HTTP qui transforme un texte en voix (Google TTS via la bibliothèque `gTTS`) et le fait lire par un ou plusieurs haut-parleurs de la maison.

Chaque pièce correspond à un **serveur PulseAudio** (un Raspberry Pi Zero W relié à une enceinte). Le service gère :

- une **file d'attente** globale avec priorités ;
- un **cache disque** des fichiers audio déjà synthétisés ;
- le **multi-pièces** : une annonce peut viser une pièce, plusieurs, ou une zone entière, lues en parallèle ;
- un **fichier de configuration** décrivant les pièces et les zones.

Il s'agit uniquement de **voix** (annonces, notifications), pas de musique.

## Sommaire

1. [Fonctionnement](#fonctionnement)
2. [Contenu du dossier](#contenu-du-dossier)
3. [Configuration des pièces et zones](#configuration-des-pièces-et-zones)
4. [Variables d'environnement](#variables-denvironnement)
5. [API](#api)
6. [Cache](#cache)
7. [File d'attente et priorités](#file-dattente-et-priorités)
8. [Prérequis côté Raspberry Pi](#prérequis-côté-raspberry-pi)
9. [Déploiement](#déploiement)
10. [Tests](#tests)
11. [Dépannage](#dépannage)
12. [Limites connues et évolutions](#limites-connues-et-évolutions)

## Fonctionnement

```
Client (Gladys, curl, script...)
        │  POST /dire {"dire": "...", "cibles": ["maison"]}
        ▼
┌──────────────────────────── Conteneur Text2Speak ───────────────────────────┐
│  API Flask ──► file d'attente globale (priorités, FIFO à priorité égale)    │
│                        │                                                    │
│                        ▼  un seul worker                                    │
│             1. cache disque (MP3 indexé par hash du texte)                  │
│                  ├─ présent : réutilisé                                     │
│                  └─ absent  : synthèse gTTS puis mise en cache              │
│             2. résolution des cibles (pièces / zones → liste de pièces)     │
│             3. test de connexion en parallèle (2 s max par pièce)           │
│             4. lancement simultané d'un `paplay -s <serveur>` par pièce     │
└─────────────────────────────────────────────────────────────────────────────┘
        │ flux audio via le protocole Pulse (TCP 4713)
        ▼
 airpi01 (salon)     airpi02 (cuisine)     airpi03 (salle de bain)
```

Points clés :

- `paplay` décode le MP3 **dans le conteneur** et envoie le son au Pulse du Pi. Les Pi n'exécutent donc aucun code applicatif : ce sont de simples haut-parleurs réseau.
- Les Pi sont joints **par leur nom** (DNS local, par exemple Pi-hole).
- La file est globale : un message destiné à plusieurs pièces joue dans toutes en même temps, et le message suivant attend la fin du plus lent.
- Une pièce injoignable n'empêche pas les autres de jouer : le job se termine alors en `partiel`.

## Contenu du dossier

```
.
├── README.md             Ce document
├── tts_server.py         Le service (API, file, cache, lecture multi-pièces)
├── requirements.txt      Dépendances Python : Flask, gTTS, PyYAML
├── Dockerfile            Image du service (Python 3.12 slim + paplay)
├── docker-compose.yml    Déploiement : ports, volumes, variables
├── test_tts.sh           Test d'usage (bash + curl)
├── config/
│   └── pieces.yml        Pièces, zones, cible par défaut
└── cache/                Cache audio (créé par toi, monté dans le conteneur)
```

### `tts_server.py`

Un seul fichier, organisé en blocs :

| Bloc | Rôle |
|---|---|
| Configuration | Lecture des variables d'environnement `TTS_*` |
| Pièces et zones | `Piece`, `Layout`, `load_layout` : lecture et validation du YAML, résolution des zones (imbriquées, détection des cycles) |
| `AudioCache` | Cache MP3 sur disque, clé SHA-256 de `langue\|tld\|texte`, écriture atomique, éviction LRU |
| `Job` et file | `PriorityQueue`, registre des jobs (historique des 100 derniers terminés) |
| Lecture | `check_reachable` (test TCP), `play_on` (un `paplay`), `play_all` (parallélisme et statuts) |
| `worker` | Thread unique qui dépile les jobs, synthétise et joue |
| API Flask | Routes `/dire`, `/pieces`, `/file`, `/cache` |

### `requirements.txt`

`Flask` (API), `gTTS` (synthèse vocale), `PyYAML` (lecture de `pieces.yml`). Les versions ne sont pas épinglées : pour un build reproductible, fige-les avec `pip freeze` une fois le service validé.

### `Dockerfile`

- Image `python:3.12-slim-bookworm` (Python ≥ 3.10 requis par le code).
- Installe `pulseaudio-utils` (fournit `paplay`) et `tzdata` (pour que `TZ=Europe/Paris` fonctionne).
- Crée un utilisateur non-root `text2speak` (UID réglable par `ARG UID`, 1000 par défaut). Cet UID doit être propriétaire de `./cache` sur l'hôte.
- Déclare un `HEALTHCHECK` sur `GET /cache`.

### `docker-compose.yml`

- Publie le port `1337`.
- Monte `./config` en lecture seule sur `/config` et `./cache` sur `/var/cache/tts`.
- Règle le fuseau horaire et les limites de cache et de file.
- `init: true` (arrêt propre du processus) et `restart: unless-stopped`.
- Option `dns:` (commentée) si le conteneur ne résout pas les noms des Pi.

### `test_tts.sh`

Test d'usage de bout en bout (voir [Tests](#tests)). Il fait réellement parler les haut-parleurs.

## Configuration des pièces et zones

Fichier `config/pieces.yml` :

```yaml
pieces:
  salon:
    serveur: airpi01
  cuisine:
    serveur: airpi02
  salle_de_bain:
    serveur: airpi03
    volume: 60

zones:
  rez_de_chaussee: [salon, cuisine]
  maison: [salon, cuisine, salle_de_bain]

defaut: [cuisine]
```

### Champs d'une pièce

| Champ | Obligatoire | Description |
|---|---|---|
| `serveur` | oui | Valeur passée à `paplay -s` : nom d'hôte du Pi, ou `tcp:hôte:port`. Port par défaut : 4713. |
| `volume` | non | Pourcentage entre 0 et 150 (100 = volume nominal), converti pour `paplay --volume`. |
| `decalage_ms` | non | Retard (≥ 0) ajouté au départ de cette pièce, pour compenser un Pi plus rapide qu'un autre. |
| `peripherique` | non | Nom d'un sink précis sur ce serveur (`paplay -d`). |

### Zones et cible par défaut

- Une **zone** est une liste non vide de pièces et/ou d'autres zones. Les doublons sont éliminés.
- Le nom d'une zone ne peut pas être celui d'une pièce.
- `defaut` indique les cibles utilisées quand un appel ne précise pas `cibles`. Il est obligatoire dès qu'il y a plusieurs pièces.
- Les noms sont insensibles à la casse et aux espaces de bord.

### Validation

La configuration est vérifiée au démarrage : pièce sans `serveur`, zone vide, cible inconnue, zones circulaires, doublons. En cas d'erreur, le service s'arrête avec un message explicite dans les logs. Les modifications ne sont prises en compte qu'après `docker compose restart`.

Si le fichier est absent, le service démarre avec une seule pièce `defaut` pointant vers `TTS_SINK` (compatibilité avec l'ancien fonctionnement) et émet un avertissement.

## Variables d'environnement

| Variable | Défaut | Rôle |
|---|---|---|
| `TTS_CONFIG` | `/config/pieces.yml` | Chemin du fichier de configuration |
| `TTS_SINK` | `airpi02` | Serveur utilisé seulement si le fichier de configuration est absent |
| `TTS_LANG` | `fr` | Langue de synthèse |
| `TTS_TLD` | `fr` | Domaine Google utilisé par gTTS (accent) |
| `TTS_CACHE_DIR` | `/var/cache/tts` | Dossier du cache dans le conteneur |
| `TTS_CACHE_MAX_FILES` | `500` | Nombre maximal de fichiers en cache |
| `TTS_CACHE_MAX_BYTES` | `104857600` | Taille maximale du cache (100 Mo) |
| `TTS_QUEUE_MAX` | `50` | Taille maximale de la file d'attente |
| `TTS_TEXT_MAX_CHARS` | `500` | Longueur maximale d'un texte |
| `TTS_PLAY_TIMEOUT` | `120` | Durée maximale d'une lecture sur une pièce (s) |
| `TTS_CONNECT_TIMEOUT` | `2` | Délai du test de connexion à un Pi (s) |
| `TTS_START_MARGIN` | `0.05` | Marge avant le départ commun des lectures (s) |
| `TTS_PORT` | `1337` | Port d'écoute |
| `TTS_HOST` | `0.0.0.0` | Adresse d'écoute |

## API

### `POST /dire`

Met un texte en file d'attente.

```bash
curl -X POST http://localhost:1337/dire \
     -H 'Content-Type: application/json' \
     -d '{"dire": "Le repas est prêt", "cibles": ["maison"], "priorite": 5}'
```

| Champ | Obligatoire | Description |
|---|---|---|
| `dire` | oui | Texte à lire (500 caractères max par défaut) |
| `cibles` | non | Nom ou liste de noms de pièces et/ou zones. Sans ce champ : `defaut`. |
| `priorite` | non | Entier de 0 (le plus urgent) à 9. Défaut : 5. |

Réponse `202` :

```json
{"OK": "Le repas est prêt", "id": 12, "cibles": ["salon", "cuisine", "salle_de_bain"],
 "position": 1, "en_cache": true}
```

Codes d'erreur : `400` (champ manquant, cible inconnue, type invalide), `413` (texte trop long), `503` (file pleine).

Compatibilité : un appel `{"dire": "..."}` sans `cibles` fonctionne comme avant et vise la cible par défaut. La réponse est désormais `202` (la lecture est asynchrone) et contient un `id` de suivi.

### `GET /file/<id>`

Suivi d'un job :

```json
{"id": 12, "texte": "Le repas est prêt", "priorite": 5,
 "cibles": ["salon", "cuisine"], "statut": "termine", "depuis_cache": true,
 "erreur": null,
 "pieces": {"salon": {"etat": "termine"}, "cuisine": {"etat": "termine"}}}
```

Statuts d'un job :

| Statut | Signification |
|---|---|
| `en_attente` | Dans la file |
| `synthese` | Récupération ou génération de l'audio |
| `lecture` | Lecture en cours sur les pièces |
| `termine` | Toutes les pièces ont joué |
| `partiel` | Au moins une pièce a échoué, les autres ont joué (détail dans `erreur` et `pieces`) |
| `erreur` | Aucune pièce n'a joué, ou échec de la synthèse |
| `annule` | Retiré de la file par `DELETE /file` |

### Autres routes

| Route | Description |
|---|---|
| `GET /pieces` | Pièces, zones (développées en pièces) et cible par défaut |
| `GET /file` | Job en cours et jobs en attente |
| `DELETE /file` | Annule les jobs en attente (la lecture en cours se termine) |
| `GET /cache` | Statistiques : fichiers, octets, `hits`, `misses` |
| `DELETE /cache` | Vide le cache |

L'API n'a **aucune authentification** : elle est destinée à un réseau local de confiance.

## Cache

- Clé : SHA-256 de `langue|tld|texte` ; fichier `<hash>.mp3` dans `./cache`.
- Un texte déjà synthétisé est relu sans appel à Google, donc sans latence réseau. Il reste lisible même si Internet est coupé.
- Écriture atomique (fichier temporaire puis renommage) : pas de fichier à moitié écrit.
- Éviction LRU : quand `TTS_CACHE_MAX_FILES` ou `TTS_CACHE_MAX_BYTES` est dépassé, les fichiers les moins récemment utilisés sont supprimés.
- Le cache survit aux redémarrages du conteneur. Les fichiers sont régénérables, donc `cache/` n'a pas vocation à être versionné ni sauvegardé.

## File d'attente et priorités

- File unique en mémoire, traitée par un seul worker : un message à la fois, pièces en parallèle.
- Ordre : priorité croissante (0 avant 9), puis ordre d'arrivée.
- Un message urgent **ne coupe pas** la lecture en cours : il passe devant les messages en attente.
- La file est perdue à un redémarrage du service (le cache, lui, est conservé).

### Synchronisation entre pièces

Les lectures partent d'un instant commun (`TTS_START_MARGIN` après le test de connexion), plus le `decalage_ms` de chaque pièce. L'écart de lancement est de l'ordre de la milliseconde côté serveur ; la synchronisation réellement audible dépend ensuite du réseau Wi-Fi et de la latence de chaque Pi. Pour la caler, il faut mesurer et ajuster `decalage_ms`.

## Prérequis côté Raspberry Pi

Chaque Pi doit :

1. exécuter PulseAudio avec le protocole TCP activé (`module-native-protocol-tcp`, port 4713) ;
2. n'autoriser que l'IP de la machine qui héberge le conteneur (`auth-ip-acl=...`) : sans ACL, n'importe qui sur le réseau peut jouer du son ;
3. idéalement, ne pas mettre la sortie en veille : retirer `module-suspend-on-idle` de la configuration Pulse (sinon le début des phrases peut être coupé) ;
4. idéalement, désactiver l'économie d'énergie Wi-Fi (`iw wlan0 set power_save off`) pour éviter les à-coups de latence ;
5. être résolvable par son nom depuis le conteneur (`airpi01`, `airpi02`, `airpi03`).

## Déploiement

```bash
# 1. Préparer les dossiers (cache accessible à l'utilisateur du conteneur)
mkdir -p config cache
sudo chown 1000:1000 cache          # adapter à l'UID défini dans docker-compose.yml

# 2. Adapter config/pieces.yml à l'installation

# 3. Construire et démarrer
docker compose up -d --build

# 4. Suivre les logs
docker compose logs -f text2speak
```

Points à vérifier :

- L'UID du conteneur (`UID` dans `docker-compose.yml`, 1000 par défaut) doit correspondre au propriétaire de `./cache`.
- Si SELinux est actif sur l'hôte, ajouter `:z` au volume `./cache`.
- Si le conteneur ne résout pas les noms des Pi, décommenter `dns:` dans `docker-compose.yml` et y indiquer l'IP du serveur DNS local.
- Après modification de `pieces.yml` : `docker compose restart`.
- Le service utilise le serveur de développement Flask : il doit tourner dans **un seul processus** (la file et le worker sont en mémoire). Ne pas le placer derrière plusieurs workers Gunicorn ou uWSGI.

## Tests

`test_tts.sh` interroge un service déjà démarré. Il ne dépend que de `bash` et `curl`.

```bash
bash test_tts.sh                          # cible http://localhost:1337
bash test_tts.sh http://192.168.1.x:1337  # autre hôte
TTS_TEST_CIBLES=maison bash test_tts.sh   # ajoute un test multi-pièces
```

Contrôles effectués :

1. service joignable ;
2. entrées invalides refusées (code 400) ;
3. premier envoi (cache froid) puis second envoi identique (cache chaud) ;
4. file d'attente avec trois priorités ;
5. vidage de la file ;
6. statistiques du cache ;
7. `GET /pieces`, cible inconnue et `cibles` invalide ;
8. lecture multi-pièces (si `TTS_TEST_CIBLES` est défini).

Le test **parle réellement** dans les pièces ciblées et appelle gTTS (connexion Internet requise pour les textes inédits). L'étape de vidage annule aussi les messages réels éventuellement en attente : ne pas le lancer pendant une utilisation normale.

## Dépannage

| Symptôme | Cause probable | Piste |
|---|---|---|
| Pièce en erreur `injoignable (hôte:4713)` | Nom non résolu, Pi éteint, port 4713 fermé ou bloqué par l'ACL | Tester `getent hosts airpi01` et `nc -vz airpi01 4713` depuis l'hôte ou le conteneur |
| Erreur `paplay: Connection refused` / `Access denied` | Module TCP absent ou ACL qui n'autorise pas l'IP de l'hôte | Vérifier `module-native-protocol-tcp` et `auth-ip-acl` sur le Pi |
| Début de phrase coupé | Sortie Pulse en veille (`module-suspend-on-idle`) ou économie d'énergie Wi-Fi | Voir [Prérequis côté Raspberry Pi](#prérequis-côté-raspberry-pi) |
| `paplay: ... format` / MP3 non lu | libsndfile < 1.1 dans l'image | L'image `bookworm` fournit une version suffisante ; ne pas changer de base sans vérifier |
| Erreur de synthèse gTTS | Pas d'Internet, ou limitation côté Google (HTTP 429) | Les textes en cache restent lisibles ; réessayer plus tard |
| Service qui redémarre en boucle | Configuration invalide | `docker compose logs text2speak` : le message indique la ligne fautive |
| Erreur de permission sur `/var/cache/tts` | `./cache` non accessible à l'UID du conteneur | `chown` du dossier (ou `:z` si SELinux) |
| Heure des logs en UTC | `tzdata` absent | Il est installé par le Dockerfile fourni ; reconstruire l'image |
| `503 file d'attente pleine` | Plus de `TTS_QUEUE_MAX` messages en attente | Vider avec `DELETE /file` ou augmenter la limite |

## Limites connues et évolutions

Limites actuelles :

- Pas d'authentification sur l'API.
- File en mémoire, perdue au redémarrage ; un seul worker.
- Dépendance à un service Google non officiel (gTTS) pour les textes absents du cache.
- Pas de dédoublonnage des messages identiques déjà en attente.
- Un message urgent n'interrompt pas la lecture en cours.
- Synchronisation « au mieux » : à calibrer avec `decalage_ms`.

Pistes d'évolution (phase 2) :

- route de calibrage (bip dans toutes les pièces pour mesurer les écarts) ;
- plages silencieuses et volumes par pièce ;
- jeton d'authentification partagé ;
- pré-synthèse dès l'enfilement du message pour enchaîner les lectures sans délai ;
- recharge de la configuration sans redémarrage.
<!-- EoF -->
