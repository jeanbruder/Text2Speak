#!/usr/bin/env python3
"""Serveur TTS (gTTS) multi-pièces avec file d'attente globale et cache disque.

Chaque pièce correspond à un serveur PulseAudio (un Raspberry Pi) joint par son nom
(`paplay -s <serveur>`). Les pièces et zones sont décrites dans un fichier YAML.

API :
  POST   /dire          {"dire": "texte", "cibles": ["salon", "rez_de_chaussee"], "priorite": 5}
                        -> 202 + id du job. "cibles" (pièces et/ou zones) est facultatif :
                        sans lui, la liste "defaut" de la configuration est utilisée.
  GET    /pieces                                            -> pièces, zones et cible par défaut
  GET    /file                                              -> état de la file
  GET    /file/<id>                                         -> état d'un job, pièce par pièce
  DELETE /file                                              -> vide la file (la lecture en cours finit)
  GET    /cache                                             -> statistiques du cache
  DELETE /cache                                             -> purge du cache

Priorité : 0 = la plus urgente, 9 = la moins urgente (défaut 5).
À priorité égale, l'ordre d'arrivée (FIFO) est respecté.
"""
import hashlib
import itertools
import logging
import os
import queue
import socket
import subprocess
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from flask import Flask, jsonify, request
from gtts import gTTS

# --- Configuration (surchargeable par variables d'environnement) -------------
CONFIG_PATH = Path(os.environ.get("TTS_CONFIG", "/config/pieces.yml"))
FALLBACK_SERVER = os.environ.get("TTS_SINK", "airpi02")  # utilisé seulement sans fichier de config
LANG = os.environ.get("TTS_LANG", "fr")
TLD = os.environ.get("TTS_TLD", "fr")
CACHE_DIR = Path(os.environ.get("TTS_CACHE_DIR", "/var/cache/tts"))
CACHE_MAX_FILES = int(os.environ.get("TTS_CACHE_MAX_FILES", "500"))
CACHE_MAX_BYTES = int(os.environ.get("TTS_CACHE_MAX_BYTES", str(100 * 1024 * 1024)))
QUEUE_MAX = int(os.environ.get("TTS_QUEUE_MAX", "50"))
TEXT_MAX_CHARS = int(os.environ.get("TTS_TEXT_MAX_CHARS", "500"))
PLAY_TIMEOUT = int(os.environ.get("TTS_PLAY_TIMEOUT", "120"))
CONNECT_TIMEOUT = float(os.environ.get("TTS_CONNECT_TIMEOUT", "2"))
START_MARGIN = float(os.environ.get("TTS_START_MARGIN", "0.05"))  # marge avant le départ commun (s)
PORT = int(os.environ.get("TTS_PORT", "1337"))
HOST = os.environ.get("TTS_HOST", "0.0.0.0")
HISTORY_MAX = 100  # nombre de jobs terminés conservés pour consultation
PULSE_DEFAULT_PORT = 4713

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("tts")


# --- Pièces et zones ------------------------------------------------------------
class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Piece:
    name: str
    serveur: str                 # valeur passée à `paplay -s` (nom d'hôte, ou tcp:hôte:port)
    peripherique: str | None = None   # sink sur ce serveur (`paplay -d`), facultatif
    volume: int | None = None    # pourcentage (100 = volume nominal), facultatif
    decalage_ms: int = 0         # retard ajouté au départ de cette pièce (calage fin)


