import sys
import os
import time
import re
import json
import unicodedata
from pathlib import Path
import psutil

import requests

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service

print("Configuro encoding...")
if sys.platform == "win32":
    os.system("chcp 65001 > nul")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


# ============================================================
# CONFIGURAZIONE
# ============================================================

TELEGRAM_BOT_TOKEN = "8229344375:AAGCQAHkjzDL3YIyaP2-Em89jotb3eUblzs"
TELEGRAM_CHAT_ID = "197595708"

KEYWORDS = []
BAD_KEYWORDS = []

GROUPS = {}

with open('groups.json') as f:
    GROUPS = json.load(f)


CHECK_INTERVAL = 30  # secondi di pausa tra un ciclo completo e il successivo

CHROME_PROFILE = Path.cwd() / "chrome_profile"

ALERTED_POSTS_FILE = Path.cwd() / "alerted_posts.json"


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


def contains_keyword(text):
    """Restituisce la parola chiave trovata (case-insensitive), oppure None."""
    text_lower = text.lower()
    for keyword in KEYWORDS:
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
        container = driver.find_element(By.CSS_SELECTOR, 'div[aria-posinset="1"]')
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


def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or "INSERISCI_QUI" in TELEGRAM_BOT_TOKEN:
        print("  (Telegram non configurato: imposta TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID)")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        response = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "disable_web_page_preview": False,
            },
            timeout=10
        )
        if response.status_code != 200:
            print(f"  Errore invio Telegram: {response.status_code} - {response.text}")
    except Exception as e:
        print("  Errore invio Telegram:", e)


def process_top_post(top_post, alerted_posts):
    keyword = contains_keyword(top_post["text"])
    bad_keyword = contains_bad_keyword(top_post["text"])

    if not keyword:
        print("Nessuna keyword trovata!")
        return False
    
    if bad_keyword:
        print("Trovata keyword non desiderata!")
        return False

    if top_post["id"] in alerted_posts:
        return False

    alerted_posts.add(top_post["id"])
    save_alerted_posts(alerted_posts)

    print()
    print("=" * 60)
    print("🚨🚨 NUOVO ANNUNCIO TROVATO 🚨🚨")
    print("Gruppo:", top_post["group_name"])
    print("Parola:", keyword)
    if top_post["author"]:
        print("Autore:", top_post["author"])
    print("-" * 60)
    print(top_post["text"][:1000])
    print("=" * 60)
    print()

    message_lines = [
        "🚨🚨 NUOVO ANNUNCIO TROVATO 🚨🚨",
        f"Gruppo: {top_post['group_name']}",
        f"Parola: {keyword}",
    ]
    if top_post["author"]:
        message_lines.append(f"Autore: {top_post['author']}")
    message_lines.append("")
    message_lines.append(top_post["text"][:1000])
    message_lines.append("")
    message_lines.append(top_post["url"] or top_post["group_url"])

    send_telegram_message("\n".join(message_lines))

    return True


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


def main():
    chiudi_chrome()
    if not GROUPS:
        print("Nessun gruppo configurato in GROUPS. Aggiungine almeno uno.")
        return

    print("Avvio monitor Facebook multi-gruppo...")
    print("Profilo Chrome:", CHROME_PROFILE)

    print("Gruppi monitorati:")
    for g in GROUPS:
        print(f"  - {g['name']}: {g['url']}")

    alerted_posts = load_alerted_posts()
    print(f"Post già segnalati in sessioni precedenti: {len(alerted_posts)}")

    options = Options()

    options.binary_location = "/usr/bin/chromium"

    options.add_argument(
        f"--user-data-dir={CHROME_PROFILE}"
    )

    service = Service("/usr/bin/chromedriver")

    driver = webdriver.Chrome(
        service=service,
        options=options
    )

    last_seen_ids = {g["url"]: None for g in GROUPS}

    try:
        first_group = GROUPS[0]
        print(f"\nApro il primo gruppo ({first_group['name']}) per il login...")
        driver.get(build_group_url(first_group["url"]))
        time.sleep(5)
        select_new_posts(driver)

        print()
        print("Se necessario, effettua il login a Facebook.")
        time.sleep(10)

        print()
        print("Monitoraggio avviato.")
        print("Controllo ogni ciclo completo ogni", CHECK_INTERVAL, "secondi.")
        print("Ad ogni giro esamino solo il post in cima al feed (posinset=1) di ogni gruppo.")
        print()

        while True:

            reload_keywords()
            print("Parole cercate:", ", ".join(KEYWORDS))

            for group in GROUPS:
                group_name = group["name"]
                group_url = group["url"]

                try:
                    driver.get(build_group_url(group_url))
                    time.sleep(5)
                    select_new_posts(driver)

                    top_post = get_top_post(driver, group_name, group_url)

                    print(f"[{time.strftime('%H:%M:%S')}] [{group_name}] Controllo eseguito.")

                    if top_post is None:
                        continue

                    print(f"  Testo estratto: {top_post['text'][:200]!r}")
                    if top_post["author"]:
                        print(f"  Autore: {top_post['author']!r}")

                    if top_post["id"] == last_seen_ids[group_url]:
                        continue

                    last_seen_ids[group_url] = top_post["id"]

                    process_top_post(top_post, alerted_posts)

                except KeyboardInterrupt:
                    raise

                except Exception as e:
                    print(f"Errore durante il controllo di {group_name}:", e)

            print("Fine ciclo completo.")
            
            for i in range(CHECK_INTERVAL):
                print(".", end="", flush=True)
                time.sleep(1)
           

    except KeyboardInterrupt:
        print("\nMonitoraggio terminato.")

    finally:
        driver.quit()


if __name__ == "__main__":
    main()