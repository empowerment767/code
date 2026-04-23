import asyncio
import http.cookiejar as _cookiejar
import io
import json
import os
import re
import tempfile
import threading
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from openai import OpenAI
from PIL import Image
import yt_dlp

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ===== TOKENS =====
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8610501182:AAF_w5tOE446-4DaXJztk2dlh13rcX526Kk")
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY",   "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")

# ===== GROQ CLIENT =====
groq_client = OpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1"
)

GROQ_MODEL = "llama-3.3-70b-versatile"

# ===== MEMORY =====
user_memory = {}
_memory_lock = threading.Lock()
_user_locks: dict[int, asyncio.Lock] = {}

def _get_user_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _user_locks:
        _user_locks[user_id] = asyncio.Lock()
    return _user_locks[user_id]

# ===== FONT STATE =====
font_pending = {}  # user_id -> font_name

# ===== GIF BUILD STATE =====
gif_pending = {}  # user_id -> {"zip_bytes": bytes, "step": "fps"} | {..., "step": "maxsize", "fps": float}
GIF_CMD_RE = re.compile(r'собери\s*гиф', re.IGNORECASE)


STYLE_ALIASES = {
    "regular":        "Regular",
    "обычный":        "Regular",
    "normal":         "Regular",
    "bold":           "Bold",
    "жирный":         "Bold",
    "italic":         "Italic",
    "курсив":         "Italic",
    "наклонный":      "Italic",
    "bolditalic":     "BoldItalic",
    "bold italic":    "BoldItalic",
    "жирный курсив":  "BoldItalic",
    "light":          "Light",
    "светлый":        "Light",
    "thin":           "Thin",
    "тонкий":         "Thin",
    "medium":         "Medium",
    "медиум":         "Medium",
    "semibold":       "SemiBold",
    "полужирный":     "SemiBold",
    "extrabold":      "ExtraBold",
    "black":          "Black",
    "extralight":     "ExtraLight",
}

# ===== URL PATTERNS =====
VK_RE = re.compile(
    r'https?://(?:www\.)?vk\.com/(?:video[\-\d_]+|music/album/[\w\-]+|wall[\-\d_]+|clip[\-\d_]+)'
)
SOUNDCLOUD_RE = re.compile(
    r'https?://(?:www\.)?soundcloud\.com/[\w\-]+/[\w\-]+'
)
DEEZER_RE = re.compile(
    r'https?://(?:www\.)?deezer\.com/(?:\w+/)?(?:track|album|playlist)/\d+'
)

# Telegram bot file size limit: 50 MB
MAX_FILE_SIZE = 50 * 1024 * 1024


# ===== WEATHER KEYWORDS =====
WEATHER_RE = re.compile(
    r'\b(?:weather|погода[а-яё]*|прогноз|температур[а-яё]*|climate)\b',
    re.IGNORECASE
)
CITY_RE = re.compile(
    r'(?:в|in|для|for|во)\s+([А-ЯЁа-яёA-Za-z][а-яёa-z\-A-Za-z\s]{1,30}?)(?:\s*[?,.]|$)',
    re.IGNORECASE
)


def get_current_datetime() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("Текущая дата и время (UTC): %d.%m.%Y %H:%M")


def fetch_weather(city: str) -> str:
    city = city.strip()
    encoded = urllib.parse.quote(city)
    url = f"https://wttr.in/{encoded}?format=j1"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        cur = data["current_condition"][0]
        desc_list = cur.get("lang_ru") or cur.get("weatherDesc") or [{"value": ""}]
        desc = desc_list[0]["value"]
        temp     = cur["temp_C"]
        feels    = cur["FeelsLikeC"]
        humidity = cur["humidity"]
        wind     = cur["windspeedKmph"]
        return (
            f"[Реальная погода в {city}: {desc}, "
            f"температура {temp}°C (ощущается {feels}°C), "
            f"влажность {humidity}%, ветер {wind} км/ч]"
        )
    except Exception:
        return ""


def extract_city(text: str) -> str:
    m = CITY_RE.search(text)
    if m:
        return m.group(1).strip()
    return ""


def build_system_prompt(extra: str = "") -> str:
    base = (
        get_current_datetime() + ".\n\n"
        "Ты — живой чувак, не робот и не ассистент. Общаешься как реальный человек: "
        "по-простому, без пафоса, иногда грубовато. Используешь матерные слова там, где это уместно — "
        "естественно, не через каждое слово, а как обычный русскоязычный человек в разговоре с другом. "
        "Никогда не говоришь что ты ИИ или бот, не извиняешься за мат, не добавляешь дисклеймеры. "
        "Мнение своё высказываешь прямо. Можешь подколоть, пошутить, поспорить. "
        "При вопросах о текущей дате или времени — используй данные выше. "
        "При вопросах о погоде — используй данные ниже, если они есть."
    )
    if extra:
        base += "\n" + extra
    return base


# ===== NAVIGATION KEYBOARD =====

def main_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("🗑 Очистить память", callback_data="reset_memory"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


# ===== GROQ FUNCTION =====
def ask_groq(user_id, prompt, system_extra: str = ""):
    with _memory_lock:
        history = list(user_memory.get(user_id, []))
    history.append({"role": "user", "content": prompt})

    messages = [{"role": "system", "content": build_system_prompt(system_extra)}] + history

    response = groq_client.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages
    )

    answer = response.choices[0].message.content
    history.append({"role": "assistant", "content": answer})
    with _memory_lock:
        user_memory[user_id] = history[-10:]
    return answer


# ===== FONT DOWNLOAD =====

FONT_RE     = re.compile(r'^шрифт\s+(.+)$', re.IGNORECASE)
MUSIC_RE    = re.compile(r'^музыка\s+(.+)$', re.IGNORECASE)

# Regex to detect "fetch media from internet" intent (pre-filter before Groq classification)
INTERNET_RE = re.compile(
    r'\b(пришли|найди|покажи|скачай|дай|отправь|кинь|залей|send|find|get|show|download|fetch|give)\b'
    r'.{0,120}'
    r'\b(картинк|фото|изображен|фотографи|схем|фотоинструкц|инструкц|гайд|guide'
    r'|видео|видеоурок|видео-?инструк|туториал|урок|ролик|клип|музык|саундтрек'
    r'|ost|трек|песн|soundtrack|song|track|music)',
    re.IGNORECASE | re.DOTALL,
)
# Catch standalone "тикток" / "шортс" requests (with or without topic)
TIKTOK_SHORTS_RE = re.compile(
    r'\b(тикток|тиктоки|tiktok|шортс|шортсы|shorts)\b',
    re.IGNORECASE,
)
# Also catch "музыка из фильма/игры/сериала" without leading verb
MEDIA_FROM_RE = re.compile(
    r'\b(музык|саундтрек|ost|трек|песн|soundtrack|song).{0,60}\b(фильм|игр|сериал|мультфильм|аним|movie|game|series)',
    re.IGNORECASE,
)
# Catch requests like "как заменить смеситель в картинках / пошагово"
PHOTO_GUIDE_RE = re.compile(
    r'\b(как|how).{0,80}\b(картинк|фото|пошагово|по шагам|step.?by.?step|инструкц|схем)',
    re.IGNORECASE | re.DOTALL,
)
# Catch radio requests: "радио джаз", "включи рок радио", "радио с классикой"
RADIO_RE = re.compile(
    r'\b(?:'
    r'радио\b.{0,60}'
    r'|radio\b.{0,60}'
    r'|включи\b.{0,40}(?:радио|music|музык)'
    r'|поставь\b.{0,40}(?:радио|музык)'
    r'|хочу\s+(?:послушать|слушать)\s+радио'
    r')',
    re.IGNORECASE,
)

# Genre → search queries on YouTube (long mixes / radio shows)
RADIO_QUERIES: dict[str, list[str]] = {
    # ── Разговорные / общие ───────────────────────────────────────────────────
    "поп":          ["pop music radio mix 2024 playlist", "pop hits radio"],
    "pop":          ["pop music radio mix 2024 playlist", "pop hits radio"],
    "топ":          ["top hits radio mix 2024", "best hits radio"],
    # ── Рок ──────────────────────────────────────────────────────────────────
    "рок":          ["classic rock radio mix", "rock music radio 2024"],
    "rock":         ["classic rock radio mix", "rock music radio 2024"],
    "хардрок":      ["hard rock music radio mix", "hard rock radio"],
    "hard rock":    ["hard rock music radio mix", "hard rock radio"],
    "металл":       ["heavy metal radio mix", "metal music radio"],
    "метал":        ["heavy metal radio mix", "metal music radio"],
    "metal":        ["heavy metal radio mix", "metal music radio"],
    "панк":         ["punk rock radio mix", "punk music radio"],
    "punk":         ["punk rock radio mix", "punk music radio"],
    "альтернатива": ["alternative rock radio mix", "alternative music radio"],
    "alternative":  ["alternative rock radio mix", "alternative music radio"],
    "инди":         ["indie rock radio mix", "indie music radio"],
    "indie":        ["indie rock radio mix", "indie music radio"],
    # ── Джаз / блюз / соул ───────────────────────────────────────────────────
    "джаз":         ["jazz radio lounge mix", "smooth jazz music radio"],
    "jazz":         ["jazz radio lounge mix", "smooth jazz music radio"],
    "блюз":         ["blues music radio mix", "blues radio"],
    "blues":        ["blues music radio mix", "blues radio"],
    "соул":         ["soul music radio mix", "soul r&b radio"],
    "soul":         ["soul music radio mix", "soul r&b radio"],
    "rnb":          ["r&b soul radio mix", "rnb music radio 2024"],
    "р н б":        ["r&b soul radio mix", "rnb music radio 2024"],
    "фанк":         ["funk music radio mix", "funky radio"],
    "funk":         ["funk music radio mix", "funky radio"],
    # ── Электронная / танцевальная ────────────────────────────────────────────
    "электронная":  ["electronic music radio mix", "electronic radio"],
    "электро":      ["electronic music radio mix", "electro radio mix"],
    "electronic":   ["electronic music radio mix", "electronic radio"],
    "house":        ["house music radio mix", "house radio 2024"],
    "хаус":         ["house music radio mix", "house radio 2024"],
    "техно":        ["techno music radio mix", "techno radio"],
    "techno":       ["techno music radio mix", "techno radio"],
    "транс":        ["trance music radio mix", "trance radio"],
    "trance":       ["trance music radio mix", "trance radio"],
    "edm":          ["edm radio mix 2024", "electronic dance music radio"],
    "драм":         ["drum and bass radio mix", "dnb radio"],
    "dnb":          ["drum and bass radio mix", "dnb radio"],
    "дабстеп":      ["dubstep radio mix", "dubstep music radio"],
    "dubstep":      ["dubstep radio mix", "dubstep music radio"],
    "амбиент":      ["ambient music radio mix", "ambient chill radio"],
    "ambient":      ["ambient music radio mix", "ambient chill radio"],
    "чилл":         ["chill music radio mix", "chill out lounge radio"],
    "chill":        ["chill music radio mix", "chill out lounge radio"],
    "лаунж":        ["lounge music radio mix", "lounge bar music radio"],
    "lounge":       ["lounge music radio mix", "lounge bar music radio"],
    # ── Рэп / хип-хоп ────────────────────────────────────────────────────────
    "рэп":          ["rap hip hop radio mix", "rap music radio 2024"],
    "rap":          ["rap hip hop radio mix", "rap music radio 2024"],
    "хип-хоп":      ["hip hop radio mix", "hip hop music radio"],
    "хипхоп":       ["hip hop radio mix", "hip hop music radio"],
    "hip hop":      ["hip hop radio mix", "hip hop music radio"],
    "хип хоп":      ["hip hop radio mix", "hip hop music radio"],
    "трэп":         ["trap music radio mix", "trap radio 2024"],
    "trap":         ["trap music radio mix", "trap radio 2024"],
    # ── Классическая ─────────────────────────────────────────────────────────
    "классика":     ["classical music radio", "classical music radio 2024"],
    "классическая": ["classical music radio", "classical radio mix"],
    "classical":    ["classical music radio", "classical radio mix"],
    "опера":        ["opera music radio", "classical opera radio mix"],
    "opera":        ["opera music radio", "classical opera radio mix"],
    "оркестр":      ["orchestral music radio", "orchestra classical radio"],
    "пианино":      ["piano music radio mix", "relaxing piano radio"],
    "piano":        ["piano music radio mix", "relaxing piano radio"],
    # ── Народная / региональная ───────────────────────────────────────────────
    "фолк":         ["folk music radio mix", "folk radio"],
    "folk":         ["folk music radio mix", "folk radio"],
    "кантри":       ["country music radio mix", "country radio"],
    "country":      ["country music radio mix", "country radio"],
    "регги":        ["reggae music radio mix", "reggae radio"],
    "reggae":       ["reggae music radio mix", "reggae radio"],
    "латин":        ["latin music radio mix", "latin pop radio"],
    "latin":        ["latin music radio mix", "latin pop radio"],
    "латинская":    ["latin music radio mix", "latin radio"],
    "бразильская":  ["brazilian music radio mix", "bossa nova radio"],
    "русская":      ["russian pop music radio", "русская музыка радио"],
    "русский рок":  ["russian rock music radio", "русский рок радио"],
    # ── Настроение ───────────────────────────────────────────────────────────
    "релакс":       ["relaxing music radio mix", "relax music radio"],
    "relax":        ["relaxing music radio mix", "relax music radio"],
    "медитация":    ["meditation music radio", "meditation relax radio"],
    "meditation":   ["meditation music radio", "meditation relax radio"],
    "для сна":      ["sleep music radio mix", "relaxing sleep music radio"],
    "sleep":        ["sleep music radio mix", "relaxing sleep music radio"],
    "романтика":    ["romantic music radio mix", "romantic love songs radio"],
    "romantic":     ["romantic music radio mix", "romantic love songs radio"],
    "вечеринка":    ["party music radio mix", "party hits radio"],
    "party":        ["party music radio mix", "party hits radio"],
    "спорт":        ["workout music radio mix", "sport training music radio"],
    "workout":      ["workout music radio mix", "sport training music radio"],
    "фоновая":      ["background music radio mix", "focus study music radio"],
    "фон":          ["background music radio mix", "focus study music radio"],
    "учеба":        ["study music radio mix", "focus study lofi radio"],
    "lofi":         ["lofi hip hop radio mix", "lofi beats radio"],
    "лофи":         ["lofi hip hop radio mix", "lofi beats radio"],
    "фонк":         ["phonk music radio mix", "phonk drift radio", "dark phonk mix"],
    "phonk":        ["phonk music radio mix", "phonk drift radio", "dark phonk mix"],
    "dark phonk":   ["dark phonk music radio", "phonk radio mix"],
}