class Layout:
    """Pièces, zones et cible par défaut, validés au chargement."""

    def __init__(self, pieces: dict[str, Piece], zones: dict[str, list[str]], defaut: list[str]):
        self.pieces = pieces
        self.zones = zones
        self.defaut_names = defaut
        self.defaut = self.resolve(defaut)  # vérifie aussi la validité de "defaut"
        for zone in zones:                  # vérifie toutes les zones (inconnues, cycles)
            self.resolve([zone])

    def resolve(self, names: list[str]) -> list[Piece]:
        """Développe pièces et zones en une liste de pièces sans doublon, dans l'ordre du fichier."""
        found: set[str] = set()

        def expand(name: str, path: tuple[str, ...]) -> None:
            if name in self.pieces:
                found.add(name)
            elif name in self.zones:
                if name in path:
                    raise ConfigError(f"zones circulaires : {' -> '.join(path + (name,))}")
                for child in self.zones[name]:
                    expand(child, path + (name,))
            else:
                raise ConfigError(f"cible inconnue : '{name}'")

        for n in names:
            expand(n, ())
        return [p for name, p in self.pieces.items() if name in found]


def _norm(name) -> str:
    return str(name).strip().lower()


def load_layout(path: Path) -> Layout:
    if not path.exists():
        log.warning("config %s absente : une seule pièce 'defaut' vers '%s'", path, FALLBACK_SERVER)
        piece = Piece(name="defaut", serveur=FALLBACK_SERVER)
        return Layout({"defaut": piece}, {}, ["defaut"])

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML invalide dans {path} : {e}") from e
    if not isinstance(raw, dict) or not isinstance(raw.get("pieces"), dict) or not raw["pieces"]:
        raise ConfigError("la section 'pieces' est obligatoire et ne doit pas être vide")

    pieces: dict[str, Piece] = {}
    for name, conf in raw["pieces"].items():
        key = _norm(name)
        if not isinstance(conf, dict) or not conf.get("serveur"):
            raise ConfigError(f"pièce '{name}' : le champ 'serveur' est obligatoire")
        try:
            volume = None if conf.get("volume") is None else int(conf["volume"])
            decalage = int(conf.get("decalage_ms", 0))
        except (TypeError, ValueError) as e:
            raise ConfigError(f"pièce '{name}' : 'volume' et 'decalage_ms' doivent être des entiers") from e
        if volume is not None and not 0 <= volume <= 150:
            raise ConfigError(f"pièce '{name}' : 'volume' doit être compris entre 0 et 150")
        if decalage < 0:
            raise ConfigError(f"pièce '{name}' : 'decalage_ms' ne peut pas être négatif")
        if key in pieces:
            raise ConfigError(f"pièce en double : '{key}'")
        pieces[key] = Piece(
            name=key,
            serveur=str(conf["serveur"]).strip(),
            peripherique=str(conf["peripherique"]).strip() if conf.get("peripherique") else None,
            volume=volume,
            decalage_ms=decalage,
        )

    zones: dict[str, list[str]] = {}
    for name, members in (raw.get("zones") or {}).items():
        key = _norm(name)
        if key in pieces:
            raise ConfigError(f"'{key}' est à la fois une pièce et une zone")
        if not isinstance(members, list) or not members:
            raise ConfigError(f"zone '{name}' : une liste non vide est attendue")
        zones[key] = [_norm(m) for m in members]

    defaut = raw.get("defaut")
    if defaut is None:
        if len(pieces) != 1:
            raise ConfigError("'defaut' est obligatoire dès qu'il y a plusieurs pièces")
        defaut = list(pieces)
    elif isinstance(defaut, str):
        defaut = [defaut]
    if not isinstance(defaut, list) or not defaut:
        raise ConfigError("'defaut' doit être une liste non vide de pièces ou zones")

    return Layout(pieces, zones, [_norm(d) for d in defaut])


