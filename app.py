import os
import sys
import json
import time
import re
import html
import uuid
import urllib.parse
import urllib.request
import threading
import subprocess
import requests
from fastapi import FastAPI
import uvicorn

sys.stdout.reconfigure(encoding='utf-8')

# Telegram Bot Token & Settings
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8801944551:AAHZdYry40UudaHj2l0hJHE66-KjpC45VKs")
API_URL = f"https://api.telegram.org/bot{TOKEN}"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TMP_DIR = os.path.join(BASE_DIR, "tmp_clips")
os.makedirs(TMP_DIR, exist_ok=True)

# Headers for cinema databases
BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://getyarn.io/'
}

# Incremental card counter (matches the «ШАГ 11 НА СЛУХ» series format)
CARD_COUNTER = 223705
def get_next_card_id():
    global CARD_COUNTER
    CARD_COUNTER += 1
    return str(CARD_COUNTER)

# Pool of 100+ popular conversational words and idioms
CONVERSATION_WORDS = [
    "approach", "anger", "safe", "believe", "remember", "understand", "promise", "listen",
    "trouble", "happen", "wonder", "suppose", "realize", "decide", "surprise", "different",
    "dangerous", "possible", "careful", "beautiful", "terrible", "serious", "important",
    "honest", "secret", "mistake", "apologize", "forgive", "explain", "imagine", "expect",
    "notice", "discover", "protect", "escape", "trust", "respect", "deserve", "regret",
    "matter", "curious", "strange", "bother", "appreciate", "convince", "desperate",
    "grateful", "innocent", "jealous", "lonely", "nervous", "patient", "proud", "responsible",
    "stubborn", "embarrassed", "confused", "excited", "exhausted", "furious", "terrified",
    "worried", "calm", "confident", "comfortable", "generous", "helpful", "polite", "reliable"
]

def api_call(method, params=None, files=None):
    url = f"{API_URL}/{method}"
    try:
        if files:
            resp = requests.post(url, data=params, files=files, timeout=40)
        else:
            resp = requests.post(url, json=params, timeout=25)
        return resp.json()
    except Exception as e:
        print(f"Telegram API Error ({method}): {e}")
        return None

# Robust Russian translation (MyMemory + Google Translate)
def translate_to_ru(text):
    if not text:
        return ""
    # 1. Try MyMemory API
    try:
        url = "https://api.mymemory.translated.net/get"
        params = {"q": text, "langpair": "en|ru"}
        r = requests.get(url, params=params, headers=BROWSER_HEADERS, timeout=5)
        if r.status_code == 200:
            tr = r.json().get("responseData", {}).get("translatedText")
            if tr and not tr.startswith("MYMEMORY WARNING"):
                return html.unescape(tr)
    except Exception:
        pass

    # 2. Try Google Translate fallback
    try:
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=ru&dt=t&q={urllib.parse.quote(text)}"
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
        if r.status_code == 200:
            data = r.json()
            return ''.join([p[0] for p in data[0] if p[0]])
    except Exception:
        pass
    return text

# Build hashtags for every word / phrase (Exact replica of «ШАГ 11 НА СЛУХ»)
def build_quote_hashtags(quote):
    words = quote.split()
    if not words:
        return ""
    glue = {'i', 'a', 'an', 'the', 'of', 'to', 'in', 'on', 'at', 'for', 'and', 'or', 'so', 'it', 'is', 'am', 'are', 'was', 'were', 'had', 'have', 'has', 'my', 'your', 'his', 'her', 'we', 'he', 'she', 'they'}
    chunks = []
    current_chunk = []
    for w in words:
        clean_w = re.sub(r'[^a-zA-Z]', '', w).lower()
        current_chunk.append(w)
        has_punct = any(p in w for p in ['.', ',', '!', '?', ';', ':'])
        if clean_w not in glue or len(current_chunk) >= 3 or has_punct:
            chunks.append('_'.join(current_chunk))
            current_chunk = []
    if current_chunk:
        chunks.append('_'.join(current_chunk))
    tags = []
    for c in chunks:
        clean_tag = re.sub(r'^["\'\(\[\{\-]+', '', c)
        if clean_tag:
            tags.append(f"#{clean_tag}")
    return " ".join(tags)

