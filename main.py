import sys
import os
import time
import re
import json
import heapq
import unicodedata
import configparser
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

# Intervallo tra due controlli dello stesso gruppo: si restringe verso il
# minimo quando il gruppo produce post nuovi (per essere tempestivi nella
# prenotazione) e si allarga verso il massimo quando resta silenzioso, per
# non sprecare cicli su gruppi poco attivi e concentrarsi su quelli attivi.
GROUP_CHECK_MIN_INTERVAL = 3        # secondi minimi anche per un gruppo molto attivo
GROUP_CHECK_MAX_INTERVAL = 90       # secondi massimi per un gruppo silenzioso
GROUP_CHECK_DEFAULT_INTERVAL = 15   # intervallo di partenza, prima di sapere quanto è attivo
GROUP_CHECK_SPEEDUP_FACTOR = 2      # riduzione dell'intervallo dopo un post nuovo
GROUP_CHECK_SLOWDOWN_FACTOR = 1.3   # aumento dell'intervallo se non trova nulla di nuovo

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



def load_alerted_posts():
    """Carica da disco gli ID dei post già segnalati in passato."""
    if ALERTED_POSTS_FILE.exists():
        try:
            with open(ALERTED_POSTS_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception as e:
            print("Impossibile leggere", ALERTED_POSTS_FILE, "->", e)
    return set()


def save_alerted_posts(alerted_posts):
    """Salva su disco l'insieme aggiornato dei post già segnalati."""
    try:
        with open(ALERTED_POSTS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(alerted_posts), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Impossibile salvare", ALERTED_POSTS_FILE, "->", e)


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
    r"\d+(?:\s+\d+)?\s+(?:[A-ZÀÈÉÌÒÙ][\w'\-]*\s+){1,4}"
    r"·\s*\d+\s*(?:min|h|g|ore|giorni|sett|mese|mesi|anno|anni)\b"
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


def contains_keyword(text, keywords):
    """Restituisce la parola chiave trovata (case-insensitive), oppure None."""
    text_lower = text.lower()
    for keyword in keywords:
        if keyword.lower() in text_lower:
            return keyword
    return None

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

    try:
        raw_text = container.text.strip()
    except Exception:
        raw_text = ""

    if not raw_text:
        return None

    text = clean_facebook_text(raw_text)

    header, body = split_header_and_body(text)
    author = extract_author(header)

    description = clean_description(body)
    if description is None:
        description = body.strip() or text

    post_url, hrefs = extract_post_url(container)

    id_parts = [group_name]
    if author:
        id_parts.append(author)
    id_parts.append(description)
    post_id = " | ".join(id_parts)[:1000]

    return {
        "id": post_id,
        "author": author,
        "text": description,
        "url": post_url,
        "hrefs": hrefs,
        "group_name": group_name,
        "group_url": group_url,
    }


def send_telegram_message(text, chat_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        response = requests.post(
            url,
            data={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": False,
            },
            timeout=10
        )
        if response.status_code != 200:
            print(f"  Errore invio Telegram ({chat_id}): {response.status_code} - {response.text}")
    except Exception as e:
        print("  Errore invio Telegram:", e)


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
        keyword = contains_keyword(top_post["text"], chat_data.get("keywords", []))
        if not keyword:
            continue

        alert_key = f"{chat_id}|{top_post['id']}"
        if alert_key in alerted_posts:
            continue

        alerted_posts.add(alert_key)
        save_alerted_posts(alerted_posts)
        matched = True

        print()
        print("=" * 60)
        print(f"🚨 {str(keyword).upper()} 🚨")
        print("Chat:", chat_id)
        print("Gruppo:", top_post["group_name"])
        print("Parola:", keyword)
        if top_post["author"]:
            print("Autore:", top_post["author"])
        print("-" * 60)
        print(top_post["text"][:1000])
        print("=" * 60)
        print()

        message_lines = [
            f"🚨 TROVATO: {str(keyword).upper()} 🚨",
            f"Gruppo: {top_post['group_name']}",
            f"Parola: {keyword}",
        ]
        if top_post["author"]:
            message_lines.append(f"Autore: {top_post['author']}")
        message_lines.append("")
        message_lines.append(top_post["text"][:1000])
        message_lines.append("")
        message_lines.append(top_post["url"] or top_post["group_url"])

        send_telegram_message("\n".join(message_lines), chat_id)

    if not matched:
        print("Nessuna keyword trovata!")

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
            print(f"  Errore registrazione comandi Telegram: {response.status_code} - {response.text}")
    except Exception as e:
        print("  Errore registrazione comandi Telegram:", e)


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
        print("  Errore lettura comandi Telegram:", e)
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
            time.sleep(random.uniform(0.1, 1))
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

    print(f"[{time.strftime('%H:%M:%S')}] [{group_name}] Controllo eseguito.")

    if top_post is None:
        return "same"

    print(f"  Testo estratto: {top_post['text'][:200]!r}")
    if top_post["author"]:
        print(f"  Autore: {top_post['author']!r}")

    if top_post["id"] == last_seen_ids[group_url]:
        return "same"

    last_seen_ids[group_url] = top_post["id"]

    process_top_post(top_post, alerted_posts, chats)
    return "new"


def main():
    chiudi_chrome()
    if not GROUPS:
        print("Nessun gruppo configurato in GROUPS. Aggiungine almeno uno.")
        return

    print("Avvio monitor Facebook multi-gruppo...")
    print("Profilo Chrome:", CHROME_PROFILE)

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

    options = Options()

    if sys.platform != "win32":
        # Path fissi del Raspberry Pi: su Windows lasciamo che Selenium
        # trovi da solo Chrome e il chromedriver giusto (Selenium Manager).
        options.binary_location = "/usr/bin/chromium"

    options.add_argument(
        f"--user-data-dir={CHROME_PROFILE}"
    )
    options.add_argument("--blink-settings=imagesEnabled=false")

    if sys.platform != "win32":
        service = Service("/usr/bin/chromedriver")
        driver = webdriver.Chrome(service=service, options=options)
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

    last_seen_ids = {g["url"]: None for g in GROUPS}

    try:
        first_group = GROUPS[0]
        print(f"\nApro il primo gruppo ({first_group['name']}) per il login...")
        driver.get(build_group_url(first_group["url"]))
        time.sleep(5)
        #select_new_posts(driver)

        print()
        print("Se necessario, effettua il login a Facebook.")
        time.sleep(10)

        print()
        print("Monitoraggio avviato con intervallo adattivo per gruppo.")
        print(f"Ogni gruppo parte da {GROUP_CHECK_DEFAULT_INTERVAL}s tra un controllo e il successivo:")
        print(f"  si restringe fino a {GROUP_CHECK_MIN_INTERVAL}s se trova post nuovi di seguito,")
        print(f"  si allarga fino a {GROUP_CHECK_MAX_INTERVAL}s se resta silenzioso.")
        print("Ad ogni controllo esamino solo il post in cima al feed (posinset=1) del gruppo.")
        print()

        groups_by_url = {g["url"]: g for g in GROUPS}
        intervals = {g["url"]: GROUP_CHECK_DEFAULT_INTERVAL for g in GROUPS}
        # coda di priorità (next_check_time, group_url): tutti i gruppi
        # partono "dovuti" subito, poi ciascuno si ripianifica in base a
        # quanto si è rivelato attivo.
        schedule = [(time.time(), g["url"]) for g in GROUPS]
        heapq.heapify(schedule)

        while True:

            reload_keywords()
            process_telegram_commands(chats)

            now = time.time()
            next_time, group_url = schedule[0]
            if next_time > now:
                # nessun gruppo ancora dovuto: aspetta al massimo 1s per
                # restare comunque reattivo ai comandi Telegram
                time.sleep(min(next_time - now, 1))
                continue

            heapq.heappop(schedule)
            group = groups_by_url[group_url]

            result = check_group(driver, group, chats, alerted_posts, last_seen_ids)

            interval = intervals[group_url]
            if result == "new":
                interval = max(GROUP_CHECK_MIN_INTERVAL, interval / GROUP_CHECK_SPEEDUP_FACTOR)
                print(f"  -> {group['name']} attivo: prossimo controllo tra {interval:.0f}s")
            elif result == "same":
                interval = min(GROUP_CHECK_MAX_INTERVAL, interval * GROUP_CHECK_SLOWDOWN_FACTOR)
            # "failed": lascia l'intervallo invariato, non è indicativo
            # dell'attività del gruppo

            intervals[group_url] = interval
            heapq.heappush(schedule, (time.time() + interval, group_url))
           

    except KeyboardInterrupt:
        print("\nMonitoraggio terminato.")

    finally:
        driver.quit()


if __name__ == "__main__":
    main()