# --- Cache disque -------------------------------------------------------------
class AudioCache:
    """Cache de fichiers MP3 indexés par hash (langue, tld, texte), éviction LRU."""

    def __init__(self, directory: Path, max_files: int, max_bytes: int):
        self.dir = directory
        self.max_files = max_files
        self.max_bytes = max_bytes
        self.lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.dir.mkdir(parents=True, exist_ok=True)
        # Nettoyage des restes d'une éventuelle interruption pendant une écriture
        for leftover in self.dir.glob("*.tmp"):
            leftover.unlink(missing_ok=True)

    @staticmethod
    def key(text: str) -> str:
        raw = f"{LANG}|{TLD}|{text}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def path_for(self, text: str) -> Path:
        return self.dir / f"{self.key(text)}.mp3"

    def get_or_create(self, text: str) -> tuple[Path, bool]:
        """Retourne (chemin, depuis_cache)."""
        path = self.path_for(text)
        with self.lock:
            if path.exists() and path.stat().st_size > 0:
                os.utime(path, None)  # marque comme récemment utilisé (LRU)
                self.hits += 1
                return path, True
            self.misses += 1

        # Synthèse hors verrou : l'appel réseau peut être long
        tmp = path.with_suffix(".tmp")
        try:
            gTTS(text=text, lang=LANG, tld=TLD).save(str(tmp))
            if tmp.stat().st_size == 0:
                raise RuntimeError("fichier audio vide retourné par gTTS")
            os.replace(tmp, path)  # écriture atomique
        finally:
            tmp.unlink(missing_ok=True)

        self.evict()
        return path, False

    def evict(self) -> None:
        with self.lock:
            files = sorted(self.dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
            total = sum(p.stat().st_size for p in files)
            while files and (len(files) > self.max_files or total > self.max_bytes):
                victim = files.pop(0)
                total -= victim.stat().st_size
                victim.unlink(missing_ok=True)
                log.info("cache: éviction de %s", victim.name)

    def stats(self) -> dict:
        files = list(self.dir.glob("*.mp3"))
        return {
            "fichiers": len(files),
            "octets": sum(p.stat().st_size for p in files),
            "max_fichiers": self.max_files,
            "max_octets": self.max_bytes,
            "hits": self.hits,
            "misses": self.misses,
        }

    def purge(self) -> int:
        with self.lock:
            files = list(self.dir.glob("*.mp3"))
            for p in files:
                p.unlink(missing_ok=True)
            return len(files)


# --- File d'attente -----------------------------------------------------------
@dataclass
class Job:
    id: int
    text: str
    priority: int
    cibles: list[str]
    status: str = "en_attente"  # en_attente | synthese | lecture | termine | partiel | erreur | annule
    from_cache: bool | None = None
    error: str | None = None
    pieces: dict[str, dict] = field(default_factory=dict)  # résultat par pièce
    created: float = field(default_factory=time.time)
    finished: float | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "texte": self.text,
            "priorite": self.priority,
            "cibles": self.cibles,
            "statut": self.status,
            "depuis_cache": self.from_cache,
            "erreur": self.error,
            "pieces": self.pieces,
        }


try:
    layout = load_layout(CONFIG_PATH)
except ConfigError as e:
    log.error("configuration invalide (%s) : %s", CONFIG_PATH, e)
    raise SystemExit(1)
cache = AudioCache(CACHE_DIR, CACHE_MAX_FILES, CACHE_MAX_BYTES)
jobs_queue: queue.PriorityQueue = queue.PriorityQueue(maxsize=QUEUE_MAX)
jobs_registry: OrderedDict[int, Job] = OrderedDict()
registry_lock = threading.Lock()
counter = itertools.count(1)
current_job: Job | None = None


def register(job: Job) -> None:
    with registry_lock:
        jobs_registry[job.id] = job
        # Purge des plus anciens jobs terminés uniquement
        done = [i for i, j in jobs_registry.items()
                if j.status in ("termine", "partiel", "erreur", "annule")]
        for i in done[:-HISTORY_MAX] if len(done) > HISTORY_MAX else []:
            jobs_registry.pop(i, None)


# --- Lecture sur les pièces -------------------------------------------------------
def _host_port(serveur: str) -> tuple[str, int] | None:
    """Extrait (hôte, port) pour un test de connexion ; None pour un socket local."""
    s = serveur
    if s.startswith("unix:") or s.startswith("/"):
        return None
    if s.startswith("tcp:"):
        s = s[4:]
    host, _, port = s.rpartition(":")
    if host and port.isdigit():
        return host, int(port)
    return s, PULSE_DEFAULT_PORT


