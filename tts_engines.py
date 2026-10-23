"""Moteurs de synthèse vocale interchangeables pour Text2Speak.

Trois moteurs, choisis par la variable d'environnement TTS_ENGINE :

  gtts    Google Translate TTS (nécessite Internet). Produit du MP3. Moteur historique.
  piper   Piper, local, en processus (CPU, ONNX). Produit du WAV.
          TTS_PIPER_MODEL    chemin du modèle .onnx (le .onnx.json doit se trouver à côté)
          TTS_PIPER_SPEAKER  numéro de locuteur (modèles multi-voix), facultatif
  openai  Serveur local compatible OpenAI (POST <url>/audio/speech), par exemple
          Kokoro-FastAPI. Produit du WAV.
          TTS_OPENAI_URL     défaut http://kokoro:8880/v1
          TTS_OPENAI_MODEL   défaut kokoro
          TTS_OPENAI_VOICE   défaut ff_siwis
          TTS_OPENAI_API_KEY facultatif
          TTS_OPENAI_TIMEOUT défaut 120 (s)

Réglage commun : TTS_SPEED (1.0 = normal, 1.2 = plus rapide), ignoré par gTTS.

Chaque moteur expose :
  name, ext        nom court et extension du fichier produit (.mp3 / .wav)
  ident            chaîne qui entre dans la clé du cache (moteur, voix, vitesse...)
  warmup()         préchargement (modèle), appelé au démarrage
  synthesize(text, dest)   écrit l'audio dans dest ou lève EngineError
"""
import io
import json
import os
import struct
import threading
import urllib.error
import urllib.request
import wave
from pathlib import Path


class EngineError(Exception):
    """Échec de configuration ou de synthèse d'un moteur."""


class Engine:
    name = "base"
    ext = ".wav"
    ident = "base"

    def warmup(self) -> None:
        pass

    def synthesize(self, text: str, dest: Path) -> None:
        raise NotImplementedError


# --- gTTS ------------------------------------------------------------------------
class GTTSEngine(Engine):
    name = "gtts"
    ext = ".mp3"

    def __init__(self, lang: str = "fr", tld: str = "fr"):
        self.lang = lang
        self.tld = tld
        self.ident = f"gtts|{lang}|{tld}"

    def synthesize(self, text: str, dest: Path) -> None:
        try:
            from gtts import gTTS
        except ImportError as e:
            raise EngineError("gTTS n'est pas installé") from e
        try:
            gTTS(text=text, lang=self.lang, tld=self.tld).save(str(dest))
        except Exception as e:  # noqa: BLE001 - gTTS lève des erreurs variées (réseau, 429...)
            raise EngineError(f"gTTS : {e}") from e


# --- WAV tolérant ------------------------------------------------------------------
def parse_wav_loose(data: bytes):
    """Lit un WAV PCM même si les tailles de l'en-tête sont absentes (flux).

    Retourne (canaux, octets_par_échantillon, fréquence, pcm) ou None si le format
    n'est pas du PCM entier lisible.
    """
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    pos = 12
    fmt = None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = pos + 8
        if cid == b"fmt ":
            tag, ch, rate, _byterate, _align, bits = struct.unpack("<HHIIHH", data[body:body + 16])
            if tag == 0xFFFE and size >= 26:  # WAVE_FORMAT_EXTENSIBLE : le vrai format est dans le GUID
                tag = struct.unpack("<H", data[body + 24:body + 26])[0]
            fmt = (tag, ch, rate, bits)
        elif cid == b"data":
            if fmt is None or fmt[0] != 1 or fmt[3] % 8 or not fmt[1]:
                return None
            end = len(data) if size in (0, 0xFFFFFFFF) or body + size > len(data) else body + size
            width = fmt[3] // 8
            pcm = data[body:end]
            pcm = pcm[: len(pcm) - len(pcm) % (width * fmt[1])]
            return fmt[1], width, fmt[2], pcm
        pos = body + size + (size & 1)
    return None