# Catch image search requests: "покажи кота", "найди фото моря", "картинку котика"
IMAGE_RE = re.compile(
    r'(?:'
    r'покажи(?:\s+мне)?'
    r'|покажите'
    r'|хочу\s+посмотреть'
    r'|хочу\s+увидеть'
    r'|пришли\s+(?:фото|картинк|изображени|фотк)'
    r'|отправь\s+(?:фото|картинк|изображени)'
    r'|кинь\s+(?:фото|картинк|изображени)'
    r'|найди\s+(?:фото|картинк|изображени|фотографи)'
    r'|дай\s+(?:фото|картинк|изображени)'
    r'|как\s+выглядит'
    r'|как\s+выглядят'
    r'|покажи\s+как\s+выглядит'
    r'|фото\s+\w'
    r'|картинк[уи]\s+\w'
    r'|картинки\s+\w'
    r'|изображени[ея]\s+\w'
    r'|фотк[уи]\s+\w'
    r'|фоточк[уи]\s+\w'
    r'|скинь\s+(?:фото|картинк|изображени)'
    r')',
    re.IGNORECASE,
)

# Trigger verbs that signal "send me something"
SEND_TRIGGER_RE = re.compile(
    r'\b(покажи(?:те)?|пришли|найди|дай|кинь|скинь|отправь|хочу\s+(?:увидеть|посмотреть))\b',
    re.IGNORECASE,
)

# Catch factual/informational queries that need real web search
INFO_RE = re.compile(
    r'(?:'
    # Classic info/knowledge questions
    r'что\s+такое\b|кто\s+такой\b|кто\s+такая\b'
    r'|расскажи\s+(?:про|о|об)\b'
    r'|информаци[яю]\s+(?:про|о|об)\b'
    r'|факты\s+(?:про|о|об)\b'
    r'|история\s+(?:создания|возникновения|развития|появления|про|о|об)\b'
    r'|как\s+работает\b|как\s+устроен\b'
    r'|из\s+чего\s+(?:состоит|сделан|делают)\b'
    r'|чем\s+знаменит\b|что\s+известно\s+о\b'
    r'|tell\s+me\s+about\b|what\s+is\b|who\s+is\b|history\s+of\b|facts\s+about\b|how\s+does\b'
    # Real-time / local queries
    r'|сеанс[ыа]?\b|расписани[ея]\b|афиш[аы]?\b'
    r'|кинотеатр\w*\b'
    r'|что\s+(?:идёт|показывают|сейчас\s+идёт)\b'
    r'|какой\s+фильм|какие\s+фильмы|какое\s+кино'
    r'|сколько\s+стоит\b|цена\s+(?:на|за)\b|стоимость\b'
    r'|режим\s+работы\b|часы\s+работы\b|график\s+работы\b'
    r'|где\s+(?:купить|найти|заказать|находится|расположен\w*)\b'
    r'|открыт\w*\s+ли\b|работает\s+ли\b'
    r'|погода\s+(?:сейчас|завтра|на\s+неделю)\b'
    r'|курс\s+(?:доллара|евро|рубля|валют)\b'
    r'|новости\s+(?:о|про|по|в)\b'
    r'|последние\s+новости\b'
    r'|что\s+(?:нового|случилось|произошло|происходит)\b'
    r'|когда\s+(?:открывается|закрывается|начинается|заканчивается)\b'
    r'|адрес\s+\w|телефон\s+\w'
    r'|(?:latest|current|today|now|schedule|showtimes?|cinema|movie\s+times?)\b'
    r')',
    re.IGNORECASE,
)

def classify_intent(text: str) -> dict:
    """Ask Groq to classify what kind of media the user wants.
    Returns {"intent": "images"|"video"|"music"|"info"|"chat", "query": "..."}
    """
    try:
        resp = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a strict intent classifier. Analyze the user message and return JSON.\n\n"
                        "Fields:\n"
                        "- intent: exactly one of 'images', 'video', 'music', 'info', 'chat'\n"
                        "- query: optimized search query in the SAME language as the user's message\n\n"
                        "Classification rules (apply in order):\n"
                        "1. Use 'images' when user wants: photos, pictures, diagrams, step-by-step photo "
                        "instructions, schemes, illustrations, how-to images, instruction pictures.\n"
                        "   Examples: 'пришли инструкцию в картинках', 'покажи фото', 'как это выглядит', "
                        "'схема подключения', 'пошаговые фото'.\n"
                        "2. Use 'video' when user wants: a video, tutorial video, video instructions, "
                        "how-to video, video guide, video lesson.\n"
                        "   Examples: 'пришли видео инструкцию', 'найди ролик', 'видеоурок', 'покажи видео'.\n"
                        "3. Use 'music' when user wants: a song, music track, soundtrack, OST from a "
                        "movie/game/show/series/anime, background music, theme song.\n"
                        "   Examples: 'музыка из игры', 'саундтрек фильма', 'пришли трек', "
                        "'песня из сериала', 'OST', 'найди музыку'.\n"
                        "   For music from movie/game/show: always include the media name. "
                        "If a specific song title is mentioned, use 'Artist - Song Title'. "
                        "If no specific song is mentioned, use 'Game/Movie Name song official audio'.\n"
                        "4. Use 'info' when user wants factual information, explanations, history, "
                        "biography, science, news, definitions, any knowledge search, OR real-time/local "
                        "information such as: cinema schedules, showtimes, event schedules, store hours, "
                        "prices, addresses, phone numbers, weather, currency rates, current news, "
                        "what's on at a specific place right now.\n"
                        "   Examples: 'найди информацию о', 'что такое', 'кто такой', 'расскажи о', "
                        "'как работает', 'история', 'факты о', 'tell me about', 'what is', 'who is', "
                        "'какие сеансы в кинотеатре', 'расписание', 'сколько стоит', 'режим работы', "
                        "'что сейчас показывают', 'showtimes', 'schedule', 'cinema', 'movie times'.\n"
                        "   For info: query should be a clear, specific search query in the user's language "
                        "optimized for finding this exact info (include city name, venue name, dates if mentioned).\n"
                        "5. Use 'chat' ONLY for: jokes, creative writing, personal opinions, small talk, "
                        "greetings, hypothetical discussions. If in doubt between 'info' and 'chat' — choose 'info'.\n\n"
                        "Query optimization rules:\n"
                        "- NEVER translate the query. Keep it in the EXACT same language as the user's message.\n"
                        "- For images: make it descriptive for image search, add equivalent of 'step by step' in user's language if instructional.\n"
                        "- For video: add words like 'урок', 'инструкция', 'tutorial' only matching user's language if instructional.\n"
                        "- For music from game (no specific song): use the game name as the user wrote it, plus word for 'music' in user's language. Example (Russian): 'NFS Underground 2 музыка'. Example (English): 'NFS Underground 2 music'. Never add 'full soundtrack', 'compilation', 'full OST'.\n"
                        "- For music from movie (no specific song): use the movie name as the user wrote it plus 'музыка' (Russian) or 'music' (English).\n"
                        "- For info: keep the query focused and specific to the topic, preserve original language.\n"
                        "- CRITICAL: The query field must ALWAYS be in the same language as the user's message. Russian input → Russian query. English input → English query.\n\n"
                        "Return ONLY valid JSON, no markdown, no explanation:\n"
                        "{\"intent\": \"images\", \"query\": \"замена кухонного смесителя пошагово\"}"
                    ),
                },
                {"role": "user", "content": text},
            ],
            max_tokens=150,
            temperature=0.0,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"^```[a-z]*\s*|\s*```$", "", raw, flags=re.S).strip()
        data = json.loads(raw)
        if data.get("intent") not in ("images", "video", "music", "info"):
            data["intent"] = "chat"
        data.setdefault("query", text)
        return data
    except Exception:
        return {"intent": "chat", "query": text}




def _fetch_page_text(url: str, max_chars: int = 4000) -> str:
    """Fetch a URL and return clean readable text (no HTML tags)."""
    try:
        import requests as _req
        from bs4 import BeautifulSoup as _BS
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        }
        resp = _req.get(url, headers=headers, timeout=8, allow_redirects=True)
        resp.encoding = resp.apparent_encoding
        soup = _BS(resp.text, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
            tag.decompose()
        text = " ".join(soup.get_text(" ", strip=True).split())
        return text[:max_chars]
    except Exception as e:
        print(f"[fetch_page error] {url}: {e}", flush=True)
        return ""


_SKIP_DOMAINS = {"youtube.com", "youtu.be", "instagram.com", "facebook.com",
                 "twitter.com", "t.me", "vk.com", "tiktok.com", "pdf"}


def _should_fetch(url: str) -> bool:
    return not any(d in url for d in _SKIP_DOMAINS) and not url.endswith(".pdf")


def search_web_info(text: str) -> tuple[str | None, str]:
    """
    Multi-source web search: DuckDuckGo snippets + full page content for top results.
    Returns (answer_text or None, image_search_query or "").
    """
    try:
        from duckduckgo_search import DDGS as _DDGS

        ddgs = _DDGS()

        # Run two searches: original query + region-aware variant
        results = list(ddgs.text(text, max_results=10))
        # Try a second pass if few results
        if len(results) < 4:
            try:
                results2 = list(ddgs.text(text, region="ru-ru", max_results=8))
                seen = {r.get("href") for r in results}
                results += [r for r in results2 if r.get("href") not in seen]
            except Exception:
                pass

        if not results:
            return None, ""

        # Build base snippets from DDG
        snippet_parts = []
        for r in results[:8]:
            snippet_parts.append(
                f"[{r.get('title', '')}]\n{r.get('body', '')}\nURL: {r.get('href', '')}"
            )
        base_snippets = "\n\n".join(snippet_parts)

        # Fetch full content from top relevant pages (up to 3)
        page_texts = []
        for r in results[:6]:
            url = r.get("href", "")
            if url and _should_fetch(url):
                page_content = _fetch_page_text(url, max_chars=3500)
                if page_content:
                    page_texts.append(f"=== Содержимое: {r.get('title','')} ({url}) ===\n{page_content}")
                if len(page_texts) >= 3:
                    break

        full_context = base_snippets
        if page_texts:
            full_context += "\n\n--- ПОЛНЫЙ ТЕКСТ СТРАНИЦ ---\n\n" + "\n\n".join(page_texts)

        resp = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a precise, helpful assistant with access to fresh web search results "
                        "and full page content fetched from the web.\n"
                        "Use ALL provided data (snippets + page contents) to compose a truthful answer.\n"
                        "For real-time queries (cinema schedules, showtimes, event schedules, store hours, "
                        "prices, addresses): carefully extract and list the actual data — "
                        "specific movie titles, showtime hours, ticket prices, dates. "
                        "If page content contains a schedule table or list, reproduce it clearly.\n"
                        "If data wasn't found, say so and give the most relevant official link to check.\n\n"
                        "Formatting (Telegram Markdown — * for bold, _ for italic only):\n"
                        "• Bold title at start: *Title*\n"
                        "• Bullet points (•) for schedules, times, prices\n"
                        "• Bold headers *Section* for groups\n"
                        "• Include source URL when relevant\n"
                        "• Reply in the SAME language as the user's question\n\n"
                        "At the very end write exactly:\n"
                        "IMAGE_QUERY: <краткий поисковый запрос для картинки на языке пользователя, или NONE>"
                    ),
                },
                {
                    "role": "user",
                    "content": f"Question: {text}\n\nData from web:\n{full_context}",
                },
            ],
            max_tokens=2500,
            temperature=0.1,
        )

        full = resp.choices[0].message.content.strip()

        image_query = ""
        img_match = re.search(r'^IMAGE_QUERY:\s*(.+)$', full, re.MULTILINE)
        if img_match:
            val = img_match.group(1).strip()
            image_query = "" if val.upper() == "NONE" else val
            full = full[:img_match.start()].strip()

        return full, image_query

    except Exception as e:
        print(f"[search_web_info error] {e}", flush=True)
        return None, ""




def _fetch_video_entries_dm(query: str, count: int = 10) -> list[dict]:
    """Search Dailymotion for video entries."""
    flat_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(flat_opts) as ydl:
            meta = ydl.extract_info(f"dmsearch{count}:{query}", download=False)
        entries = meta.get("entries", []) if meta else []
        result = []
        for e in entries:
            vid_id = e.get("id") or ""
            if not vid_id:
                continue
            url = e.get("url") or e.get("webpage_url") or f"https://www.dailymotion.com/video/{vid_id}"
            result.append({
                "id": vid_id,
                "url": url,
                "title": e.get("title") or query,
                "duration": e.get("duration"),
            })
        return result
    except Exception:
        return []


def _fetch_video_entries_vimeo(query: str, count: int = 10) -> list[dict]:
    """Search Vimeo for video entries."""
    flat_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(flat_opts) as ydl:
            meta = ydl.extract_info(f"vmsearch{count}:{query}", download=False)
        entries = meta.get("entries", []) if meta else []
        result = []
        for e in entries:
            vid_id = e.get("id") or ""
            if not vid_id:
                continue
            url = e.get("url") or e.get("webpage_url") or f"https://vimeo.com/{vid_id}"
            result.append({
                "id": vid_id,
                "url": url,
                "title": e.get("title") or query,
                "duration": e.get("duration"),
            })
        return result
    except Exception:
        return []


