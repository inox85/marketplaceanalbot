import sys
import os
import time
import re
import json
import heapq
import shutil
import argparse
import threading
import html
import unicodedata
import configparser
from difflib import SequenceMatcher
from pathlib import Path
import psutil

import requests

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import random

print("Configuro encoding...")
if sys.platform == "win32":
    os.system("chcp 65001 > nul")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


class _TeeStream:
    """
    Duplica ogni scrittura su più stream (console + file di log), con un
    timestamp a inizio riga. Serve perché se il programma crasha mentre
    gira da start.bat o headless via SSH, la finestra/sessione si chiude
    subito e il traceback (stampato su stderr) andrebbe perso per sempre
    senza una copia persistente su disco.
    """
    def __init__(self, *streams):
        self._streams = streams
        self._at_line_start = True

    def write(self, data):
        for chunk in data.splitlines(keepends=True):
            if self._at_line_start and chunk.strip("\n"):
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                for s in self._streams:
                    s.write(f"[{timestamp}] ")
            for s in self._streams:
                s.write(chunk)
            self._at_line_start = chunk.endswith("\n")

    def flush(self):
        for s in self._streams:
            s.flush()

    def isatty(self):
        return False


LOG_FILE = Path.cwd() / "bot.log"
_log_file_handle = open(LOG_FILE, "a", encoding="utf-8")
sys.stdout = _TeeStream(sys.stdout, _log_file_handle)
sys.stderr = _TeeStream(sys.stderr, _log_file_handle)

# ============================================================
# CONFIGURAZIONE
# ============================================================

def load_telegram_token():
    """
    Il token va in secrets.ini (gitignored), non nel sorgente: un token
    committato in un repo pubblico viene individuato e dirottato da bot
    automatici nel giro di minuti (è già successo con il token precedente).
    """
    config = configparser.ConfigParser()
    if not config.read("secrets.ini", encoding="utf-8"):
        sys.exit(
            "secrets.ini non trovato. Copia secrets.ini.example in secrets.ini "
            "e inserisci il bot_token del tuo bot Telegram."
        )

    token = config.get("telegram", "bot_token", fallback="").strip().strip("\"'")
    if not token or "INSERISCI_QUI" in token:
        sys.exit("Imposta un bot_token valido in secrets.ini prima di avviare il bot.")

    return token


TELEGRAM_BOT_TOKEN = load_telegram_token()

KEYWORDS = []  # parole chiave di default assegnate a chi si iscrive con /subscribe
BAD_KEYWORDS = []  # parole che sopprimono l'alert, condivise da tutte le chat

GROUPS = {}

with open('groups.json', encoding="utf-8") as f:
    ALL_GROUPS = json.load(f)

# Il campo "attivo" è opzionale (assente = attivo di default), così i
# gruppi esistenti in groups.json restano validi senza modifiche: basta
# aggiungere "attivo": false per disattivarne uno senza doverlo rimuovere.
GROUPS = [g for g in ALL_GROUPS if g.get("attivo", True)]


GROUP_CHECK_RETRIES = 3  # tentativi per il controllo di un singolo gruppo prima di rinunciare
GROUP_CHECK_RETRY_DELAY = 3  # secondi di pausa tra un tentativo e il successivo

TOP_POST_TEXT_TIMEOUT = 5  # secondi massimi di attesa che il testo del post compaia dopo il caricamento
TOP_POST_TEXT_POLL_INTERVAL = 0.5  # secondi tra un tentativo di lettura del testo e il successivo

# Intervallo tra due controlli dello stesso gruppo: si restringe verso il
# minimo quando il gruppo produce post nuovi (per essere tempestivi nella
# prenotazione) e si allarga verso il massimo quando resta silenzioso, per
# non sprecare cicli su gruppi poco attivi e concentrarsi su quelli attivi.
GROUP_CHECK_MIN_INTERVAL = 3        # secondi minimi anche per un gruppo molto attivo
GROUP_CHECK_MAX_INTERVAL = 90       # secondi massimi per un gruppo silenzioso
GROUP_CHECK_DEFAULT_INTERVAL = 15   # intervallo di partenza, prima di sapere quanto è attivo
GROUP_CHECK_SPEEDUP_FACTOR = 2      # riduzione dell'intervallo dopo un post nuovo
GROUP_CHECK_SLOWDOWN_FACTOR = 1.3   # aumento dell'intervallo se non trova nulla di nuovo

# Protegge la coda di priorità (schedule/intervals) da accessi concorrenti
# quando ci sono più worker; protegge inoltre chats/alerted_posts (e i
# relativi salvataggi su disco) dato che più worker possono processare un
# match quasi contemporaneamente.
SCHEDULE_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()

POST_SELECTOR = 'div[aria-posinset="1"]'

CHROME_PROFILE = Path.cwd() / "chrome_profile"

ALERTED_POSTS_FILE = Path.cwd() / "alerted_posts.json"
CHATS_FILE = Path.cwd() / "chats.json"

LAST_UPDATE_ID = None  # avanza ad ogni comando Telegram letto, per non rileggerlo


# ============================================================
# PERSISTENZA DEI POST GIA' SEGNALATI
# ============================================================

def chiudi_chrome():
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            nome = proc.info['name'].lower()
            if 'chrome' in nome:
                proc.kill()
                print(f"Terminato: {nome} (PID {proc.info['pid']})")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass



ALERTED_RETENTION_DAYS = 60  # oltre questa età un post segnalato viene dimenticato
TEXT_SIMILARITY_THRESHOLD = 0.85  # sopra questa somiglianza due testi sono "lo stesso annuncio"


def _legacy_record(entry):
    """
    Converte una voce del vecchio formato ("<chat>|<gruppo> | <autore con
    tempo relativo> | <descrizione>") in un record strutturato, così i post
    già segnalati in passato restano riconosciuti anche col nuovo confronto.
    """
    chat_id, _, rest = entry.partition("|")
    parts = rest.split(" | ")
    group = parts[0] if parts else ""
    if len(parts) >= 3:
        meta = parse_header_meta(parts[1])
        description = " | ".join(parts[2:])
    else:
        meta = parse_header_meta("")
        description = " | ".join(parts[1:])
    post = build_post(group, "", meta, description)
    return make_alert_record(chat_id, post, ts=time.time())


def make_alert_record(chat_id, post, ts=None):
    return {
        "chat_id": str(chat_id),
        "group": post["group_name"],
        "author": post["author"],
        "published": post["published"],
        "url": post["url"],
        "text": post["norm_text"][:600],
        "ts": ts or time.time(),
    }


