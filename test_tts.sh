#!/usr/bin/env bash
# Test d'usage du service Text2Speak (file d'attente, cache, pièces et zones).
#
# Usage : ./test_tts.sh [URL]            (défaut : http://localhost:1337)
#
# Variables facultatives :
#   TTS_URL            URL du service (équivalent à l'argument)
#   TTS_TEST_PORTEE    tout (défaut) | base | pieces
#                        base   : file d'attente et cache uniquement
#                        pieces : validation des cibles + tests pièce par pièce et zone par zone
#   TTS_TEST_PIECES    pièces à tester, séparées par des espaces (défaut : toutes celles de /pieces)
#   TTS_TEST_ZONES     zones à tester, séparées par des espaces (défaut : toutes celles de /pieces)
#
# Exemples :
#   ./test_tts.sh
#   TTS_TEST_PORTEE=pieces ./test_tts.sh
#   TTS_TEST_PORTEE=pieces TTS_TEST_PIECES="salon" TTS_TEST_ZONES="" ./test_tts.sh
#
# ATTENTION : le test fait réellement parler les haut-parleurs (plusieurs messages courts)
# et appelle gTTS pour les textes absents du cache. L'étape de vidage de la file annule
# aussi les messages réels éventuellement en attente.
# Les tests par pièce et par zone nécessitent python3 ou jq pour lire GET /pieces.

BASE="${1:-${TTS_URL:-http://localhost:1337}}"
PORTEE="${TTS_TEST_PORTEE:-tout}"
WAIT_MAX=60   # secondes max d'attente par job

PASS=0
FAIL=0
FAILED=()
BODY=""
CODE=""

ok()   { echo "  [OK]    $1"; PASS=$((PASS + 1)); }
ko()   { echo "  [ÉCHEC] $1"; FAIL=$((FAIL + 1)); FAILED+=("$1"); }
step() { echo; echo "== $1"; }

# req METHOD CHEMIN [JSON] -> renseigne BODY et CODE
req() {
    local method="$1" path="$2" data="$3" out
    if [ -n "$data" ]; then
        out=$(curl -s -m 15 -w $'\n%{http_code}' -X "$method" \
              -H 'Content-Type: application/json' -d "$data" "$BASE$path")
    else
        out=$(curl -s -m 15 -w $'\n%{http_code}' -X "$method" "$BASE$path")
    fi
    CODE="${out##*$'\n'}"
    BODY="${out%$'\n'*}"
}

# jget CLE : extrait une valeur simple du JSON lu sur stdin
jget() {
    grep -oE "\"$1\": *(\"[^\"]*\"|[^,}]*)" | head -1 \
        | sed -E 's/^[^:]*: *//; s/^"//; s/"$//'
}

# jarr CLE : extrait un tableau de chaînes du JSON lu sur stdin, sous la forme a,b,c
jarr() {
    grep -oE "\"$1\": *\[[^]]*\]" | head -1 \
        | sed -E 's/^[^:]*: *\[//; s/\]$//; s/"//g; s/ //g'
}

# norm_set : trie une liste a,b,c lue sur stdin pour comparer sans tenir compte de l'ordre
norm_set() { tr ',' '\n' | sort | paste -sd, -; }

# wait_job ID -> attend la fin du job ; renseigne BODY
# retourne 0 si "termine", 1 si erreur/partiel/annule, 2 si délai dépassé
wait_job() {
    local id="$1" waited=0 statut
    while [ "$waited" -lt "$WAIT_MAX" ]; do
        req GET "/file/$id"
        statut=$(echo "$BODY" | jget statut)
        case "$statut" in
            termine) return 0 ;;
            erreur|annule|partiel) return 1 ;;
        esac
        sleep 1
        waited=$((waited + 1))
    done
    return 2
}