def _search_rutube(query: str, count: int = 10) -> list[dict]:
    """Search Rutube via its public search API and return video entries."""
    try:
        import urllib.parse as _up
        q = _up.quote(query)
        api_url = f"https://rutube.ru/api/search/video/?query={q}&page=1&format=json"
        req = urllib.request.Request(api_url, headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        results = data.get("results", [])
        entries = []
        for r in results[:count]:
            vid_id = r.get("id") or ""
            if not vid_id:
                continue
            if r.get("is_deleted") or r.get("is_hidden") or r.get("is_paid"):
                continue
            url = r.get("video_url") or f"https://rutube.ru/video/{vid_id}/"
            entries.append({
                "id": vid_id,
                "url": url,
                "title": r.get("title") or query,
                "duration": r.get("duration"),
            })
        return entries
    except Exception:
        return []


def _kp_find_film_id(query: str) -> tuple[str, str] | None:
    """
    Search Kinopoisk for the movie and return (film_id, title), or None.
    Tries the search page __NEXT_DATA__ JSON, then falls back to URL pattern matching.
    """
    import json as _json
    import urllib.parse as _up

    q_enc = _up.quote(query)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
    }

    # Method 1: Scrape the search results page
    try:
        url = f"https://www.kinopoisk.ru/search/?text={q_enc}&type=movie"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        # Look for __NEXT_DATA__ embedded JSON
        nd = re.search(r'id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>', html, re.DOTALL)
        if nd:
            try:
                data = _json.loads(nd.group(1))
                # Walk through common data paths
                for path in [
                    ["props", "pageProps", "searchResults", "films", "items"],
                    ["props", "pageProps", "searchData", "items"],
                    ["props", "pageProps", "films"],
                ]:
                    try:
                        node = data
                        for k in path:
                            node = node[k]
                        if node and isinstance(node, list):
                            f = node[0]
                            fid = str(f.get("id") or f.get("filmId") or f.get("kinopoiskId") or "")
                            title = (
                                (f.get("title") or {}).get("russian")
                                or (f.get("title") or {}).get("original")
                                or f.get("nameRu") or f.get("nameEn")
                                or query
                            )
                            if fid.isdigit():
                                return fid, title
                    except (KeyError, TypeError, IndexError):
                        continue
            except _json.JSONDecodeError:
                pass

        # Fallback: scan /film/ID/ href patterns from the HTML
        film_ids = re.findall(r'href=["\'](?:https://www\.kinopoisk\.ru)?/film/(\d{4,})[/"\'?]', html)
        if film_ids:
            return film_ids[0], query
    except Exception as e:
        print(f"[_kp_find_film_id] {e}", flush=True)

    # Method 2: Kinopoisk suggest API (Yandex CDN)
    try:
        sug_url = (
            f"https://suggest-kinopoisk.yandex.net/suggest-kinopoisk"
            f"?srv=kinopoisk&part={q_enc}&limit=5&lang=ru"
        )
        req2 = urllib.request.Request(sug_url, headers={
            "User-Agent": headers["User-Agent"],
            "Referer": "https://www.kinopoisk.ru/",
        })
        with urllib.request.urlopen(req2, timeout=8) as resp2:
            raw = resp2.read().decode("utf-8", errors="replace")
        data2 = _json.loads(raw)
        # Response: [query, [items...], [], [metadata...]] or {"data": [...]}
        items = None
        if isinstance(data2, list) and len(data2) > 1 and isinstance(data2[1], list):
            items = data2[1]
        elif isinstance(data2, dict):
            items = data2.get("data") or data2.get("items") or []
        if items:
            for item in items:
                if isinstance(item, dict):
                    fid = str(item.get("id") or item.get("entityId") or "")
                    typ = str(item.get("type") or item.get("entityType") or "").lower()
                    title = item.get("title") or item.get("name") or query
                    if fid.isdigit() and ("film" in typ or "movie" in typ or typ == ""):
                        return fid, title
    except Exception as e:
        print(f"[_kp_find_film_id suggest] {e}", flush=True)

    return None


def _download_video_url(url: str, title_fallback: str = "") -> tuple[bytes, str]:
    """Download a video from any yt-dlp supported URL. Returns (bytes, title)."""
    import subprocess
    for fmt in [
        "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]",
        "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]/best",
        "best[ext=mp4]/best",
    ]:
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                opts = _base_opts(tmpdir, {
                    "format": fmt,
                    "outtmpl": os.path.join(tmpdir, "video.%(ext)s"),
                    "noplaylist": True,
                    "merge_output_format": "mp4",
                })
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                filepath = _find_file(tmpdir, preferred_ext=".mp4")
                size = os.path.getsize(filepath)
                if size > MAX_FILE_SIZE:
                    small_path = os.path.join(tmpdir, "small.mp4")
                    r = subprocess.run(
                        ["ffmpeg", "-y", "-i", filepath,
                         "-vf", "scale=-2:480", "-c:v", "libx264",
                         "-crf", "28", "-preset", "fast",
                         "-c:a", "aac", "-b:a", "96k", small_path],
                        capture_output=True,
                    )
                    if r.returncode == 0 and os.path.exists(small_path):
                        new_size = os.path.getsize(small_path)
                        if new_size <= MAX_FILE_SIZE:
                            filepath = small_path
                        else:
                            continue
                    else:
                        continue
                title = (info or {}).get("title", title_fallback) if isinstance(info, dict) else title_fallback
                with open(filepath, "rb") as f:
                    return f.read(), title or title_fallback
        except Exception:
            continue
    raise RuntimeError(f"Не удалось скачать видео по ссылке: {url}")


# ── Persistent state for video "Next" button ───────────────────────────────
# Maps user_id → {"query": str, "platform": str, "entries": [...], "sent_ids": set}
video_search_state: dict[int, dict] = {}


def _next_unsent_entry(state: dict) -> dict | None:
    """Return the first entry from state['entries'] not in state['sent_ids'], or None."""
    sent = state.get("sent_ids", set())
    for entry in state.get("entries", []):
        if entry["id"] not in sent:
            return entry
    return None


def _download_raw(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


# ===== IMAGE SEARCH =====

import threading as _threading
_img_lock = _threading.Lock()
_img_last_time: float = 0.0


def _bing_images(query: str, max_results: int = 5, safe: bool = True) -> list[dict]:
    """Scrape Bing image search (no API key needed)."""
    import urllib.parse
    q = urllib.parse.quote(query)
    safesearch = "Off" if not safe else "Moderate"
    url = (
        f"https://www.bing.com/images/async?q={q}"
        f"&count={max_results * 6}&safeSearch={safesearch}"
        f"&FORM=HDRSC2"
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.bing.com/",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    urls = re.findall(r'murl&quot;:&quot;(https?://[^&"<]+?)&quot;', html)
    results = []
    seen = set()
    for img_url in urls:
        if img_url and img_url not in seen:
            seen.add(img_url)
            results.append({"url": img_url, "title": query})
        if len(results) >= max_results:
            break
    return results


def _ddg_images(query: str, max_results: int = 5, safe: bool = True) -> list[dict]:
    """DuckDuckGo image search fallback."""
    import time
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS
    safesearch = "moderate" if safe else "off"
    ddgs = DDGS(timeout=10)
    raw = list(ddgs.images(query, safesearch=safesearch, max_results=max_results * 4))
    results = []
    for r in raw:
        url = r.get("image") or ""
        if url and "duckduckgo.com/i.js" not in url:
            results.append({"url": url, "title": r.get("title", query)})
        if len(results) >= max_results:
            break
    return results


def search_images(query: str, max_results: int = 5, safe: bool = True) -> list[dict]:
    """Search images via Bing (primary) with DuckDuckGo fallback.
    Thread-safe with rate limiting."""
    import time
    global _img_last_time

    with _img_lock:
        elapsed = time.time() - _img_last_time
        if elapsed < 3.0:
            time.sleep(3.0 - elapsed)
        _img_last_time = time.time()

        # 1. Try Bing first
        for attempt in range(2):
            try:
                results = _bing_images(query, max_results, safe)
                if results:
                    return results
            except Exception:
                if attempt == 0:
                    time.sleep(2)

        # 2. Fallback to DuckDuckGo
        for attempt in range(2):
            try:
                results = _ddg_images(query, max_results, safe)
                if results:
                    return results
            except Exception:
                if attempt == 0:
                    time.sleep(3)

        return []


def download_image(url: str, timeout: int = 10) -> bytes:
    """Download image bytes from URL, reject if > 10 MB."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.google.com/",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(10 * 1024 * 1024 + 1)
    if len(data) > 10 * 1024 * 1024:
        raise ValueError("Image too large")
    return data


def extract_image_query(text: str) -> str:
    """Strip trigger words and return the clean search query."""
    text = text.strip()
    # Try specific multi-word patterns first (order matters — longest first)
    for pat in [
        r'покажи\s+как\s+выглядит\s+',
        r'покажи(?:те)?\s+мне\s+',
        r'покажи(?:те)?\s+',
        r'хочу\s+(?:увидеть|посмотреть)\s+(?:фото|картинк\w*|изображени\w*\s+)?',
        r'пришли\s+(?:мне\s+)?(?:фото|картинк\w+|фотк\w+|изображени\w+|фоточк\w+)\s+',
        r'отправь\s+(?:мне\s+)?(?:фото|картинк\w+|изображени\w+)\s+',
        r'кинь\s+(?:мне\s+)?(?:фото|картинк\w+|изображени\w+)\s+',
        r'скинь\s+(?:мне\s+)?(?:фото|картинк\w+|изображени\w+)\s+',
        r'дай\s+(?:мне\s+)?(?:фото|картинк\w+|изображени\w+)\s+',
        r'найди\s+(?:мне\s+)?(?:фото|картинк\w+|изображени\w+|фотографи\w+)\s+',
        r'как\s+выглядят?\s+',
        r'(?:фото|картинк[уи]|картинки|изображени[ея]|фотк[уи]|фоточк[уи])\s+',
        # Fallback: strip bare trigger verb even without photo word
        r'^\s*(?:пришли|найди|дай|кинь|скинь|отправь)\s+(?:мне\s+)?',
    ]:
        new_text = re.sub(pat, '', text, flags=re.IGNORECASE).strip()
        if new_text and new_text != text:
            text = new_text
            break
    return text


# ===== RADIO =====

# Direct internet-radio stream URLs (live, no download needed — ffmpeg records N seconds)
# Sources: SomaFM (free, reliable), RadioParadise, others
_SF = "https://ice1.somafm.com/"   # SomaFM base (verified 200)
_RP = "https://stream.radioparadise.com/"  # Radio Paradise base

RADIO_STREAMS: dict[str, list[tuple[str, str]]] = {
    # ── Verified working streams only ──────────────────────────────────────────
    # Jazz/Soul/Funk — SomaFM Sonic Universe (jazzandblues = 404, sonicuniverse = 200)
    "джаз":         [(_SF + "sonicuniverse-128-mp3",  "Sonic Universe Jazz · SomaFM")],
    "jazz":         [(_SF + "sonicuniverse-128-mp3",  "Sonic Universe Jazz · SomaFM")],
    "блюз":         [(_SF + "sonicuniverse-128-mp3",  "Sonic Universe Blues · SomaFM")],
    "blues":        [(_SF + "sonicuniverse-128-mp3",  "Sonic Universe Blues · SomaFM")],
    "соул":         [(_SF + "sonicuniverse-128-mp3",  "Sonic Universe Soul · SomaFM")],
    "soul":         [(_SF + "sonicuniverse-128-mp3",  "Sonic Universe Soul · SomaFM")],
    "фанк":         [(_SF + "sonicuniverse-128-mp3",  "Sonic Universe Funk · SomaFM")],
    "funk":         [(_SF + "sonicuniverse-128-mp3",  "Sonic Universe Funk · SomaFM")],
    # Metal
    "металл":       [(_SF + "metal-128-mp3",          "Metal Detector · SomaFM")],
    "metall":       [(_SF + "metal-128-mp3",          "Metal Detector · SomaFM")],
    "metal":        [(_SF + "metal-128-mp3",          "Metal Detector · SomaFM")],
    "хеви":         [(_SF + "metal-128-mp3",          "Metal Detector · SomaFM")],
    # Indie / Alternative
    "инди":         [(_SF + "indiepop-128-mp3",       "Indie Pop Rocks · SomaFM")],
    "indie":        [(_SF + "indiepop-128-mp3",       "Indie Pop Rocks · SomaFM")],
    "альтернатива": [(_SF + "indiepop-128-mp3",       "Indie Pop Rocks · SomaFM")],
    # 80s
    "80":           [(_SF + "u80s-128-mp3",           "Underground 80s · SomaFM")],
    "80s":          [(_SF + "u80s-128-mp3",           "Underground 80s · SomaFM")],
    "восьмидесятые":[(_SF + "u80s-128-mp3",           "Underground 80s · SomaFM")],
    # Folk / Country
    "фолк":         [(_SF + "folkfwd-128-mp3",        "Folk Forward · SomaFM")],
    "folk":         [(_SF + "folkfwd-128-mp3",        "Folk Forward · SomaFM")],
    "кантри":       [(_SF + "folkfwd-128-mp3",        "Folk Forward · SomaFM")],
    "country":      [(_SF + "folkfwd-128-mp3",        "Folk Forward · SomaFM")],
    # Lounge / Chill
    "лаунж":        [(_SF + "lush-128-mp3",           "Lush Lounge · SomaFM")],
    "lounge":       [(_SF + "lush-128-mp3",           "Lush Lounge · SomaFM")],
    "чилл":         [(_SF + "lush-128-mp3",           "Lush Lounge · SomaFM"),
                     (_SF + "groovesalad-128-mp3",    "Groove Salad · SomaFM")],
    "chill":        [(_SF + "lush-128-mp3",           "Lush Lounge · SomaFM")],
    "романтика":    [(_SF + "lush-128-mp3",           "Lush Lounge · SomaFM")],
    # Ambient / Relax / Sleep
    "амбиент":      [(_SF + "deepspaceone-128-mp3",   "Deep Space One · SomaFM"),
                     (_SF + "groovesalad-128-mp3",    "Groove Salad · SomaFM")],
    "ambient":      [(_SF + "deepspaceone-128-mp3",   "Deep Space One · SomaFM")],
    "релакс":       [(_SF + "deepspaceone-128-mp3",   "Deep Space One · SomaFM")],
    "relax":        [(_SF + "deepspaceone-128-mp3",   "Deep Space One · SomaFM")],
    "медитация":    [(_SF + "deepspaceone-128-mp3",   "Deep Space One · SomaFM")],
    "meditation":   [(_SF + "deepspaceone-128-mp3",   "Deep Space One · SomaFM")],
    "для сна":      [(_SF + "deepspaceone-128-mp3",   "Deep Space One · SomaFM")],
    "sleep":        [(_SF + "deepspaceone-128-mp3",   "Deep Space One · SomaFM")],
    "фоновая":      [(_SF + "groovesalad-128-mp3",    "Groove Salad · SomaFM")],
    "фон":          [(_SF + "groovesalad-128-mp3",    "Groove Salad · SomaFM")],
    # Electronic / Dance
    "электронная":  [(_SF + "groovesalad-128-mp3",    "Groove Salad · SomaFM"),
                     (_SF + "beatblender-128-mp3",    "Beat Blender · SomaFM")],
    "электро":      [(_SF + "beatblender-128-mp3",    "Beat Blender · SomaFM")],
    "electronic":   [(_SF + "groovesalad-128-mp3",    "Groove Salad · SomaFM")],
    "house":        [(_SF + "beatblender-128-mp3",    "Beat Blender · SomaFM")],
    "хаус":         [(_SF + "beatblender-128-mp3",    "Beat Blender · SomaFM")],
    "edm":          [(_SF + "beatblender-128-mp3",    "Beat Blender · SomaFM")],
    # Lofi
    "лофи":         [(_SF + "groovesalad-128-mp3",    "Groove Salad · SomaFM")],
    "lofi":         [(_SF + "groovesalad-128-mp3",    "Groove Salad · SomaFM")],
    # Rock — RadioParadise Rock (verified 200)
    "рок":          [(_RP + "rock-128",               "Rock · Radio Paradise")],
    "rock":         [(_RP + "rock-128",               "Rock · Radio Paradise")],
    "хардрок":      [(_RP + "rock-128",               "Rock · Radio Paradise")],
    "hard rock":    [(_RP + "rock-128",               "Rock · Radio Paradise")],
    # Pop / General — RadioParadise main (verified 200)
    "поп":          [(_RP + "mp3-128",                "Radio Paradise")],
    "pop":          [(_RP + "mp3-128",                "Radio Paradise")],
    "топ":          [(_RP + "mp3-128",                "Radio Paradise")],
    # Reggae
    "регги":        [(_SF + "reggae-128-mp3",         "Reggae · SomaFM")],
    "reggae":       [(_SF + "reggae-128-mp3",         "Reggae · SomaFM")],
    # Phonk / Trap / Rap / Hip-hop — no live stream → YouTube only (no entry here)
}


def _ffmpeg_record_stream(stream_url: str, out_path: str, duration_sec: int, timeout: int) -> None:
    """Record a radio stream (or any URL) for duration_sec seconds using ffmpeg subprocess."""
    import subprocess
    cmd = [
        "ffmpeg", "-y",
        "-i", stream_url,
        "-t", str(duration_sec),
        "-c:a", "libmp3lame", "-b:a", "128k",
        "-f", "mp3",
        out_path,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise TimeoutError(f"ffmpeg timed out after {timeout}s")
    if proc.returncode not in (0, 255):  # 255 = EOF on stream (normal)
        raise RuntimeError(f"ffmpeg exited with code {proc.returncode}")


def extract_radio_genre(text: str) -> str:
    """Strip trigger words and return the genre keyword."""
    cleaned = re.sub(
        r'\b(радио|radio|включи|поставь|запусти|найди|дай|хочу|слушать|послушать|'
        r'музыку|музыка|стиль|в\s+стиле|со?\s+стилем)\b',
        ' ', text, flags=re.IGNORECASE,
    )
    return cleaned.strip(" ,-–—.!?").lower()


def match_radio_genre(raw: str) -> tuple[str, list[str], list[tuple[str, str]]]:
    """Return (matched_key, yt_queries, stream_urls). Falls back to YouTube-only."""
    raw = raw.strip().lower()
    streams = RADIO_STREAMS.get(raw, [])
    yt_q = RADIO_QUERIES.get(raw, [])
    if streams or yt_q:
        return raw, yt_q, streams
    for key in RADIO_QUERIES:
        if key in raw or (len(raw) > 2 and raw in key):
            return key, RADIO_QUERIES[key], RADIO_STREAMS.get(key, [])
    fallback_q = f"{raw} music radio mix" if raw else "music radio mix"
    return raw, [fallback_q, f"{raw} radio"], []


def download_radio_audio(
    yt_queries: list[str],
    streams: list[tuple[str, str]],
    genre_label: str,
    target_sec: int = 1500,
) -> tuple[bytes, str]:
    """
    Try live radio streams (ffmpeg records N seconds).
    Returns (mp3_bytes, title).
    """
    import subprocess

    for stream_url, stream_title in streams:
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "radio.mp3")
            try:
                _ffmpeg_record_stream(stream_url, out, target_sec, timeout=target_sec + 30)
                if os.path.exists(out) and os.path.getsize(out) > 100_000:
                    with open(out, "rb") as f:
                        return f.read(), stream_title
            except Exception:
                continue

    raise RuntimeError(f"Не удалось найти поток для радио «{genre_label}»")


def _github_font_listing(slug: str) -> list | None:
    """Return list of file dicts from google/fonts GitHub repo for the given slug."""
    for lic in ("ofl", "apache", "ufl"):
        url = (
            f"https://api.github.com/repos/google/fonts/contents/{lic}/{slug}"
        )
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/vnd.github.v3+json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except Exception:
            pass
    return None


def _pick_font_file(
    ttf_files: list[tuple[str, str]], canonical: str | None, style: str, is_italic: bool
) -> tuple[str, str] | None:
    """Choose the best matching (name, download_url) from ttf_files list."""
    static = [(n, u) for n, u in ttf_files if "[" not in n]
    variable = [(n, u) for n, u in ttf_files if "[" in n]

    # Try exact static match first
    if static and canonical:
        for name, url in static:
            base = os.path.splitext(name)[0]
            if base.endswith(f"-{canonical}") or base.endswith(f"_{canonical}"):
                return name, url
        # Substring match
        for name, url in static:
            if canonical.lower() in os.path.splitext(name)[0].lower():
                return name, url

    # Style substring match on user input
    if static:
        style_clean = style.lower().replace(" ", "")
        for name, url in static:
            base = os.path.splitext(name)[0].lower().replace("-", "").replace("_", "")
            if style_clean in base:
                return name, url
        # First static as fallback
        return static[0]

    # Variable fonts fallback
    if variable:
        if is_italic:
            cands = [(n, u) for n, u in variable if "italic" in n.lower()]
        else:
            cands = [(n, u) for n, u in variable if "italic" not in n.lower()]
        return (cands or variable)[0]

    return None


def _pick_from_zip(zip_bytes: bytes, canonical: str | None, style: str, is_italic: bool) -> tuple[str, bytes] | None:
    """Extract the best matching TTF/OTF file from a ZIP archive."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            entries = [
                n for n in zf.namelist()
                if n.lower().endswith((".ttf", ".otf"))
                and not os.path.basename(n).startswith(".")
            ]
            if not entries:
                return None
            # Build list as (basename, fullpath)
            ttf_files = [(os.path.basename(n), n) for n in entries]

            static = [(b, p) for b, p in ttf_files if "[" not in b]
            variable = [(b, p) for b, p in ttf_files if "[" in b]

            def _read(path):
                return os.path.basename(path), zf.read(path)

            if static and canonical:
                for b, p in static:
                    base = os.path.splitext(b)[0]
                    if base.endswith(f"-{canonical}") or base.endswith(f"_{canonical}"):
                        return _read(p)
                for b, p in static:
                    if canonical.lower() in os.path.splitext(b)[0].lower():
                        return _read(p)

            if static:
                style_clean = style.lower().replace(" ", "")
                for b, p in static:
                    base = os.path.splitext(b)[0].lower().replace("-", "").replace("_", "")
                    if style_clean in base:
                        return _read(p)
                return _read(static[0][1])

            if variable:
                if is_italic:
                    cands = [(b, p) for b, p in variable if "italic" in b.lower()]
                else:
                    cands = [(b, p) for b, p in variable if "italic" not in b.lower()]
                return _read((cands or variable)[0][1])
    except Exception:
        pass
    return None