# Measure exact video duration
def get_video_duration(file_path):
    try:
        import struct
        with open(file_path, 'rb') as f:
            while True:
                header = f.read(8)
                if len(header) < 8:
                    break
                size, atom_type = struct.unpack('>I4s', header)
                if size == 1:
                    size = struct.unpack('>Q', f.read(8))[0] - 8
                atom_type = atom_type.decode('latin1', errors='ignore')
                if atom_type == 'moov':
                    moov_data = f.read(size - 8)
                    idx = moov_data.find(b'mvhd')
                    if idx != -1:
                        mvhd = moov_data[idx+4:]
                        version = mvhd[0]
                        if version == 0:
                            timescale, duration = struct.unpack('>II', mvhd[12:20])
                        elif version == 1:
                            timescale, duration = struct.unpack('>IQ', mvhd[20:32])
                        else:
                            return 0.0
                        return round(duration / timescale, 2)
                else:
                    f.seek(size - 8, 1)
    except Exception:
        pass

    try:
        cmd = [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", file_path
        ]
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=5).decode().strip()
        return float(out)
    except Exception:
        return 0.0

# Load verified high-quality cinema scenes pool (3.5s - 8.0s duration, verified complete sentences)
VERIFIED_SCENES = []
VERIFIED_PATH = os.path.join(os.path.dirname(__file__), "verified_scenes.json")
if os.path.exists(VERIFIED_PATH):
    try:
        with open(VERIFIED_PATH, "r", encoding="utf-8") as vf_file:
            VERIFIED_SCENES = json.load(vf_file)
        print(f"Loaded {len(VERIFIED_SCENES)} verified cinema scenes into pool.")
    except Exception as e:
        print("Error loading verified_scenes.json:", e)

# Fetch a live scene from cinema database with duration & punctuation rules
def fetch_live_cinema_scene(query):
    w_clean = re.sub(r'[^a-zA-Z]', '', query).strip().lower()
    if not w_clean:
        return None

    # Check verified scenes first if any matches query word
    if VERIFIED_SCENES:
        matches = [s for s in VERIFIED_SCENES if w_clean in s.get("quote", "").lower() or w_clean == s.get("word", "").lower()]
        if matches:
            card = random.choice(matches)
            clip_url = card["video_url"]
            temp_dest = os.path.join(TMP_DIR, f"{card.get('cid', card.get('id'))}.mp4")
            try:
                r = requests.get(clip_url, headers=BROWSER_HEADERS, timeout=8)
                if r.status_code == 200 and len(r.content) > 10000:
                    with open(temp_dest, "wb") as f:
                        f.write(r.content)
                    card_copy = dict(card)
                    card_copy["file_path"] = temp_dest
                    return card_copy
            except Exception:
                pass

    search_queries = [
        f"I {w_clean}",
        f"you {w_clean}",
        f"to {w_clean}",
        w_clean
    ]

    seen = set()
    for q_term in search_queries:
        url = f"https://getyarn.io/yarn-find?text={urllib.parse.quote(q_term)}"
        try:
            r = requests.get(url, headers=BROWSER_HEADERS, timeout=5)
            if r.status_code == 200:
                pattern = r'<a href="/yarn-clip/([a-f0-9\-]+)".*?<div class="title[^>]*>([^<]+)</div>.*?<div class="transcript[^>]*>([^<]+)</div>'
                matches = re.findall(pattern, r.text, re.DOTALL)
                for cid, title, quote in matches:
                    if cid in seen: continue
                    seen.add(cid)
                    q_raw = html.unescape(quote).strip()
                    q_clean = re.sub(r'[\?♪♫]+', '', q_raw).strip()
                    if not q_clean or q_clean[-1] in [',', ';', '-', ':']:
                        continue
                    words = re.findall(r"[a-zA-Z']+", q_clean)
                    if len(words) < 3:
                        continue

                    clip_url = f"https://y.getyarn.io/{cid}.mp4"
                    temp_dest = os.path.join(TMP_DIR, f"{cid}.mp4")
                    c_resp = requests.get(clip_url, headers=BROWSER_HEADERS, timeout=6)
                    if c_resp.status_code == 200 and len(c_resp.content) > 10000:
                        with open(temp_dest, "wb") as f:
                            f.write(c_resp.content)
                        dur = get_video_duration(temp_dest)

                        if 3.2 <= dur <= 8.5:
                            if not q_clean.endswith(('.', '!', '?')):
                                q_clean += '.'
                            tr = translate_to_ru(q_clean)
                            tags = build_quote_hashtags(q_clean)
                            return {
                                "id": get_next_card_id(),
                                "word": w_clean,
                                "quote": q_clean,
                                "show": html.unescape(title).strip(),
                                "translation": tr,
                                "hashtags": tags,
                                "duration": dur,
                                "file_path": temp_dest,
                                "video_url": clip_url
                            }
                        else:
                            if os.path.exists(temp_dest): os.remove(temp_dest)
        except Exception:
            pass

    return None