# check_target LIBELLÉ CIBLES_JSON ENSEMBLE_ATTENDU TEXTE
#   CIBLES_JSON vide : le champ "cibles" est omis (cible par défaut)
#   Vérifie le code 202, les pièces résolues, puis la lecture jusqu'au statut "termine".
check_target() {
    local label="$1" cibles_json="$2" expected="$3" texte="$4" payload got id rc
    if [ -n "$cibles_json" ]; then
        payload="{\"dire\":\"$texte\",\"cibles\":$cibles_json}"
    else
        payload="{\"dire\":\"$texte\"}"
    fi
    req POST /dire "$payload"
    if [ "$CODE" != "202" ]; then
        ko "$label : POST /dire -> $CODE : $BODY"
        return
    fi
    got=$(echo "$BODY" | jarr cibles | norm_set)
    if [ "$got" = "$expected" ]; then
        ok "$label : pièces résolues = $got"
    else
        ko "$label : pièces résolues = '$got' (attendu '$expected')"
    fi
    id=$(echo "$BODY" | jget id)
    wait_job "$id"; rc=$?
    case "$rc" in
        0) ok "$label : lecture terminée (job $id)" ;;
        2) ko "$label : délai dépassé (${WAIT_MAX}s, job $id)" ;;
        *) ko "$label : lecture en échec (job $id) : $(echo "$BODY" | jget erreur)" ;;
    esac
}

# --- Découverte des pièces et zones via GET /pieces ------------------------------
PIECES=()       # noms des pièces
ZONES=()        # noms des zones
ZMEMBERS=()     # pièces de chaque zone (a,b,c), même indice que ZONES
DEFAUT=""       # pièces ciblées par défaut (a,b)
LAYOUT_OK=0

load_layout() {
    local parsed kind a b
    req GET /pieces
    [ "$CODE" = "200" ] || return 1

    if command -v python3 >/dev/null 2>&1; then
        parsed=$(echo "$BODY" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("PIECES " + " ".join(d["pieces"]))
print("DEFAUT " + ",".join(d["defaut"]))
for z, m in d["zones"].items():
    print("ZONE " + z + " " + ",".join(m))
') || return 1
    elif command -v jq >/dev/null 2>&1; then
        parsed=$(echo "$BODY" | jq -r '
          "PIECES " + (.pieces | keys_unsorted | join(" ")),
          "DEFAUT " + (.defaut | join(",")),
          (.zones | to_entries[] | "ZONE " + .key + " " + (.value | join(",")))') || return 1
    else
        return 2
    fi

    while read -r kind a b; do
        case "$kind" in
            PIECES) read -r -a PIECES <<< "$a${b:+ $b}" ;;
            DEFAUT) DEFAUT="$a" ;;
            ZONE)   ZONES+=("$a"); ZMEMBERS+=("$b") ;;
        esac
    done <<< "$parsed"
    return 0
}

# PIECES contient tout le reste de la ligne : on la relit proprement
load_pieces_line() {
    local line
    if command -v python3 >/dev/null 2>&1; then
        line=$(echo "$BODY" | python3 -c 'import json,sys; print(" ".join(json.load(sys.stdin)["pieces"]))')
    else
        line=$(echo "$BODY" | jq -r '.pieces | keys_unsorted | join(" ")')
    fi
    read -r -a PIECES <<< "$line"
}

zone_members() {
    local i
    for i in "${!ZONES[@]}"; do
        if [ "${ZONES[$i]}" = "$1" ]; then echo "${ZMEMBERS[$i]}"; return 0; fi
    done
    return 1
}

# --- Vérifications de base ---------------------------------------------------------
command -v curl >/dev/null || { echo "curl est requis"; exit 2; }
case "$PORTEE" in
    tout|base|pieces) ;;
    *) echo "TTS_TEST_PORTEE doit valoir tout, base ou pieces (reçu : $PORTEE)"; exit 2 ;;
esac

echo "Cible : $BASE   (portée : $PORTEE)"

step "1. Service joignable"
req GET /cache
if [ "$CODE" = "200" ]; then
    ok "GET /cache -> 200"
else
    ko "GET /cache -> $CODE (service arrêté ? mauvaise URL ?)"
    echo "Abandon : service injoignable."
    exit 1
fi