def _download_from_google_fonts(font_name: str, canonical: str | None, style: str, is_italic: bool) -> tuple[str, bytes] | None:
    """Try Google Fonts via GitHub repository."""
    slug_dash = font_name.lower().replace(" ", "-")
    slug_none = font_name.lower().replace(" ", "")

    listing = _github_font_listing(slug_dash) or _github_font_listing(slug_none)
    if listing is None:
        return None

    ttf_files = [
        (f["name"], f["download_url"])
        for f in listing
        if f["name"].lower().endswith((".ttf", ".otf"))
    ]

    # Check static/ subdirectory
    static_dirs = [f for f in listing if f["name"] == "static" and f["type"] == "dir"]
    if static_dirs:
        req = urllib.request.Request(
            static_dirs[0]["url"],
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/vnd.github.v3+json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                sfiles = json.loads(resp.read())
            static_ttfs = [
                (f["name"], f["download_url"])
                for f in sfiles
                if f["name"].lower().endswith((".ttf", ".otf"))
            ]
            if static_ttfs:
                ttf_files = static_ttfs
        except Exception:
            pass

    if not ttf_files:
        return None

    match = _pick_font_file(ttf_files, canonical, style, is_italic)
    if match is None:
        return None

    filename, dl_url = match
    return filename, _download_raw(dl_url)


def _download_from_dafont(font_name: str, canonical: str | None, style: str, is_italic: bool) -> tuple[str, bytes] | None:
    """Try DaFont as a fallback source."""
    slug = font_name.lower().replace(" ", "_")
    url = f"https://dl.dafont.com/dl/?f={slug}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": f"https://www.dafont.com/{font_name.lower().replace(' ', '-')}.font",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            zip_bytes = resp.read()
        if len(zip_bytes) < 5000:
            return None
        return _pick_from_zip(zip_bytes, canonical, style, is_italic)
    except Exception:
        return None


def _download_from_ofont(font_name: str, canonical: str | None, style: str, is_italic: bool) -> tuple[str, bytes] | None:
    """Try ofont.ru — large Russian font database with direct TTF downloads."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    search_url = f"https://ofont.ru/search/?q={urllib.parse.quote(font_name)}"
    try:
        req = urllib.request.Request(search_url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return None

    view_ids = re.findall(r'href=["\'](/view/(\d+))["\']', html)
    if not view_ids:
        return None

    best_id: str | None = None
    best_score = -1
    best_style_in_title = ""

    style_lc = (canonical or style).lower()
    font_name_lc = font_name.lower()

    for _, font_id in view_ids[:10]:
        try:
            vurl = f"https://ofont.ru/view/{font_id}"
            req = urllib.request.Request(vurl, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                vhtml = resp.read().decode("utf-8", errors="ignore")
        except Exception:
            continue

        title_m = re.search(r"<title>(.*?)</title>", vhtml)
        if not title_m:
            continue
        raw_title = title_m.group(1)
        # "Шрифт Proxima Nova Bold - скачать на oFont.ru"
        title_clean = re.sub(r"шрифт\s*", "", raw_title, flags=re.IGNORECASE)
        title_clean = re.sub(r"\s*-\s*скачать.*", "", title_clean, flags=re.IGNORECASE).strip()
        # Extract style part: remove the font name prefix
        style_in_title = title_clean
        for word in font_name.split():
            style_in_title = re.sub(re.escape(word), "", style_in_title, flags=re.IGNORECASE)
        style_in_title = style_in_title.strip().lower()

        score = 0
        if style_lc and style_lc in style_in_title:
            score += 10
        if not style_in_title or style_in_title in ("regular", "обычный", "кириллица", ""):
            if style_lc in ("regular", "обычный", "normal", ""):
                score += 8
            else:
                score += 1
        if is_italic and ("italic" in style_in_title or "курсив" in style_in_title):
            score += 5
        if not is_italic and "italic" not in style_in_title:
            score += 2

        if score > best_score:
            best_score = score
            best_id = font_id
            best_style_in_title = style_in_title

    if best_id is None:
        return None

    dl_url = f"https://ofont.ru/index.php?act=download&font_id={best_id}"
    try:
        req = urllib.request.Request(dl_url, headers={
            **headers,
            "Referer": f"https://ofont.ru/view/{best_id}",
        })
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = resp.read()
    except Exception:
        return None

    if len(data) < 1000:
        return None

    safe_name = font_name.replace(" ", "")
    style_suffix = best_style_in_title.title().replace(" ", "") if best_style_in_title else "Regular"
    fname = f"{safe_name}-{style_suffix}.ttf"

    if data[:4] in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
        return fname, data
    if data[:2] == b"PK":
        return _pick_from_zip(data, canonical, style, is_italic)
    return None


def download_font(font_name: str, style: str) -> tuple[str, bytes]:
    canonical = STYLE_ALIASES.get(style.lower().strip()) or STYLE_ALIASES.get(
        style.lower().replace(" ", "")
    )
    is_italic = "italic" in style.lower() or "курсив" in style.lower()

    result = (
        _download_from_google_fonts(font_name, canonical, style, is_italic)
        or _download_from_dafont(font_name, canonical, style, is_italic)
        or _download_from_ofont(font_name, canonical, style, is_italic)
    )

    if result is None:
        raise ValueError(
            f"Шрифт «{font_name}» не найден в бесплатных источниках.\n\n"
            "Возможные причины:\n"
            "• Опечатка в названии — используй английское написание\n"
            "• Шрифт коммерческий и недоступен бесплатно\n\n"
            "Бесплатные аналоги популярных шрифтов:\n"
            "• Helvetica → Inter или Roboto\n"
            "• Futura → Jost или Urbanist\n"
            "• Gotham → Raleway или Outfit"
        )

    return result


def _get_all_google_fonts_files(font_name: str) -> list[tuple[str, str]] | None:
    """Return all TTF/OTF files from Google Fonts GitHub for the given font."""
    slug_dash = font_name.lower().replace(" ", "-")
    slug_none = font_name.lower().replace(" ", "")

    listing = _github_font_listing(slug_dash) or _github_font_listing(slug_none)
    if listing is None:
        return None

    ttf_files = [
        (f["name"], f["download_url"])
        for f in listing
        if f["name"].lower().endswith((".ttf", ".otf"))
    ]

    static_dirs = [f for f in listing if f["name"] == "static" and f["type"] == "dir"]
    if static_dirs:
        req = urllib.request.Request(
            static_dirs[0]["url"],
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/vnd.github.v3+json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                sfiles = json.loads(resp.read())
            static_ttfs = [
                (f["name"], f["download_url"])
                for f in sfiles
                if f["name"].lower().endswith((".ttf", ".otf"))
            ]
            if static_ttfs:
                ttf_files = static_ttfs
        except Exception:
            pass

    return ttf_files if ttf_files else None


def _download_all_from_ofont(font_name: str) -> list[tuple[str, bytes]] | None:
    """Download all matching font styles from ofont.ru."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    search_url = f"https://ofont.ru/search/?q={urllib.parse.quote(font_name)}"
    try:
        req = urllib.request.Request(search_url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return None

    view_ids = re.findall(r'href=["\'](/view/(\d+))["\']', html)
    if not view_ids:
        return None

    font_words = [w for w in font_name.split() if len(w) > 2]
    results = []
    seen_files: set[str] = set()

    for _, font_id in view_ids[:20]:
        try:
            vurl = f"https://ofont.ru/view/{font_id}"
            req = urllib.request.Request(vurl, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                vhtml = resp.read().decode("utf-8", errors="ignore")
        except Exception:
            continue

        title_m = re.search(r"<title>(.*?)</title>", vhtml)
        if not title_m:
            continue
        raw_title = title_m.group(1)
        title_clean = re.sub(r"шрифт\s*", "", raw_title, flags=re.IGNORECASE)
        title_clean = re.sub(r"\s*-\s*скачать.*", "", title_clean, flags=re.IGNORECASE).strip()

        if not any(w.lower() in title_clean.lower() for w in font_words):
            continue

        dl_url = f"https://ofont.ru/index.php?act=download&font_id={font_id}"
        try:
            req = urllib.request.Request(dl_url, headers={
                **headers,
                "Referer": f"https://ofont.ru/view/{font_id}",
            })
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = resp.read()
        except Exception:
            continue

        if len(data) < 1000:
            continue

        style_in_title = title_clean
        for word in font_name.split():
            style_in_title = re.sub(re.escape(word), "", style_in_title, flags=re.IGNORECASE)
        style_in_title = style_in_title.strip().title().replace(" ", "") or "Regular"

        safe_name = font_name.replace(" ", "")

        if data[:4] in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
            fname = f"{safe_name}-{style_in_title}.ttf"
            if fname not in seen_files:
                seen_files.add(fname)
                results.append((fname, data))
        elif data[:2] == b"PK":
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    for entry in zf.namelist():
                        if entry.lower().endswith((".ttf", ".otf")):
                            bname = os.path.basename(entry)
                            if bname not in seen_files:
                                seen_files.add(bname)
                                results.append((bname, zf.read(entry)))
            except Exception:
                pass

    return results if results else None


def download_all_fonts(font_name: str) -> tuple[str, bytes]:
    """Download all font styles and return as a ZIP archive."""
    safe = font_name.replace(" ", "")

    # 1. Try Google Fonts (GitHub)
    files = _get_all_google_fonts_files(font_name)
    if files:
        buf = io.BytesIO()
        downloaded = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for filename, dl_url in files:
                try:
                    data = _download_raw(dl_url)
                    if data:
                        zf.writestr(filename, data)
                        downloaded += 1
                except Exception:
                    pass
        if downloaded > 0:
            buf.seek(0)
            return f"{safe}_all.zip", buf.read()

    # 2. Try DaFont (returns a ZIP with all styles)
    slug = font_name.lower().replace(" ", "_")
    url = f"https://dl.dafont.com/dl/?f={slug}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": f"https://www.dafont.com/{font_name.lower().replace(' ', '-')}.font",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            zip_bytes = resp.read()
        if len(zip_bytes) > 5000 and zip_bytes[:2] == b"PK":
            return f"{safe}_all.zip", zip_bytes
    except Exception:
        pass

    # 3. Try ofont.ru (supports commercial fonts)
    ofont_files = _download_all_from_ofont(font_name)
    if ofont_files:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for filename, data in ofont_files:
                zf.writestr(filename, data)
        buf.seek(0)
        return f"{safe}_all.zip", buf.read()

    raise ValueError(
        f"Не удалось скачать все начертания шрифта «{font_name}».\n"
        "Возможно, шрифт коммерческий и недоступен бесплатно.\n"
        "Попробуй скачать конкретное начертание."
    )


# ===== PHOTO UPSCALE x3 =====

UPSCALE_RE = re.compile(r'улучши\s*фото', re.IGNORECASE)
FIND_IMG_RE = re.compile(
    r'\b(найди|найти|поиск|откуда|источник|оригинал|найди\s*оригинал|найди\s*фото|найди\s*картинку)\b',
    re.IGNORECASE
)

_MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".models")
_ESPCN_PATH = os.path.join(_MODEL_DIR, "ESPCN_x3.pb")
_ESPCN_URL = "https://github.com/fannymonori/TF-ESPCN/raw/master/export/ESPCN_x3.pb"

_sr_instance = None


def _get_sr():
    global _sr_instance
    if _sr_instance is not None:
        return _sr_instance
    import cv2
    os.makedirs(_MODEL_DIR, exist_ok=True)
    if not os.path.exists(_ESPCN_PATH):
        urllib.request.urlretrieve(_ESPCN_URL, _ESPCN_PATH)
    sr = cv2.dnn_superres.DnnSuperResImpl_create()
    sr.readModel(_ESPCN_PATH)
    sr.setModel("espcn", 3)
    _sr_instance = sr
    return sr


def upscale_image_x4(image_bytes: bytes) -> tuple[bytes, tuple[int, int], tuple[int, int]]:
    """
    Upscale image 3x using ESPCN neural network super-resolution via OpenCV DNN.
    Falls back to LANCZOS if model unavailable.
    Returns (png_bytes, original_size, new_size).
    """
    import cv2
    import numpy as np
    from PIL import ImageEnhance

    img_pil = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    orig_size = img_pil.size
    new_w = orig_size[0] * 3
    new_h = orig_size[1] * 3

    try:
        sr = _get_sr()
        img_np = np.array(img_pil)
        img_bgr = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
        upscaled_bgr = sr.upsample(img_bgr)
        upscaled_rgb = cv2.cvtColor(upscaled_bgr, cv2.COLOR_BGR2RGB)
        upscaled_pil = Image.fromarray(upscaled_rgb)
        upscaled_pil = ImageEnhance.Sharpness(upscaled_pil).enhance(1.3)
    except Exception:
        from PIL import ImageFilter
        upscaled_pil = img_pil.resize((new_w, new_h), Image.LANCZOS)
        upscaled_pil = ImageEnhance.Sharpness(upscaled_pil).enhance(1.5)
        upscaled_pil = upscaled_pil.filter(ImageFilter.UnsharpMask(radius=1.5, percent=120, threshold=3))

    buf = io.BytesIO()
    upscaled_pil.save(buf, format="PNG", optimize=False, compress_level=1)
    buf.seek(0)
    actual_size = upscaled_pil.size
    return buf.read(), orig_size, actual_size


# ===== TEXT PERCENTAGE ANALYSIS =====

TEXT_PCT_RE = re.compile(r'процент\s*текста', re.IGNORECASE)


def analyze_text_percentage(image_bytes: bytes) -> str:
    """
    Use pytesseract (local OCR) to estimate the percentage of image area
    covered by text (bounding boxes of recognized words).
    """
    import pytesseract

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img_w, img_h = img.size
    total_area = img_w * img_h

    data = pytesseract.image_to_data(
        img,
        lang="rus+eng",
        output_type=pytesseract.Output.DICT,
    )

    text_area = 0
    words_found = []
    n = len(data["text"])
    for i in range(n):
        conf = int(data["conf"][i])
        word = str(data["text"][i]).strip()
        if conf > 0 and word:
            w = int(data["width"][i])
            h = int(data["height"][i])
            if w > 0 and h > 0:
                text_area += w * h
                words_found.append(word)

    pct = min(round(text_area / total_area * 100, 1), 100.0) if total_area > 0 else 0.0

    sample = " ".join(words_found[:12])
    if len(words_found) > 12:
        sample += "…"

    if pct == 0:
        desc = "Текст на изображении не обнаружен."
    elif pct < 5:
        desc = f"Текст занимает очень мало места. Найдено слов: {len(words_found)}."
    else:
        desc = f"Найдено слов: {len(words_found)}. Фрагмент: «{sample}»"

    return f"Процент текста: {pct}%\nОписание: {desc}"


# ===== IMAGE COMPRESSION =====

def parse_target_size(text: str) -> int | None:
    """Parse size like '500кб', '1.5мб', '200kb', '2mb' → bytes."""
    m = re.search(r'(\d+(?:[.,]\d+)?)\s*(мб|mb|кб|kb|к\b|k\b|м\b|m\b)', text.lower())
    if not m:
        return None
    value = float(m.group(1).replace(',', '.'))
    unit = m.group(2)
    if unit in ('мб', 'mb', 'м', 'm'):
        return int(value * 1024 * 1024)
    return int(value * 1024)


def compress_image_to_size(image_bytes: bytes, target_bytes: int) -> bytes:
    """Compress JPEG to fit within target_bytes via binary search on quality."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    low, high, best = 1, 95, None
    for _ in range(12):
        mid = (low + high) // 2
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=mid, optimize=True)
        size = buf.tell()
        if size <= target_bytes:
            best = buf.getvalue()
            low = mid + 1
        else:
            high = mid - 1
        if low > high:
            break
    if best is None:
        # Even quality=1 is too large — shrink resolution
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=1, optimize=True)
        ratio = (target_bytes / buf.tell()) ** 0.5
        new_w = max(1, int(img.width * ratio))
        new_h = max(1, int(img.height * ratio))
        img = img.resize((new_w, new_h), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=1, optimize=True)
        best = buf.getvalue()
    return best


# Buffer for album (media group) photos
_album_buffer: dict = {}


async def _process_album(media_group_id: str, context):
    """Wait for all photos in an album to arrive, then compress & zip."""
    await asyncio.sleep(2)
    grp = _album_buffer.pop(media_group_id, None)
    if not grp:
        return

    photos = grp["photos"]
    target_bytes = grp["target_bytes"]
    chat_id = grp["chat_id"]
    reply_to = grp["reply_to"]

    if not target_bytes:
        await context.bot.send_message(
            chat_id=chat_id,
            reply_to_message_id=reply_to,
            text=(
                f"📦 Получил {len(photos)} фото!\n"
                "Укажи целевой размер каждого — например:\n"
                "/compress 500кб\n\n"
                "Или перешли фото снова с подписью, например: «сожми до 300кб»"
            ),
        )
        return

    status = await context.bot.send_message(
        chat_id=chat_id,
        reply_to_message_id=reply_to,
        text=f"🗜 Сжимаю {len(photos)} фото...",
    )
    try:
        loop = asyncio.get_running_loop()
        zip_buf = io.BytesIO()
        actual_sizes = []
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_STORED) as zf:
            for i, photo in enumerate(photos, 1):
                file = await context.bot.get_file(photo.file_id)
                dl = io.BytesIO()
                await file.download_to_memory(dl)
                result = await loop.run_in_executor(
                    None, compress_image_to_size, dl.getvalue(), target_bytes
                )
                actual_sizes.append(len(result))
                zf.writestr(f"photo_{i:02d}.jpg", result)
        zip_buf.seek(0)

        avg_kb = sum(actual_sizes) / len(actual_sizes) / 1024
        await context.bot.send_document(
            chat_id=chat_id,
            document=zip_buf,
            filename="compressed.zip",
            caption=(
                f"✅ {len(photos)} фото сжато\n"
                f"Средний размер: {avg_kb:.1f} КБ"
            ),
        )
        await context.bot.delete_message(chat_id=chat_id, message_id=status.message_id)
    except Exception as e:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status.message_id,
            text=f"⚠ Ошибка: {e}",
        )


async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle videos sent by user — inform them it's not supported."""
    await update.message.reply_text(
        "📥 Для скачивания видео — отправь ссылку на VK.\n"
        "Например: https://vk.com/video..."
    )


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user_id = message.from_user.id
    photo = message.photo[-1]
    caption = (message.caption or "").strip()
    target_bytes = parse_target_size(caption)
    media_group_id = message.media_group_id

    if UPSCALE_RE.search(caption):
        msg = await message.reply_text("🔬 Увеличиваю фото в 3x...")
        try:
            file = await context.bot.get_file(photo.file_id)
            dl = io.BytesIO()
            await file.download_to_memory(dl)
            loop = asyncio.get_running_loop()
            result_bytes, orig, new_sz = await loop.run_in_executor(
                None, upscale_image_x4, dl.getvalue()
            )
            size_mb = len(result_bytes) / (1024 * 1024)
            filename = "upscaled_3x.png"
            fmt = "PNG"
            if size_mb > 45:
                from PIL import Image as _Image, ImageEnhance as _IE, ImageFilter as _IF
                img_big = _Image.open(io.BytesIO(result_bytes))
                buf2 = io.BytesIO()
                img_big.convert("RGB").save(buf2, format="JPEG", quality=95, optimize=True)
                result_bytes = buf2.getvalue()
                size_mb = len(result_bytes) / (1024 * 1024)
                filename = "upscaled_3x.jpg"
                fmt = "JPEG"
            await message.reply_document(
                document=io.BytesIO(result_bytes),
                filename=filename,
                caption=(
                    f"✅ Готово! Увеличено в 3x ({fmt})\n"
                    f"📐 {orig[0]}×{orig[1]} → {new_sz[0]}×{new_sz[1]}\n"
                    f"💾 Размер файла: {size_mb:.1f} МБ"
                ),
            )
            await msg.delete()
        except Exception as e:
            await msg.edit_text(f"⚠ Ошибка: {e}")
        return

    if TEXT_PCT_RE.search(caption):
        msg = await message.reply_text("🔍 Анализирую текст на фото...")
        try:
            file = await context.bot.get_file(photo.file_id)
            dl = io.BytesIO()
            await file.download_to_memory(dl)
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, analyze_text_percentage, dl.getvalue())
            await message.reply_text(f"📊 {result}")
            await msg.delete()
        except Exception as e:
            await msg.edit_text(f"⚠ Ошибка: {e}")
        return

    if media_group_id:
        # Part of an album — buffer and schedule processing
        if media_group_id not in _album_buffer:
            _album_buffer[media_group_id] = {
                "photos": [],
                "target_bytes": target_bytes,
                "chat_id": message.chat_id,
                "reply_to": message.message_id,
                "task": None,
            }
        grp = _album_buffer[media_group_id]
        grp["photos"].append(photo)
        if target_bytes:
            grp["target_bytes"] = target_bytes

        if grp["task"]:
            grp["task"].cancel()
        grp["task"] = asyncio.create_task(_process_album(media_group_id, context))

    elif target_bytes:
        # Single photo + size → compress and send as zip
        msg = await message.reply_text("🗜 Сжимаю фото...")
        try:
            file = await context.bot.get_file(photo.file_id)
            dl = io.BytesIO()
            await file.download_to_memory(dl)
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, compress_image_to_size, dl.getvalue(), target_bytes
            )
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_STORED) as zf:
                zf.writestr("photo_01.jpg", result)
            zip_buf.seek(0)
            await message.reply_document(
                document=zip_buf,
                filename="compressed.zip",
                caption=f"✅ Готово. Размер: {len(result) / 1024:.1f} КБ",
            )
            await msg.delete()
        except Exception as e:
            await msg.edit_text(f"⚠ Ошибка: {e}")

    else:
        await message.reply_text(
            "📷 Фото получено!\n\n"
            "Что я умею с фото:\n"
            "• Подпиши «улучши фото» — увеличу в 3x\n"
            "• Подпиши «до 500кб» (любой размер) — сожму\n"
            "• Подпиши «процент текста» — определю долю текста"
        )


