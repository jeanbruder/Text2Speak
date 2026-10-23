#!/usr/bin/env python3
"""Comparaison des moteurs de synthèse vocale sur la machine qui héberge Text2Speak.

À lancer DANS le conteneur, sur la machine cible (Rock64) : les mesures n'ont de sens que là.

    docker compose run --rm text2speak python /text2speak/bench_tts.py
    docker compose run --rm text2speak python /text2speak/bench_tts.py --engines piper
    docker compose run --rm text2speak python /text2speak/bench_tts.py \\
        --piper-model /models/fr_FR-siwis-medium.onnx --piper-model /models/fr_FR-tom-medium.onnx

Pour chaque moteur, le script synthétise les mêmes phrases, mesure :
  - le temps de synthèse (c'est le délai ajouté avant que la voix ne démarre) ;
  - la durée de l'audio produit ;
  - le facteur temps réel (RTF) = temps de synthèse / durée audio : < 1 signifie plus rapide que
    la parole, > 1 signifie plus lent ;
  - la mémoire maximale du processus (moteurs en processus uniquement) ;
  - la température et la fréquence du processeur avant/après, pour repérer un ralentissement
    thermique (si le système les expose).

Les fichiers audio sont écrits dans le dossier de sortie (./bench_out sur l'hôte) pour être
écoutés : la qualité perçue ne se mesure pas.

Chaque moteur est évalué dans un processus séparé : un moteur qui dépasse la mémoire disponible
est arrêté sans interrompre le reste, et sa mémoire maximale n'est pas mélangée à celle des autres.
Le moteur « openai » (Kokoro, etc.) s'exécute dans un autre conteneur : pour sa mémoire, lance en
parallèle `docker stats --no-stream`.
"""
import argparse
import json
import os
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tts_engines as E  # noqa: E402

PHRASES = [
    "Bonjour.",
    "Le repas est prêt, à table !",
    "Attention, la porte du garage est restée ouverte.",
    "Il est 7 h 45, il fait 12 °C dehors et le ciel est dégagé.",
    "Rendez-vous le 3 novembre 2026 à 14 h 30 chez le docteur Müller.",
    "La batterie du capteur de la salle de bain est faible : il reste 15 % de charge.",
    "Le niveau d'eau du puits est descendu à 2,40 mètres. La pompe a été arrêtée par sécurité.",
    "Ce soir, les enfants rentrent à 18 h. Pense à sortir les poubelles, à nourrir les poules et à "
    "vérifier que le lapin a assez d'eau pour la nuit.",
]


