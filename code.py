import asyncio
import html as _html
import io
import json
import os
import re
import socket
import tempfile
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from openai import OpenAI
from PIL import Image
from rembg import remove as rembg_remove
import yt_dlp

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ===== TOKENS =====
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8610501182:AAF_w5tOE446-4DaXJztk2dlh13rcX526Kk")
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY",   "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")

# Optional: paste Netscape-format YouTube cookies here or in env var YOUTUBE_COOKIES
YOUTUBE_COOKIES = os.environ.get("YOUTUBE_COOKIES", "")

# ===== GROQ CLIENT =====
groq_client = OpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1"
)

# ===== MEMORY =====
user_memory = {}

# ===== FONT STATE =====
font_pending = {}  # user_id -> font_name

# ===== "ЕЩЕ" CACHE =====
# user_id -> {"type": "shorts"|"tiktok"|"video"|"music", "query": str, "entries": list, "index": int}
user_more_cache: dict = {}

MORE_RE = re.compile(
    r'^\s*(?:ещ[её]|еще|more|ещ[её]?\s+один|ещ[её]?\s+раз|другой|другое|другую|следующ\w+|next)\s*[!?.]*\s*$',
    re.IGNORECASE
)

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
INSTAGRAM_RE = re.compile(
    r'https?://(?:www\.)?instagram\.com/(?:p|reel|tv|stories)/[\w\-]+'
)
YOUTUBE_RE = re.compile(
    r'https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)[\w\-]+'
)
SHORTS_SEARCH_RE = re.compile(
    r'(?:пришли|найди|скачай|покажи|дай)?\s*шортс[ы]?\s+(.+)',
    re.IGNORECASE
)
TIKTOK_SEARCH_RE = re.compile(
    r'(?:пришли|найди|скачай|покажи|дай)?\s*тикток\s+(.+)',
    re.IGNORECASE
)
TIKTOK_URL_RE = re.compile(
    r'https?://(?:www\.|vm\.|vt\.)?tiktok\.com/[\w@/\-\.]+'
)

VK_RE = re.compile(
    r'https?://(?:www\.)?vk\.com/(?:video[\-\d_]+|music/album/[\w\-]+|wall[\-\d_]+|clip[\-\d_]+)'
)
SOUNDCLOUD_RE = re.compile(
    r'https?://(?:www\.)?soundcloud\.com/[\w\-]+/[\w\-]+'
)
DEEZER_RE = re.compile(
    r'https?://(?:www\.)?deezer\.com/(?:\w+/)?(?:track|album|playlist)/\d+'
)
PROXY_RE = re.compile(
    r'^\s*(?:прокси|proxy|прокс[иы]|vpn|впн|найди\s+прокси|дай\s+прокси|пришли\s+прокси)\s*$',
    re.IGNORECASE
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
    history = user_memory.get(user_id, [])
    history.append({"role": "user", "content": prompt})

    messages = [{"role": "system", "content": build_system_prompt(system_extra)}] + history

    response = groq_client.chat.completions.create(
        model="moonshotai/kimi-k2-instruct-0905",
        messages=messages
    )

    answer = response.choices[0].message.content
    history.append({"role": "assistant", "content": answer})
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
# Catch factual/informational queries that need real web search
INFO_RE = re.compile(
    r'\b(?:'
    r'что\s+такое'
    r'|кто\s+такой'
    r'|кто\s+такая'
    r'|расскажи\s+(?:про|о|об)\b'
    r'|информаци[яю]\s+(?:про|о|об)\b'
    r'|факты\s+(?:про|о|об)\b'
    r'|история\s+(?:создания|возникновения|развития|появления|про|о|об)\b'
    r'|как\s+работает\b'
    r'|как\s+устроен\b'
    r'|из\s+чего\s+(?:состоит|сделан|делают)\b'
    r'|чем\s+знаменит\b'
    r'|что\s+известно\s+о\b'
    r'|tell\s+me\s+about\b'
    r'|what\s+is\b'
    r'|who\s+is\b'
    r'|history\s+of\b'
    r'|facts\s+about\b'
    r'|how\s+does\b'
    r')',
    re.IGNORECASE,
)


def classify_intent(text: str) -> dict:
    """Ask Groq to classify what kind of media the user wants.
    Returns {"intent": "images"|"video"|"music"|"info"|"chat", "query": "..."}
    """
    try:
        resp = groq_client.chat.completions.create(
            model="moonshotai/kimi-k2-instruct-0905",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a strict intent classifier. Analyze the user message and return JSON.\n\n"
                        "Fields:\n"
                        "- intent: exactly one of 'images', 'video', 'music', 'info', 'chat'\n"
                        "- query: optimized English or Russian search query for the best result\n\n"
                        "Classification rules (apply in order):\n"
                        "1. Use 'images' when user wants: photos, pictures, diagrams, step-by-step photo "
                        "instructions, schemes, illustrations, how-to images, instruction pictures.\n"
                        "   Examples: 'пришли инструкцию в картинках', 'покажи фото', 'как это выглядит', "
                        "'схема подключения', 'пошаговые фото'.\n"
                        "2. Use 'video' when user wants: a video, tutorial video, video instructions, "
                        "how-to video, video guide, youtube video, video lesson.\n"
                        "   Examples: 'пришли видео инструкцию', 'найди ролик', 'видеоурок', 'покажи видео'.\n"
                        "3. Use 'music' when user wants: a song, music track, soundtrack, OST from a "
                        "movie/game/show/series/anime, background music, theme song.\n"
                        "   Examples: 'музыка из игры', 'саундтрек фильма', 'пришли трек', "
                        "'песня из сериала', 'OST', 'найди музыку'.\n"
                        "   For music from movie/game/show: always include the media name. "
                        "If a specific song title is mentioned, use 'Artist - Song Title'. "
                        "If no specific song is mentioned, use 'Game/Movie Name song official audio'.\n"
                        "4. Use 'info' when user wants factual information, explanations, history, "
                        "biography, science, news, definitions, or any knowledge search.\n"
                        "   Examples: 'найди информацию о', 'что такое', 'кто такой', 'расскажи о', "
                        "'как работает', 'история', 'факты о', 'tell me about', 'what is', 'who is'.\n"
                        "   For info: query should be a clear, concise English or Russian search query.\n"
                        "5. Use 'chat' for everything else (jokes, creative writing, personal chat, opinions).\n\n"
                        "Query optimization rules:\n"
                        "- For images: make it descriptive for image search, add 'step by step' if instructional.\n"
                        "- For video: add 'tutorial' or 'how to' if instructional.\n"
                        "- For music from game (no specific song): use 'Game Name best song official audio'. Never add 'full soundtrack' or 'compilation'.\n"
                        "- For music from movie (no specific song): use 'Movie Name theme song official audio'.\n"
                        "- For info: keep the query focused on the topic, translate to English for better results.\n"
                        "- Keep the query in the same language as the user or translate to English for better results.\n\n"
                        "Return ONLY valid JSON, no markdown, no explanation:\n"
                        "{\"intent\": \"images\", \"query\": \"how to replace kitchen faucet step by step\"}"
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


def search_images(query: str, max_results: int = 6) -> list[bytes]:
    """Search DuckDuckGo for images and return list of raw image bytes."""
    try:
        from ddgs import DDGS as _DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS as _DDGS
        except ImportError:
            return []

    collected: list[bytes] = []
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }
    try:
        ddgs = _DDGS()
        results = ddgs.images(query, max_results=max_results * 4)
        for r in results:
            url = r.get("image") or r.get("url")
            if not url:
                continue
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = resp.read()
                if len(data) < 2000:
                    continue
                # Validate it's a real image
                try:
                    Image.open(io.BytesIO(data)).verify()
                except Exception:
                    continue
                collected.append(data)
                if len(collected) >= max_results:
                    break
            except Exception:
                continue
    except Exception:
        pass
    return collected


def search_web_info(text: str) -> tuple[str | None, str]:
    """
    Search DuckDuckGo text for factual information, synthesize a structured answer via Groq.
    Returns (answer_text or None, image_search_query or "").
    image_search_query is non-empty only when the topic benefits from visual illustration.
    """
    try:
        try:
            from ddgs import DDGS as _DDGS
        except ImportError:
            from duckduckgo_search import DDGS as _DDGS

        ddgs = _DDGS()
        results = list(ddgs.text(text, max_results=6))
        if not results:
            return None, ""

        snippets = "\n\n".join(
            f"[{r.get('title', '')}]\n{r.get('body', '')}"
            for r in results[:5]
        )

        resp = groq_client.chat.completions.create(
            model="moonshotai/kimi-k2-instruct-0905",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a precise fact-checker and knowledge assistant. "
                        "Use ONLY the provided search snippets to compose a truthful, well-structured answer. "
                        "Never hallucinate — if the snippets don't contain enough info, say so briefly.\n\n"
                        "Formatting (Telegram Markdown — use * for bold, _ for italic):\n"
                        "• Start with a bold title: *Topic Name*\n"
                        "• Write a short intro paragraph\n"
                        "• Use bullet points (•) for key facts or lists\n"
                        "• Add sub-sections with *bold headers* when the topic has distinct parts\n"
                        "• Be concise but informative (200-400 words max)\n"
                        "• Respond in the SAME language as the user's question\n\n"
                        "At the very end, on a new line, write exactly:\n"
                        "IMAGE_QUERY: <short English image search query for this topic, or NONE if no image is useful>\n"
                        "Use NONE for: abstract concepts, math, opinions, programming.\n"
                        "Use an image query for: people, places, animals, science, technology, historical events, objects."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Question: {text}\n\nSearch results:\n{snippets}",
                },
            ],
            max_tokens=1500,
            temperature=0.1,
        )

        full = resp.choices[0].message.content.strip()

        # Extract IMAGE_QUERY line
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