# ===== VIDEO DOWNLOAD =====

def _find_file(tmpdir, preferred_ext=None):
    """Return path to the best matching file in tmpdir, preferring preferred_ext."""
    files = [
        os.path.join(tmpdir, name)
        for name in os.listdir(tmpdir)
        if not name.startswith(".")
    ]
    if not files:
        raise FileNotFoundError("yt-dlp did not produce any file")
    if preferred_ext:
        preferred = [f for f in files if f.lower().endswith(preferred_ext)]
        if preferred:
            return preferred[0]
    return files[0]


def _base_opts(tmpdir, extra=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    if extra:
        opts.update(extra)
    return opts


def download_video(url):
    with tempfile.TemporaryDirectory() as tmpdir:
        opts = _base_opts(tmpdir, {
            "format": "best[ext=mp4][filesize<50M]/best[ext=mp4]/best[filesize<50M]/best",
            "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
        })
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filepath = ydl.prepare_filename(info)
                if not os.path.exists(filepath):
                    filepath = _find_file(tmpdir)
                size = os.path.getsize(filepath)
                if size > MAX_FILE_SIZE:
                    raise ValueError(
                        f"Видео слишком большое ({size // (1024 * 1024)} МБ). Лимит Telegram — 50 МБ."
                    )
                with open(filepath, "rb") as f:
                    return f.read(), info.get("title", "Video")
        except yt_dlp.utils.DownloadError as e:
            raise RuntimeError(str(e)[:300]) from e


# ===== MUSIC DOWNLOAD =====

def _audio_opts(tmpdir, extra=None):
    opts = _base_opts(tmpdir, {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(tmpdir, "%(title)s.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    })
    if extra:
        opts.update(extra)
    return opts


def _read_audio_result(tmpdir, info, fallback_title: str):
    import subprocess
    if isinstance(info, dict) and "entries" in info:
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise RuntimeError(
                f"Ничего не нашёл по запросу «{fallback_title}».\n"
                "Попробуй написать точнее: исполнитель + название трека."
            )
        info = entries[0]
    filepath = _find_file(tmpdir, preferred_ext=".mp3")
    size = os.path.getsize(filepath)
    # If file is too large, re-encode at lower bitrate to fit within limit
    if size > MAX_FILE_SIZE:
        low_path = filepath + "_low.mp3"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", filepath, "-b:a", "96k", low_path],
                capture_output=True, check=True,
            )
            low_size = os.path.getsize(low_path)
            if low_size <= MAX_FILE_SIZE:
                filepath = low_path
                size = low_size
            else:
                raise ValueError(f"Файл слишком большой ({size // (1024 * 1024)} МБ). Лимит — 50 МБ.")
        except subprocess.CalledProcessError:
            raise ValueError(f"Файл слишком большой ({size // (1024 * 1024)} МБ). Лимит — 50 МБ.")
    with open(filepath, "rb") as f:
        data = f.read()
    title = info.get("title", fallback_title) if isinstance(info, dict) else fallback_title
    artist = info.get("uploader", "") if isinstance(info, dict) else ""
    return data, title, artist


def _best_match_score(title: str, query: str) -> float:
    """
    Score how well a result title matches the query.
    Higher = better match. Used to pick the best result from multi-result searches.
    """
    title_l = title.lower()
    query_l = query.lower()
    query_words = set(query_l.split())
    title_words = set(title_l.split())
    # Word overlap ratio
    overlap = len(query_words & title_words) / max(len(query_words), 1)
    # Bonus if query is a substring of title or vice versa
    exact_bonus = 0.3 if query_l in title_l else (0.1 if title_l in query_l else 0)
    return overlap + exact_bonus


def _sc_search_best(query: str, tmpdir: str, count: int = 5):
    """
    Search SoundCloud for `count` results, pick the best matching one, and download it.
    Returns (info_dict, downloaded_filepath_prefix) or raises.
    """
    flat_opts = {"quiet": True, "no_warnings": True, "extract_flat": True}
    with yt_dlp.YoutubeDL(flat_opts) as ydl:
        flat_info = ydl.extract_info(f"scsearch{count}:{query}", download=False)
    entries = [e for e in (flat_info or {}).get("entries", []) if e]
    if not entries:
        raise RuntimeError("Нет результатов на SoundCloud")
    # Score and sort — pick best match
    scored = sorted(entries, key=lambda e: _best_match_score(e.get("title", ""), query), reverse=True)
    best = scored[0]
    url = best.get("url") or best.get("webpage_url", "")
    if not url:
        raise RuntimeError("Нет URL у лучшего результата")
    # Download the chosen track
    with yt_dlp.YoutubeDL(_audio_opts(tmpdir)) as ydl:
        info = ydl.extract_info(url, download=True)
    return info


def download_music(query: str):
    """Search and download audio from multiple sources: SoundCloud (smart pick) → SoundCloud (1st) → Rutube."""
    last_err = None

    # 1. SoundCloud — fetch 5 results, pick best match by title similarity
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            info = _sc_search_best(query, tmpdir, count=5)
            return _read_audio_result(tmpdir, info, query)
        except Exception as e:
            last_err = e

    # 2. Rutube music search — good coverage of Russian tracks
    for q in [query, f"{query} official audio", f"{query} аудио"]:
        entries = _search_rutube(q, count=5)
        for e in entries:
            # Only try entries that look like audio/music (duration < 15 min)
            dur = e.get("duration") or 0
            if dur and dur > 900:
                continue
            with tempfile.TemporaryDirectory() as tmpdir:
                try:
                    opts = _audio_opts(tmpdir)
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(e["url"], download=True)
                    return _read_audio_result(tmpdir, info, query)
                except Exception as ex:
                    last_err = ex
                    continue

    raise RuntimeError(
        f"Не удалось найти «{query}».\n"
        f"Попробуй другой запрос или отправь прямую ссылку на трек."
    )


def download_audio_url(url: str):
    """Download audio from a direct URL (VK, SoundCloud, Deezer, YouTube, etc.)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        opts = _audio_opts(tmpdir)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
            return _read_audio_result(tmpdir, info, url)
        except yt_dlp.utils.DownloadError as e:
            raise RuntimeError(str(e)[:400]) from e




async def music_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text(
            "🎵 Напиши запрос после команды.\n"
            "Пример: /music The Beatles - Hey Jude"
        )
        return

    msg = await update.message.reply_text(f"🔍 Ищу: {query}...")

    try:
        loop = asyncio.get_running_loop()
        audio_bytes, title, artist = await loop.run_in_executor(
            None, download_music, query
        )
        await msg.edit_text("📤 Загружаю файл...")
        await update.message.reply_audio(
            audio=io.BytesIO(audio_bytes),
            filename=f"{title}.mp3",
            title=title,
            performer=artist,
        )
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"⚠ Ошибка: {e}")


# ===== COMMANDS =====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    await update.message.reply_text(
        "🤖 *AI Бот* — пиши что угодно, отвечаю как живой человек.\n"
        "\n"
        "\n"
        "💬 *Общение и поиск в интернете*\n"
        "• *Любой вопрос* — отвечу развёрнуто\n"
        "• *«Что такое квантовая механика?»* — найду актуальную информацию с картинками\n"
        "• *«Какие сеансы в кинотеатре Россия в Саранске?»* — найду расписание онлайн\n"
        "• *«Какой курс доллара?»* или *«последние новости»* — актуальные данные из сети\n"
        "\n"
        "\n"
        "🎵 *Музыка и аудио*\n"
        "• *«Пришли музыку из фильма Интерстеллар»* — найду и скачаю саундтрек\n"
        "• *«музыка Imagine Dragons Believer»* — скачаю трек\n"
        "• Ссылка *VK / SoundCloud / Deezer* — скачаю аудио\n"
        "\n"
        "\n"
        "🖼 *Работа с фото*\n"
        "• Фото-файл + подпись *«найди»* или *«оригинал»* — найду оригинал в интернете (до 50МБ)\n"
        "• Фото + подпись *«улучши фото»* — увеличу в 3x в высоком качестве\n"
        "• Фото + подпись *«до 500кб»* — сожму до нужного размера\n"
        "• Несколько фото + *«до 300кб»* — сожму каждое и упакую в архив\n"
        "• Фото + подпись *«процент текста»* — покажу сколько % занимает текст\n"
        "• Архив *.zip* с картинками — переименую файлы по размеру (напр. 1920x1080.jpg)\n"
        "• Архив *.zip* + подпись *«собери гиф»* — соберу GIF из групп картинок\n"
        "\n"
        "\n"
        "🔤 *Шрифты*\n"
        "• *«шрифт Roboto»* — найду и пришлю бесплатный TTF-файл с выбором начертания",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )


async def groq_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚡ Groq активирован", reply_markup=main_keyboard())



async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    with _memory_lock:
        user_memory[user_id] = []
    await update.message.reply_text("🗑 Память очищена", reply_markup=main_keyboard())


def _font_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Regular", callback_data="fs:Regular"),
            InlineKeyboardButton("Bold", callback_data="fs:Bold"),
        ],
        [
            InlineKeyboardButton("Italic", callback_data="fs:Italic"),
            InlineKeyboardButton("Bold Italic", callback_data="fs:BoldItalic"),
        ],
        [
            InlineKeyboardButton("Light", callback_data="fs:Light"),
            InlineKeyboardButton("SemiBold", callback_data="fs:SemiBold"),
        ],
        [
            InlineKeyboardButton("Thin", callback_data="fs:Thin"),
            InlineKeyboardButton("Black", callback_data="fs:Black"),
        ],
        [
            InlineKeyboardButton("📦 Все начертания (ZIP)", callback_data="fs:all"),
        ],
    ])


async def font_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    font_name = " ".join(context.args).strip()
    if not font_name:
        await update.message.reply_text(
            "Использование: /font <название шрифта>\n"
            "Или напиши просто: шрифт Roboto\n\n"
            "Пример: /font Open Sans"
        )
        return
    font_pending[user_id] = font_name
    await update.message.reply_text(
        f"🔤 Шрифт: *{font_name}*\n\n"
        "Выбери начертание или нажми «Все начертания» для ZIP-архива.\n"
        "Можно также написать своё, например: ExtraLight Italic",
        parse_mode="Markdown",
        reply_markup=_font_keyboard()
    )


# ===== BUTTON CALLBACKS =====

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    try:
        await query.answer()
    except Exception:
        pass  # Ignore expired callback queries

    if query.data == "reset_memory":
        with _memory_lock:
            user_memory[user_id] = []
        await query.message.reply_text("🗑 Память очищена")

    elif query.data.startswith("fs:"):
        style = query.data[3:]
        font_name = font_pending.pop(user_id, None)
        if font_name is None:
            await query.message.reply_text("⚠ Сессия устарела, введи /font заново.")
            return
        loop = asyncio.get_running_loop()
        if style == "all":
            msg = await query.message.reply_text(
                f"📦 Скачиваю все начертания *{font_name}*...",
                parse_mode="Markdown"
            )
            try:
                filename, file_bytes = await loop.run_in_executor(
                    None, download_all_fonts, font_name
                )
                await query.message.reply_document(
                    document=io.BytesIO(file_bytes),
                    filename=filename,
                    caption=f"✅ {font_name} — все начертания\n🆓 Источник: Google Fonts / DaFont"
                )
                await msg.delete()
            except Exception as e:
                await msg.edit_text(f"⚠ {e}")
        else:
            msg = await query.message.reply_text(
                f"🔍 Ищу шрифт *{font_name}* — начертание *{style}*...",
                parse_mode="Markdown"
            )
            try:
                filename, file_bytes = await loop.run_in_executor(
                    None, download_font, font_name, style
                )
                await query.message.reply_document(
                    document=io.BytesIO(file_bytes),
                    filename=filename,
                    caption=f"✅ {font_name} — {style}\n🆓 Источник: Google Fonts (бесплатный некоммерческий шрифт)"
                )
                await msg.delete()
            except Exception as e:
                await msg.edit_text(f"⚠ {e}")


# ===== CHAT =====

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text.strip()

    if not text:
        await update.message.reply_text("Отправь сообщение.")
        return

    # Check if user is in GIF building flow
    if user_id in gif_pending:
        state = gif_pending[user_id]
        if state["step"] == "fps":
            try:
                fps = float(text.replace(",", "."))
                if fps <= 0:
                    raise ValueError()
            except ValueError:
                await update.message.reply_text("⚠ Введи корректное число, например `0.5` или `1`", parse_mode="Markdown")
                return
            state["fps"] = fps
            state["step"] = "maxsize"
            await update.message.reply_text(
                "📏 До какого веса (в КБ) сжать каждый GIF?\n"
                "Напиши число, например: `200` или `500`",
                parse_mode="Markdown"
            )
            return
        elif state["step"] == "maxsize":
            try:
                max_kb = int(text.replace(",", ".").split(".")[0])
                if max_kb <= 0:
                    raise ValueError()
            except ValueError:
                await update.message.reply_text("⚠ Введи целое число в КБ, например `200`", parse_mode="Markdown")
                return
            pending = gif_pending.pop(user_id)
            zip_bytes = pending["zip_bytes"]
            fps = pending["fps"]
            msg = await update.message.reply_text(
                f"🎞 Собираю GIF-файлы (⏱ {fps} сек/кадр, 📏 до {max_kb} КБ каждый)..."
            )
            try:
                loop = asyncio.get_running_loop()
                result_zip, created = await loop.run_in_executor(
                    None, build_gifs_from_zip, zip_bytes, fps, max_kb
                )
                if not created:
                    await msg.edit_text(
                        "⚠ Не нашёл подходящих групп изображений.\n"
                        "Убедись, что файлы в архиве называются как `240х400_1.png`, `240х400_2.png` и т.д."
                    )
                    return
                names_list = "\n".join(f"• {n}" for n in created)
                await update.message.reply_document(
                    document=io.BytesIO(result_zip),
                    filename="gifs.zip",
                    caption=f"✅ Готово! Создано GIF-файлов: {len(created)}\n{names_list}"
                )
                await msg.delete()
            except Exception as e:
                await msg.edit_text(f"⚠ Ошибка при создании GIF: {e}")
            return

    # Check if user is waiting to provide font style
    if user_id in font_pending:
        font_name = font_pending.pop(user_id)
        style = text.strip()
        loop = asyncio.get_running_loop()
        if style.lower() in ("все", "all", "всё"):
            msg = await update.message.reply_text(
                f"📦 Скачиваю все начертания *{font_name}*...",
                parse_mode="Markdown"
            )
            try:
                filename, file_bytes = await loop.run_in_executor(
                    None, download_all_fonts, font_name
                )
                await update.message.reply_document(
                    document=io.BytesIO(file_bytes),
                    filename=filename,
                    caption=f"✅ {font_name} — все начертания\n🆓 Источник: Google Fonts / DaFont"
                )
                await msg.delete()
            except Exception as e:
                await msg.edit_text(f"⚠ {e}")
        else:
            msg = await update.message.reply_text(
                f"🔍 Ищу шрифт *{font_name}* — начертание *{style}*...",
                parse_mode="Markdown"
            )
            try:
                filename, file_bytes = await loop.run_in_executor(
                    None, download_font, font_name, style
                )
                await update.message.reply_document(
                    document=io.BytesIO(file_bytes),
                    filename=filename,
                    caption=f"✅ {font_name} — {style}\n🆓 Источник: Google Fonts (бесплатный некоммерческий шрифт)"
                )
                await msg.delete()
            except Exception as e:
                await msg.edit_text(f"⚠ {e}")
        return

    # Check if message is a font request ("шрифт <name>")
    font_match = FONT_RE.match(text)
    if font_match:
        font_name = font_match.group(1).strip()
        font_pending[user_id] = font_name
        await update.message.reply_text(
            f"🔤 Шрифт: *{font_name}*\n\n"
            "Выбери начертание или нажми «Все начертания» для ZIP-архива.\n"
            "Можно также написать своё, например: ExtraLight Italic",
            parse_mode="Markdown",
            reply_markup=_font_keyboard()
        )
        return

    # Check if message is a music request ("музыка <query>")
    music_match = MUSIC_RE.match(text)
    if music_match:
        query = music_match.group(1).strip()
        msg = await update.message.reply_text(f"🔍 Ищу: {query}...")
        try:
            loop = asyncio.get_running_loop()
            audio_bytes, title, artist = await loop.run_in_executor(
                None, download_music, query
            )
            await msg.edit_text("📤 Загружаю файл...")
            await update.message.reply_audio(
                audio=io.BytesIO(audio_bytes),
                filename=f"{title}.mp3",
                title=title,
                performer=artist,
            )
            await msg.delete()
        except Exception as e:
            await msg.edit_text(f"⚠ Ошибка: {e}")
        return

    # Check for VK / SoundCloud / Deezer audio links
    vk_match = VK_RE.search(text)
    sc_match = SOUNDCLOUD_RE.search(text)
    dz_match = DEEZER_RE.search(text)
    audio_link = vk_match or sc_match or dz_match

    if audio_link:
        url = audio_link.group(0)
        if vk_match:
            source = "VK"
        elif sc_match:
            source = "SoundCloud"
        else:
            source = "Deezer"
        msg = await update.message.reply_text(f"🎵 Скачиваю аудио с {source}...")
        try:
            loop = asyncio.get_running_loop()
            audio_bytes, title, artist = await loop.run_in_executor(None, download_audio_url, url)
            await update.message.reply_audio(
                audio=io.BytesIO(audio_bytes),
                filename=f"{title}.mp3",
                title=title,
                performer=artist,
            )
            await msg.delete()
        except Exception as e:
            await msg.edit_text(f"⚠ {e}")
        return

    # ── Radio: "радио джаз", "включи рок радио", "хочу слушать радио" ───────
    if RADIO_RE.search(text):
        raw_genre = extract_radio_genre(text)
        genre_key, yt_queries, streams = match_radio_genre(raw_genre)
        genre_label = genre_key.capitalize() if genre_key else "музыка"
        src_hint = "прямого стрима" if streams else "потока"
        status = await update.message.reply_text(
            f"📻 Ищу радио «{genre_label}» ({src_hint})…"
        )
        loop = asyncio.get_running_loop()
        try:
            audio_bytes, title = await asyncio.wait_for(
                loop.run_in_executor(
                    None, download_radio_audio, yt_queries, streams, genre_label
                ),
                timeout=300,
            )
        except asyncio.TimeoutError:
            await status.edit_text("⏱ Превышено время ожидания. Попробуй ещё раз.")
            return
        except Exception as e:
            await status.edit_text(f"⚠ Не удалось загрузить радио: {e}")
            return
        await status.edit_text("📤 Отправляю аудио…")
        try:
            size_mb = len(audio_bytes) / (1024 * 1024)
            await update.message.reply_audio(
                audio=io.BytesIO(audio_bytes),
                title=title,
                performer=f"📻 Радио · {genre_label}",
                caption=f"🎵 {title}\n📻 Жанр: {genre_label} · {size_mb:.0f} МБ",
            )
            await status.delete()
        except Exception as e:
            await status.edit_text(f"⚠ Ошибка отправки: {e}")
        return

    # ── Image search: "покажи кота", "фото машины", "как выглядит ..." ────────
    if IMAGE_RE.search(text):
        query = extract_image_query(text)
        if not query:
            await update.message.reply_text("🔍 Что именно показать? Напиши, например: «покажи закат»")
            return
        status = await update.message.reply_text("🔍 Ищу фото…")
        loop = asyncio.get_running_loop()
        try:
            results = await asyncio.wait_for(
                loop.run_in_executor(None, search_images, query, 5, True),
                timeout=20,
            )
        except Exception:
            results = []
        if not results:
            await status.edit_text("😔 Не удалось найти подходящие фото. Попробуй другой запрос.")
            return
        await status.edit_text("📥 Загружаю фото…")
        sent = 0
        media_group = []
        for item in results:
            try:
                img_bytes = await asyncio.wait_for(
                    loop.run_in_executor(None, download_image, item["url"]),
                    timeout=8,
                )
                from telegram import InputMediaPhoto as _IMP
                media_group.append(
                    _IMP(
                        media=io.BytesIO(img_bytes),
                        caption=item["title"][:1000] if sent == 0 else None,
                    )
                )
                sent += 1
            except Exception:
                continue
            if sent >= 4:
                break
        if not media_group:
            await status.edit_text(f"😔 Не удалось загрузить фото по запросу «{query}».")
            return
        try:
            if len(media_group) == 1:
                await update.message.reply_photo(
                    photo=media_group[0].media,
                    caption=media_group[0].caption,
                )
            else:
                await update.message.reply_media_group(media=media_group)
            await status.delete()
        except Exception as e:
            await status.edit_text(f"⚠ Ошибка отправки: {e}")
        return

    # ── Smart internet media search ──────────────────────────────────────────
    if INTERNET_RE.search(text) or MEDIA_FROM_RE.search(text) or PHOTO_GUIDE_RE.search(text):
        loop = asyncio.get_running_loop()
        msg = await update.message.reply_text("🔍 Понимаю запрос…")
        try:
            intent_data = await loop.run_in_executor(None, classify_intent, text)
            intent = intent_data.get("intent", "chat")
            query  = intent_data.get("query", text)

            if intent == "images":
                await msg.edit_text(f"🖼 Ищу картинки: «{query}»…")
                images = await loop.run_in_executor(None, search_images, query, 5, True)
                if not images and query != text:
                    images = await loop.run_in_executor(None, search_images, text, 5, True)
                if not images:
                    await msg.edit_text(
                        "😔 Не удалось найти картинки по этому запросу.\n"
                        "Попробуй сформулировать иначе или уточнить запрос."
                    )
                    return
                from telegram import InputMediaPhoto
                media_group = []
                for i, item in enumerate(images):
                    try:
                        img_bytes = await asyncio.wait_for(
                            loop.run_in_executor(None, download_image, item["url"]),
                            timeout=8,
                        )
                        media_group.append(InputMediaPhoto(
                            media=io.BytesIO(img_bytes),
                            caption=f"🔎 {query}" if i == 0 else None,
                        ))
                    except Exception:
                        continue
                    if len(media_group) >= 4:
                        break
                if media_group:
                    if len(media_group) == 1:
                        await update.message.reply_photo(
                            photo=media_group[0].media,
                            caption=media_group[0].caption,
                        )
                    else:
                        await update.message.reply_media_group(media=media_group)
                    await msg.delete()
                else:
                    await msg.edit_text("😔 Не удалось загрузить картинки.")
                return

            if intent == "video":
                await msg.edit_text(
                    "⚠ Поиск видео по запросу недоступен.\n"
                    "Отправь прямую ссылку на VK для скачивания видео."
                )
                return

            if intent == "music":
                await msg.edit_text(f"🎵 Ищу музыку: «{query}»…")
                try:
                    audio_bytes, title, artist = await loop.run_in_executor(
                        None, download_music, query
                    )
                    await update.message.reply_audio(
                        audio=io.BytesIO(audio_bytes),
                        filename=f"{title}.mp3",
                        title=title,
                        performer=artist,
                    )
                    await msg.delete()
                except Exception as e:
                    await msg.edit_text(f"⚠ Не нашёл музыку: {e}")
                return

            if intent == "info":
                await msg.edit_text(f"🔍 Ищу информацию: «{query}»…")
                answer, image_query = await loop.run_in_executor(None, search_web_info, query)
                if not answer:
                    await msg.edit_text(
                        "😔 Не нашёл актуальной информации по этому запросу.\n"
                        "Попробуй переформулировать или задать вопрос точнее."
                    )
                    return
                try:
                    await msg.edit_text(answer, parse_mode="Markdown")
                except Exception:
                    await msg.edit_text(answer)
                if image_query:
                    imgs = await loop.run_in_executor(None, search_images, image_query, 3)
                    if imgs:
                        from telegram import InputMediaPhoto
                        media = []
                        for item in imgs:
                            try:
                                img_bytes = await asyncio.wait_for(
                                    loop.run_in_executor(None, download_image, item["url"]),
                                    timeout=8,
                                )
                                media.append(InputMediaPhoto(media=io.BytesIO(img_bytes)))
                            except Exception:
                                continue
                        if media:
                            await update.message.reply_media_group(media=media)
                return

            # intent == "chat" — fall through to regular AI chat
            await msg.delete()

        except Exception as e:
            try:
                await msg.edit_text(f"⚠ Ошибка поиска: {e}")
            except Exception:
                pass
            return
    # ─────────────────────────────────────────────────────────────────────────

    # ── Direct info search (расписания, факты, локальные запросы, вопросы с ?) ──
    is_question = text.rstrip().endswith('?')
    if INFO_RE.search(text) or is_question:
        loop = asyncio.get_running_loop()
        msg = await update.message.reply_text("🔍 Ищу актуальную информацию…")
        try:
            # Use classify_intent to get an optimized search query
            intent_data = await loop.run_in_executor(None, classify_intent, text)
            intent = intent_data.get("intent", "info")
            search_query = intent_data.get("query", text)

            # If AI says it's a chat question (joke, opinion), skip web search
            if intent == "chat" and not INFO_RE.search(text):
                await msg.delete()
            else:
                await msg.edit_text(f"🔍 Ищу: «{search_query}»…")
                answer, image_query = await loop.run_in_executor(
                    None, search_web_info, search_query
                )
                if answer:
                    try:
                        await msg.edit_text(answer, parse_mode="Markdown")
                    except Exception:
                        await msg.edit_text(answer)
                    if image_query:
                        imgs = await loop.run_in_executor(None, search_images, image_query, 3)
                        if imgs:
                            from telegram import InputMediaPhoto
                            media = []
                            for item in imgs:
                                try:
                                    img_bytes = await asyncio.wait_for(
                                        loop.run_in_executor(None, download_image, item["url"]),
                                        timeout=8,
                                    )
                                    media.append(InputMediaPhoto(media=io.BytesIO(img_bytes)))
                                except Exception:
                                    continue
                            if media:
                                await update.message.reply_media_group(media=media)
                    return
                # If no results — fall through to AI chat
                await msg.delete()
        except Exception as e:
            try:
                await msg.edit_text(f"⚠ Ошибка: {e}")
            except Exception:
                pass
            return
    # ─────────────────────────────────────────────────────────────────────────

    # Regular AI chat
    loop = asyncio.get_running_loop()

    # Fetch live weather in thread if asked
    system_extra = ""
    if WEATHER_RE.search(text):
        city = extract_city(text)
        if city:
            system_extra = await loop.run_in_executor(None, fetch_weather, city)

    try:
        answer = await loop.run_in_executor(None, ask_groq, user_id, text, system_extra)
        await update.message.reply_text(answer, reply_markup=main_keyboard())
    except Exception as e:
        await update.message.reply_text(f"⚠ Ошибка: {e}")


# ===== GIF BUILDER =====

def _make_gif(frames: list, duration_ms: int, max_kb: int) -> bytes:
    max_bytes = max_kb * 1024

    def render(imgs, n_colors):
        buf = io.BytesIO()
        processed = []
        for img in imgs:
            rgba = img.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.split()[3])
            q = background.quantize(colors=n_colors, method=Image.Quantize.MEDIANCUT, dither=0)
            processed.append(q)
        processed[0].save(
            buf, format="GIF", save_all=True,
            append_images=processed[1:],
            duration=duration_ms, loop=0, optimize=True
        )
        return buf.getvalue()

    for n_colors in [256, 128, 64, 32, 16, 8, 4, 2]:
        data = render(frames, n_colors)
        if len(data) <= max_bytes:
            return data
    return render(frames, 2)


def build_gifs_from_zip(zip_bytes: bytes, fps: float, max_kb: int) -> tuple[bytes, list[str]]:
    duration_ms = max(1, int(fps * 1000))
    src = zipfile.ZipFile(io.BytesIO(zip_bytes), "r")

    groups: dict[str, list[tuple[int, str, bytes]]] = {}
    for item in src.infolist():
        if item.is_dir():
            continue
        fname = os.path.basename(item.filename)
        ext = os.path.splitext(fname)[1].lower()
        if ext not in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}:
            continue
        m = re.match(r'^(.+?)_(\d+)(\.[^.]+)$', fname)
        if m:
            base = m.group(1)
            idx = int(m.group(2))
            groups.setdefault(base, []).append((idx, item.filename, src.read(item.filename)))

    out_buf = io.BytesIO()
    created: list[str] = []
    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for base, frames_list in sorted(groups.items()):
            frames_list.sort(key=lambda x: x[0])
            frames = []
            for _, _, data in frames_list:
                try:
                    frames.append(Image.open(io.BytesIO(data)))
                except Exception:
                    continue
            if len(frames) < 2:
                continue
            gif_bytes = _make_gif(frames, duration_ms, max_kb)
            gif_name = f"{base}.gif"
            dst.writestr(gif_name, gif_bytes)
            created.append(gif_name)

    return out_buf.getvalue(), created


# ===== ZIP RENAME BY DIMENSIONS =====

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff"}


def rename_zip_by_dimensions(zip_bytes: bytes) -> tuple[bytes, int, int]:
    """
    Re-pack a ZIP renaming every image file to WxH.ext.
    Returns (new_zip_bytes, total_files, renamed_count).
    """
    src = zipfile.ZipFile(io.BytesIO(zip_bytes), "r")
    out_buf = io.BytesIO()
    name_count: dict[str, int] = {}
    total = renamed = 0

    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            total += 1
            ext = os.path.splitext(item.filename)[1].lower()

            if ext in IMAGE_EXTS:
                try:
                    img = Image.open(io.BytesIO(data))
                    w, h = img.size
                    base = f"{w}x{h}"
                    out_ext = ".jpg" if ext in (".jpg", ".jpeg") else ext
                    key = base + out_ext
                    count = name_count.get(key, 0)
                    name_count[key] = count + 1
                    final_name = f"{base}_{count}{out_ext}" if count else key
                    dst.writestr(final_name, data)
                    renamed += 1
                    continue
                except Exception:
                    pass  # not a valid image — keep original name

            # Non-image or unreadable: keep original filename
            dst.writestr(item.filename, data)

    src.close()
    out_buf.seek(0)
    return out_buf.read(), total, renamed


# ===== REVERSE IMAGE SEARCH =====

_YANDEX_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _make_yandex_opener() -> urllib.request.OpenerDirector:
    """Build an opener with a cookie jar and init a Yandex session."""
    cj = _cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    try:
        req = urllib.request.Request(
            "https://yandex.ru/images/",
            headers={
                "User-Agent": _YANDEX_UA,
                "Accept": "text/html,application/xhtml+xml,*/*",
                "Accept-Language": "ru-RU,ru;q=0.9",
            },
        )
        opener.open(req, timeout=12)
    except Exception as e:
        print(f"[yandex_session] init warning: {e}", flush=True)
    return opener


def _yandex_cbir_upload(opener: urllib.request.OpenerDirector,
                         image_bytes: bytes, ext: str) -> tuple[str, str]:
    """Upload image to Yandex via HTML form. Returns (final_url, html)."""
    content_type_map = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "webp": "image/webp",
        "gif": "image/gif", "bmp": "image/bmp",
    }
    mime = content_type_map.get(ext.lower(), "image/jpeg")
    boundary = "----WebKitFormBoundaryYnDeZ9vvmB2iHa3W"

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="upfile"; filename="image.{ext}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("utf-8") + image_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

    # Use plain HTML upload endpoint (not JSON) — Yandex follows with a redirect
    upload_url = "https://yandex.ru/images/search?rpt=imageview"

    req = urllib.request.Request(
        upload_url,
        data=body,
        headers={
            "User-Agent": _YANDEX_UA,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Referer": "https://yandex.ru/images/",
            "Origin": "https://yandex.ru",
        },
    )

    with opener.open(req, timeout=40) as resp:
        final_url = resp.geturl()
        html = resp.read().decode("utf-8", errors="ignore")

    print(f"[yandex_upload] final_url={final_url[:200]}", flush=True)

    # Verify cbir_id is in the final URL
    parsed = urllib.parse.urlparse(final_url)
    qs = urllib.parse.parse_qs(parsed.query)
    cbir_id = qs.get("cbir_id", [None])[0]

    if not cbir_id:
        # Try to find in HTML
        m = re.search(r'cbir[_-]id[=:]["\']?([A-Za-z0-9/_\-]{5,})', html)
        if m:
            cbir_id = m.group(1)

    if not cbir_id:
        raise RuntimeError(
            f"Яндекс не вернул cbir-id.\n"
            f"URL после загрузки: {final_url[:200]}"
        )

    print(f"[yandex_upload] cbir_id={cbir_id}", flush=True)
    return final_url, html


def _extract_image_urls(html: str) -> list[str]:
    """Extract candidate original image URLs from Yandex results page HTML."""
    seen: set[str] = set()
    out: list[str] = []

    bad = {"yandex.ru", "yandex.net", "yandex.com", "ya.ru",
           "avatars.mds", "mc.yandex", "clck.ru"}
    bad_parts = {"thumb", "thumbnail", "favicon", "icon",
                 "avatar", "logo", "sprite", "pixel", "1x1"}
    bad_exts = {".svg", ".ico", ".css", ".js", ".woff", ".woff2", ".ttf"}

    def ok(url: str) -> bool:
        u = url.lower()
        if any(d in u for d in bad):
            return False
        if any(p in u for p in bad_parts):
            return False
        path = u.split("?")[0]
        if any(path.endswith(e) for e in bad_exts):
            return False
        return True

    patterns = [
        r'"originUrl"\s*:\s*"(https?://[^"]{10,})"',
        r'"imgUrl"\s*:\s*"(https?://[^"]{10,})"',
        r'"url"\s*:\s*"(https?://[^"]{10,}\.(?:jpe?g|png|webp|bmp)[^"]*)"',
        r'(https?://[^\s"\'<>]{20,}\.(?:jpe?g|png|webp|bmp)(?:\?[^\s"\'<>]*)?)',
    ]

    for pat in patterns:
        for m in re.finditer(pat, html, re.IGNORECASE):
            url = m.group(1).replace("\\/", "/").replace("\\u002F", "/")
            if url not in seen and ok(url):
                seen.add(url)
                out.append(url)
        if len(out) >= 25:
            break

    return out[:25]


def find_original_image(image_bytes: bytes, ext: str = "jpg") -> tuple[bytes, str]:
    opener = _make_yandex_opener()
    # Upload returns (final_url, html) — no need for a second request
    _final_url, html = _yandex_cbir_upload(opener, image_bytes, ext)

    candidates = _extract_image_urls(html)
    print(f"[yandex_results] {len(candidates)} candidates", flush=True)

    if not candidates:
        raise RuntimeError(
            "Оригинал не найден: Яндекс не вернул ссылки на изображения.\n"
            "Попробуй другое фото."
        )

    MAX_SIZE = 50 * 1024 * 1024

    for url in candidates:
        try:
            dl_req = urllib.request.Request(
                url,
                headers={"User-Agent": _YANDEX_UA},
            )
            with urllib.request.urlopen(dl_req, timeout=20) as resp:
                ct = resp.headers.get("Content-Type", "")
                if not any(t in ct for t in ["image/", "octet-stream"]):
                    continue
                data = resp.read()

            if len(data) < 5000 or len(data) > MAX_SIZE:
                continue

            try:
                chk = Image.open(io.BytesIO(data))
                chk.verify()
            except Exception:
                continue

            url_path = url.split("?")[0]
            url_ext = url_path.rsplit(".", 1)[-1].lower()
            if url_ext not in ("jpg", "jpeg", "png", "webp", "gif", "bmp"):
                url_ext = "jpg" if "jpeg" in ct or "jpg" in ct else (
                    "png" if "png" in ct else ("webp" if "webp" in ct else "jpg")
                )

            print(f"[yandex_dl] OK {len(data)//1024}KB {url[:80]}", flush=True)
            return data, url_ext

        except Exception as e:
            print(f"[yandex_dl] skip {url[:60]}: {e}", flush=True)
            continue

    raise RuntimeError(
        "Не удалось скачать ни один из найденных оригиналов.\n"
        "Источники закрыты или требуют авторизации."
    )


async def image_document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle images sent as files (uncompressed documents)."""
    doc = update.message.document
    if not doc.mime_type or not doc.mime_type.startswith("image/"):
        return

    caption = (update.message.caption or "").strip()

    if FIND_IMG_RE.search(caption):
        msg = await update.message.reply_text("🔍 Ищу оригинал изображения в интернете...")
        try:
            file = await context.bot.get_file(doc.file_id)
            dl = io.BytesIO()
            await file.download_to_memory(dl)
            img_bytes = dl.getvalue()
            ext = (doc.file_name or "image.jpg").rsplit(".", 1)[-1].lower()
            if ext not in ("jpg", "jpeg", "png", "webp", "gif", "bmp"):
                ext = "jpg"
            loop = asyncio.get_running_loop()
            result_bytes, result_ext = await loop.run_in_executor(
                None, find_original_image, img_bytes, ext
            )
            size_kb = len(result_bytes) / 1024
            size_str = f"{size_kb / 1024:.1f} МБ" if size_kb > 1024 else f"{size_kb:.0f} КБ"
            await update.message.reply_document(
                document=io.BytesIO(result_bytes),
                filename=f"original.{result_ext}",
                caption=f"✅ Найден оригинал — {size_str}",
            )
            await msg.delete()
        except Exception as e:
            await msg.edit_text(f"⚠ {e}")

    elif UPSCALE_RE.search(caption):
        msg = await update.message.reply_text("🔬 Увеличиваю фото в 3x...")
        try:
            file = await context.bot.get_file(doc.file_id)
            dl = io.BytesIO()
            await file.download_to_memory(dl)
            loop = asyncio.get_running_loop()
            result_bytes, orig, new_sz = await loop.run_in_executor(
                None, upscale_image_x4, dl.getvalue()
            )
            size_mb = len(result_bytes) / (1024 * 1024)
            filename = "upscaled_3x.png"
            fmt = "PNG"
            if size_mb > 45:
                from PIL import Image as _Image
                img_big = _Image.open(io.BytesIO(result_bytes))
                buf2 = io.BytesIO()
                img_big.convert("RGB").save(buf2, format="JPEG", quality=95, optimize=True)
                result_bytes = buf2.getvalue()
                size_mb = len(result_bytes) / (1024 * 1024)
                filename = "upscaled_3x.jpg"
                fmt = "JPEG"
            await update.message.reply_document(
                document=io.BytesIO(result_bytes),
                filename=filename,
                caption=(
                    f"✅ Готово! Увеличено в 3x ({fmt})\n"
                    f"📐 {orig[0]}×{orig[1]} → {new_sz[0]}×{new_sz[1]}\n"
                    f"💾 Размер файла: {size_mb:.1f} МБ"
                ),
            )
            await msg.delete()
        except Exception as e:
            await msg.edit_text(f"⚠ Ошибка: {e}")

    elif TEXT_PCT_RE.search(caption):
        msg = await update.message.reply_text("🔍 Анализирую текст на фото...")
        try:
            file = await context.bot.get_file(doc.file_id)
            dl = io.BytesIO()
            await file.download_to_memory(dl)
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, analyze_text_percentage, dl.getvalue())
            await update.message.reply_text(f"📊 {result}")
            await msg.delete()
        except Exception as e:
            await msg.edit_text(f"⚠ Ошибка: {e}")

    elif parse_target_size(caption):
        target_bytes_val = parse_target_size(caption)
        msg = await update.message.reply_text("🗜 Сжимаю фото...")
        try:
            file = await context.bot.get_file(doc.file_id)
            dl = io.BytesIO()
            await file.download_to_memory(dl)
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, compress_image_to_size, dl.getvalue(), target_bytes_val
            )
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_STORED) as zf:
                zf.writestr("photo_01.jpg", result)
            zip_buf.seek(0)
            await update.message.reply_document(
                document=zip_buf,
                filename="compressed.zip",
                caption=f"✅ Готово. Размер: {len(result) / 1024:.1f} КБ",
            )
            await msg.delete()
        except Exception as e:
            await msg.edit_text(f"⚠ Ошибка: {e}")

    else:
        await update.message.reply_text(
            "📷 Изображение получено!\n\n"
            "Что я умею с фото:\n"
            "• Подпиши «найди» или «оригинал» — найду оригинал в интернете (до 50МБ)\n"
            "• Подпиши «улучши фото» — увеличу в 3x\n"
            "• Подпиши «до 500кб» (любой размер) — сожму\n"
            "• Подпиши «процент текста» — определю долю текста"
        )


