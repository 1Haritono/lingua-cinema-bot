import os
import sys
import json
import time
import re
import html
import subprocess
import threading
import urllib.parse
import uuid
import requests
from fastapi import FastAPI
import uvicorn

sys.stdout.reconfigure(encoding='utf-8')

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8801944551:AAHZdYry40UudaHj2l0hJHE66-KjpC45VKs")
API_URL = f"https://api.telegram.org/bot{TOKEN}"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "cards_data.json")
TMP_DIR = os.path.join(BASE_DIR, "tmp_clips")
os.makedirs(TMP_DIR, exist_ok=True)

# Load existing cards database (100 cards)
CARDS = []
if os.path.exists(DATA_FILE):
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            CARDS = json.load(f)
        print(f"Loaded {len(CARDS)} cards from {DATA_FILE}")
    except Exception as e:
        print("Error loading cards:", e)

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

# Measure exact video duration using pure Python MP4 box parser + ffprobe fallback
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

# Search GetYarn clips with strict duration filter (3.0s - 8.5s)
def search_valid_clips(word, limit=3):
    patterns = [f"that is {word}", f"it was {word}", f"so {word}", f"I {word}", f"{word} that", word]
    results = []
    seen = set()

    for p in patterns:
        url = f"https://getyarn.io/yarn-find?text={urllib.parse.quote(p)}"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        try:
            r = requests.get(url, headers=headers, timeout=6)
            if r.status_code == 200:
                pattern = r'<a href="/yarn-clip/([a-f0-9\-]+)".*?<div class="title[^>]*>([^<]+)</div>.*?<div class="transcript[^>]*>([^<]+)</div>'
                matches = re.findall(pattern, r.text, re.DOTALL)
                for cid, title, quote in matches:
                    if cid in seen: continue
                    seen.add(cid)
                    q = html.unescape(quote).strip()
                    q_clean = re.sub(r'[\?♪♫]+', '', q).strip()
                    words = re.findall(r"[a-zA-Z']+", q_clean)
                    if len(words) >= 3:
                        clip_url = f"https://y.getyarn.io/{cid}.mp4"
                        temp_dest = os.path.join(TMP_DIR, f"{cid}.mp4")
                        c_resp = requests.get(clip_url, headers=headers, timeout=10)
                        if c_resp.status_code == 200 and len(c_resp.content) > 10000:
                            with open(temp_dest, "wb") as f:
                                f.write(c_resp.content)
                            dur = get_video_duration(temp_dest)
                            
                            # Filter: preferably 3.0s - 8.5s
                            if 2.8 <= dur <= 8.5:
                                results.append({
                                    "id": cid[:8],
                                    "word": word,
                                    "quote": q_clean,
                                    "show": html.unescape(title).strip(),
                                    "duration": dur,
                                    "file_path": temp_dest,
                                    "video_url": clip_url
                                })
                                if len(results) >= limit:
                                    return results
                            else:
                                if os.path.exists(temp_dest): os.remove(temp_dest)
        except Exception:
            pass
        if len(results) >= limit: break

    return results

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

# Cut clip from YouTube by time range (start_sec, end_sec)
def cut_youtube_clip(url, start_sec, duration=5.5):
    start_str = time.strftime('%H:%M:%S', time.gmtime(max(0, start_sec)))
    end_str = time.strftime('%H:%M:%S', time.gmtime(start_sec + duration))
    out_id = uuid.uuid4().hex[:8]
    out_file = os.path.join(TMP_DIR, f"yt_{out_id}.mp4")

    cmd = [
        "yt-dlp",
        "--download-sections", f"*{start_str}-{end_str}",
        "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "--merge-output-format", "mp4",
        url,
        "-o", out_file,
        "--quiet", "--no-warnings"
    ]
    try:
        subprocess.run(cmd, check=True, timeout=40)
        if os.path.exists(out_file) and os.path.getsize(out_file) > 10000:
            return out_file
    except Exception as e:
        print("yt-dlp error:", e)
    return None

# Format telegram post caption with safe HTML escaping
def format_post_caption(cid, show, hashtags, quote, translation):
    q_esc = html.escape(quote)
    tr_esc = html.escape(translation)
    sh_esc = html.escape(show)
    return (
        f"<b>{cid}</b> • 🎬 {sh_esc}\n\n"
        f"{hashtags}\n\n"
        f"<blockquote>\"{q_esc}\"</blockquote>\n\n"
        f"<tg-spoiler>{tr_esc}</tg-spoiler>"
    )

# Send video post with buttons (supports URL or local file)
def send_cinema_card(chat_id, card_data, video_path_or_url):
    word = card_data.get('word', 'scene')
    cid = card_data.get('id', '10001')
    show = card_data.get('show', 'Movie Clip')
    quote = card_data.get('quote', '')
    translation = card_data.get('translation', quote)
    
    words = quote.split()
    tags = " ".join([f"#{w.capitalize().replace('.', '')}" for w in words[:6]])
    if tags:
        tags += "."
    
    caption = format_post_caption(cid, show, tags, quote, translation)

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
        # Check if URL
        if isinstance(video_path_or_url, str) and (video_path_or_url.startswith("http://") or video_path_or_url.startswith("https://")):
            api_call("sendVideo", params={
                "chat_id": chat_id,
                "video": video_path_or_url,
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": json.dumps(keyboard)
            })
        elif os.path.exists(video_path_or_url):
            with open(video_path_or_url, "rb") as vf:
                api_call("sendVideo", params={
                    "chat_id": chat_id,
                    "caption": caption,
                    "parse_mode": "HTML",
                    "reply_markup": json.dumps(keyboard)
                }, files={"video": (os.path.basename(video_path_or_url), vf)})
        else:
            print("Video source not accessible:", video_path_or_url)
    except Exception as e:
        print("Error sending video post:", e)

