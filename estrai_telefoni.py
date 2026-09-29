#!/usr/bin/env python3
"""
estrai_telefoni.py
------------------
Legge il MASTER DATABASE UFFICIALE SCUOLE PARITARIE (export CSV del foglio "indice"),
scarica la scheda "Scuola in chiaro" di ogni codice sede e ne estrae telefono e fax.

Chiavi di riconciliazione con il CRM (mantenute in tutti gli output):
  - CODICE_SCUOLA      = colonna A del Master (es. AG002P)  -> una riga = un ente/scuola
  - CODICE_SEDE        = codice meccanografico della sede (es. AG1A00900R, colonna H del Master)

Output (cartella --out, default ./output):
  telefoni_per_sede.csv     una riga per sede (formato "lungo", il piu' adatto per il CRM)
  colonna_O_telefoni.csv    una riga per riga del Master, NELLO STESSO ORDINE, pronta da
                            incollare nella colonna O "NUMERO DI TELEFONO"
  cache_schede.jsonl        cache: rilanciando lo script non riscarica cio' che ha gia'

Uso:
  python estrai_telefoni.py --test TP1A03000G,ANPS025003        # prova su 2 codici
  python estrai_telefoni.py --input master.csv --limit 30        # pilota su 30 sedi
  python estrai_telefoni.py --input master.csv                   # run completo (riprendibile)
"""
import argparse
import csv
import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import robotparser

import requests
from bs4 import BeautifulSoup

# !!! Metti un contatto reale: e' buona prassi far sapere chi sta scaricando.
USER_AGENT = "ScuolaPayDataBot/1.0 (contatto: stefano.clemente@scuolapay.it)"
# La scheda vuole un secondo segmento (il nome scuola): il server lo ignora, basta un segnaposto.
BASE_URL = "https://unica.istruzione.gov.it/cercalatuascuola/istituti/{code}/scuola"
ROBOTS_URL = "https://unica.istruzione.gov.it/robots.txt"

COL_CODICE_SCUOLA = 0    # A
COL_DENOMINAZIONE = 2    # C
COL_LISTA_SEDI = 7       # H  (CODICESCUOLA_LIST, separati da ';')
COL_TELEFONO = 14        # O
CODE_RE = re.compile(r"^[A-Z0-9]{10}$")


# ----------------------------------------------------------------------------
# Parsing e normalizzazione (funzioni pure, testabili senza rete)
# ----------------------------------------------------------------------------
def valore_dopo_etichetta(lines, etichetta):
    """Cerca 'Telefono ...' / 'Fax ...' sia sulla stessa riga sia sulla riga successiva."""
    pattern = re.compile(rf"^{etichetta}\s*:?\s*(.*)$", re.I)
    for i, line in enumerate(lines):
        m = pattern.match(line)
        if not m:
            continue
        val = m.group(1).strip()
        if not val and i + 1 < len(lines):
            val = lines[i + 1].strip()
        if re.search(r"\d", val):
            return val
    return ""