def read_first(path: str):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def thermal() -> dict:
    """Température (°C) et fréquence (MHz) du processeur, si le noyau les expose."""
    out = {}
    t = read_first("/sys/class/thermal/thermal_zone0/temp")
    if t and t.lstrip("-").isdigit():
        out["temp_c"] = round(int(t) / 1000, 1)
    f = read_first("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    if f and f.isdigit():
        out["freq_mhz"] = round(int(f) / 1000)
    return out


# --- Mode « un seul moteur » (exécuté dans un sous-processus) ---------------------------------
def run_single(args) -> int:
    env = dict(os.environ)
    if args.single == "piper":
        env["TTS_PIPER_MODEL"] = args.model
    label = args.single if args.single != "piper" else f"piper:{Path(args.model).stem}"
    outdir = Path(args.out) / label.replace(":", "_")

    result = {"label": label, "phrases": [], "error": None}
    try:
        engine = E.build_engine(args.single, env)
        t0 = time.perf_counter()
        engine.warmup()
        result["warmup_s"] = round(time.perf_counter() - t0, 2)
    except E.EngineError as e:
        result["error"] = str(e)
        print(json.dumps(result))
        return 0

    outdir.mkdir(parents=True, exist_ok=True)
    result["ident"] = engine.ident
    result["thermal_before"] = thermal()
    phrases = [p for p in Path(args.phrases).read_text(encoding="utf-8").splitlines() if p.strip()] \
        if args.phrases else PHRASES
    for i, text in enumerate(phrases, 1):
        dest = outdir / f"{i:02d}{engine.ext}"
        row = {"n": i, "chars": len(text), "text": text}
        try:
            runs = []
            for _ in range(args.repeat):
                t0 = time.perf_counter()
                engine.synthesize(text, dest)
                runs.append(time.perf_counter() - t0)
            row["synth_s"] = round(statistics.median(runs), 2)
            row["audio_s"] = round(E.wav_duration(dest), 2) if engine.ext == ".wav" else None
            row["rtf"] = round(row["synth_s"] / row["audio_s"], 2) if row["audio_s"] else None
        except E.EngineError as e:
            row["error"] = str(e)
        result["phrases"].append(row)
    result["thermal_after"] = thermal()
    result["peak_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
    print(json.dumps(result))
    return 0


# --- Mode principal ---------------------------------------------------------------------------
def variants(args):
    """Liste (moteur, modèle) à évaluer."""
    out = []
    for name in [e.strip() for e in args.engines.split(",") if e.strip()]:
        if name == "piper":
            models = args.piper_model or [os.environ.get("TTS_PIPER_MODEL", "/models/fr_FR-siwis-medium.onnx")]
            out.extend(("piper", m) for m in models)
        else:
            out.append((name, None))
    return out


def run_variant(engine: str, model, args) -> dict:
    cmd = [sys.executable, str(Path(__file__).resolve()), "--single", engine,
           "--out", args.out, "--repeat", str(args.repeat)]
    if model:
        cmd += ["--model", model]
    if args.phrases:
        cmd += ["--phrases", args.phrases]
    label = engine if not model else f"{engine}:{Path(model).stem}"
    print(f"\n>>> {label} ...", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    if proc.returncode != 0 or not lines:
        why = "tué par le système (mémoire insuffisante ?)" if proc.returncode in (-9, 137) \
            else f"code {proc.returncode}"
        tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        return {"label": label, "error": f"processus interrompu : {why} {tail[0]}", "phrases": []}
    return json.loads(lines[-1])


def fmt(v, suffix=""):
    return "n/a" if v is None else f"{v}{suffix}"


def report(results, args) -> str:
    md = ["# Comparaison des moteurs de synthèse", ""]
    th = thermal()
    md.append(f"Machine : {os.uname().machine}, {os.cpu_count()} cœurs, charge {os.getloadavg()[0]:.2f}"
              + (f", {th['temp_c']} °C" if "temp_c" in th else "") + ".")
    md.append("RTF = temps de synthèse / durée audio (< 1 : plus rapide que la parole). "
              "Le temps de synthèse est le délai ajouté avant que la voix ne démarre.")
    for r in results:
        md += ["", f"## {r['label']}", ""]
        if r.get("error"):
            md.append(f"Indisponible : {r['error']}")
            continue
        md.append(f"Identifiant : `{r.get('ident')}`  ·  chargement du modèle : {fmt(r.get('warmup_s'), ' s')}"
                  f"  ·  mémoire max du processus : {fmt(r.get('peak_rss_mb'), ' Mo')}")
        tb, ta = r.get("thermal_before", {}), r.get("thermal_after", {})
        if tb or ta:
            md.append(f"Température : {fmt(tb.get('temp_c'), ' °C')} → {fmt(ta.get('temp_c'), ' °C')}"
                      f"  ·  fréquence : {fmt(tb.get('freq_mhz'), ' MHz')} → {fmt(ta.get('freq_mhz'), ' MHz')}")
        md += ["", "| # | Caractères | Synthèse (s) | Audio (s) | RTF | Remarque |", "|---|---|---|---|---|---|"]
        rtfs = []
        for p in r["phrases"]:
            if p.get("error"):
                md.append(f"| {p['n']} | {p['chars']} | – | – | – | {p['error']} |")
                continue
            if p.get("rtf") is not None:
                rtfs.append(p["rtf"])
            md.append(f"| {p['n']} | {p['chars']} | {fmt(p['synth_s'])} | {fmt(p['audio_s'])} | {fmt(p['rtf'])} | |")
        if rtfs:
            md.append(f"\nRTF médian : **{statistics.median(rtfs)}**, pire cas : **{max(rtfs)}**.")
    md += ["", "## Phrases", ""]
    base = next((r for r in results if r.get("phrases")), None)
    if base:
        md += [f"{p['n']}. {p['text']}" for p in base["phrases"]]
    md += ["", "Les fichiers audio sont dans le dossier de sortie, un sous-dossier par moteur, "
               "numérotés comme ci-dessus : à écouter."]
    return "\n".join(md) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engines", default="piper,openai",
                    help="moteurs à comparer, séparés par des virgules : piper, openai, gtts (défaut : piper,openai)")
    ap.add_argument("--piper-model", action="append",
                    help="modèle Piper (.onnx), répétable ; défaut : TTS_PIPER_MODEL")
    ap.add_argument("--phrases", help="fichier texte, une phrase par ligne (défaut : phrases intégrées)")
    ap.add_argument("--repeat", type=int, default=1, help="synthèses par phrase, la médiane est retenue (défaut 1)")
    ap.add_argument("--out", default=os.environ.get("TTS_BENCH_OUT", "/bench-out"), help="dossier de sortie")
    ap.add_argument("--single", help=argparse.SUPPRESS)
    ap.add_argument("--model", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.single:
        return run_single(args)

    Path(args.out).mkdir(parents=True, exist_ok=True)
    results = [run_variant(e, m, args) for e, m in variants(args)]
    text = report(results, args)
    (Path(args.out) / "resultats.md").write_text(text, encoding="utf-8")
    (Path(args.out) / "resultats.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n" + text)
    print(f"Résultats et fichiers audio : {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
# EoF