def check_reachable(piece: Piece) -> str | None:
    """None si le serveur Pulse répond ; sinon un message d'erreur (évite un paplay bloqué)."""
    target = _host_port(piece.serveur)
    if target is None:
        return None
    try:
        with socket.create_connection(target, timeout=CONNECT_TIMEOUT):
            return None
    except OSError as e:
        return f"injoignable ({target[0]}:{target[1]}) : {e}"


def play_on(piece: Piece, path: Path, t0: float) -> str | None:
    """Joue le fichier sur une pièce à l'instant monotone t0 + décalage. None si OK, sinon erreur."""
    delay = t0 + piece.decalage_ms / 1000 - time.monotonic()
    if delay > 0:
        time.sleep(delay)
    cmd = ["paplay", "-s", piece.serveur]
    if piece.peripherique:
        cmd += ["-d", piece.peripherique]
    if piece.volume is not None:
        cmd += [f"--volume={round(65536 * piece.volume / 100)}"]
    cmd.append(str(path))
    try:
        # Liste d'arguments (pas de shell) : pas d'injection possible
        subprocess.run(cmd, check=True, timeout=PLAY_TIMEOUT, capture_output=True)
        return None
    except subprocess.CalledProcessError as e:
        return f"paplay: {e.stderr.decode(errors='replace').strip() or e}"
    except subprocess.TimeoutExpired:
        return f"délai dépassé ({PLAY_TIMEOUT}s)"
    except FileNotFoundError:
        return "paplay introuvable (pulseaudio-utils manquant ?)"


def play_all(job: Job, path: Path) -> None:
    """Lit sur toutes les pièces ciblées, en parallèle, avec un départ commun."""
    pieces = [layout.pieces[name] for name in job.cibles]
    with ThreadPoolExecutor(max_workers=len(pieces)) as pool:
        # 1) joignabilité en parallèle : une pièce hors ligne ne retarde ni ne bloque les autres
        reach = list(pool.map(check_reachable, pieces))
        results: dict[str, str | None] = {}
        playable = []
        for piece, err in zip(pieces, reach):
            if err:
                results[piece.name] = err
            else:
                playable.append(piece)

        # 2) départ commun : tous les threads visent le même instant, plus le décalage de la pièce
        t0 = time.monotonic() + START_MARGIN
        futures = {p.name: pool.submit(play_on, p, path, t0) for p in playable}
        for name, fut in futures.items():
            results[name] = fut.result()

    for name in job.cibles:
        err = results.get(name)
        job.pieces[name] = {"etat": "erreur", "erreur": err} if err else {"etat": "termine"}

    failed = [n for n in job.cibles if job.pieces[n]["etat"] == "erreur"]
    if not failed:
        job.status = "termine"
    elif len(failed) == len(job.cibles):
        job.status = "erreur"
        job.error = "; ".join(f"{n}: {job.pieces[n]['erreur']}" for n in failed)
    else:
        job.status = "partiel"
        job.error = "; ".join(f"{n}: {job.pieces[n]['erreur']}" for n in failed)


def worker() -> None:
    global current_job
    while True:
        _prio, _seq, job = jobs_queue.get()
        try:
            if job.status == "annule":
                continue
            current_job = job
            job.status = "synthese"
            path, hit = cache.get_or_create(job.text)
            job.from_cache = hit
            job.status = "lecture"
            play_all(job, path)
            log.info("job %s -> %s (cache=%s, cibles=%s)", job.id, job.status, hit, job.cibles)
        except Exception as e:  # noqa: BLE001 - on isole chaque job
            job.status = "erreur"
            job.error = str(e)
            log.exception("job %s en erreur", job.id)
        finally:
            job.finished = time.time()
            current_job = None
            jobs_queue.task_done()