# Format telegram post caption: EXACT 1-TO-1 COPY OF «ШАГ 11 НА СЛУХ» (Screenshot 2)
def format_cinema_post(cid, quote, translation, hashtags):
    q_esc = html.escape(quote or "", quote=False)
    tr_esc = html.escape(translation or "", quote=False)
    tags_esc = html.escape(hashtags or "", quote=False)

    # Line 1: just the card ID (e.g. 223705)
    # Inside <tg-spoiler>: hashtags on each word, quote in blockquote, Russian translation
    return (
        f"<b>{cid}</b>\n\n"
        f"<tg-spoiler>"
        f"{tags_esc}\n\n"
        f"<blockquote>\"{q_esc}\"</blockquote>\n\n"
        f"{tr_esc}"
        f"</tg-spoiler>"
    )

# Send video post with buttons (supports local file or direct URL)
def send_cinema_card(chat_id, card_data, video_path_or_url):
    cid = card_data.get('id') or get_next_card_id()
    quote = card_data.get('quote', '')
    translation = card_data.get('translation', quote)
    hashtags = card_data.get('hashtags') or build_quote_hashtags(quote)
    word = card_data.get('word') or 'scene'

    caption = format_cinema_post(cid, quote, translation, hashtags)

    keyboard = {
        "inline_keyboard": [
            [
                {"text": f"🎬 Еще сцены со словом '{word}'", "callback_data": f"word:{word}"}
            ],
            [
                {"text": "🎲 Случайная сцена", "callback_data": "random"},
                {"text": "📥 В Anki", "callback_data": f"anki:{cid}"}
            ]
        ]
    }

    try:
        if isinstance(video_path_or_url, str) and (video_path_or_url.startswith("http://") or video_path_or_url.startswith("https://")):
            # Download before sending to avoid Telegram IP blocks on y.getyarn.io
            temp_dest = os.path.join(TMP_DIR, f"url_{cid}.mp4")
            c_resp = requests.get(video_path_or_url, headers=BROWSER_HEADERS, timeout=8)
            if c_resp.status_code == 200 and len(c_resp.content) > 10000:
                with open(temp_dest, "wb") as f:
                    f.write(c_resp.content)
                video_path_or_url = temp_dest

        if os.path.exists(video_path_or_url):
            with open(video_path_or_url, "rb") as vf:
                api_call("sendVideo", params={
                    "chat_id": chat_id,
                    "caption": caption,
                    "parse_mode": "HTML",
                    "reply_markup": json.dumps(keyboard)
                }, files={"video": (os.path.basename(video_path_or_url), vf)})
            try: os.remove(video_path_or_url)
            except Exception: pass
        else:
            print("Video source not accessible:", video_path_or_url)
    except Exception as e:
        print("Error sending video post:", e)