async def zip_rename_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc.file_name or not doc.file_name.lower().endswith(".zip"):
        return

    caption = (update.message.caption or "").strip()
    user_id = update.message.from_user.id

    if GIF_CMD_RE.search(caption):
        msg = await update.message.reply_text("📥 Получил архив. Скачиваю...")
        try:
            file = await context.bot.get_file(doc.file_id)
            dl = io.BytesIO()
            await file.download_to_memory(dl)
            gif_pending[user_id] = {"zip_bytes": dl.getvalue(), "step": "fps"}
            await msg.edit_text(
                "⏱ Сколько секунд на один кадр?\n"
                "Напиши число, например: `0.5` или `1`",
                parse_mode="Markdown"
            )
        except Exception as e:
            await msg.edit_text(f"⚠ Ошибка при загрузке архива: {e}")
        return

    msg = await update.message.reply_text("📦 Переименовываю файлы в архиве...")
    try:
        file = await context.bot.get_file(doc.file_id)
        dl = io.BytesIO()
        await file.download_to_memory(dl)

        loop = asyncio.get_running_loop()
        result_bytes, total, renamed = await loop.run_in_executor(
            None, rename_zip_by_dimensions, dl.getvalue()
        )

        original_name = os.path.splitext(doc.file_name)[0]
        await update.message.reply_document(
            document=io.BytesIO(result_bytes),
            filename=f"{original_name}_renamed.zip",
            caption=f"✅ Готово: {renamed} из {total} файлов переименованы по размеру в пикселях",
        )
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"⚠ Ошибка: {e}")