def write_clean_wav(dest: Path, channels: int, width: int, rate: int, pcm: bytes) -> None:
    with wave.open(str(dest), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(width)
        wf.setframerate(rate)
        wf.writeframes(pcm)


def wav_duration(path: Path) -> float:
    """Durée en secondes d'un fichier WAV, même avec un en-tête de flux."""
    data = Path(path).read_bytes()
    parsed = parse_wav_loose(data)
    if parsed is None:
        raise EngineError("WAV illisible")
    ch, width, rate, pcm = parsed
    return len(pcm) / (ch * width * rate)


# --- Piper (local, en processus) ----------------------------------------------------
class PiperEngine(Engine):
    name = "piper"
    ext = ".wav"

    def __init__(self, model_path: str, speaker: int | None = None, speed: float = 1.0):
        self.model_path = Path(model_path)
        if not self.model_path.is_file():
            raise EngineError(
                f"modèle Piper introuvable : {self.model_path} "
                "(télécharge-le : voir README, section « Moteurs de synthèse »)")
        if not Path(str(self.model_path) + ".json").is_file():
            raise EngineError(f"fichier de configuration manquant : {self.model_path}.json")
        if speed <= 0:
            raise EngineError("TTS_SPEED doit être strictement positif")
        self.speaker = speaker
        self.speed = speed
        self.ident = f"piper|{self.model_path.name}|{speaker}|{speed}"
        self._voice = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._voice is None:
                try:
                    from piper import PiperVoice
                except ImportError as e:
                    raise EngineError("le paquet piper-tts n'est pas installé") from e
                self._voice = PiperVoice.load(self.model_path)
        return self._voice

    def _syn_config(self, voice):
        from piper import SynthesisConfig
        length_scale = None
        if self.speed != 1.0:
            # length_scale est une durée : plus il est petit, plus la voix est rapide
            length_scale = voice.config.length_scale / self.speed
        return SynthesisConfig(speaker_id=self.speaker, length_scale=length_scale)

    def warmup(self) -> None:
        # Charge le modèle et fait une première inférence (la toute première est plus lente)
        voice = self._load()
        with wave.open(io.BytesIO(), "wb") as wf:
            voice.synthesize_wav("Bonjour.", wf, syn_config=self._syn_config(voice))

    def synthesize(self, text: str, dest: Path) -> None:
        try:
            voice = self._load()
            with wave.open(str(dest), "wb") as wf:
                voice.synthesize_wav(text, wf, syn_config=self._syn_config(voice))
        except EngineError:
            raise
        except Exception as e:  # noqa: BLE001
            raise EngineError(f"Piper : {e}") from e
        try:
            with wave.open(str(dest), "rb") as wf:
                empty = wf.getnframes() == 0
        except wave.Error:
            empty = True
        if empty:
            raise EngineError("Piper : audio vide (texte sans contenu prononçable ?)")


# --- Serveur compatible OpenAI (Kokoro-FastAPI, etc.) -------------------------------------
class OpenAIEngine(Engine):
    name = "openai"
    ext = ".wav"

    def __init__(self, base_url: str, model: str, voice: str, speed: float = 1.0,
                 api_key: str | None = None, timeout: float = 120):
        self.url = base_url.rstrip("/") + "/audio/speech"
        self.model = model
        self.voice = voice
        self.speed = speed
        self.api_key = api_key
        self.timeout = timeout
        self.ident = f"openai|{model}|{voice}|{speed}"

    def synthesize(self, text: str, dest: Path) -> None:
        body = json.dumps({
            "model": self.model,
            "input": text,
            "voice": self.voice,
            "response_format": "wav",
            "speed": self.speed,
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read()[:200].decode("utf-8", errors="replace")
            raise EngineError(f"serveur TTS : HTTP {e.code} {detail}") from e
        except (urllib.error.URLError, OSError) as e:
            raise EngineError(f"serveur TTS injoignable ({self.url}) : {e}") from e

        parsed = parse_wav_loose(data)
        if parsed is None:
            if data[:4] == b"RIFF":
                dest.write_bytes(data)  # WAV non PCM entier : conservé tel quel
                return
            raise EngineError("le serveur TTS n'a pas renvoyé un fichier WAV")
        channels, width, rate, pcm = parsed
        if not pcm:
            raise EngineError("le serveur TTS a renvoyé un audio vide")
        # Réécriture avec un en-tête propre (certains serveurs diffusent un WAV sans taille)
        write_clean_wav(dest, channels, width, rate, pcm)


# --- Fabrique -------------------------------------------------------------------------------
def build_engine(name: str, env=os.environ) -> Engine:
    name = (name or "").strip().lower()
    try:
        speed = float(env.get("TTS_SPEED", "1.0"))
    except ValueError as e:
        raise EngineError("TTS_SPEED doit être un nombre") from e

    if name == "gtts":
        return GTTSEngine(env.get("TTS_LANG", "fr"), env.get("TTS_TLD", "fr"))
    if name == "piper":
        speaker = env.get("TTS_PIPER_SPEAKER")
        try:
            speaker = int(speaker) if speaker not in (None, "") else None
        except ValueError as e:
            raise EngineError("TTS_PIPER_SPEAKER doit être un entier") from e
        return PiperEngine(env.get("TTS_PIPER_MODEL", "/models/fr_FR-siwis-medium.onnx"), speaker, speed)
    if name == "openai":
        try:
            timeout = float(env.get("TTS_OPENAI_TIMEOUT", "120"))
        except ValueError as e:
            raise EngineError("TTS_OPENAI_TIMEOUT doit être un nombre") from e
        return OpenAIEngine(
            env.get("TTS_OPENAI_URL", "http://kokoro:8880/v1"),
            env.get("TTS_OPENAI_MODEL", "kokoro"),
            env.get("TTS_OPENAI_VOICE", "ff_siwis"),
            speed,
            env.get("TTS_OPENAI_API_KEY") or None,
            timeout,
        )
    raise EngineError(f"moteur inconnu : '{name}' (valeurs possibles : gtts, piper, openai)")
