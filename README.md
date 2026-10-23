# Text2Speak : TTS multi-pièces

Service HTTP qui transforme un texte en voix et le fait lire par un ou plusieurs haut-parleurs de la maison. Le moteur de synthèse est interchangeable : gTTS (Google, nécessite Internet), Piper (local, hors ligne) ou un serveur local compatible OpenAI comme Kokoro.

Chaque pièce correspond à un **serveur PulseAudio** (un Raspberry Pi Zero W relié à une enceinte). Le service gère :

- une **file d'attente** globale avec priorités ;
- un **cache disque** des fichiers audio déjà synthétisés ;
- un **moteur de synthèse interchangeable**, utilisable hors ligne (Piper, Kokoro) avec gTTS en secours possible ;
- le **multi-pièces** : une annonce peut viser une pièce, plusieurs, ou une zone entière, lues en parallèle ;
- un **fichier de configuration** décrivant les pièces et les zones.

Il s'agit uniquement de **voix** (annonces, notifications), pas de musique.

## Sommaire

1. [Fonctionnement](#fonctionnement)
2. [Contenu du dossier](#contenu-du-dossier)
3. [Configuration des pièces et zones](#configuration-des-pièces-et-zones)
4. [Variables d'environnement](#variables-denvironnement)
5. [Moteurs de synthèse](#moteurs-de-synthèse)
6. [API](#api)
7. [Cache](#cache)
8. [File d'attente et priorités](#file-dattente-et-priorités)
9. [Prérequis côté Raspberry Pi](#prérequis-côté-raspberry-pi)
10. [Déploiement](#déploiement)
11. [Tests](#tests)
12. [Dépannage](#dépannage)
13. [Limites connues et évolutions](#limites-connues-et-évolutions)

## Fonctionnement

```
Client (Gladys, curl, script...)
        │  POST /dire {"dire": "...", "cibles": ["maison"]}
        ▼
┌──────────────────────────── Conteneur Text2Speak ───────────────────────────┐
│  API Flask ──► file d'attente globale (priorités, FIFO à priorité égale)    │
│                        │                                                    │
│                        ▼  un seul worker                                    │
│             1. cache disque (audio indexé par hash moteur+voix+texte)       │
│                  ├─ présent : réutilisé                                     │
│                  └─ absent  : synthèse par le moteur choisi, puis cache     │
│             2. résolution des cibles (pièces / zones → liste de pièces)     │
│             3. test de connexion en parallèle (2 s max par pièce)           │
│             4. lancement simultané d'un `paplay -s <serveur>` par pièce     │
└─────────────────────────────────────────────────────────────────────────────┘
        │ flux audio via le protocole Pulse (TCP 4713)
        ▼
 airpi01 (salon)     airpi02 (cuisine)     airpi03 (salle de bain)
```

Points clés :

- `paplay` décode le fichier audio (MP3 ou WAV selon le moteur) **dans le conteneur** et envoie le son au Pulse du Pi. Les Pi n'exécutent donc aucun code applicatif : ce sont de simples haut-parleurs réseau.
- Les Pi sont joints **par leur nom** (DNS local, par exemple Pi-hole).
- La synthèse se fait **sur la machine qui héberge le conteneur** (un Rock64), jamais sur les Pi.
- La file est globale : un message destiné à plusieurs pièces joue dans toutes en même temps, et le message suivant attend la fin du plus lent.
- Une pièce injoignable n'empêche pas les autres de jouer : le job se termine alors en `partiel`.

## Contenu du dossier

```
.
├── README.md             Ce document
├── tts_server.py         Le service (API, file, cache, lecture multi-pièces)
├── tts_engines.py        Moteurs de synthèse interchangeables (gTTS, Piper, serveur OpenAI)
├── bench_tts.py          Comparaison des moteurs (vitesse, mémoire) sur la machine cible
├── requirements.txt      Dépendances Python : Flask, gTTS, PyYAML, piper-tts
├── Dockerfile            Image du service (Python 3.12 slim + paplay)
├── docker-compose.yml    Déploiement : ports, volumes, variables, serveur Kokoro (profil)
├── update.sh             Reconstruit et relance le conteneur (docker-compose)
├── test_tts.sh           Test d'usage (bash + curl)
├── .dockerignore         Exclut cache, modèles et dépôt Git du contexte de build
├── .gitignore            Exclut cache, modèles, résultats de comparaison et .env
├── config/
│   └── pieces.yml        Pièces, zones, cible par défaut
├── cache/                Cache audio (créé par toi, monté dans le conteneur)
├── models/               Voix Piper (.onnx), créé par toi, monté dans le conteneur
└── bench_out/            Résultats et fichiers audio de bench_tts.py (créé par toi)
```

### `tts_server.py`

Organisé en blocs :

| Bloc | Rôle |
|---|---|
| Configuration | Lecture des variables d'environnement `TTS_*` |
| Pièces et zones | `Piece`, `Layout`, `load_layout` : lecture et validation du YAML, résolution des zones (imbriquées, détection des cycles) |
| `AudioCache` | Cache audio (MP3 ou WAV) sur disque, clé SHA-256 de `moteur\|voix\|vitesse\|texte`, écriture atomique, éviction LRU |
| `Job` et file | `PriorityQueue`, registre des jobs (historique des 100 derniers terminés) |
| Lecture | `check_reachable` (test TCP), `play_on` (un `paplay`), `play_all` (parallélisme et statuts) |
| `worker` | Thread unique qui dépile les jobs, synthétise (moteur principal, puis moteur de secours si configuré) et joue |
| API Flask | Routes `/dire`, `/pieces`, `/moteur`, `/file`, `/cache` |

### `tts_engines.py`

Les moteurs de synthèse, indépendants de Flask (utilisés aussi par `bench_tts.py`) :

| Classe | Moteur | Format produit |
|---|---|---|
| `GTTSEngine` | gTTS (Google, Internet requis) | MP3 |
| `PiperEngine` | Piper, local, en processus (CPU, ONNX) | WAV |
| `OpenAIEngine` | Serveur local compatible OpenAI (`/v1/audio/speech`), par exemple Kokoro-FastAPI | WAV |

Chaque moteur fournit un identifiant (entré dans la clé du cache), un préchargement et une méthode de synthèse. Le moteur `openai` réécrit les WAV de type « flux » (en-tête sans taille) avec un en-tête propre.

### `bench_tts.py`

Compare les moteurs sur la machine cible (voir [Moteurs de synthèse](#moteurs-de-synthèse)).

### `requirements.txt`

`Flask` (API), `gTTS` (synthèse Google), `PyYAML` (lecture de `pieces.yml`), `piper-tts` (synthèse locale ; installe aussi `onnxruntime`). Les versions ne sont pas épinglées : pour un build reproductible, fige-les avec `pip freeze` une fois le service validé.

### `Dockerfile`

- Image `python:3.12-slim-bookworm` (Python ≥ 3.10 requis par le code).
- Installe `pulseaudio-utils` (fournit `paplay`) et `tzdata` (pour que `TZ=Europe/Paris` fonctionne).
- Crée un utilisateur non-root `text2speak` (UID réglable par `ARG UID`, 1000 par défaut). Cet UID doit être propriétaire de `./cache` (et de `./models` pour y télécharger des voix) sur l'hôte.
- Déclare un `HEALTHCHECK` sur `GET /cache`, avec 60 s de délai au démarrage (le chargement d'un modèle est lent sur un petit processeur).

### `docker-compose.yml`

- Publie le port `1337`.
- Monte `./config` en lecture seule sur `/config`, `./cache` sur `/var/cache/tts`, `./models` sur `/models` et `./bench_out` sur `/bench-out`.
- Transmet les variables du moteur de synthèse, modifiables dans un fichier `.env` placé à côté (`TTS_ENGINE=piper`, etc.).
- Définit le service `kokoro`, démarré seulement avec `--profile kokoro`.
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
| `TTS_ENGINE` | `gtts` | Moteur de synthèse : `gtts`, `piper` ou `openai` |
| `TTS_FALLBACK_ENGINE` | (vide) | Moteur de secours si le principal échoue (par exemple `gtts`, qui nécessite Internet) |
| `TTS_SPEED` | `1.0` | Vitesse de la voix (1.2 = plus rapide) ; sans effet avec gTTS |
| `TTS_LANG` | `fr` | Langue de synthèse (gTTS uniquement) |
| `TTS_TLD` | `fr` | Domaine Google utilisé par gTTS (accent, gTTS uniquement) |
| `TTS_PIPER_MODEL` | `/models/fr_FR-siwis-medium.onnx` | Modèle Piper ; le fichier `.onnx.json` doit se trouver à côté |
| `TTS_PIPER_SPEAKER` | (vide) | Numéro de locuteur, pour les modèles multi-voix |
| `TTS_OPENAI_URL` | `http://kokoro:8880/v1` | Adresse du serveur compatible OpenAI |
| `TTS_OPENAI_MODEL` | `kokoro` | Nom du modèle envoyé au serveur |
| `TTS_OPENAI_VOICE` | `ff_siwis` | Voix demandée au serveur |
| `TTS_OPENAI_API_KEY` | (vide) | Clé d'API, si le serveur en exige une |
| `TTS_OPENAI_TIMEOUT` | `120` | Délai maximal d'une synthèse (s) |
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

## Moteurs de synthèse

`TTS_ENGINE` choisit le moteur. Les trois sont conçus pour le français ; le choix dépend du matériel et de ce que tu entends comme qualité.

| Moteur | Hors ligne | Format | Matériel | Remarques |
|---|---|---|---|---|
| `gtts` | Non | MP3 | Aucun | Moteur historique. Service Google non officiel, limitations possibles (HTTP 429). |
| `piper` | Oui | WAV | CPU modeste | Léger (modèles bien sous 100 Mo), voix françaises `siwis`, `tom`, `upmc`, etc. Moins naturel que les moteurs récents. |
| `openai` | Oui | WAV | CPU, plus de mémoire | Serveur séparé, par exemple Kokoro (voix française `ff_siwis`). Plus naturel, plus lourd. |

### Piper

1. Créer les dossiers et télécharger une voix (le conteneur doit pouvoir joindre Hugging Face une fois) :

```bash
mkdir -p models bench_out
sudo chown 1000:1000 models bench_out          # adapter à l'UID du docker-compose.yml
docker compose build
docker compose run --rm text2speak python -m piper.download_voices fr_FR-siwis-medium --download-dir /models
```

2. Activer le moteur : créer un fichier `.env` à côté du `docker-compose.yml` :

```
TTS_ENGINE=piper
# TTS_PIPER_MODEL=/models/fr_FR-siwis-medium.onnx
```

3. `docker compose up -d`, puis `curl localhost:1337/moteur` pour vérifier.

D'autres voix françaises existent (`tom`, `upmc`, `gilles`, `mls`...) : la liste et les niveaux de qualité disponibles figurent sur la page des voix Piper (Hugging Face, `rhasspy/piper-voices`, dossier `fr/fr_FR`), avec la licence de chacune.

### Kokoro (serveur compatible OpenAI)

Le service `kokoro` du `docker-compose.yml` démarre l'image CPU de Kokoro-FastAPI (amd64 et arm64) :

```bash
docker compose --profile kokoro up -d
# .env :
#   TTS_ENGINE=openai
#   TTS_OPENAI_VOICE=ff_siwis
```

Nécessite docker compose ≥ 1.28 (profils). L'image n'est pas épinglée : fixe un tag de version une fois validée. Le premier démarrage peut être long (chargement du modèle). La mémoire est plafonnée à 2 Go (`mem_limit`) pour protéger l'hôte ; à ajuster d'après `docker stats`.

Tout autre serveur compatible OpenAI (`POST /v1/audio/speech`, réponse WAV) fonctionne en changeant `TTS_OPENAI_URL`, `TTS_OPENAI_MODEL` et `TTS_OPENAI_VOICE`.

### Comparer les moteurs sur ta machine

Les performances dépendent fortement du processeur : **mesure sur le Rock64 lui-même**.

```bash
docker compose --profile kokoro up -d kokoro        # seulement pour comparer Kokoro
docker compose run --rm text2speak python /text2speak/bench_tts.py --engines piper,openai
# plusieurs voix Piper :
docker compose run --rm text2speak python /text2speak/bench_tts.py --engines piper \
    --piper-model /models/fr_FR-siwis-medium.onnx --piper-model /models/fr_FR-tom-medium.onnx
docker stats --no-stream                            # mémoire du conteneur Kokoro, dans un autre terminal
```

Le script synthétise 8 phrases (courtes, longues, avec heures, unités et dates) et écrit dans `./bench_out/` : `resultats.md`, `resultats.json` et un dossier de fichiers audio par moteur, à **écouter**. Il mesure le temps de synthèse (le délai ajouté avant que la voix ne démarre), le facteur temps réel (RTF = synthèse / durée audio ; inférieur à 1 = plus rapide que la parole), la mémoire maximale des moteurs en processus, et la température et la fréquence du processeur avant et après (pour repérer un ralentissement thermique). Chaque moteur tourne dans un processus séparé : un dépassement de mémoire n'arrête pas les autres mesures. Options : `--phrases fichier.txt` (tes propres phrases), `--repeat N`.

### Changer de moteur

- La clé du cache inclut le moteur, la voix et la vitesse : changer de moteur ne relit jamais un ancien fichier d'un autre moteur. Les anciens fichiers restent sur le disque jusqu'à leur éviction ; `curl -X DELETE localhost:1337/cache` les supprime tout de suite.
- `TTS_FALLBACK_ENGINE=gtts` permet de basculer sur Google si le moteur local échoue. Le job indique alors `"moteur": "gtts"`. Sans cette variable, un échec du moteur local donne un job en `erreur`.
- Le modèle est chargé en tâche de fond au démarrage (visible dans `docker compose logs`) : la première phrase ne le paie pas.

### Points de vigilance

- **Première synthèse plus lente** que gTTS sur un processeur modeste. Le cache la rend instantanée ensuite ; les phrases récurrentes peuvent être envoyées une fois pour être pré-générées.
- **Chiffres, heures, unités, abréviations** : un moteur local les lit parfois moins bien que Google (« 12 °C », « 3 h 45 », « 15 % »). À vérifier à l'écoute avec le script de comparaison ; un prétraitement du texte peut être ajouté si nécessaire.
- **WAV plus volumineux que MP3** (environ 44 Ko par seconde en mono 22 kHz pour Piper) : à surveiller sur une carte SD, et à prendre en compte dans `TTS_CACHE_MAX_BYTES`.
- **Mémoire** : Kokoro et le service partagent la RAM de l'hôte (4 Go sur un Rock64). Garde la limite `mem_limit` et surveille `docker stats`.
- **Licences** : Piper (code en GPL-3.0, licence propre à chaque voix), Kokoro (Apache-2.0). À relire avant tout usage autre que domestique.

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
 "moteur": "piper", "erreur": null,
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
| `GET /moteur` | Moteur de synthèse actif, format produit et moteur de secours |
| `GET /file` | Job en cours et jobs en attente |
| `DELETE /file` | Annule les jobs en attente (la lecture en cours se termine) |
| `GET /cache` | Statistiques : fichiers, octets, `hits`, `misses` |
| `DELETE /cache` | Vide le cache |

L'API n'a **aucune authentification** : elle est destinée à un réseau local de confiance.

## Cache

- Clé : SHA-256 de `moteur|voix|vitesse|texte` ; fichier `<hash>.mp3` (gTTS) ou `<hash>.wav` (Piper, Kokoro) dans `./cache`.
- Un texte déjà synthétisé est relu sans appeler le moteur : pas de latence de synthèse, et aucune connexion nécessaire, même avec gTTS.
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
# 1. Préparer les dossiers (accessibles à l'utilisateur du conteneur)
mkdir -p config cache models bench_out
sudo chown 1000:1000 cache models bench_out   # adapter à l'UID défini dans docker-compose.yml

# 2. Adapter config/pieces.yml à l'installation

# 3. Construire et démarrer (ou ./update.sh, qui utilise docker-compose)
docker compose up -d --build

# 4. Suivre les logs
docker compose logs -f text2speak
```

Points à vérifier :

- L'UID du conteneur (`UID` dans `docker-compose.yml`, 1000 par défaut) doit correspondre au propriétaire de `./cache` (et de `./models` pour télécharger des voix).
- `update.sh` appelle `docker-compose` (avec un tiret) et `sudo` ; avec le plugin `docker compose` seul, adapte le script.
- Le moteur local se règle dans `.env` (voir [Moteurs de synthèse](#moteurs-de-synthèse)) ; sans `.env`, le service reste sur gTTS.
- Si SELinux est actif sur l'hôte, ajouter `:z` au volume `./cache`.
- Si le conteneur ne résout pas les noms des Pi, décommenter `dns:` dans `docker-compose.yml` et y indiquer l'IP du serveur DNS local.
- Après modification de `pieces.yml` : `docker compose restart`.
- Le service utilise le serveur de développement Flask : il doit tourner dans **un seul processus** (la file et le worker sont en mémoire). Ne pas le placer derrière plusieurs workers Gunicorn ou uWSGI.

## Tests

`test_tts.sh` interroge un service déjà démarré. Il ne dépend que de `bash` et `curl` ; les tests par pièce et par zone lisent `GET /pieces` avec `python3` ou `jq` (l'un des deux suffit).

```bash
bash test_tts.sh                                   # tout, sur http://localhost:1337
bash test_tts.sh http://192.168.1.x:1337           # autre hôte
TTS_TEST_PORTEE=base bash test_tts.sh              # file d'attente et cache seulement
TTS_TEST_PORTEE=pieces bash test_tts.sh            # pièces et zones seulement
TTS_TEST_PORTEE=pieces TTS_TEST_PIECES="salon" TTS_TEST_ZONES="" bash test_tts.sh   # une seule pièce
```

`TTS_TEST_PIECES` et `TTS_TEST_ZONES` (listes séparées par des espaces) restreignent les pièces et zones testées ; sans elles, tout ce que renvoie `/pieces` est testé.

Contrôles effectués :

1. service joignable ;
2. entrées invalides refusées (code 400) ;
3. premier envoi (cache froid) puis second envoi identique (cache chaud) ;
4. file d'attente avec trois priorités ;
5. vidage de la file ;
6. statistiques du cache ;
7. `GET /pieces`, cible inconnue, `cibles` invalide ou vide ;
8. lecture **pièce par pièce** : pièces résolues et lecture terminée pour chacune ;
9. lecture **zone par zone** : la zone se développe en les bonnes pièces et chacune joue ;
10. zone et pièce déjà incluse (le doublon est éliminé), nom de pièce en majuscules ;
11. cible par défaut (sans champ `cibles`).

Le bilan liste les contrôles en échec avec le message d'erreur du serveur (par exemple une pièce injoignable).

Le test **parle réellement** dans les pièces ciblées et appelle le moteur de synthèse (avec gTTS, une connexion Internet est requise pour les textes inédits). L'étape de vidage annule aussi les messages réels éventuellement en attente : ne pas le lancer pendant une utilisation normale.

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
| Au démarrage : `modèle Piper introuvable` | Voix non téléchargée, ou `TTS_PIPER_MODEL` faux | Voir [Piper](#piper) ; le `.onnx.json` doit être à côté du `.onnx` |
| Job en `erreur` : `serveur TTS injoignable` | Conteneur `kokoro` non démarré ou pas encore prêt | `docker compose --profile kokoro up -d`, puis `docker compose logs kokoro` |
| Service ou Kokoro tué sans message | Mémoire de l'hôte épuisée (OOM) | `docker stats`, baisser `mem_limit`, essayer Piper |
| Synthèse très lente, voix qui tarde | Processeur lent ou ralenti par la chaleur | Lancer `bench_tts.py` et regarder le RTF et la température |
| Chiffres ou heures mal lus | Limite du moteur local | Écouter les fichiers de `bench_out/`, comparer les voix ou les moteurs |
| `503 file d'attente pleine` | Plus de `TTS_QUEUE_MAX` messages en attente | Vider avec `DELETE /file` ou augmenter la limite |

## Limites connues et évolutions

Limites actuelles :

- Pas d'authentification sur l'API.
- File en mémoire, perdue au redémarrage ; un seul worker.
- Avec gTTS : dépendance à un service Google non officiel pour les textes absents du cache. Les moteurs locaux (Piper, Kokoro) n'ont pas cette dépendance.
- Les performances des moteurs locaux sur le Rock64 doivent être mesurées (`bench_tts.py`) ; elles ne sont pas garanties.
- Pas de prétraitement du texte (chiffres, unités, abréviations) avant la synthèse.
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