step "2. Validation des entrées"
req POST /dire '{"dire":""}'
[ "$CODE" = "400" ] && ok "texte vide -> 400" || ko "texte vide -> $CODE (attendu 400)"
req POST /dire '{"autre":"x"}'
[ "$CODE" = "400" ] && ok "champ manquant -> 400" || ko "champ manquant -> $CODE (attendu 400)"

# --- Tests de base : cache, file d'attente ---------------------------------------
if [ "$PORTEE" != "pieces" ]; then

    step "3. Premier envoi (cache froid)"
    TEXTE="Test d usage numéro $RANDOM$RANDOM"
    req POST /dire "{\"dire\":\"$TEXTE\"}"
    if [ "$CODE" = "202" ]; then ok "POST /dire -> 202"; else ko "POST /dire -> $CODE : $BODY"; fi
    ID1=$(echo "$BODY" | jget id)
    if [ -n "$ID1" ] && wait_job "$ID1"; then
        ok "job $ID1 terminé"
        [ "$(echo "$BODY" | jget depuis_cache)" = "false" ] \
            && ok "synthèse réseau (depuis_cache=false)" \
            || ko "depuis_cache inattendu : $BODY"
    else
        ko "job $ID1 non terminé : $BODY"
    fi

    step "4. Second envoi identique (cache chaud)"
    req POST /dire "{\"dire\":\"$TEXTE\"}"
    ID2=$(echo "$BODY" | jget id)
    if [ -n "$ID2" ] && wait_job "$ID2"; then
        ok "job $ID2 terminé"
        [ "$(echo "$BODY" | jget depuis_cache)" = "true" ] \
            && ok "lu depuis le cache (depuis_cache=true)" \
            || ko "le cache n'a pas été utilisé : $BODY"
    else
        ko "job $ID2 non terminé : $BODY"
    fi

    step "5. File d'attente et priorités"
    req POST /dire "{\"dire\":\"Message un $RANDOM\",\"priorite\":5}"; A=$(echo "$BODY" | jget id)
    req POST /dire "{\"dire\":\"Message deux $RANDOM\",\"priorite\":5}"; B=$(echo "$BODY" | jget id)
    req POST /dire "{\"dire\":\"Message urgent $RANDOM\",\"priorite\":0}"; C=$(echo "$BODY" | jget id)
    req GET /file
    echo "  État de la file : $BODY"
    RESULT=0
    for id in "$A" "$B" "$C"; do
        [ -n "$id" ] && wait_job "$id" || RESULT=1
    done
    [ "$RESULT" = "0" ] && ok "les 3 messages ont été lus (jobs $A, $B, $C)" || ko "au moins un message n'a pas été lu"

    step "6. Vidage de la file"
    req POST /dire "{\"dire\":\"Message à annuler $RANDOM\"}"
    req POST /dire "{\"dire\":\"Autre message à annuler $RANDOM\"}"
    req DELETE /file
    [ "$CODE" = "200" ] && ok "DELETE /file -> 200 ($BODY)" || ko "DELETE /file -> $CODE"

    step "7. Statistiques du cache"
    req GET /cache
    echo "  $BODY"
    HITS=$(echo "$BODY" | jget hits)
    [ "${HITS:-0}" -ge 1 ] 2>/dev/null && ok "hits >= 1" || ko "aucun hit enregistré"
fi