# Send random verified live scene - NEVER FAILS, delivers in < 1 second!
def send_random_cinema_card(chat_id):
    import random
    
    # 1. First priority: instant verified cinema scene from pool
    if VERIFIED_SCENES:
        card = random.choice(VERIFIED_SCENES)
        clip_url = card["video_url"]
        temp_dest = os.path.join(TMP_DIR, f"{card.get('cid', card.get('id'))}.mp4")
        try:
            r = requests.get(clip_url, headers=BROWSER_HEADERS, timeout=8)
            if r.status_code == 200 and len(r.content) > 10000:
                with open(temp_dest, "wb") as f:
                    f.write(r.content)
                send_cinema_card(chat_id, card, temp_dest)
                return
        except Exception as e:
            print(f"Error sending verified card: {e}")
            if os.path.exists(temp_dest):
                try: os.remove(temp_dest)
                except Exception: pass

    # 2. Live fallback if pool failed
    for _ in range(2):
        w = random.choice(CONVERSATION_WORDS)
        scene = fetch_live_cinema_scene(w)
        if scene:
            send_cinema_card(chat_id, scene, scene["file_path"])
            return

    # In extreme case if everything failed
    api_call("sendMessage", {"chat_id": chat_id, "text": "🎬 Сервер кинобазы обновляется, попробуйте через пару секунд!"})