# ===== START BOT =====

app = (
    ApplicationBuilder()
    .token(TELEGRAM_TOKEN)
    .connect_timeout(30)
    .read_timeout(120)
    .write_timeout(120)
    .pool_timeout(30)
    .concurrent_updates(True)
    .build()
)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    import traceback
    err_text = "".join(traceback.format_exception(type(context.error), context.error, context.error.__traceback__))
    print(f"[ERROR] {err_text}", flush=True)
    if update and hasattr(update, "message") and update.message:
        try:
            await update.message.reply_text("⚠ Что-то пошло не так, попробуй ещё раз.")
        except Exception:
            pass

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("groq", groq_cmd))
app.add_handler(CommandHandler("music", music_cmd))
app.add_handler(CommandHandler("reset", reset))
app.add_handler(CommandHandler("font", font_cmd))
app.add_handler(CallbackQueryHandler(button_callback))
app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
app.add_handler(MessageHandler(filters.VIDEO, video_handler))
app.add_handler(MessageHandler(filters.Document.VIDEO, video_handler))
app.add_handler(MessageHandler(filters.Document.IMAGE, image_document_handler))
app.add_handler(MessageHandler(filters.Document.FileExtension("zip"), zip_rename_handler))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
app.add_error_handler(error_handler)

print("🤖 Bot running...", flush=True)

_RENDER_HOST = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")
_PORT = int(os.environ.get("PORT", 8443))

if _RENDER_HOST:
    # ── Webhook mode (Render) ──────────────────────────────────────────────
    _WEBHOOK_URL = f"https://{_RENDER_HOST}/{TELEGRAM_TOKEN}"
    print(f"🌐 Webhook mode: {_WEBHOOK_URL}", flush=True)
    app.run_webhook(
        listen="0.0.0.0",
        port=_PORT,
        url_path=TELEGRAM_TOKEN,
        webhook_url=_WEBHOOK_URL,
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )
else:
    # ── Polling mode (local / Replit) ──────────────────────────────────────
    print("🔄 Polling mode", flush=True)
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