# --- Tests des pièces et zones -------------------------------------------------------
if [ "$PORTEE" != "base" ]; then

    step "8. Configuration des pièces et zones"
    req GET /pieces
    if [ "$CODE" = "200" ]; then ok "GET /pieces -> 200"; echo "  $BODY"; else ko "GET /pieces -> $CODE"; fi
    req POST /dire '{"dire":"Test","cibles":["piece_qui_nexiste_pas"]}'
    [ "$CODE" = "400" ] && ok "cible inconnue -> 400" || ko "cible inconnue -> $CODE (attendu 400)"
    req POST /dire '{"dire":"Test","cibles":42}'
    [ "$CODE" = "400" ] && ok "cibles invalide -> 400" || ko "cibles invalide -> $CODE (attendu 400)"
    req POST /dire '{"dire":"Test","cibles":[]}'
    [ "$CODE" = "400" ] && ok "cibles vide -> 400" || ko "cibles vide -> $CODE (attendu 400)"

    load_layout; LAYOUT_RC=$?
    if [ "$LAYOUT_RC" = "0" ]; then
        LAYOUT_OK=1
        load_pieces_line
        echo "  Pièces : ${PIECES[*]}"
        echo "  Zones  : ${ZONES[*]:-(aucune)}"
        echo "  Défaut : $DEFAUT"
    elif [ "$LAYOUT_RC" = "2" ]; then
        echo "  python3 et jq sont absents : les tests par pièce et par zone sont ignorés."
    else
        ko "lecture de GET /pieces impossible"
    fi

    if [ "$LAYOUT_OK" = "1" ]; then
        # Sélection : variables d'environnement si définies (même vides), sinon tout
        if [ -n "${TTS_TEST_PIECES+x}" ]; then read -r -a TEST_PIECES <<< "$TTS_TEST_PIECES"; else TEST_PIECES=("${PIECES[@]}"); fi
        if [ -n "${TTS_TEST_ZONES+x}" ];  then read -r -a TEST_ZONES  <<< "$TTS_TEST_ZONES";  else TEST_ZONES=("${ZONES[@]}");  fi

        step "9. Lecture pièce par pièce"
        if [ "${#TEST_PIECES[@]}" -eq 0 ]; then
            echo "  (aucune pièce à tester)"
        fi
        for p in "${TEST_PIECES[@]}"; do
            check_target "pièce $p" "\"$p\"" "$p" "Test de la pièce ${p//_/ }"
        done

        step "10. Lecture zone par zone"
        if [ "${#TEST_ZONES[@]}" -eq 0 ]; then
            echo "  (aucune zone à tester)"
        fi
        for z in "${TEST_ZONES[@]}"; do
            members=$(zone_members "$z")
            if [ -z "$members" ]; then
                ko "zone $z : absente de GET /pieces"
                continue
            fi
            check_target "zone $z" "[\"$z\"]" "$(echo "$members" | norm_set)" "Test de la zone ${z//_/ }"
        done

        step "11. Doublons et casse"
        if [ "${#TEST_ZONES[@]}" -gt 0 ] && members=$(zone_members "${TEST_ZONES[0]}") && [ -n "$members" ]; then
            z="${TEST_ZONES[0]}"
            first="${members%%,*}"
            check_target "zone $z + pièce $first (doublon éliminé)" "[\"$z\",\"$first\"]" \
                "$(echo "$members" | norm_set)" "Test de doublon"
        else
            echo "  (test de doublon ignoré : aucune zone sélectionnée)"
        fi
        if [ "${#TEST_PIECES[@]}" -gt 0 ] && [[ "${TEST_PIECES[0]}" =~ ^[a-z0-9_]+$ ]]; then
            p="${TEST_PIECES[0]}"
            up=$(echo "$p" | tr 'a-z' 'A-Z')
            check_target "casse ($up accepté pour $p)" "\"$up\"" "$p" "Test de casse"
        else
            echo "  (test de casse ignoré)"
        fi

        step "12. Cible par défaut (sans champ \"cibles\")"
        check_target "défaut" "" "$(echo "$DEFAUT" | norm_set)" "Test de la cible par défaut"

    elif [ "$LAYOUT_RC" = "2" ] && [ -n "$TTS_TEST_PIECES" ]; then
        # Sans outil JSON : pièces fournies à la main, sans vérification des zones ni du défaut
        step "9. Lecture pièce par pièce (sans analyse de /pieces)"
        for p in $TTS_TEST_PIECES; do
            check_target "pièce $p" "\"$p\"" "$p" "Test de la pièce ${p//_/ }"
        done
    fi
fi

# --- Bilan -----------------------------------------------------------------------------
echo
echo "== Bilan : $PASS réussi(s), $FAIL échec(s)"
if [ "$FAIL" -gt 0 ]; then
    echo "Échecs :"
    for f in "${FAILED[@]}"; do echo "  - $f"; done
fi
[ "$FAIL" -eq 0 ]
# EoF