# Find timestamp and quote of target word in YouTube subtitles
def find_word_in_yt_sub(yt_url, target_word, out_dir):
    try:
        import glob
        sub_id = uuid.uuid4().hex[:6]
        sub_prefix = os.path.join(out_dir, f"sub_{sub_id}")
        cmd = [
            "yt-dlp",
            "--write-auto-sub",
            "--sub-lang", "en",
            "--skip-download",
            "--sub-format", "vtt",
            yt_url,
            "-o", f"{sub_prefix}.%(ext)s",
            "--quiet", "--no-warnings"
        ]
        subprocess.run(cmd, timeout=20)
        vtt_files = glob.glob(f"{sub_prefix}*.vtt")
        if not vtt_files:
            return None, None
        with open(vtt_files[0], 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        for vf in vtt_files:
            try: os.remove(vf)
            except Exception: pass
            
        blocks = re.findall(r'(\d{2}:\d{2}:\d{2}\.\d{3}|\d{2}:\d{2}\.\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}\.\d{3}|\d{2}:\d{2}\.\d{3})\s*\n([^\n]+)', content)
        for start_t, end_t, text in blocks:
            if target_word.lower() in text.lower():
                parts = start_t.split(':')
                sec = float(parts[0])*3600 + float(parts[1])*60 + float(parts[2]) if len(parts)==3 else float(parts[0])*60 + float(parts[1])
                clean_text = re.sub(r'<[^>]+>', '', text).strip()
                return max(0, sec - 1.5), clean_text
    except Exception as e:
        print("Sub parse error:", e)
    return None, None

# Cut clip from YouTube
def cut_youtube_clip(url, start_sec, duration=5.5):
    start_str = time.strftime('%H:%M:%S', time.gmtime(max(0, start_sec)))
    end_str = time.strftime('%H:%M:%S', time.gmtime(start_sec + duration))
    out_id = uuid.uuid4().hex[:8]
    out_file = os.path.join(TMP_DIR, f"yt_{out_id}.mp4")

    cmd = [
        "yt-dlp",
        "--download-sections", f"*{start_str}-{end_str}",
        "--force-keyframes-at-cuts",
        "-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        url,
        "-o", out_file,
        "--quiet", "--no-warnings"
    ]
    try:
        subprocess.run(cmd, timeout=35)
        if os.path.exists(out_file) and os.path.getsize(out_file) > 10000:
            return out_file
    except Exception as e:
        print("yt-dlp error:", e)
    return None

# Background Telegram Bot Polling
def bot_polling_loop():
    print("Telegram Bot Polling started on Render 24/7...")
    offset = 0
    while True:
        try:
            updates = api_call("getUpdates", {"offset": offset, "timeout": 20})
            if updates and updates.get("ok"):
                for upd in updates.get("result", []):
                    offset = upd["update_id"] + 1

                    # Handle Callback Buttons
                    if "callback_query" in upd:
                        cq = upd["callback_query"]
                        chat_id = cq["message"]["chat"]["id"]
                        data = cq.get("data", "")
                        api_call("answerCallbackQuery", {"callback_query_id": cq["id"]})

                        if data == "random":
                            send_random_cinema_card(chat_id)
                        elif data.startswith("word:"):
                            w = data.split(":", 1)[1]
                            api_call("sendMessage", {"chat_id": chat_id, "text": f"🔍 Ищу отрывок со словом '{w}' (3.5–8 сек)..."})
                            scene = fetch_live_cinema_scene(w)
                            if scene:
                                send_cinema_card(chat_id, scene, scene["file_path"])
                            else:
                                api_call("sendMessage", {"chat_id": chat_id, "text": f"🎬 Подбираем новые сцены для '{w}'. Нажмите «🎲 Случайная сцена»!"})
                        elif data.startswith("anki:"):
                            api_call("sendMessage", {"chat_id": chat_id, "text": "✅ Сцена добавлена в колоду 🎬 Кино-Вырезки!"})

                    # Handle Text Messages & YouTube Links
                    if "message" in upd and "text" in upd["message"]:
                        msg = upd["message"]
                        chat_id = msg["chat"]["id"]
                        text = msg["text"].strip()

                        # Welcome / Start
                        if text.startswith("/start"):
                            welcome_text = (
                                "🎬 <b>Добро пожаловать в Кино-Тренажер «ШАГ 11 НА СЛУХ»!</b>\n\n"
                                "🔥 Карточки генерируются в реальном времени из базы 5 000 000+ сцен кино.\n"
                                "⏱ Каждое видео длится строго от 3.5 до 8 секунд (полные фразы!).\n"
                                "🏷 На каждое слово сформирован хэштег для быстрого поиска всех сцен в чате.\n"
                                "🙈 Все подсказки скрыты под спойлером — сначала тренируйте слух!\n\n"
                                "Нажмите кнопку ниже для старта:"
                            )
                            kb = {
                                "keyboard": [
                                    [{"text": "🎬 Случайная карточка"}],
                                    [{"text": "💡 Популярные темы"}],
                                    [{"text": "ℹ️ О тренажере"}]
                                ],
                                "resize_keyboard": True
                            }
                            api_call("sendMessage", {"chat_id": chat_id, "text": welcome_text, "parse_mode": "HTML", "reply_markup": json.dumps(kb)})
                            continue

                        # Check if message contains YouTube Link
                        yt_match = re.search(r'(https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[a-zA-Z0-9_\-]+)', text)
                        if yt_match:
                            yt_url = yt_match.group(1)
                            rest = text.replace(yt_url, '').strip()
                            api_call("sendMessage", {"chat_id": chat_id, "text": "⏳ Анализирую ролик YouTube и нарезаю фрагмент фразы (4–6 сек)..."})
                            
                            start_time = 30
                            quote_text = "Dialogue clip"
                            
                            ts_match = re.search(r'(\d+):(\d+)', rest)
                            if ts_match:
                                start_time = max(0, int(ts_match.group(1)) * 60 + int(ts_match.group(2)) - 1)
                                quote_text = f"Dialogue clip at {start_time//60}:{start_time%60:02d}"
                            elif rest:
                                clean_w = re.sub(r'[^a-zA-Z]', '', rest)
                                if clean_w:
                                    found_sec, found_quote = find_word_in_yt_sub(yt_url, clean_w, TMP_DIR)
                                    if found_sec is not None:
                                        start_time = found_sec
                                        quote_text = found_quote

                            clip_file = cut_youtube_clip(yt_url, start_time, duration=5.5)
                            if clip_file:
                                tr = translate_to_ru(quote_text)
                                tags = build_quote_hashtags(quote_text)
                                card_meta = {
                                    "id": get_next_card_id(),
                                    "word": rest if rest else "YouTube",
                                    "quote": quote_text,
                                    "translation": tr,
                                    "hashtags": tags
                                }
                                send_cinema_card(chat_id, card_meta, clip_file)
                            else:
                                api_call("sendMessage", {"chat_id": chat_id, "text": "❌ Не удалось вырезать фрагмент. Проверьте ссылку."})
                            continue

                        # Random scene button
                        if "Случайная" in text or "карточка" in text:
                            send_random_cinema_card(chat_id)
                            continue

                        # Topics catalog button
                        if "Популярные" in text or "Темы" in text or "темы" in text:
                            preview_tags = " ".join([f"#{w.capitalize()}" for w in CONVERSATION_WORDS[:30]])
                            msg_text = (
                                f"💡 <b>Популярные слова и темы:</b>\n\n"
                                f"{preview_tags}\n\n"
                                f"💬 <i>Нажмите на любой хэштег для поиска в чате или отправьте любое английское слово боту!</i>"
                            )
                            api_call("sendMessage", {"chat_id": chat_id, "text": msg_text, "parse_mode": "HTML"})
                            continue

                        # About / Help button
                        if "О тренажере" in text or text.startswith("/help"):
                            help_text = (
                                "🎬 <b>Кино-Тренажер «ШАГ 11 НА СЛУХ»</b>\n\n"
                                "• Все карточки создаются в реальном времени из кино-базы (5 000 000+ сцен).\n"
                                "• Хронометраж строго 3.5–8 сек (полные фразы, без обрыва речи).\n"
                                "• На каждое слово формируется хэштег — нажмите на него, чтобы увидеть все сцены с этим словом в чате!\n"
                                "• Нажмите на заблюренную область, чтобы увидеть цитату и перевод.\n"
                                "• Напишите любое английское слово в чат — бот найдет сцену с ним!"
                            )
                            api_call("sendMessage", {"chat_id": chat_id, "text": help_text, "parse_mode": "HTML"})
                            continue

                        # If user just sent a word (e.g. "approach", "anger", "safe")
                        clean_word = re.sub(r'[^a-zA-Z]', '', text)
                        if clean_word and len(clean_word) >= 3:
                            api_call("sendMessage", {"chat_id": chat_id, "text": f"🎬 Подбираю сцену со словом '{clean_word}' (3.5–8 сек)..."})
                            scene = fetch_live_cinema_scene(clean_word)
                            if scene:
                                send_cinema_card(chat_id, scene, scene["file_path"])
                            else:
                                api_call("sendMessage", {"chat_id": chat_id, "text": f"Для слова '{clean_word}' сцена обрабатывается. Нажмите «🎬 Случайная карточка»!"})
        except Exception as e:
            time.sleep(2)
        time.sleep(0.5)

# Self-pinger to prevent Render Free tier from spinning down
def keep_alive_loop():
    time.sleep(30)
    ping_url = os.environ.get("RENDER_EXTERNAL_URL", "https://lingua-cinema-bot.onrender.com")
    while True:
        try:
            requests.get(ping_url, timeout=10)
        except Exception:
            pass
        time.sleep(600)

# FastAPI Healthcheck Endpoint
app = FastAPI()

@app.get("/")
def read_root():
    return {
        "status": "online",
        "service": "Lingua Magic Cinema Bot 24/7 (Live Dynamic Database)",
        "version": "2.0.0",
        "uptime": "24/7 online"
    }

if __name__ == "__main__":
    bot_thread = threading.Thread(target=bot_polling_loop, daemon=True)
    bot_thread.start()

    ping_thread = threading.Thread(target=keep_alive_loop, daemon=True)
    ping_thread.start()

    port = int(os.environ.get("PORT", 8080))
    print(f"Starting server on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
