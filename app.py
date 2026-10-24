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

# Load existing cards database
CARDS = []
if os.path.exists(DATA_FILE):
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            CARDS = json.load(f)
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
    # Try pure Python MP4 parser first (0 external dependencies)
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

    # Fallback to ffprobe if available
    try:
        cmd = [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", file_path
        ]
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=5).decode().strip()
        return float(out)
    except Exception:
        return 0.0

# Search GetYarn clips with strict duration filter (3.5s - 8.0s)
def search_valid_clips(word, limit=5):
    patterns = [f"I {word}", f"you {word}", f"{word} that", f"to {word}", f"so {word}", word]
    results = []
    seen = set()

    for p in patterns:
        url = f"https://getyarn.io/yarn-find?text={urllib.parse.quote(p)}"
        try:
            r = requests.get(url, timeout=7)
            if r.status_code == 200:
                pattern = r'<a href="/yarn-clip/([a-f0-9\-]+)".*?<div class="title[^>]*>([^<]+)</div>.*?<div class="transcript[^>]*>([^<]+)</div>'
                matches = re.findall(pattern, r.text, re.DOTALL)
                for cid, title, quote in matches:
                    if cid in seen: continue
                    seen.add(cid)
                    q = html.unescape(quote).strip()
                    q_clean = re.sub(r'[\?♪♫]+', '', q).strip()
                    words = re.findall(r"[a-zA-Z']+", q_clean)
                    if len(words) >= 4 and len(set(w.lower() for w in words)) >= 3:
                        # Download and check duration
                        clip_url = f"https://y.getyarn.io/{cid}.mp4"
                        temp_dest = os.path.join(TMP_DIR, f"{cid}.mp4")
                        c_resp = requests.get(clip_url, timeout=10)
                        if c_resp.status_code == 200 and len(c_resp.content) > 10000:
                            with open(temp_dest, "wb") as f:
                                f.write(c_resp.content)
                            dur = get_video_duration(temp_dest)
                            
                            # STRICT FILTER: between 3.5 and 8.0 seconds
                            if 3.2 <= dur <= 8.5:
                                results.append({
                                    "id": cid[:8],
                                    "word": word,
                                    "quote": q_clean,
                                    "show": html.unescape(title).strip(),
                                    "duration": dur,
                                    "file_path": temp_dest
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

# Format telegram post caption
def format_post_caption(cid, show, hashtags, quote, translation):
    return (
        f"<b>{cid}</b> • 🎬 {show}\n\n"
        f"{hashtags}\n\n"
        f"<blockquote>\"{quote}\" ”</blockquote>\n\n"
        f"<tg-spoiler>{translation}</tg-spoiler>"
    )

# Send video post with buttons
def send_cinema_card(chat_id, card_data, video_path):
    word = card_data.get('word', 'scene')
    cid = card_data.get('id', '10001')
    show = card_data.get('show', 'Movie Clip')
    quote = card_data.get('quote', '')
    translation = card_data.get('translation', quote)
    
    # Generate hashtags
    words = quote.split()
    tags = " ".join([f"#{w.capitalize().replace('.', '')}" for w in words[:6]]) + "."
    
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
        with open(video_path, "rb") as vf:
            api_call("sendVideo", params={
                "chat_id": chat_id,
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": json.dumps(keyboard)
            }, files={"video": (os.path.basename(video_path), vf)})
    except Exception as e:
        print("Error sending video post:", e)

# Background Telegram Bot Polling
def bot_polling_loop():
    print("Telegram Bot Polling started on Hugging Face 24/7...")
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
                            import random
                            c = random.choice(CARDS) if CARDS else None
                            if c:
                                # Ensure valid duration clip
                                clips = search_valid_clips(c["word"], limit=1)
                                if clips:
                                    send_cinema_card(chat_id, clips[0], clips[0]["file_path"])
                                    continue
                        elif data.startswith("word:"):
                            w = data.split(":", 1)[1]
                            api_call("sendMessage", {"chat_id": chat_id, "text": f"🔍 Ищу отрывки со словом '{w}' (хронометраж 3.5–8 сек)..."})
                            clips = search_valid_clips(w, limit=1)
                            if clips:
                                send_cinema_card(chat_id, clips[0], clips[0]["file_path"])
                            else:
                                api_call("sendMessage", {"chat_id": chat_id, "text": f"Для слова '{w}' готовятся новые сцены."})
                        elif data.startswith("anki:"):
                            api_call("sendMessage", {"chat_id": chat_id, "text": "✅ Сцена добавлена в очередь колоды 🎬 Кино-Вырезки!"})

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
                                    [{"text": "🎲 Случайная карточка"}],
                                    [{"text": "🎬 Все слова базы"}]
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
                                        print(f"Found word '{clean_w}' in YT sub at {start_time}s: '{quote_text}'")

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
                        if "Случайная" in text:
                            import random
                            c = random.choice(CARDS) if CARDS else None
                            if c:
                                clips = search_valid_clips(c["word"], limit=1)
                                if clips:
                                    send_cinema_card(chat_id, clips[0], clips[0]["file_path"])
                            continue

                        # If user just sent a word (e.g. "anger", "terrible", "agree")
                        clean_word = re.sub(r'[^a-zA-Z]', '', text)
                        if clean_word and len(clean_word) >= 3:
                            api_call("sendMessage", {"chat_id": chat_id, "text": f"🎬 Подбираю отрывок со словом '{clean_word}' (хронометраж 3.5–8 сек)..."})
                            clips = search_valid_clips(clean_word, limit=1)
                            if clips:
                                send_cinema_card(chat_id, clips[0], clips[0]["file_path"])
                            else:
                                api_call("sendMessage", {"chat_id": chat_id, "text": f"Для слова '{clean_word}' сцена обрабатывается."})
        except Exception as e:
            time.sleep(2)
        time.sleep(0.5)

# FastAPI Healthcheck Endpoint for Hugging Face (port 7860)
app = FastAPI()

@app.get("/")
def read_root():
    return {
        "status": "online",
        "service": "Lingua Magic Cinema Bot 24/7",
        "version": "1.0.0",
        "huggingface": "Spaces Community 16GB RAM"
    }

if __name__ == "__main__":
    # Start bot in a background daemon thread
    bot_thread = threading.Thread(target=bot_polling_loop, daemon=True)
    bot_thread.start()

    # Run FastAPI server (reads PORT dynamically from environment for Render/Cloud)
    port = int(os.environ.get("PORT", 8080))
    print(f"Starting server on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