# --- API ----------------------------------------------------------------------
app = Flask(__name__)


@app.route("/dire", methods=["POST"])
def dire():
    data = request.get_json(silent=True) or {}
    text = data.get("dire")
    if not isinstance(text, str) or not text.strip():
        return jsonify({"ERROR": "champ 'dire' manquant ou vide"}), 400
    text = text.strip()
    if len(text) > TEXT_MAX_CHARS:
        return jsonify({"ERROR": f"texte trop long (max {TEXT_MAX_CHARS} caractères)"}), 413

    try:
        priority = max(0, min(9, int(data.get("priorite", 5))))
    except (TypeError, ValueError):
        return jsonify({"ERROR": "'priorite' doit être un entier entre 0 et 9"}), 400

    cibles = data.get("cibles")
    if cibles is None:
        pieces = layout.defaut
    else:
        if isinstance(cibles, str):
            cibles = [cibles]
        if not isinstance(cibles, list) or not cibles or not all(isinstance(c, str) for c in cibles):
            return jsonify({"ERROR": "'cibles' doit être un nom ou une liste non vide de noms"}), 400
        try:
            pieces = layout.resolve([_norm(c) for c in cibles])
        except ConfigError as e:
            return jsonify({"ERROR": str(e),
                            "pieces": sorted(layout.pieces), "zones": sorted(layout.zones)}), 400

    job = Job(id=next(counter), text=text, priority=priority, cibles=[p.name for p in pieces])
    try:
        jobs_queue.put_nowait((priority, job.id, job))
    except queue.Full:
        return jsonify({"ERROR": "file d'attente pleine"}), 503
    register(job)
    return jsonify({
        "OK": text,
        "id": job.id,
        "cibles": job.cibles,
        "position": jobs_queue.qsize(),
        "en_cache": cache.path_for(text).exists(),
    }), 202


@app.route("/pieces", methods=["GET"])
def pieces_info():
    return jsonify({
        "pieces": {
            name: {
                "serveur": p.serveur,
                "peripherique": p.peripherique,
                "volume": p.volume,
                "decalage_ms": p.decalage_ms,
            }
            for name, p in layout.pieces.items()
        },
        "zones": {z: [p.name for p in layout.resolve([z])] for z in layout.zones},
        "defaut": [p.name for p in layout.defaut],
    })


@app.route("/file", methods=["GET"])
def file_status():
    with registry_lock:
        pending = [j.to_dict() for j in jobs_registry.values() if j.status == "en_attente"]
    return jsonify({
        "en_cours": current_job.to_dict() if current_job else None,
        "en_attente": pending,
        "taille": len(pending),
    })


@app.route("/file/<int:job_id>", methods=["GET"])
def job_status(job_id: int):
    with registry_lock:
        job = jobs_registry.get(job_id)
    if job is None:
        return jsonify({"ERROR": "job inconnu (ou purgé de l'historique)"}), 404
    return jsonify(job.to_dict())


@app.route("/file", methods=["DELETE"])
def file_clear():
    cancelled = 0
    with registry_lock:
        for job in jobs_registry.values():
            if job.status == "en_attente":
                job.status = "annule"  # le worker les ignorera au dépilage
                cancelled += 1
    return jsonify({"OK": True, "annules": cancelled})


@app.route("/cache", methods=["GET"])
def cache_stats():
    return jsonify(cache.stats())


@app.route("/cache", methods=["DELETE"])
def cache_purge():
    return jsonify({"OK": True, "supprimes": cache.purge()})


if __name__ == "__main__":
    threading.Thread(target=worker, name="tts-worker", daemon=True).start()
    # Pas de debug=True : le débogueur Werkzeug exposé sur 0.0.0.0 permet l'exécution de code à distance
    app.run(port=PORT, host=HOST, debug=False, threaded=True)
# EoF