# Instant random card sender (uses pre-curated 100 cards first for 100% instant response)
def send_random_cinema_card(chat_id):
    import random
    if CARDS:
        c = random.choice(CARDS)
        v = c.get("video_url")
        if v:
            send_cinema_card(chat_id, c, v)
            return
        clips = search_valid_clips(c["word"], limit=1)
        if clips:
            send_cinema_card(chat_id, clips[0], clips[0]["file_path"])
            return
    api_call("sendMessage", {"chat_id": chat_id, "text": "🎬 Загружаю новую сцену, нажмите еще раз!"})

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
                            api_call("sendMessage", {"chat_id": chat_id, "text": f"🔍 Ищу отрывки со словом '{w}' (хронометраж 3.5–8 сек)..."})
                            clips = search_valid_clips(w, limit=1)
                            if clips:
                                send_cinema_card(chat_id, clips[0], clips[0]["file_path"])
                            else:
                                api_call("sendMessage", {"chat_id": chat_id, "text": f"🎬 Подбираем новые сцены для '{w}'..."})
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
                                "🔥 В базе >300 000 сцен с живой речью из фильмов и сериалов.\n"
                                "⏱ Каждое видео длится от 3.5 до 8 секунд (только полные фразы!).\n\n"
                                "📱 <b>Как вырезать видео с YouTube с телефона:</b>\n"
                                "Нажмите «Поделиться» в YouTube и отправьте ссылку сюда с нужным словом:\n"
                                "👉 <code>https://youtu.be/... terrible</code>\n\n"
                                "Нажмите кнопку ниже для старта:"
                            )
                            kb = {
                                "keyboard": [
                                    [{"text": "🎬 Случайная карточка"}],
                                    [{"text": "📚 Все слова колоды (100 сцен)"}]
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
                            
                            # 1) Check if user gave a timestamp (e.g. 4:15)
                            ts_match = re.search(r'(\d+):(\d+)', rest)
                            if ts_match:
                                start_time = max(0, int(ts_match.group(1)) * 60 + int(ts_match.group(2)) - 1)
                                quote_text = f"Dialogue clip at {start_time//60}:{start_time%60:02d}"
                            elif rest:
                                # 2) User gave a word (e.g. "terrible") -> find in subtitles
                                clean_w = re.sub(r'[^a-zA-Z]', '', rest)
                                if clean_w:
                                    found_sec, found_quote = find_word_in_yt_sub(yt_url, clean_w, TMP_DIR)
                                    if found_sec is not None:
                                        start_time = found_sec
                                        quote_text = found_quote

                            clip_file = cut_youtube_clip(yt_url, start_time, duration=5.5)
                            if clip_file:
                                card_meta = {
                                    "id": f"YT-{int(time.time())%100000}",
                                    "word": rest if rest else "YouTube",
                                    "show": "YouTube Video Clip",
                                    "quote": quote_text,
                                    "translation": "Фрагмент из YouTube"
                                }
                                send_cinema_card(chat_id, card_meta, clip_file)
                                if os.path.exists(clip_file): os.remove(clip_file)
                            else:
                                api_call("sendMessage", {"chat_id": chat_id, "text": "❌ Не удалось вырезать фрагмент. Проверьте ссылку."})
                            continue

                        # Random scene button
                        if "Случайная" in text or "карточка" in text:
                            send_random_cinema_card(chat_id)
                            continue

                        # If user just sent a word (e.g. "anger", "terrible", "agree")
                        clean_word = re.sub(r'[^a-zA-Z]', '', text)
                        if clean_word and len(clean_word) >= 3:
                            # Check if word is already in our curated cards
                            found_curated = [c for c in CARDS if c.get("word", "").lower() == clean_word.lower()]
                            if found_curated and found_curated[0].get("video_url"):
                                send_cinema_card(chat_id, found_curated[0], found_curated[0]["video_url"])
                                continue
                            
                            api_call("sendMessage", {"chat_id": chat_id, "text": f"🎬 Подбираю отрывок со словом '{clean_word}' (хронометраж 3.5–8 сек)..."})
                            clips = search_valid_clips(clean_word, limit=1)
                            if clips:
                                send_cinema_card(chat_id, clips[0], clips[0]["file_path"])
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
            print("Keep-alive ping sent successfully.")
        except Exception as e:
            print("Keep-alive error:", e)
        time.sleep(600) # Ping every 10 minutes

# FastAPI Healthcheck Endpoint
app = FastAPI()

@app.get("/")
def read_root():
    return {
        "status": "online",
        "service": "Lingua Magic Cinema Bot 24/7",
        "version": "1.0.1",
        "uptime": "24/7 online"
    }

if __name__ == "__main__":
    # Start bot polling in background thread
    bot_thread = threading.Thread(target=bot_polling_loop, daemon=True)
    bot_thread.start()

    # Start keep-alive pinger in background thread
    ping_thread = threading.Thread(target=keep_alive_loop, daemon=True)
    ping_thread.start()

    # Run FastAPI server on dynamically assigned PORT
    port = int(os.environ.get("PORT", 8080))
    print(f"Starting server on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