def estrai_contatti(html):
    """Ritorna (telefono_raw, fax_raw) dalla pagina della scheda."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    testo = soup.get_text("\n")
    lines = [re.sub(r"\s+", " ", l).strip() for l in testo.splitlines()]
    lines = [l for l in lines if l]
    return valore_dopo_etichetta(lines, "Telefono"), valore_dopo_etichetta(lines, "Fax")


def normalizza(raw):
    """
    Trasforma il testo grezzo in una lista di numeri in formato +39XXXXXXXXX.
    I fissi italiani mantengono lo 0 iniziale (+390923360764), come richiede il formato E.164.
    """
    risultati = []
    for parte in re.split(r"\s+-\s+|;|,|\s/\s|\se\s", raw or ""):
        s = re.sub(r"[^\d+]", "", parte)
        if not s:
            continue
        if s.startswith("00"):
            s = "+" + s[2:]
        if not s.startswith("+"):
            s = "+39" + s
        cifre = s.lstrip("+")
        if not (9 <= len(cifre) <= 14):
            continue
        risultati.append(s)
    return risultati


# ----------------------------------------------------------------------------
# Rete
# ----------------------------------------------------------------------------
def robots_ok():
    # Lettura via requests: robotparser.read() tratta un 403 come "vieta tutto",
    # mentre qui il 403 e' la CDN che nega il file a chiunque (anche ai browser).
    # RFC 9309: robots.txt non disponibile (4xx) = nessuna restrizione.
    try:
        r = requests.get(ROBOTS_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    except Exception as e:  # robots non raggiungibile: avvisa ma non blocca
        print(f"[!] robots.txt non leggibile ({e}). Verifica a mano i termini d'uso.")
        return True
    if r.status_code != 200 or "text/plain" not in r.headers.get("Content-Type", ""):
        print(f"[!] robots.txt non disponibile (HTTP {r.status_code}). Verifica a mano i termini d'uso.")
        return True
    rp = robotparser.RobotFileParser()
    rp.parse(r.text.splitlines())
    return rp.can_fetch(USER_AGENT, BASE_URL.format(code="XXXXXXXXXX"))


def scarica(session, code, retries=4):
    url = BASE_URL.format(code=code)
    for tentativo in range(retries):
        try:
            r = session.get(url, timeout=30, allow_redirects=True)
        except requests.RequestException:
            time.sleep(2 ** tentativo)
            continue
        # codice inesistente: redirect a /errore/scuola-non-trovata (che poi risponde 403)
        if "scuola-non-trovata" in r.url:
            return "not_found", url, ""
        if r.status_code == 200:
            return "ok", r.url, r.text
        if r.status_code == 404:
            return "not_found", url, ""
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(5 * 2 ** tentativo)  # backoff
            continue
        return f"http_{r.status_code}", url, ""
    return "errore", url, ""


# ----------------------------------------------------------------------------
# Cache
# ----------------------------------------------------------------------------
def carica_cache(path):
    cache = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    cache[rec["codice_sede"]] = rec
                except Exception:
                    pass
    return cache


def scrivi_cache(path, rec):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def processa_codice(session, code, cache, cache_path, delay):
    """Usa la cache se possibile (ok / not_found), altrimenti scarica."""
    prec = cache.get(code)
    if prec and prec["status"] in ("ok", "not_found"):
        return prec
    status, url, html = scarica(session, code)
    tel_raw, fax_raw = estrai_contatti(html) if status == "ok" else ("", "")
    rec = {
        "codice_sede": code,
        "status": status,
        "url": url,
        "telefono_raw": tel_raw,
        "telefono_e164": "; ".join(normalizza(tel_raw)),
        "fax_raw": fax_raw,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    cache[code] = rec
    scrivi_cache(cache_path, rec)
    time.sleep(delay + random.uniform(0, delay * 0.3))
    return rec


# ----------------------------------------------------------------------------
# Master
# ----------------------------------------------------------------------------
def leggi_master(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    header, data = rows[0], rows[1:]
    if not header[COL_CODICE_SCUOLA].upper().startswith("CODICE_SCUOLA") or \
       not header[COL_LISTA_SEDI].upper().startswith("CODICESCUOLA_LIST"):
        sys.exit("Intestazioni inattese: esporta il foglio 'indice' del Master "
                 "senza spostare/eliminare colonne (A=CODICE_SCUOLA, H=CODICESCUOLA_LIST).")
    master = []
    for r in data:
        if len(r) <= COL_LISTA_SEDI or not r[COL_CODICE_SCUOLA].strip():
            continue
        sedi = [c.strip().upper() for c in r[COL_LISTA_SEDI].split(";") if c.strip()]
        master.append({
            "codice_scuola": r[COL_CODICE_SCUOLA].strip(),
            "denominazione": r[COL_DENOMINAZIONE].strip() if len(r) > COL_DENOMINAZIONE else "",
            "sedi": [c for c in sedi if CODE_RE.match(c)],
        })
    return master


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="CSV esportato dal foglio 'indice' del Master")
    ap.add_argument("--out", default="output")
    ap.add_argument("--delay", type=float, default=1.0, help="secondi tra le richieste")
    ap.add_argument("--limit", type=int, help="processa solo le prime N sedi (pilota)")
    ap.add_argument("--test", help="codici sede separati da virgola, senza usare il Master")
    ap.add_argument("--max-minutes", type=float,
                    help="si ferma in modo pulito dopo N minuti (rilanciando riparte dalla cache)")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cache_path = out / "cache_schede.jsonl"
    cache = carica_cache(cache_path)

    if not robots_ok():
        sys.exit("robots.txt vieta l'accesso a questo percorso: mi fermo.")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "it-IT,it;q=0.9"})

    # --- modalita' test -------------------------------------------------------
    if args.test:
        for code in [c.strip().upper() for c in args.test.split(",") if c.strip()]:
            rec = processa_codice(session, code, cache, cache_path, args.delay)
            print(json.dumps(rec, ensure_ascii=False, indent=2))
        return

    if not args.input:
        sys.exit("Serve --input master.csv (oppure --test CODICE1,CODICE2).")

    master = leggi_master(args.input)
    tutte_sedi = [(m["codice_scuola"], c) for m in master for c in m["sedi"]]
    if args.limit:
        tutte_sedi = tutte_sedi[: args.limit]
    print(f"Righe Master: {len(master)} | sedi da processare: {len(tutte_sedi)}")

    # --- download -------------------------------------------------------------
    inizio = time.time()
    for i, (_, code) in enumerate(tutte_sedi, 1):
        if args.max_minutes and (time.time() - inizio) / 60 > args.max_minutes:
            print(f"[!] Limite di {args.max_minutes:.0f} minuti raggiunto dopo {i - 1} sedi. "
                  "Rilancia per continuare: le sedi gia' fatte non vengono riscaricate.")
            break
        processa_codice(session, code, cache, cache_path, args.delay)
        if i % 50 == 0 or i == len(tutte_sedi):
            ok = sum(1 for _, c in tutte_sedi[:i] if cache.get(c, {}).get("telefono_e164"))
            print(f"  {i}/{len(tutte_sedi)} sedi | con telefono: {ok}")

    # --- output 1: una riga per sede -----------------------------------------
    with open(out / "telefoni_per_sede.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["CODICE_SCUOLA", "CODICE_SEDE", "TELEFONO_E164", "TELEFONO_ORIGINALE",
                    "FAX_ORIGINALE", "STATUS", "URL_SCHEDA", "SCARICATO_IL"])
        for cs, code in tutte_sedi:
            r = cache.get(code, {})
            w.writerow([cs, code, r.get("telefono_e164", ""), r.get("telefono_raw", ""),
                        r.get("fax_raw", ""), r.get("status", ""), r.get("url", ""),
                        r.get("fetched_at", "")])

    # --- output 2: una riga per riga del Master, stesso ordine ----------------
    with open(out / "colonna_O_telefoni.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["CODICE_SCUOLA", "NUMERO DI TELEFONO", "N_SEDI", "N_SEDI_CON_TELEFONO"])
        limite = {c for _, c in tutte_sedi}
        for m in master:
            numeri, con_tel = [], 0
            for code in m["sedi"]:
                if code not in limite:
                    continue
                for n in cache.get(code, {}).get("telefono_e164", "").split("; "):
                    if n and n not in numeri:
                        numeri.append(n)
                if cache.get(code, {}).get("telefono_e164"):
                    con_tel += 1
            w.writerow([m["codice_scuola"], "; ".join(numeri), len(m["sedi"]), con_tel])

    print(f"Fatto. File in: {out.resolve()}")


if __name__ == "__main__":
    main()