def search_video_yt(query: str) -> tuple[bytes, str]:
    """Search YouTube for the query and download the first result video."""
    with tempfile.TemporaryDirectory() as tmpdir:
        last_err = None
        for client in _YT_CLIENTS:
            opts = _base_opts(tmpdir, {
                "format": "best[ext=mp4][filesize<50M]/best[ext=mp4]/best[filesize<50M]/best",
                "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
                "extractor_args": {"youtube": {"player_client": [client]}},
                "noplaylist": True,
            })
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(f"ytsearch1:{query}", download=True)
                if info and "entries" in info:
                    info = info["entries"][0]
                filepath = _find_file(tmpdir)
                size = os.path.getsize(filepath)
                if size > MAX_FILE_SIZE:
                    raise ValueError(
                        f"Видео слишком большое ({size // (1024*1024)} МБ). Лимит — 50 МБ."
                    )
                with open(filepath, "rb") as f:
                    return f.read(), info.get("title", query) if isinstance(info, dict) else query
            except yt_dlp.utils.DownloadError as e:
                last_err = e
                if _is_bot_block(e):
                    continue
                raise RuntimeError(str(e)[:300]) from e
    raise RuntimeError(
        f"Не удалось найти видео «{query}». Попробуй другой запрос."
    ) from last_err