def load_alerted_posts():
    """Carica da disco i post già segnalati (convertendo il vecchio formato)."""
    records = []
    if ALERTED_POSTS_FILE.exists():
        try:
            with open(ALERTED_POSTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            migrated = 0
            for entry in data:
                if isinstance(entry, str):
                    records.append(_legacy_record(entry))
                    migrated += 1
                else:
                    records.append(entry)
            if migrated:
                print(f"Convertiti {migrated} post segnalati dal vecchio formato.")
        except Exception as e:
            print("Impossibile leggere", ALERTED_POSTS_FILE, "->", e)

    cutoff = time.time() - ALERTED_RETENTION_DAYS * 86400
    return [r for r in records if r.get("ts", 0) >= cutoff]


def save_alerted_posts(alerted_posts):
    """Salva su disco l'elenco aggiornato dei post già segnalati."""
    try:
        tmp = ALERTED_POSTS_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(alerted_posts, f, ensure_ascii=False, indent=1)
        os.replace(tmp, ALERTED_POSTS_FILE)
    except Exception as e:
        print("Impossibile salvare", ALERTED_POSTS_FILE, "->", e)


def texts_match(a, b):
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    # testo troncato ("Altro...") vs espanso, o post modificato aggiungendo
    # una riga ("Assegnato", "Zona coop..."): uno contiene l'altro
    if len(short) >= 30 and short in long_:
        return True
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    return (matcher.real_quick_ratio() >= TEXT_SIMILARITY_THRESHOLD
            and matcher.quick_ratio() >= TEXT_SIMILARITY_THRESHOLD
            and matcher.ratio() >= TEXT_SIMILARITY_THRESHOLD)


def is_duplicate(record, previous):
    """
    Due segnalazioni sono lo stesso annuncio se coincide il link del post,
    oppure stesso autore + stessa data di pubblicazione, oppure stesso
    autore + testo molto simile (anche in un gruppo diverso: chi pubblica
    lo stesso oggetto in più gruppi genera un solo avviso). Senza autore,
    serve lo stesso gruppo e un testo molto simile.
    """
    if record["url"] and record["url"] == previous.get("url"):
        return True

    author = record["author"].lower()
    same_author = bool(author) and author == (previous.get("author") or "").lower()

    if same_author and record["published"] and record["published"] == previous.get("published"):
        return True
    if same_author or (not author and record["group"] == previous.get("group")):
        return texts_match(record["text"], previous.get("text", ""))
    return False


def find_previous_alert(record, alerted_posts):
    for previous in alerted_posts:
        if previous.get("chat_id") == record["chat_id"] and is_duplicate(record, previous):
            return previous
    return None


# ============================================================
# GESTIONE CHAT ISCRITTE E LORO PAROLE CHIAVE
# ============================================================

def load_chats():
    """Carica da disco le chat iscritte, ciascuna con le proprie parole chiave."""
    if CHATS_FILE.exists():
        try:
            with open(CHATS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print("Impossibile leggere", CHATS_FILE, "->", e)
    return {}


def save_chats(chats):
    """Salva su disco le chat iscritte e le loro parole chiave."""
    try:
        with open(CHATS_FILE, "w", encoding="utf-8") as f:
            json.dump(chats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Impossibile salvare", CHATS_FILE, "->", e)


# ============================================================
# GESTIONE URL GRUPPI
# ============================================================

def build_group_url(base_url):
    if "sorting_setting=" in base_url:
        return base_url
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}sorting_setting=CHRONOLOGICAL&locale=it_IT"


# ============================================================
# FUNZIONI DI SUPPORTO PER LA PULIZIA DEL TESTO
# ============================================================

def clean_facebook_text(text):
    """
    Facebook inserisce nel testo caratteri Unicode "invisibili"
    (segni diacritici combinanti, caratteri di formattazione tipo
    zero-width joiner, marcatori direzionali RTL/LTR) per rendere
    il testo illeggibile/confuso per gli scraper automatici.
    Qui li rimuoviamo, lasciando solo i caratteri "veri".
    """
    cleaned_chars = []
    for ch in text:
        category = unicodedata.category(ch)
        if category in ("Mn", "Me", "Cf"):
            continue
        cleaned_chars.append(ch)

    text = "".join(cleaned_chars)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ------------------------------------------------------------
# Etichette dell'interfaccia che possono comparire come segmento a
# sé stante tra due "·" (es. il pulsante "Segui" accanto al nome di
# autori che non segui ancora). Non fanno parte né del nome né del
# contenuto del post, quindi vanno scartate.
# ------------------------------------------------------------
UI_LABEL_SEGMENTS = {"segui", "messaggio", "iscriviti", "segui già"}

# ------------------------------------------------------------
# Anteprima di un commento "attaccata" al testo del post, senza un
# marcatore testuale tipo "Visualizza altri commenti" davanti: lo
# schema è "<contatori numerici> <Nome Cognome> · <tempo relativo>
# <testo commento> Rispondi", ripetuto per ogni commento mostrato.
# Il tempo relativo (minuti/ore) cambia ad ogni controllo anche se il
# post e il commento sono identici, quindi tutto ciò che segue questo
# schema va scartato dalla descrizione.
# ------------------------------------------------------------
COMMENT_PREVIEW_RE = re.compile(
    r"\d+(?:\s+\d+)?\s+(?:[A-ZÀÈÉÌÒÙ][\w'\-.]*\s+){1,4}"
    r"·\s*\d+\s*(?:min(?:uti?)?|h|ore?|g|giorni|sett(?:imane)?|mese|mesi|anno|anni)\b"
)


def looks_like_noise_segment(segment):
    """
    Un segmento di testo compreso tra due '·' viene considerato
    "rumore" (intestazione da scartare) se:
    - è vuoto, oppure
    - coincide (ignorando maiuscole/spazi) con un'etichetta nota
      dell'interfaccia (es. "Segui"), oppure
    - contiene una lunga sequenza (6+) di "parole" da un solo
      carattere, il pattern con cui Facebook mescola nome/orario per
      confondere gli scraper.

    Un segmento che NON soddisfa nessuna di queste condizioni è
    considerato contenuto vero del post (anche se il post stesso usa
    "·" come separatore interno tra due frasi: in quel caso il
    segmento successivo sarà testo normale, non rumore, e la funzione
    smette di scartare).
    """
    stripped = segment.strip()
    if not stripped:
        return True
    if stripped.lower() in UI_LABEL_SEGMENTS:
        return True
    if re.search(r"(?:\b\S\b\s+){6,}", segment):
        return True
    return False


def split_header_and_body(text):
    """
    Divide il testo in (header, body). L'intestazione può essere
    composta da PIÙ segmenti separati da "·" (nome autore, pulsante
    "Segui", orario mescolato carattere per carattere...): consumiamo
    come header tutti i segmenti iniziali che "sembrano rumore"
    (etichette note o lunghe sequenze di caratteri isolati). Il primo
    segmento che non sembra rumore, e tutto ciò che segue (compresi
    eventuali altri "·" che l'autore ha scritto di suo pugno nel
    testo), è il contenuto vero del post.
    """
    if "·" not in text:
        return "", text

    segments = text.split("·")

    header_segments = [segments[0]]
    body_start_index = 1

    for i in range(1, len(segments)):
        if looks_like_noise_segment(segments[i]):
            header_segments.append(segments[i])
            body_start_index = i + 1
        else:
            break

    header = "·".join(header_segments)
    body = "·".join(segments[body_start_index:])

    return header, body


def extract_author(header):
    """
    L'header contiene tipicamente "<Nome Autore reale> <rumore
    mescolato carattere per carattere>", eventualmente con "·" ed
    etichette come "Segui" residue (quando split_header_and_body ha
    dovuto consumare più di un segmento). Isoliamo il nome:
    1) tagliamo alla prima lunga sequenza di token da un carattere;
    2) ripuliamo eventuali "·" residui ed etichette note ("Segui",
       "Messaggio", "Iscriviti") rimaste attaccate al nome.
    """
    match = re.search(r"(?:\b\S\b\s+){6,}", header)
    if match:
        header = header[:match.start()]

    header = header.replace("·", " ")
    header = re.sub(r"\b(Segui|Messaggio|Iscriviti)\b", " ", header, flags=re.IGNORECASE)
    header = re.sub(r"\s+", " ", header)
    return header.strip()


# ------------------------------------------------------------
# Metadati nell'intestazione. Oggi Facebook non mescola più l'orario
# carattere per carattere: l'header arriva come testo leggibile, tipo
#   "Maria Pepe 2 minuti fa Venerdì 6 novembre 2026 alle ore 09:06"
#   "Pannee Siriwong si trova a Caldine . circa un'ora fa"
#   "Giuseppe Gubinelli Amministratore 3 ore fa"
# Il tempo RELATIVO ("2 minuti fa") cambia ad ogni controllo: se resta
# attaccato al nome, l'ID del post cambia ogni minuto e lo stesso
# annuncio viene notificato più volte. La data ASSOLUTA invece è stabile
# ed è un ottimo identificatore del post.
# ------------------------------------------------------------
_WEEKDAYS = r"(?:lunedì|martedì|mercoledì|giovedì|venerdì|sabato|domenica)"
_MONTHS = (r"(?:gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|"
           r"settembre|ottobre|novembre|dicembre)")

ABS_DATE_RE = re.compile(
    rf"{_WEEKDAYS}\s+\d{{1,2}}\s+{_MONTHS}\s+\d{{4}}(?:\s+alle\s+ore\s+\d{{1,2}}:\d{{2}})?",
    re.IGNORECASE,
)
REL_TIME_RE = re.compile(
    r"(?:circa\s+)?(?:\d+|un|una|un')\s*"
    r"(?:secondi?|minut[oi]|or[ae]|giorn[oi]|settiman[ae]|mes[ei]|ann[oi])\s+fa\b"
    r"|\b(?:adesso|proprio ora|ieri(?:\s+alle\s+ore\s+\d{1,2}:\d{2})?)\b"
    rf"|\b\d{{1,2}}\s+{_MONTHS}(?:\s+\d{{4}})?(?:\s+alle\s+ore\s+\d{{1,2}}:\d{{2}})?"
    r"|\b\d+\s*(?:min|h|g|sett)\b",
    re.IGNORECASE,
)
LOCATION_RE = re.compile(r"\s+si trova (?:a|in|presso)\s+(.+?)\s*\.?\s*$", re.IGNORECASE)
AUTHOR_BADGES_RE = re.compile(
    r"\s+(?:Amministratore|Amministratrice|Moderatore|Moderatrice|"
    r"Esperto del gruppo|Esperta del gruppo|Fan più attivo|Collaboratore|"
    r"Nuovo membro|Membro principiante)\b.*$",
    re.IGNORECASE,
)


def parse_header_meta(raw_author):
    """
    Separa dall'intestazione grezza: nome autore, luogo ("si trova a"),
    tempo relativo ("2 ore fa", solo per mostrarlo nella notifica) e data
    assoluta di pubblicazione (stabile, usata per riconoscere il post).
    """
    published = ""
    abs_match = ABS_DATE_RE.search(raw_author)
    if abs_match:
        published = re.sub(r"\s+", " ", abs_match.group(0)).strip()

    rel_match = REL_TIME_RE.search(raw_author)
    rel_time = rel_match.group(0).strip() if rel_match else ""

    cut = min([m.start() for m in (abs_match, rel_match) if m] or [len(raw_author)])

    name = raw_author[:cut].strip(" .·")

    location = ""
    loc_match = LOCATION_RE.search(name)
    if loc_match:
        location = loc_match.group(1).strip(" .")
        name = name[:loc_match.start()]

    name = AUTHOR_BADGES_RE.sub("", name).strip(" .·")
    return {"author": name, "location": location, "rel_time": rel_time, "published": published}


# ------------------------------------------------------------
# Rumore nel testo del post che non scrive l'autore: descrizioni
# automatiche delle foto (che cambiano tra un controllo e l'altro, da
# "Nessuna descrizione..." a "Potrebbe essere un'immagine..." appena
# Facebook le calcola), etichette dell'interfaccia, contatori foto.
# ------------------------------------------------------------
ALT_TEXT_RE = re.compile(
    r"(?:Potrebbe essere )?un'immagine raffigurante\s+([^.]*?)\s*(?:\.|(?=\s\d+\s*€)|(?=\s*Post condiviso)|$)"
    r"|Potrebbe essere un'immagine\s*\.?"
    r"|Nessuna descrizione della foto disponibile\.*",
    re.IGNORECASE,
)
DISPLAY_NOISE_RE = re.compile(
    r"\bPost condiviso\b|\bAltro\s*(?:\.\.\.|…)|(?<!\S)\+\d+(?!\S)|\bMessaggio\s*$",
    re.IGNORECASE,
)


def split_text_and_photos(description):
    """Ritorna (testo leggibile senza rumore, lista descrizioni foto)."""
    photos = []
    for m in ALT_TEXT_RE.finditer(description):
        if m.group(1):
            for p in re.split(r",\s*|\s+e\s+", m.group(1).replace("_", " ")):
                p = p.strip(" ,.")
                if p and p.lower() != "testo" and p not in photos:
                    photos.append(p)
    text = ALT_TEXT_RE.sub(" ", description)
    text = DISPLAY_NOISE_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text, photos


def normalize_for_compare(text):
    text = text.lower().replace("_", " ")
    text = re.sub(r"[^\w€]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_description(body):
    """
    Ripulisce il contenuto vero del post (il "body") rimuovendo:
    - il footer con i pulsanti di interazione (Mi piace/Commenta/
      Condividi) e il placeholder del box commenti;
    - l'ANTEPRIMA di un commento ("Visualizza altri commenti", nome
      di chi ha commentato, "· X min ·", testo del commento,
      "Rispondi"): il tempo trascorso cambia ad ogni controllo anche
      se il post e il commento sono gli stessi, quindi va tagliato;
    - un eventuale gruppo di contatori numerici isolati rimasto in
      fondo (reazioni/commenti), che cambia nel tempo pur restando lo
      stesso post.
    """
    footer_markers = [
        "Mi piace",
        "Commenta come",
        "Condividi",
        "Visualizza altri commenti",
        "Vedi altri commenti",
        "Mostra altri commenti",
        "Visualizza commenti precedenti",
        "Rispondi come",
        "Scrivi un commento",
        "Scrivi una risposta",
    ]
    cut_positions = [body.find(m) for m in footer_markers if body.find(m) != -1]

    comment_match = COMMENT_PREVIEW_RE.search(body)
    if comment_match:
        cut_positions.append(comment_match.start())

    if cut_positions:
        body = body[:min(cut_positions)]

    body = re.sub(r"(?:\s*\d+)+\s*$", "", body)
    body = body.strip()

    return body if body else None


def find_keywords(text, keywords):
    """Restituisce tutte le parole chiave trovate nel testo (case-insensitive)."""
    text_lower = text.lower()
    return [k for k in keywords if k and k.lower() in text_lower]

def contains_bad_keyword(text):
    """Restituisce la parola chiave trovata (case-insensitive), oppure None."""
    text_lower = text.lower()
    for keyword in BAD_KEYWORDS:
        if keyword.lower() in text_lower:
            return keyword
    return None

def extract_post_url(container):
    hrefs = []
    try:
        links = container.find_elements(By.TAG_NAME, "a")
    except Exception:
        links = []

    for link in links:
        try:
            href = link.get_attribute("href")
        except Exception:
            href = None
        if href and href not in hrefs:
            hrefs.append(href)

    for href in hrefs:
        match = re.search(r'(https://www\.facebook\.com/groups/\d+/posts/\d+)', href)
        if match:
            return match.group(1), hrefs
        match = re.search(r'(https://www\.facebook\.com/groups/\d+/permalink/\d+)', href)
        if match:
            return match.group(1), hrefs
        match = re.search(r'[?&]story_fbid=(\d+)', href)
        if match:
            group_match = re.search(r'/groups/(\d+)', href)
            group_id = group_match.group(1) if group_match else None
            if group_id:
                return f"https://www.facebook.com/groups/{group_id}/posts/{match.group(1)}", hrefs
            return href, hrefs

    return None, hrefs


def get_top_post(driver, group_name, group_url):
    try:
        container = driver.find_element(By.CSS_SELECTOR, POST_SELECTOR)
    except Exception:
        print(f"  [{group_name}] Nessun post con aria-posinset=1 trovato in questo giro.")
        return None

    # Il testo può comparire con un attimo di ritardo rispetto alla presenza
    # dell'elemento nel DOM (il post è già inserito, ma il contenuto testuale
    # non ha ancora finito di renderizzare): riprova per qualche secondo
    # prima di concludere che il post è genuinamente senza testo.
    raw_text = ""
    deadline = time.time() + TOP_POST_TEXT_TIMEOUT
    while time.time() < deadline:
        try:
            raw_text = container.text.strip()
        except Exception:
            raw_text = ""
        if raw_text:
            break
        time.sleep(TOP_POST_TEXT_POLL_INTERVAL)

    if not raw_text:
        print(f"  [{group_name}] Post trovato ma senza testo dopo {TOP_POST_TEXT_TIMEOUT}s di attesa (probabile immagine/video senza didascalia).")
        return None

    text = clean_facebook_text(raw_text)

    header, body = split_header_and_body(text)
    meta = parse_header_meta(extract_author(header))

    description = clean_description(body)
    if description is None:
        description = body.strip() or text

    post_url, hrefs = extract_post_url(container)

    return build_post(group_name, group_url, meta, description, post_url, hrefs)


def build_post(group_name, group_url, meta, description, post_url=None, hrefs=()):
    display_text, photos = split_text_and_photos(description)
    norm_text = normalize_for_compare(display_text) or normalize_for_compare(description)

    # ID usato solo per capire se il post in cima è cambiato dall'ultimo
    # controllo: niente tempi relativi, contatori o descrizioni foto, che
    # cambiano anche quando il post è lo stesso.
    post_id = " | ".join(
        p for p in (group_name, meta["author"], meta["published"], norm_text[:300]) if p
    )

    return {
        "id": post_id,
        "author": meta["author"],
        "location": meta["location"],
        "rel_time": meta["rel_time"],
        "published": meta["published"],
        "text": description,          # testo completo, usato per le keyword
        "display_text": display_text,  # testo pulito, mostrato nella notifica
        "photos": photos,
        "norm_text": norm_text,
        "url": post_url,
        "hrefs": list(hrefs),
        "group_name": group_name,
        "group_url": group_url,
    }


def redact(value):
    """Toglie il token del bot dai messaggi di errore prima di stamparli nel log."""
    return str(value).replace(TELEGRAM_BOT_TOKEN, "<TOKEN>")


def send_telegram_message(text, chat_id, as_html=False):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": chat_id,
        "text": text,
        # l'anteprima di un link Facebook è quasi sempre la pagina di login:
        # occupa spazio senza aggiungere informazioni
        "disable_web_page_preview": True,
    }
    if as_html:
        data["parse_mode"] = "HTML"
    try:
        response = requests.post(url, data=data, timeout=10)
        if response.status_code == 400 and as_html:
            # HTML rifiutato da Telegram: ripiega sul testo semplice
            plain = html.unescape(re.sub(r"<[^>]+>", "", text))
            return send_telegram_message(plain, chat_id)
        if response.status_code != 200:
            print(f"  Errore invio Telegram ({chat_id}): {response.status_code} - {redact(response.text)}")
        else:
            print(f"  Messaggio Telegram inviato correttamente a {chat_id}.")
    except Exception as e:
        print("  Errore invio Telegram:", redact(e))


def _esc(text):
    return html.escape(text, quote=False)


def _highlight(text, keywords):
    """Escapa il testo per l'HTML di Telegram mettendo in grassetto le keyword."""
    if not keywords:
        return _esc(text)
    pattern = re.compile("|".join(re.escape(k) for k in sorted(keywords, key=len, reverse=True)), re.IGNORECASE)
    out, last = [], 0
    for m in pattern.finditer(text):
        out.append(_esc(text[last:m.start()]))
        out.append(f"<b>{_esc(m.group(0))}</b>")
        last = m.end()
    out.append(_esc(text[last:]))
    return "".join(out)


def keyword_snippet(text, keyword, before=50, after=110):
    """Porzione di testo attorno alla prima occorrenza della keyword, tagliata su confini di parola."""
    pos = text.lower().find(keyword.lower())
    if pos == -1:
        return None
    start = max(0, pos - before)
    end = min(len(text), pos + len(keyword) + after)
    if start > 0:
        space = text.find(" ", start, pos)
        start = space + 1 if space != -1 else start
    if end < len(text):
        space = text.rfind(" ", pos + len(keyword), end)
        end = space if space != -1 else end
    snippet = text[start:end].strip()
    return ("…" if start > 0 else "") + snippet + ("…" if end < len(text) else "")


TEXT_IN_MESSAGE_MAX = 700


def build_alert_message(post, keywords):
    """
    La prima riga è quella che compare nell'anteprima della notifica sul
    telefono: ci mettiamo subito la frase in cui compare la parola chiave,
    così si capisce di che annuncio si tratta senza aprire la chat.
    """
    text = post["display_text"] or post["text"]
    photos_text = ", ".join(post["photos"])

    snippet, from_photo = None, False
    for k in keywords:
        snippet = keyword_snippet(text, k)
        if snippet:
            break
    if not snippet:
        for k in keywords:
            snippet = keyword_snippet(photos_text, k)
            if snippet:
                from_photo = True
                break
    if not snippet:
        snippet = text[:150] + ("…" if len(text) > 150 else "")

    kw_label = ", ".join(_esc(k) for k in keywords)
    first_line = f"🔔 <b>{kw_label}</b>: {'📷 ' if from_photo else ''}«{_highlight(snippet, keywords)}»"

    details = []
    if post["author"]:
        details.append(f"👤 {_esc(post['author'])}")
    if post["location"]:
        details.append(f"📍 {_esc(post['location'])}")
    if post["rel_time"]:
        details.append(f"🕒 {_esc(post['rel_time'])}")

    lines = [first_line]
    if details:
        lines.append(" · ".join(details))
    lines.append(f"👥 {_esc(post['group_name'])}")

    body = text if len(text) <= TEXT_IN_MESSAGE_MAX else text[:TEXT_IN_MESSAGE_MAX].rsplit(" ", 1)[0] + "…"
    if body and body != snippet.strip("…"):
        lines += ["", _highlight(body, keywords)]
    if photos_text:
        lines.append(f"📷 Foto: {_highlight(photos_text[:200], keywords)}")

    link = post["url"] or post["group_url"]
    if link:
        label = "Apri il post" if post["url"] else "Apri il gruppo"
        lines += ["", f'🔗 <a href="{html.escape(link, quote=True)}">{label}</a>']
    return "\n".join(lines)


def process_top_post(top_post, alerted_posts, chats):
    """
    A differenza delle parole chiave (una lista per ogni chat, vedi
    `chats`), le "bad keyword" restano condivise da tutte le chat: se
    una compare nel testo, nessuna chat viene avvisata per quel post.
    """
    if contains_bad_keyword(top_post["text"]):
        print("Trovata keyword non desiderata!")
        return False

    matched = False

    for chat_id, chat_data in chats.items():
        keywords = find_keywords(top_post["text"], chat_data.get("keywords", []))
        if not keywords:
            continue

        record = make_alert_record(chat_id, top_post)
        previous = find_previous_alert(record, alerted_posts)
        if previous:
            print(f"  Già segnalato a {chat_id} (gruppo {previous.get('group')!r}): salto.")
            continue

        alerted_posts.append(record)
        save_alerted_posts(alerted_posts)
        matched = True

        message = build_alert_message(top_post, keywords)
        print()
        print("=" * 60)
        print("Chat:", chat_id)
        print(html.unescape(re.sub(r"<[^>]+>", "", message)))
        print("=" * 60)
        print()

        send_telegram_message(message, chat_id, as_html=True)

    if not matched:
        print("Nessun nuovo annuncio da segnalare.")

    return matched


# ============================================================
# COMANDI TELEGRAM (/subscribe, /add, /remove)
# ============================================================

SUBSCRIBE_MESSAGE = (
    "✅ Iscrizione completata!\n\n"
    "Da ora riceverai un messaggio ogni volta che viene trovato un "
    "annuncio che contiene una delle tue parole chiave.\n\n"
    "Comandi disponibili:\n"
    "/add <parola> - aggiunge una parola chiave da cercare\n"
    "/remove <parola> - rimuove una parola chiave\n"
    "/keywords - mostra le tue parole chiave\n"
    "/clear - rimuove tutte le parole chiave\n\n"
    "Parole chiave di partenza:\n{keywords}"
)


def handle_subscribe(chat_id, chats):
    if chat_id not in chats:
        chats[chat_id] = {"keywords": list(KEYWORDS)}
        save_chats(chats)

    keywords_list = ", ".join(sorted(chats[chat_id]["keywords"], key=str.lower)) or "(nessuna)"
    send_telegram_message(SUBSCRIBE_MESSAGE.format(keywords=keywords_list), chat_id)


def handle_add(chat_id, argument, chats):
    if chat_id not in chats:
        send_telegram_message("Devi prima iscriverti con /subscribe.", chat_id)
        return

    parola = argument.strip().lower()
    if not parola:
        send_telegram_message("Uso: /add <parola>", chat_id)
        return

    keywords = chats[chat_id]["keywords"]
    if any(k.lower() == parola for k in keywords):
        send_telegram_message(f"'{parola}' è già tra le tue parole chiave.", chat_id)
        return

    keywords.append(parola)
    save_chats(chats)
    send_telegram_message(f"✅ Aggiunta parola chiave: '{parola}'", chat_id)


def handle_remove(chat_id, argument, chats):
    if chat_id not in chats:
        send_telegram_message("Devi prima iscriverti con /subscribe.", chat_id)
        return

    parola = argument.strip().lower()
    if not parola:
        send_telegram_message("Uso: /remove <parola>", chat_id)
        return

    keywords = chats[chat_id]["keywords"]
    match = next((k for k in keywords if k.lower() == parola), None)
    if not match:
        send_telegram_message(f"'{parola}' non è tra le tue parole chiave.", chat_id)
        return

    keywords.remove(match)
    save_chats(chats)
    send_telegram_message(f"🗑️ Rimossa parola chiave: '{parola}'", chat_id)


def handle_keywords(chat_id, chats):
    if chat_id not in chats:
        send_telegram_message("Devi prima iscriverti con /subscribe.", chat_id)
        return

    keywords = chats[chat_id]["keywords"]
    keywords_list = ", ".join(sorted(keywords, key=str.lower)) or "(nessuna)"
    send_telegram_message(f"Le tue parole chiave:\n{keywords_list}", chat_id)


def handle_clear(chat_id, chats):
    if chat_id not in chats:
        send_telegram_message("Devi prima iscriverti con /subscribe.", chat_id)
        return

    chats[chat_id]["keywords"] = []
    save_chats(chats)
    send_telegram_message("🗑️ Tutte le parole chiave sono state rimosse.", chat_id)


TELEGRAM_COMMANDS = [
    {"command": "subscribe", "description": "Iscrivi questa chat agli avvisi"},
    {"command": "add", "description": "Aggiungi una parola chiave"},
    {"command": "remove", "description": "Rimuovi una parola chiave"},
    {"command": "keywords", "description": "Mostra le tue parole chiave"},
    {"command": "clear", "description": "Rimuovi tutte le parole chiave"},
]


def register_telegram_commands():
    """
    Registra i comandi presso Telegram così che il client mostri il menu
    di autocompletamento quando l'utente digita "/". Va rifatto solo
    quando la lista cambia, ma richiamarlo ad ogni avvio non ha effetti
    collaterali: Telegram sovrascrive semplicemente la lista precedente.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setMyCommands"
    try:
        response = requests.post(url, json={"commands": TELEGRAM_COMMANDS}, timeout=10)
        if response.status_code != 200:
            print(f"  Errore registrazione comandi Telegram: {response.status_code} - {redact(response.text)}")
    except Exception as e:
        print("  Errore registrazione comandi Telegram:", redact(e))


def get_telegram_updates(offset):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json().get("result", [])
    except Exception as e:
        print("  Errore lettura comandi Telegram:", redact(e))
        return []


def process_telegram_commands(chats):
    """Legge i comandi Telegram in sospeso e li applica alla relativa chat."""
    global LAST_UPDATE_ID

    offset = LAST_UPDATE_ID + 1 if LAST_UPDATE_ID is not None else None
    updates = get_telegram_updates(offset)

    for update in updates:
        LAST_UPDATE_ID = update["update_id"]

        message = update.get("message")
        if not message or "text" not in message:
            continue

        chat_id = str(message["chat"]["id"])
        text = message["text"].strip()
        if not text.startswith("/"):
            continue

        parts = text.split(maxsplit=1)
        command = parts[0].split("@")[0].lower()
        argument = parts[1] if len(parts) > 1 else ""

        if command == "/subscribe":
            handle_subscribe(chat_id, chats)
        elif command == "/add":
            handle_add(chat_id, argument, chats)
        elif command == "/remove":
            handle_remove(chat_id, argument, chats)
        elif command == "/keywords":
            handle_keywords(chat_id, chats)
        elif command == "/clear":
            handle_clear(chat_id, chats)


def select_new_posts(driver):
    """
    Apre il menu di ordinamento del gruppo e seleziona "Nuovi post",
    come fallback nel caso il parametro sorting_setting nell'URL non
    venga applicato da Facebook (capita, non è affidabile al 100%).

    Il pulsante che apre il menu può mostrare etichette diverse a
    seconda di cosa è attualmente selezionato (es. "Più pertinenti" è
    il default se non è mai stato cambiato), quindi ne controlliamo
    diverse varianti invece di una sola.
    """
    SORT_BUTTON_HINTS = [
        "pertinenti", "rilevanti", "recenti", "recente",
        "cronologico", "attività", "post più",
    ]
    SORT_OPTION_HINTS = [
        "nuovi post", "più recenti", "data di pubblicazione",
    ]

    buttons = driver.find_elements(By.XPATH, "//div[@role='button']")
    for button in buttons:
        try:
            text = button.text.strip().lower()
            if any(hint in text for hint in SORT_BUTTON_HINTS):
                button.click()
                time.sleep(1)
                break
        except Exception:
            continue

    # Prima proviamo con il ruolo tipico delle voci di menu di Facebook
    menu_items = driver.find_elements(By.XPATH, "//div[@role='menuitem']")
    for item in menu_items:
        try:
            text = item.text.strip().lower()
            # testo corto: evitiamo di intercettare per sbaglio un
            # contenitore enorme che contiene la frase incidentalmente
            if text and len(text) < 60 and any(hint in text for hint in SORT_OPTION_HINTS) and item.is_displayed():
                item.click()
                time.sleep(3)
                print("  Ordinamento impostato su: Nuovi post")
                return True
        except Exception:
            continue

    # Fallback: cerca span/testo brevi con l'etichetta, se il menu non
    # usa role="menuitem"
    candidates = driver.find_elements(By.XPATH, "//span")
    for el in candidates:
        try:
            text = el.text.strip().lower()
            if text and len(text) < 60 and any(hint in text for hint in SORT_OPTION_HINTS) and el.is_displayed():
                el.click()
                time.sleep(3)
                print("  Ordinamento impostato su: Nuovi post")
                return True
        except Exception:
            continue

    return False


# ============================================================
# MAIN
# ============================================================

def parse_args():
    parser = argparse.ArgumentParser(description="Monitor Facebook multi-gruppo.")
    parser.add_argument(
        "--headless",
        action="store_true",
        help=(
            "Avvia Chrome senza finestra grafica (--headless=new). Richiede "
            "che chrome_profile/ sia già autenticato su Facebook: il primo "
            "login va fatto senza questa opzione, dato che in headless non "
            "si vede la pagina per inserire le credenziali."
        ),
    )
    parser.add_argument(
        "--fixed-interval",
        action="store_true",
        help=(
            "Ignora l'algoritmo di priorità adattivo: ogni gruppo resta "
            f"all'intervallo di partenza ({GROUP_CHECK_DEFAULT_INTERVAL}s), "
            "senza accelerare sui gruppi attivi né rallentare su quelli "
            "silenziosi. Utile per confronto/debug."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help=(
            "Numero di istanze Chrome parallele che pescano i gruppi da "
            "controllare dalla stessa coda di priorità (non una divisione "
            "statica dei gruppi): un worker libero prende semplicemente il "
            "prossimo gruppo più urgente. Default 1 (nessun parallelismo). "
            "Ogni worker oltre il primo clona chrome_profile/ (può volerci "
            "qualche minuto la prima volta) e apre un'istanza Chrome in più: "
            "da usare con --headless per limitare il consumo di RAM."
        ),
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help=(
            "Richiesta di login su FB, comporta una pausa per logarsi"
        ),
    )
    return parser.parse_args()


def clone_chrome_profile(worker_index):
    """
    Ogni istanza Chrome ha bisogno del proprio --user-data-dir: due processi
    non possono condividere la stessa cartella profilo. Duplichiamo quindi il
    profilo principale (già loggato su Facebook) una volta sola per ciascun
    worker aggiuntivo, così partono già autenticati senza rifare il login.
    """
    worker_profile = CHROME_PROFILE.parent / f"{CHROME_PROFILE.name}_worker{worker_index}"
    if not worker_profile.exists():
        print(f"Clono il profilo Chrome per il worker {worker_index} (può volerci qualche minuto)...")
        shutil.copytree(
                CHROME_PROFILE,
                worker_profile,
                ignore=shutil.ignore_patterns(
                    "SingletonSocket",
                    "SingletonCookie",
                    "SingletonLock"
                )
                )
    return worker_profile


def build_driver(profile_dir, args):
    options = Options()

    if args.headless:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")

    if sys.platform != "win32":
        # Path fissi del Raspberry Pi: su Windows lasciamo che Selenium
        # trovi da solo Chrome e il chromedriver giusto (Selenium Manager).
        options.binary_location = "/usr/bin/chromium"

    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--blink-settings=imagesEnabled=false")

    if sys.platform != "win32":
        service = Service("/usr/bin/chromedriver")
        #driver = webdriver.Chrome(service=service, options=options)
        driver = webdriver.Chrome(options=options)
    else:
        driver = webdriver.Chrome(options=options)

    try:
        # Font e media non servono a estrarre il testo del post: li
        # blocchiamo via CDP oltre alle immagini (già disattivate sopra)
        # per alleggerire ulteriormente il caricamento. Non tocchiamo i
        # CSS: senza stile, elementi che Facebook nasconde a video
        # potrebbero risultare "visibili" nel DOM e sporcare il testo
        # estratto da get_top_post().
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setBlockedURLs", {
            "urls": [
                "*.woff", "*.woff2", "*.ttf", "*.otf", "*.eot",
                "*.mp4", "*.webm", "*.ogg", "*.mp3", "*.avi", "*.mov",
            ]
        })
    except Exception as e:
        print("Impossibile impostare il blocco extra di font/media via CDP:", e)

    return driver


def reload_keywords():
    global KEYWORDS
    global BAD_KEYWORDS

    with open("keywords.json", encoding="utf-8") as f:
        KEYWORDS = json.load(f)

    with open("bad_keywords.json", encoding="utf-8") as f:
        BAD_KEYWORDS = json.load(f)


def check_group(driver, group, chats, alerted_posts, last_seen_ids):
    """
    Controlla un singolo gruppo (con retry) ed eventualmente processa il
    nuovo post. Ritorna "new" se ha trovato un post diverso dall'ultimo
    visto, "same" se non c'è nulla di nuovo, "failed" se tutti i tentativi
    sono falliti: lo scheduler in main() usa questo esito per decidere se
    controllare il gruppo più spesso o più di rado.
    """
    group_name = group["name"]
    group_url = group["url"]

    top_post = None
    for attempt in range(1, GROUP_CHECK_RETRIES + 1):
        try:
            driver.get(build_group_url(group_url))
            # piccola pausa casuale, non per aspettare il caricamento ma per stealth
            time.sleep(5)
            #select_new_posts(driver)

            top_post = get_top_post(driver, group_name, group_url)
            break

        except KeyboardInterrupt:
            raise

        except Exception as e:
            print(
                f"  Tentativo {attempt}/{GROUP_CHECK_RETRIES} fallito per "
                f"{group_name}: {type(e).__name__}: {e}"
            )
            if attempt < GROUP_CHECK_RETRIES:
                time.sleep(GROUP_CHECK_RETRY_DELAY)
                # forza un reload vero e proprio (non un semplice
                # driver.get sullo stesso URL) prima di ritentare,
                # nel caso la pagina sia rimasta bloccata in uno
                # stato non valido (es. checkpoint, spinner fisso)
                try:
                    driver.refresh()
                except Exception:
                    pass
    else:
        print(f"Errore durante il controllo di {group_name}: tutti i {GROUP_CHECK_RETRIES} tentativi falliti.")
        return "failed"

    print(f"[{time.strftime('%H:%M:%S')}] [{group_name} : {group_url}] Controllo eseguito.")

    if top_post is None:
        return "same"

    print(f"  Testo estratto: {top_post['text'][:200]!r}")
    if top_post["author"]:
        print(f"  Autore: {top_post['author']!r}")

    if top_post["id"] == last_seen_ids[group_url]:
        return "same"

    last_seen_ids[group_url] = top_post["id"]

    with STATE_LOCK:
        process_top_post(top_post, alerted_posts, chats)
    return "new"


def pop_due_group(schedule):
    """Estrae dalla coda il prossimo gruppo dovuto, o None se non c'è ancora nulla da controllare."""
    with SCHEDULE_LOCK:
        if not schedule:
            return None
        next_time, group_url = schedule[0]
        if next_time > time.time():
            return None
        heapq.heappop(schedule)
        return group_url


def reschedule_group(schedule, intervals, group_url, group_name, result, fixed_interval):
    with SCHEDULE_LOCK:
        interval = intervals[group_url]
        if fixed_interval:
            pass  # algoritmo di priorità disattivato: intervallo sempre quello di partenza
        elif result == "new":
            interval = max(GROUP_CHECK_MIN_INTERVAL, interval / GROUP_CHECK_SPEEDUP_FACTOR)
            print(f"  -> {group_name} attivo: prossimo controllo tra {interval:.0f}s")
        elif result == "same":
            interval = min(GROUP_CHECK_MAX_INTERVAL, interval * GROUP_CHECK_SLOWDOWN_FACTOR)
        # "failed": lascia l'intervallo invariato, non è indicativo
        # dell'attività del gruppo

        intervals[group_url] = interval
        heapq.heappush(schedule, (time.time() + interval, group_url))


def worker_loop(driver, chats, alerted_posts, last_seen_ids, groups_by_url, intervals, schedule, args, stop_event):
    while not stop_event.is_set():
        group_url = pop_due_group(schedule)
        if group_url is None:
            # nessun gruppo ancora dovuto: aspetta un attimo e riprova,
            # invece di consumare CPU in un loop stretto
            time.sleep(0.2)
            continue

        group = groups_by_url[group_url]
        try:
            result = check_group(driver, group, chats, alerted_posts, last_seen_ids)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"Errore inatteso nel worker sul gruppo {group['name']}:", e)
            result = "failed"

        reschedule_group(schedule, intervals, group_url, group["name"], result, args.fixed_interval)


def main():
    args = parse_args()

    chiudi_chrome()
    if not GROUPS:
        print("Nessun gruppo configurato in GROUPS. Aggiungine almeno uno.")
        return

    print("Avvio monitor Facebook multi-gruppo...")
    print("Profilo Chrome:", CHROME_PROFILE)
    print("Modalità:", "headless (senza finestra)" if args.headless else "con finestra grafica")

    if args.headless and not CHROME_PROFILE.exists():
        print(
            "ATTENZIONE: chrome_profile/ non esiste ancora, quindi non risulta "
            "nessun login salvato. In modalità headless non potrai vedere la "
            "pagina per effettuare il login manuale: riavvia senza --headless "
            "per il primo login, poi torna a usare --headless."
        )

    

    disattivati = len(ALL_GROUPS) - len(GROUPS)
    if disattivati:
        print(f"Gruppi disattivati (attivo=false), esclusi dal monitoraggio: {disattivati}")

    print("Gruppi monitorati:")
    for g in GROUPS:
        print(f"  - {g['name']}: {g['url']}")

    alerted_posts = load_alerted_posts()
    print(f"Post già segnalati in sessioni precedenti: {len(alerted_posts)}")

    chats = load_chats()
    print(f"Chat iscritte: {len(chats)}")

    register_telegram_commands()

    worker_count = max(1, min(args.workers, len(GROUPS)))
    print(f"Worker paralleli: {worker_count}")

    driver = build_driver(CHROME_PROFILE, args)
    drivers = [driver]

    last_seen_ids = {g["url"]: None for g in GROUPS}

    try:
        first_group = GROUPS[0]
        print(f"\nApro il primo gruppo ({first_group['name']})...")
        
        driver.get(build_group_url(first_group["url"]))

        time.sleep(5)
        #select_new_posts(driver)

        if args.login:
            print()
            input("Se necessario, effettua il login a Facebook!")


        print()
        if args.fixed_interval:
            print(f"Monitoraggio avviato con intervallo fisso di {GROUP_CHECK_DEFAULT_INTERVAL}s per gruppo (algoritmo di priorità disattivato).")
        else:
            print("Monitoraggio avviato con intervallo adattivo per gruppo.")
            print(f"Ogni gruppo parte da {GROUP_CHECK_DEFAULT_INTERVAL}s tra un controllo e il successivo:")
            print(f"  si restringe fino a {GROUP_CHECK_MIN_INTERVAL}s se trova post nuovi di seguito,")
            print(f"  si allarga fino a {GROUP_CHECK_MAX_INTERVAL}s se resta silenzioso.")
        print("Ad ogni controllo esamino solo il post in cima al feed (posinset=1) del gruppo.")
        print()

        # Gli eventuali worker aggiuntivi partono dopo il login sul profilo
        # principale, così ereditano subito la sessione già autenticata.
        for i in range(1, worker_count):
            profile_dir = clone_chrome_profile(i)
            drivers.append(build_driver(profile_dir, args))

        groups_by_url = {g["url"]: g for g in GROUPS}
        intervals = {g["url"]: GROUP_CHECK_DEFAULT_INTERVAL for g in GROUPS}
        # coda di priorità (next_check_time, group_url): tutti i gruppi
        # partono "dovuti" subito, poi ciascuno si ripianifica in base a
        # quanto si è rivelato attivo. Condivisa tra tutti i worker: un
        # worker libero pesca semplicemente il prossimo gruppo più urgente.
        schedule = [(time.time(), g["url"]) for g in GROUPS]
        heapq.heapify(schedule)

        stop_event = threading.Event()
        threads = [
            threading.Thread(
                target=worker_loop,
                args=(d, chats, alerted_posts, last_seen_ids, groups_by_url, intervals, schedule, args, stop_event),
                daemon=True,
            )
            for d in drivers
        ]
        for t in threads:
            t.start()

        # I comandi Telegram e il reload delle keyword restano sul thread
        # principale, indipendenti dalla frequenza di controllo dei gruppi.
        while True:
            try:
                reload_keywords()
                with STATE_LOCK:
                    process_telegram_commands(chats)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                # non deve mai far cadere il processo: un errore qui (es.
                # keywords.json momentaneamente non valido perché in fase
                # di salvataggio, o un problema di rete verso Telegram) non
                # deve fermare il monitoraggio dei gruppi sugli altri thread
                print(f"Errore nel loop comandi Telegram/keyword: {type(e).__name__}: {e}")
            time.sleep(1)

    except KeyboardInterrupt:
        print("\nMonitoraggio terminato.")

    except Exception:
        import traceback
        print("ERRORE FATALE, il monitoraggio si interrompe:")
        traceback.print_exc()
        raise

    finally:
        for d in drivers:
            try:
                d.quit()
            except Exception:
                pass


if __name__ == "__main__":
    main()