def _download_raw(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


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


# ===== BACKGROUND REMOVAL =====

def remove_background(image_bytes: bytes) -> bytes:
    input_image = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    output_image = rembg_remove(input_image)
    buf = io.BytesIO()
    output_image.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()


# ===== PHOTO UPSCALE x3 =====

UPSCALE_RE = re.compile(r'улучши\s*фото', re.IGNORECASE)

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
        "📥 Для скачивания видео — отправь ссылку на YouTube или Instagram.\n"
        "Например: https://youtube.com/watch?v=..."
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
        # Single photo, no size → remove background
        msg = await message.reply_text("✂️ Удаляю фон...")
        try:
            file = await context.bot.get_file(photo.file_id)
            dl = io.BytesIO()
            await file.download_to_memory(dl)
            loop = asyncio.get_running_loop()
            result_bytes = await loop.run_in_executor(None, remove_background, dl.getvalue())
            await message.reply_document(
                document=io.BytesIO(result_bytes),
                filename="no_background.png",
                caption="✅ Фон удалён",
            )
            await msg.delete()
        except Exception as e:
            await msg.edit_text(f"⚠ Ошибка: {e}")


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


_YT_CLIENTS = ["android", "ios", "tv_embedded", "web"]


def _base_opts(tmpdir, extra=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    if extra:
        opts.update(extra)
    if YOUTUBE_COOKIES:
        cookies_path = os.path.join(tmpdir, "cookies.txt")
        with open(cookies_path, "w") as f:
            f.write(YOUTUBE_COOKIES)
        opts["cookiefile"] = cookies_path
    return opts


def _is_bot_block(exc):
    msg = str(exc).lower()
    return "sign in" in msg or "bot" in msg or "confirm your age" in msg or "429" in msg


def download_video(url):
    with tempfile.TemporaryDirectory() as tmpdir:
        last_err = None
        for client in _YT_CLIENTS:
            opts = _base_opts(tmpdir, {
                "format": "best[ext=mp4][filesize<50M]/best[ext=mp4]/best[filesize<50M]/best",
                "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
                "extractor_args": {"youtube": {"player_client": [client]}},
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
                last_err = e
                if _is_bot_block(e):
                    continue  # try next client
                raise RuntimeError(str(e)[:300]) from e
        raise RuntimeError(
            "YouTube заблокировал скачивание на всех клиентах.\n"
            "Добавь куки браузера через переменную YOUTUBE_COOKIES."
        ) from last_err


# ===== YOUTUBE SHORTS SEARCH =====

_SHORTS_FORMAT = (
    "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]"
    "/bestvideo[ext=mp4]+bestaudio"
    "/bestvideo+bestaudio"
    "/best[ext=mp4]"
    "/best"
)


def _yt_search_entries(query: str, count: int = 20):
    """Return flat list of YouTube search entries without downloading."""
    search_url = f"ytsearch{count}:{query}"
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
    }
    # pass cookies if configured
    if YOUTUBE_COOKIES:
        import tempfile as _tf
        _tmp = _tf.mktemp(suffix=".txt")
        with open(_tmp, "w") as _f:
            _f.write(YOUTUBE_COOKIES)
        opts["cookiefile"] = _tmp
    with yt_dlp.YoutubeDL(opts) as ydl:
        results = ydl.extract_info(search_url, download=False)
    if not results or "entries" not in results:
        return []
    return [e for e in results["entries"] if e and e.get("id")]


def _yt_download_one(video_id: str, tmpdir: str, client: str) -> tuple[bytes, str]:
    """Download a single YouTube video by ID, return (bytes, title)."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    opts = _base_opts(tmpdir, {
        "format": _SHORTS_FORMAT,
        "outtmpl": os.path.join(tmpdir, f"{video_id}.%(ext)s"),
        "merge_output_format": "mp4",
        "extractor_args": {"youtube": {"player_client": [client]}},
        "quiet": True,
        "no_warnings": True,
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    filepath = _find_file(tmpdir, preferred_ext=".mp4")
    size = os.path.getsize(filepath)
    if size > MAX_FILE_SIZE:
        raise ValueError(f"Видео слишком большое ({size // (1024*1024)} МБ). Лимит — 50 МБ.")
    with open(filepath, "rb") as f:
        return f.read(), info.get("title", video_id)


_AUTH_ERRORS = ("sign in", "confirm your age", "inappropriate for some users", "age-restricted")


# ===== CANDIDATE SEARCH HELPERS =====

def _search_shorts_candidates(query: str) -> list:
    """Return sorted list of YouTube Shorts candidate entry dicts."""
    all_entries: list = []
    for search_q in [f"{query} #shorts", f"{query} shorts"]:
        found = _yt_search_entries(search_q, count=20)
        for e in found:
            if e.get("id") and e["id"] not in {x["id"] for x in all_entries}:
                all_entries.append(e)
        if len(all_entries) >= 20:
            break
    def _sort_key(e):
        d = e.get("duration")
        if d is None:
            return 1
        return 0 if d <= 60 else 2
    return sorted(all_entries, key=_sort_key)


def _dl_short_by_entry(entry: dict) -> tuple[bytes, str] | None:
    """Download a single Shorts entry, return (bytes, title) or None on failure."""
    vid_id = entry.get("id")
    if not vid_id:
        return None
    with tempfile.TemporaryDirectory() as tmpdir:
        for client in _YT_CLIENTS:
            try:
                data, title = _yt_download_one(vid_id, tmpdir, client)
                return data, title
            except yt_dlp.utils.DownloadError as e:
                if any(k in str(e).lower() for k in _AUTH_ERRORS):
                    break
            except Exception:
                continue
    return None


def _search_tiktok_candidates(query: str) -> list:
    """Return list of TikTok video dicts from tikwm API."""
    encoded = urllib.parse.quote(query)
    api_url = (
        f"https://www.tikwm.com/api/feed/search"
        f"?keywords={encoded}&count=20&cursor=0&web=1&hd=1"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Referer": "https://www.tikwm.com/",
    }
    try:
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return (data.get("data") or {}).get("videos") or []
    except Exception:
        return []


def _dl_tiktok_by_video(video: dict) -> tuple[bytes, str] | None:
    """Download a single TikTok video dict, return (bytes, caption) or None."""
    def _make_url(raw: str) -> str:
        if not raw:
            return ""
        if raw.startswith("http"):
            return raw
        return "https://www.tikwm.com" + (raw if raw.startswith("/") else "/" + raw)

    play_url = _make_url(video.get("play") or video.get("wmplay") or "")
    if not play_url:
        return None
    title = (video.get("title") or video.get("content_desc") or video.get("desc") or "").strip()
    author = (video.get("author") or {}).get("nickname") or ""
    caption = f"🎵 {title[:200]}" + (f"\n👤 @{author}" if author else "")
    try:
        req = urllib.request.Request(play_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.tikwm.com/",
        })
        with urllib.request.urlopen(req, timeout=40) as r:
            video_bytes = r.read()
        if 50_000 <= len(video_bytes) <= MAX_FILE_SIZE:
            return video_bytes, caption
    except Exception:
        pass
    return None


def _search_yt_video_candidates(query: str, count: int = 8) -> list:
    """Return list of YouTube video entry dicts (flat, no download)."""
    return _yt_search_entries(query, count=count)


def _dl_yt_video_by_entry(entry: dict) -> tuple[bytes, str] | None:
    """Download a YouTube video entry, return (bytes, title) or None."""
    vid_id = entry.get("id")
    if not vid_id:
        return None
    url = f"https://www.youtube.com/watch?v={vid_id}"
    with tempfile.TemporaryDirectory() as tmpdir:
        last_err = None
        for client in _YT_CLIENTS:
            opts = _base_opts(tmpdir, {
                "format": "best[ext=mp4][filesize<50M]/best[ext=mp4]/best[filesize<50M]/best",
                "outtmpl": os.path.join(tmpdir, f"{vid_id}.%(ext)s"),
                "extractor_args": {"youtube": {"player_client": [client]}},
            })
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                filepath = _find_file(tmpdir)
                size = os.path.getsize(filepath)
                if size > MAX_FILE_SIZE:
                    return None
                with open(filepath, "rb") as f:
                    data = f.read()
                title = info.get("title", vid_id) if isinstance(info, dict) else vid_id
                return data, title
            except yt_dlp.utils.DownloadError as e:
                last_err = e
                if _is_bot_block(e):
                    continue
                break
            except Exception as e:
                last_err = e
    return None


def _search_music_candidates(query: str, count: int = 8) -> list:
    """Return list of YouTube entry dicts suitable for music download."""
    return _yt_search_entries(f"{query} audio", count=count)


def _dl_music_by_entry(entry: dict) -> tuple[bytes, str, str] | None:
    """Download music for a YouTube entry, return (bytes, title, artist) or None."""
    vid_id = entry.get("id")
    if not vid_id:
        return None
    url = f"https://www.youtube.com/watch?v={vid_id}"
    with tempfile.TemporaryDirectory() as tmpdir:
        for client in _YT_CLIENTS:
            opts = _audio_opts(tmpdir, {
                "extractor_args": {"youtube": {"player_client": [client]}},
            })
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                return _read_audio_result(tmpdir, info, url)
            except Exception:
                continue
    return None


def search_and_download_shorts(query: str) -> tuple[bytes, str]:
    """Search YouTube Shorts by keyword and download the first working result."""
    # Try two search queries: with #shorts hashtag and without
    all_entries: list = []
    for search_q in [f"{query} #shorts", f"{query} shorts"]:
        found = _yt_search_entries(search_q, count=20)
        for e in found:
            if e.get("id") and e["id"] not in {x["id"] for x in all_entries}:
                all_entries.append(e)
        if len(all_entries) >= 20:
            break

    if not all_entries:
        raise RuntimeError(
            f"YouTube не вернул результатов по запросу «{query}».\n"
            "Попробуй другое название."
        )

    # Sort: prefer true Shorts (duration ≤ 60 s); put unknowns (None) in middle
    def _sort_key(e):
        d = e.get("duration")
        if d is None:
            return 1          # unknown — might be a short
        return 0 if d <= 60 else 2

    candidates = sorted(all_entries, key=_sort_key)[:10]

    last_err: Exception | None = None
    for entry in candidates:
        vid_id = entry["id"]
        with tempfile.TemporaryDirectory() as tmpdir:
            skip_video = False
            for client in _YT_CLIENTS:
                if skip_video:
                    break
                try:
                    data, title = _yt_download_one(vid_id, tmpdir, client)
                    return data, title
                except yt_dlp.utils.DownloadError as e:
                    last_err = e
                    err_s = str(e).lower()
                    if any(k in err_s for k in _AUTH_ERRORS):
                        skip_video = True   # age/auth — no client will fix it
                    # else: try next client (format/network issues may be client-specific)
                except Exception as e:
                    last_err = e
                    # unexpected error — try next client

    raise RuntimeError(
        f"YouTube не отдаёт шортс по запросу «{query}».\n"
        "Попробуй другое название или добавь куки через YOUTUBE_COOKIES."
    ) from last_err


# ===== TIKTOK SEARCH =====

def search_and_download_tiktok(query: str) -> tuple[bytes, str]:
    """Search TikTok videos by keyword via tikwm.com API and download best result."""
    import urllib.parse
    encoded = urllib.parse.quote(query)
    api_url = (
        f"https://www.tikwm.com/api/feed/search"
        f"?keywords={encoded}&count=10&cursor=0&web=1&hd=1"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Referer": "https://www.tikwm.com/",
    }
    req = urllib.request.Request(api_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        raise RuntimeError(f"Ошибка поиска TikTok: {e}") from e

    videos = (data.get("data") or {}).get("videos") or []
    if not videos:
        raise RuntimeError(f"Ничего не найдено в TikTok по запросу «{query}».")

    def _make_full_url(raw: str) -> str:
        if not raw:
            return ""
        if raw.startswith("http"):
            return raw
        if raw.startswith("/"):
            return "https://www.tikwm.com" + raw
        return "https://www.tikwm.com/" + raw

    last_err: Exception | None = None
    for video in videos[:8]:
        play_url = _make_full_url(video.get("play") or video.get("wmplay") or "")
        title = (video.get("title") or video.get("content_desc") or video.get("desc") or query).strip()
        author = (video.get("author") or {}).get("nickname") or ""
        caption = f"🎵 {title[:200]}" + (f"\n👤 @{author}" if author else "")
        if not play_url:
            continue
        try:
            dl_req = urllib.request.Request(play_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://www.tikwm.com/",
            })
            with urllib.request.urlopen(dl_req, timeout=40) as r:
                video_bytes = r.read()
            if len(video_bytes) < 50_000:
                continue
            if len(video_bytes) > MAX_FILE_SIZE:
                last_err = ValueError("Видео слишком большое. Лимит — 50 МБ.")
                continue
            return video_bytes, caption
        except Exception as e:
            last_err = e
            continue

    raise RuntimeError(
        f"Не удалось скачать видео из TikTok по запросу «{query}».\n"
        "Попробуй другое название."
    ) from last_err


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
        info = info["entries"][0]
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


def _find_short_yt_url(query: str, max_duration: int = 600) -> str | None:
    """Search YouTube for query, return URL of the first result under max_duration seconds."""
    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "noplaylist": False,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch10:{query}", download=False)
        if not info or "entries" not in info:
            return None
        for entry in (info.get("entries") or []):
            duration = entry.get("duration") or 0
            vid_id = entry.get("id", "")
            if vid_id and 20 < duration < max_duration:
                return f"https://www.youtube.com/watch?v={vid_id}"
    except Exception:
        pass
    return None


def download_music(query: str):
    """Search and download audio from multiple sources: YouTube → SoundCloud → Deezer."""
    last_err = None

    # 1a. YouTube — try to find an individual track (< 10 min) via metadata search first
    short_url = _find_short_yt_url(query, max_duration=600)
    yt_targets = []
    if short_url:
        yt_targets.append(short_url)        # specific short video URL
    yt_targets.append(f"ytsearch1:{query}")  # fallback: top result regardless of length

    for yt_target in yt_targets:
        with tempfile.TemporaryDirectory() as tmpdir:
            for client in _YT_CLIENTS:
                opts = _audio_opts(tmpdir, {
                    "extractor_args": {"youtube": {"player_client": [client]}},
                })
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(yt_target, download=True)
                    return _read_audio_result(tmpdir, info, query)
                except yt_dlp.utils.DownloadError as e:
                    last_err = e
                    if _is_bot_block(e):
                        continue   # try next client
                    break          # non-bot-block error: stop rotating clients, try next target
                except ValueError:
                    # File too large even after re-encoding — try next target
                    break
            # Always continue to the next target (short URL → ytsearch1:)

    # 2. SoundCloud
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            with yt_dlp.YoutubeDL(_audio_opts(tmpdir)) as ydl:
                info = ydl.extract_info(f"scsearch1:{query}", download=True)
            return _read_audio_result(tmpdir, info, query)
        except Exception as e:
            last_err = e

    # 3. Deezer
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            with yt_dlp.YoutubeDL(_audio_opts(tmpdir)) as ydl:
                info = ydl.extract_info(f"dzsearch1:{query}", download=True)
            return _read_audio_result(tmpdir, info, query)
        except Exception as e:
            last_err = e

    raise RuntimeError(
        f"Не удалось найти «{query}» ни на YouTube, SoundCloud, ни на Deezer.\n"
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


# ===== TELEGRAM PROXY =====

_PROXY_SOURCES = [
    "https://t.me/s/ProxyMTProto",
    "https://t.me/s/MTProxyT",
    "https://t.me/s/freeproxy_mtproto",
    "https://t.me/s/mtproto_proxy_list",
]

_PROXY_PATTERN = re.compile(r'https://t\.me/(?:proxy|socks)\?[^\s"\'<>]+')
_PROXY_PARAM_RE = re.compile(r'server=([^&\s]+).*?port=(\d+)', re.DOTALL)


def _check_proxy_port(proxy_url: str, timeout: float = 2.5) -> bool:
    """Return True if the proxy server:port is reachable via TCP."""
    m = _PROXY_PARAM_RE.search(proxy_url)
    if not m:
        return False
    server, port = m.group(1), int(m.group(2))
    try:
        with socket.create_connection((server, port), timeout=timeout):
            return True
    except Exception:
        return False


def fetch_telegram_proxies(max_results: int = 5, check_count: int = 40) -> list[str]:
    """
    Fetch MTProto/SOCKS5 proxy links from public Telegram channels,
    verify TCP reachability, return up to max_results working links.
    """
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    raw_links: list[str] = []

    def _scrape(url: str) -> list[str]:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as r:
                text = _html.unescape(_html.unescape(r.read().decode("utf-8", errors="ignore")))
            return _PROXY_PATTERN.findall(text)
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=len(_PROXY_SOURCES)) as ex:
        for links in ex.map(_scrape, _PROXY_SOURCES):
            raw_links.extend(links)

    # Deduplicate preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for link in raw_links:
        if link not in seen:
            seen.add(link)
            unique.append(link)

    candidates = unique[:check_count]
    working: list[str] = []

    with ThreadPoolExecutor(max_workers=20) as ex:
        futures = {ex.submit(_check_proxy_port, p): p for p in candidates}
        for f in as_completed(futures):
            if f.result():
                working.append(futures[f])
            if len(working) >= max_results:
                break

    return working[:max_results]


# ===== MUSIC RECOGNITION (SHAZAM-LIKE) =====

def convert_audio_to_mp3(audio_bytes: bytes) -> bytes:
    """Convert audio (OGG/OGA/WAV/etc.) to MP3 via ffmpeg."""
    import subprocess
    with tempfile.TemporaryDirectory() as tmpdir:
        in_path = os.path.join(tmpdir, "input.oga")
        out_path = os.path.join(tmpdir, "output.mp3")
        with open(in_path, "wb") as f:
            f.write(audio_bytes)
        subprocess.run(
            ["ffmpeg", "-y", "-i", in_path, "-ar", "44100", "-ac", "2", "-b:a", "128k", out_path],
            capture_output=True,
            check=True,
        )
        with open(out_path, "rb") as f:
            return f.read()


def recognize_with_audd(mp3_bytes: bytes) -> dict | None:
    """Recognize music via AudD free API (no key required)."""
    boundary = "----WebKitFormBoundaryAudd"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="audio"; filename="audio.mp3"\r\n'
        f"Content-Type: audio/mpeg\r\n\r\n"
    ).encode() + mp3_bytes + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        "https://api.audd.io/",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "Mozilla/5.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        if data.get("status") == "success" and data.get("result"):
            r = data["result"]
            return {"title": r.get("title", ""), "artist": r.get("artist", "")}
    except Exception:
        pass
    return None


async def recognize_with_shazam(mp3_bytes: bytes) -> dict | None:
    """Recognize music via Shazam (shazamio unofficial client)."""
    try:
        from shazamio import Shazam
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(mp3_bytes)
            tmp = f.name
        try:
            shazam = Shazam()
            out = await shazam.recognize(tmp)
            if out and out.get("matches"):
                track = out.get("track", {})
                title = track.get("title", "")
                artist = track.get("subtitle", "")
                if title:
                    return {"title": title, "artist": artist}
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass
    except Exception:
        pass
    return None


async def recognize_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voice/audio messages: recognise song via multiple services, then download it."""
    message = update.message
    voice = message.voice or message.audio
    if voice is None:
        return

    msg = await message.reply_text("🎵 Распознаю музыку…")
    loop = asyncio.get_running_loop()

    try:
        file = await context.bot.get_file(voice.file_id)
        buf = io.BytesIO()
        await file.download_to_memory(buf)
        raw_bytes = buf.getvalue()

        # Convert to MP3 (Telegram voice = OGG Opus)
        try:
            mp3_bytes = await loop.run_in_executor(None, convert_audio_to_mp3, raw_bytes)
        except Exception:
            mp3_bytes = raw_bytes

        # 1. Shazam (primary)
        result = await recognize_with_shazam(mp3_bytes)

        # 2. AudD fallback
        if not result:
            await msg.edit_text("🔍 Shazam не смог, пробую AudD…")
            result = await loop.run_in_executor(None, recognize_with_audd, mp3_bytes)

        if not result:
            await msg.edit_text(
                "😔 Не удалось распознать.\n\n"
                "*Советы:*\n"
                "• Запись 10–30 секунд без шума\n"
                "• Музыка должна быть чёткой\n"
                "• При напевании — пой ровно, без слов, просто мелодию",
                parse_mode="Markdown",
            )
            return

        title = result["title"]
        artist = result.get("artist", "")
        search_query = f"{artist} - {title}".strip(" -") if artist else title

        caption = f"✅ *{title}*"
        if artist:
            caption += f"\n👤 {artist}"

        await msg.edit_text(caption + "\n\n🎵 Скачиваю…", parse_mode="Markdown")

        try:
            audio_out, dl_title, dl_artist = await loop.run_in_executor(
                None, download_music, search_query
            )
            await message.reply_audio(
                audio=io.BytesIO(audio_out),
                filename=f"{search_query}.mp3",
                title=title,
                performer=artist or dl_artist,
                caption=caption,
                parse_mode="Markdown",
            )
            await msg.delete()
        except Exception as e:
            await msg.edit_text(
                caption + f"\n\n⚠ Не удалось скачать файл: {e}",
                parse_mode="Markdown",
            )

    except Exception as e:
        await msg.edit_text(f"⚠ Ошибка: {e}")


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
        user_id = update.message.from_user.id
        candidates = await loop.run_in_executor(None, _search_music_candidates, query)
        if candidates:
            user_more_cache[user_id] = {"type": "music", "query": query, "entries": candidates, "index": 1}
            result = await loop.run_in_executor(None, _dl_music_by_entry, candidates[0])
            if result:
                audio_bytes, title, artist = result
                await update.message.reply_audio(
                    audio=io.BytesIO(audio_bytes),
                    filename=f"{title}.mp3",
                    title=title,
                    performer=artist,
                    caption="💬 Напиши «еще» чтобы получить другой трек",
                )
                await msg.delete()
                return
        audio_bytes, title, artist = await loop.run_in_executor(None, download_music, query)
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
        "🤖 AI Bot\n\n"
        "Просто пиши — отвечаю как живой человек.\n\n"
        "🔍 «Что такое квантовая механика?» или «расскажи про Маска» — найду актуальную информацию в интернете и пришлю со структурой и картинками.\n"
        "🎤 Отправь голосовое с музыкой — распознаю песню и пришлю файлом (Shazam + AudD).\n"
        "🎤 Можешь напевать мелодию голосом — тоже попробую распознать.\n"
        "🖼 «Пришли инструкцию в картинках как заменить смеситель» — найду и пришлю фото.\n"
        "🎬 «Пришли видео инструкцию по замене масла в машине» — найду видео на YouTube.\n"
        "🎵 «Пришли музыку из фильма Интерстеллар» — найду и скачаю саундтрек.\n"
        "🎵 Напиши «музыка <артист - название>» — скачаю трек (YouTube, SoundCloud, Deezer).\n"
        "📥 Отправь ссылку Instagram или YouTube — скачаю видео.\n"
        "▶️ Напиши «шортс [название]» — найду и пришлю YouTube Shorts по ключевым словам.\n"
        "🎵 Напиши «тикток [название]» — найду и пришлю видео из TikTok по ключевым словам.\n"
        "🔁 Напиши «еще» после любого видео или музыки — пришлю следующий результат.\n"
        "🎧 Отправь ссылку VK / SoundCloud / Deezer — скачаю аудио.\n"
        "✂️ Отправь одно фото — удалю фон.\n"
        "🗜 Отправь фото с подписью «до 500кб» — сожму.\n"
        "📦 Отправь несколько фото с подписью «до 300кб» — сожму и пришлю архивом.\n"
        "🏷 Отправь .zip с картинками — переименую файлы по размеру в пикселях (напр. 240x400.jpg).\n"
        "📝 Отправь фото с подписью «процент текста» — покажу сколько % площади занимает текст.\n"
        "🔬 Отправь фото с подписью «улучши фото» — увеличу в 3x и пришлю файлом в высоком качестве.\n"
        "🔤 Напиши «шрифт <название>» — найду и пришлю бесплатный TTF-файл (например: шрифт Roboto).",
        reply_markup=main_keyboard()
    )


async def groq_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚡ Groq активирован", reply_markup=main_keyboard())



async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
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


# ===== "ЕЩЕ" HANDLER =====

async def _send_more_result(update, context, user_id: int) -> None:
    """Send the next cached result when user says 'еще'."""
    cache = user_more_cache.get(user_id)
    if not cache:
        await update.message.reply_text("Сначала сделай запрос, потом скажи «еще».")
        return

    kind = cache["type"]
    entries = cache["entries"]
    idx = cache["index"]
    query = cache["query"]
    loop = asyncio.get_running_loop()

    if idx >= len(entries):
        await update.message.reply_text(
            f"Больше результатов по запросу «{query}» нет.\n"
            "Попробуй другой запрос."
        )
        user_more_cache.pop(user_id, None)
        return

    msg = await update.message.reply_text(f"🔄 Ищу другой результат ({idx + 1}/{len(entries)})...")
    result = None

    # Advance index so repeated "еще" won't retry same entry on failure
    cache["index"] = idx + 1

    try:
        if kind == "shorts":
            result = await loop.run_in_executor(None, _dl_short_by_entry, entries[idx])
            if result:
                data, title = result
                await update.message.reply_video(
                    video=io.BytesIO(data),
                    caption=f"▶️ {title}",
                    supports_streaming=True,
                )
                await msg.delete()
            else:
                await msg.edit_text("Этот шортс не загружается. Напиши «еще» для следующего.")

        elif kind == "tiktok":
            result = await loop.run_in_executor(None, _dl_tiktok_by_video, entries[idx])
            if result:
                data, caption = result
                await update.message.reply_video(
                    video=io.BytesIO(data),
                    caption=caption,
                    supports_streaming=True,
                )
                await msg.delete()
            else:
                await msg.edit_text("Это видео недоступно. Напиши «еще» для следующего.")

        elif kind == "video":
            result = await loop.run_in_executor(None, _dl_yt_video_by_entry, entries[idx])
            if result:
                data, title = result
                await update.message.reply_video(
                    video=io.BytesIO(data),
                    caption=f"📹 {title}",
                    supports_streaming=True,
                )
                await msg.delete()
            else:
                await msg.edit_text("Видео недоступно. Напиши «еще» для следующего.")

        elif kind == "music":
            result = await loop.run_in_executor(None, _dl_music_by_entry, entries[idx])
            if result:
                audio_bytes, title, artist = result
                await update.message.reply_audio(
                    audio=io.BytesIO(audio_bytes),
                    filename=f"{title}.mp3",
                    title=title,
                    performer=artist,
                )
                await msg.delete()
            else:
                await msg.edit_text("Трек недоступен. Напиши «еще» для следующего.")
    except Exception as e:
        await msg.edit_text(f"⚠ Ошибка: {e}\nНапиши «еще» для следующего.")


# ===== CHAT =====

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text.strip()

    if not text:
        await update.message.reply_text("Отправь сообщение.")
        return

    # "Еще" — send next cached result
    if MORE_RE.match(text) and user_id in user_more_cache:
        await _send_more_result(update, context, user_id)
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
            candidates = await loop.run_in_executor(None, _search_music_candidates, query)
            if candidates:
                user_more_cache[user_id] = {"type": "music", "query": query, "entries": candidates, "index": 1}
                result = await loop.run_in_executor(None, _dl_music_by_entry, candidates[0])
                if result:
                    audio_bytes, title, artist = result
                    await update.message.reply_audio(
                        audio=io.BytesIO(audio_bytes),
                        filename=f"{title}.mp3",
                        title=title,
                        performer=artist,
                        caption="💬 Напиши «еще» чтобы получить другой трек",
                    )
                    await msg.delete()
                    return
            audio_bytes, title, artist = await loop.run_in_executor(None, download_music, query)
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

    # Check for TikTok search request
    tiktok_match = TIKTOK_SEARCH_RE.match(text)
    if tiktok_match:
        query = tiktok_match.group(1).strip()
        msg = await update.message.reply_text(f"🔍 Ищу в TikTok: «{query}»...")
        try:
            loop = asyncio.get_running_loop()
            candidates = await loop.run_in_executor(None, _search_tiktok_candidates, query)
            if not candidates:
                await msg.edit_text(f"Ничего не найдено в TikTok по запросу «{query}».")
                return
            user_more_cache[user_id] = {"type": "tiktok", "query": query, "entries": candidates, "index": 1}
            result = await loop.run_in_executor(None, _dl_tiktok_by_video, candidates[0])
            if result:
                data, caption = result
                await update.message.reply_video(
                    video=io.BytesIO(data),
                    caption=caption + "\n\n💬 Напиши «еще» чтобы получить другой результат",
                    supports_streaming=True,
                )
                await msg.delete()
            else:
                await msg.edit_text("Видео недоступно. Напиши «еще» для следующего.")
        except Exception as e:
            await msg.edit_text(f"⚠ {e}")
        return

    # Check for Shorts search request
    shorts_match = SHORTS_SEARCH_RE.match(text)
    if shorts_match:
        query = shorts_match.group(1).strip()
        msg = await update.message.reply_text(f"🔍 Ищу шортс: «{query}»...")
        try:
            loop = asyncio.get_running_loop()
            candidates = await loop.run_in_executor(None, _search_shorts_candidates, query)
            if not candidates:
                await msg.edit_text(f"YouTube не вернул результатов по запросу «{query}».")
                return
            user_more_cache[user_id] = {"type": "shorts", "query": query, "entries": candidates, "index": 1}
            result = await loop.run_in_executor(None, _dl_short_by_entry, candidates[0])
            if result:
                data, title = result
                await update.message.reply_video(
                    video=io.BytesIO(data),
                    caption=f"▶️ {title}\n\n💬 Напиши «еще» чтобы получить другой шортс",
                    supports_streaming=True,
                )
                await msg.delete()
            else:
                await msg.edit_text("Шортс не загрузился. Напиши «еще» для следующего.")
        except Exception as e:
            await msg.edit_text(f"⚠ {e}")
        return

    # Check for Instagram, YouTube or TikTok link
    ig_match = INSTAGRAM_RE.search(text)
    yt_match = YOUTUBE_RE.search(text)
    tt_match = TIKTOK_URL_RE.search(text)
    video_match = ig_match or yt_match or tt_match

    if video_match:
        url = video_match.group(0)
        if yt_match:
            source = "YouTube"
        elif ig_match:
            source = "Instagram"
        else:
            source = "TikTok"
        msg = await update.message.reply_text(f"📥 Скачиваю видео с {source}...")
        try:
            loop = asyncio.get_running_loop()
            video_bytes, title = await loop.run_in_executor(None, download_video, url)
            await update.message.reply_video(
                video=io.BytesIO(video_bytes),
                caption=f"📹 {title}",
                supports_streaming=True
            )
            await msg.delete()
        except Exception as e:
            await msg.edit_text(f"⚠ {e}")
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
                images = await loop.run_in_executor(None, search_images, query)
                # If nothing found with the primary query, try a simpler fallback
                if not images and query != text:
                    images = await loop.run_in_executor(None, search_images, text)
                if not images:
                    await msg.edit_text(
                        "😔 Не удалось найти картинки по этому запросу.\n"
                        "Попробуй сформулировать иначе или на английском."
                    )
                    return
                from telegram import InputMediaPhoto
                media_group = [
                    InputMediaPhoto(
                        media=io.BytesIO(img),
                        caption=f"🔎 {query}" if i == 0 else None,
                    )
                    for i, img in enumerate(images)
                ]
                await update.message.reply_media_group(media=media_group)
                await msg.delete()
                return

            if intent == "video":
                await msg.edit_text(f"🎬 Ищу видео: «{query}»…")
                try:
                    candidates = await loop.run_in_executor(None, _search_yt_video_candidates, query)
                    if candidates:
                        user_more_cache[user_id] = {"type": "video", "query": query, "entries": candidates, "index": 1}
                        result = await loop.run_in_executor(None, _dl_yt_video_by_entry, candidates[0])
                        if result:
                            data, title = result
                            await update.message.reply_video(
                                video=io.BytesIO(data),
                                caption=f"📹 {title}\n\n💬 Напиши «еще» чтобы получить другое видео",
                                supports_streaming=True,
                            )
                            await msg.delete()
                            return
                    video_bytes, title = await loop.run_in_executor(None, search_video_yt, query)
                    await update.message.reply_video(
                        video=io.BytesIO(video_bytes),
                        caption=f"📹 {title}",
                        supports_streaming=True,
                    )
                    await msg.delete()
                except Exception as e:
                    await msg.edit_text(f"⚠ Не нашёл видео: {e}")
                return

            if intent == "music":
                await msg.edit_text(f"🎵 Ищу музыку: «{query}»…")
                try:
                    candidates = await loop.run_in_executor(None, _search_music_candidates, query)
                    if candidates:
                        user_more_cache[user_id] = {"type": "music", "query": query, "entries": candidates, "index": 1}
                        result = await loop.run_in_executor(None, _dl_music_by_entry, candidates[0])
                        if result:
                            audio_bytes, title, artist = result
                            await update.message.reply_audio(
                                audio=io.BytesIO(audio_bytes),
                                filename=f"{title}.mp3",
                                title=title,
                                performer=artist,
                                caption="💬 Напиши «еще» чтобы получить другой трек",
                            )
                            await msg.delete()
                            return
                    audio_bytes, title, artist = await loop.run_in_executor(None, download_music, query)
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
                        media = [InputMediaPhoto(media=io.BytesIO(img)) for img in imgs]
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

    # ── Direct info search (что такое X, кто такой X, как работает X, etc.) ──
    if INFO_RE.search(text):
        loop = asyncio.get_running_loop()
        msg = await update.message.reply_text("🔍 Ищу актуальную информацию…")
        try:
            answer, image_query = await loop.run_in_executor(None, search_web_info, text)
            if answer:
                try:
                    await msg.edit_text(answer, parse_mode="Markdown")
                except Exception:
                    await msg.edit_text(answer)
                if image_query:
                    imgs = await loop.run_in_executor(None, search_images, image_query, 3)
                    if imgs:
                        from telegram import InputMediaPhoto
                        media = [InputMediaPhoto(media=io.BytesIO(img)) for img in imgs]
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


async def image_document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle images sent as files (uncompressed documents)."""
    doc = update.message.document
    if not doc.mime_type or not doc.mime_type.startswith("image/"):
        return

    caption = (update.message.caption or "").strip()

    if UPSCALE_RE.search(caption):
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
        msg = await update.message.reply_text("✂️ Удаляю фон...")
        try:
            file = await context.bot.get_file(doc.file_id)
            dl = io.BytesIO()
            await file.download_to_memory(dl)
            loop = asyncio.get_running_loop()
            result_bytes = await loop.run_in_executor(None, remove_background, dl.getvalue())
            await update.message.reply_document(
                document=io.BytesIO(result_bytes),
                filename="no_background.png",
                caption="✅ Фон удалён",
            )
            await msg.delete()
        except Exception as e:
            await msg.edit_text(f"⚠ Ошибка: {e}")


async def zip_rename_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc.file_name or not doc.file_name.lower().endswith(".zip"):
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
app.add_handler(MessageHandler(filters.VOICE, recognize_voice))
app.add_handler(MessageHandler(filters.AUDIO, recognize_voice))
app.add_handler(MessageHandler(filters.Document.FileExtension("zip"), zip_rename_handler))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
app.add_error_handler(error_handler)

print("🤖 Bot running...", flush=True)

app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
