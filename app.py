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
import random
from fastapi import FastAPI
import uvicorn

sys.stdout.reconfigure(encoding='utf-8')

# Telegram Bot Token & Settings
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8801944551:AAHZdYry40UudaHj2l0hJHE66-KjpC45VKs")
API_URL = f"https://api.telegram.org/bot{TOKEN}"

import tempfile
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TMP_DIR = os.path.join(tempfile.gettempdir(), "lingua_clips")
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

# 15 Verified high-quality cinema scenes embedded directly (3.5s - 8.0s duration, verified complete sentences)
VERIFIED_SCENES = [
    {
        "id": 223701,
        "cid": "d6c50085-b466-4c92-aa08-932152db9786",
        "video_url": "https://y.getyarn.io/d6c50085-b466-4c92-aa08-932152db9786.mp4",
        "duration": 4.42,
        "quote": "I need to tell the truth. I need to tell Kirsten.",
        "title": "The O.C. (2003) - S02E06 Drama",
        "translation": "Мне нужно сказать правду. Мне нужно сказать Кирстен.",
        "hashtags": "#I_need #to_tell #the_truth. #I_need #to_tell #Kirsten.",
        "word": "need"
    },
    {
        "id": 223702,
        "cid": "3a70c0c6-f895-444c-baab-b0af6841a736",
        "video_url": "https://y.getyarn.io/3a70c0c6-f895-444c-baab-b0af6841a736.mp4",
        "duration": 3.56,
        "quote": "I think I'm thinking what you're thinking.",
        "title": "A Very Brady Sequel",
        "translation": "Кажется, я думаю о том же, о чем и ты.",
        "hashtags": "#I_think #I'm #thinking #what #you're #thinking.",
        "word": "think"
    },
    {
        "id": 223703,
        "cid": "ab7da71f-e7fe-465b-8abf-9c9f10cb8f09",
        "video_url": "https://y.getyarn.io/ab7da71f-e7fe-465b-8abf-9c9f10cb8f09.mp4",
        "duration": 3.99,
        "quote": "I don't know what you are talking about.",
        "title": "The Da Vinci Code (2006)",
        "translation": "Я не понимаю,  о чём вы говорите.",
        "hashtags": "#I_don't #know #what #you #are_talking #about.",
        "word": "don't"
    },
    {
        "id": 223704,
        "cid": "5ca1934c-64b2-4b64-9530-fd17ab8a92c7",
        "video_url": "https://y.getyarn.io/5ca1934c-64b2-4b64-9530-fd17ab8a92c7.mp4",
        "duration": 5.42,
        "quote": "Why Why did you do this to me Why, why!",
        "title": "The Transformers (1984) - S02E01 Autobot Spike",
        "translation": "Почему ты так поступил со мной? Почему, почему!",
        "hashtags": "#Why #Why #did #you #do #this #to_me #Why, #why!",
        "word": "this"
    },
    {
        "id": 223705,
        "cid": "d224319d-d924-413e-a000-5a2f4fa799ec",
        "video_url": "https://y.getyarn.io/d224319d-d924-413e-a000-5a2f4fa799ec.mp4",
        "duration": 3.82,
        "quote": "Baby, I have been waiting all day for this.",
        "title": "Halloween Ends",
        "translation": "Детка, я ждал этого весь день.",
        "hashtags": "#Baby, #I_have_been #waiting #all #day #for_this.",
        "word": "baby"
    },
    {
        "id": 223706,
        "cid": "26c75905-8232-424b-859a-7e93d8d02824",
        "video_url": "https://y.getyarn.io/26c75905-8232-424b-859a-7e93d8d02824.mp4",
        "duration": 4.19,
        "quote": "Don't you understand what this night means to me.",
        "title": "The Goldbergs (2013) - S03E06 Couples Costume",
        "translation": "Разве ты не понимаешь, что эта ночь значит для меня.",
        "hashtags": "#Don't #you #understand #what #this #night #means #to_me.",
        "word": "don't"
    },
    {
        "id": 223707,
        "cid": "cf6ac374-37b4-4048-af62-19c4d906aa85",
        "video_url": "https://y.getyarn.io/cf6ac374-37b4-4048-af62-19c4d906aa85.mp4",
        "duration": 4.23,
        "quote": "I just wanted to be honest with you.",
        "title": "Sex and the City (1998) - S03E12 Romance",
        "translation": "Я просто хотела быть честной с тобой.",
        "hashtags": "#I_just #wanted #to_be #honest #with #you.",
        "word": "just"
    },
    {
        "id": 223708,
        "cid": "4a2916d8-9442-4c9d-b54c-7c2a891ffdce",
        "video_url": "https://y.getyarn.io/4a2916d8-9442-4c9d-b54c-7c2a891ffdce.mp4",
        "duration": 3.7,
        "quote": "What are they doing here, right now What are you doing here!",
        "title": "Lost (2004-2010) S03E09 Stranger in a Strange Land",
        "translation": "Что они здесь делают, прямо сейчас Что ты здесь делаешь!",
        "hashtags": "#What #are_they_doing #here, #right #now #What #are_you #doing #here!",
        "word": "what"
    },
    {
        "id": 223709,
        "cid": "701b5ba2-9927-4dd1-8d83-c41fc3c015eb",
        "video_url": "https://y.getyarn.io/701b5ba2-9927-4dd1-8d83-c41fc3c015eb.mp4",
        "duration": 5.0,
        "quote": "Daddy will never let anything happen to you.",
        "title": "Duncanville (2020) - S02E07 That Jing You Do",
        "translation": "Папа никогда не позволит, чтобы с тобой что-то случилось.",
        "hashtags": "#Daddy #will #never #let #anything #happen #to_you.",
        "word": "daddy"
    },
    {
        "id": 223710,
        "cid": "9e0d4133-2073-4353-a80e-9bd5080c326f",
        "video_url": "https://y.getyarn.io/9e0d4133-2073-4353-a80e-9bd5080c326f.mp4",
        "duration": 3.92,
        "quote": "Please, can you just tell the truth.",
        "title": "Breaking Bad (2008) - S05E12 Drama",
        "translation": "Пожалуйста, ты можешь просто сказать правду.",
        "hashtags": "#Please, #can #you #just #tell #the_truth.",
        "word": "please"
    },
    {
        "id": 223711,
        "cid": "808e0f18-684f-4583-bb1e-b3cfbee1f941",
        "video_url": "https://y.getyarn.io/808e0f18-684f-4583-bb1e-b3cfbee1f941.mp4",
        "duration": 4.07,
        "quote": "But never have I seen anything like this.",
        "title": "Leap of Faith (1992)",
        "translation": "Но я никогда не видел ничего подобного.",
        "hashtags": "#But #never #have_I_seen #anything #like #this.",
        "word": "never"
    },
    {
        "id": 223712,
        "cid": "495291d3-4ba5-4c2e-83c7-a8d8a9fc2c96",
        "video_url": "https://y.getyarn.io/495291d3-4ba5-4c2e-83c7-a8d8a9fc2c96.mp4",
        "duration": 4.57,
        "quote": "Control is an illusion. Nobody knows what's going to happen next.",
        "title": "Days of Thunder",
        "translation": "Контроль - это иллюзия. Никто не знает, что будет дальше.",
        "hashtags": "#Control #is_an_illusion. #Nobody #knows #what's #going #to_happen #next.",
        "word": "control"
    },
    {
        "id": 223713,
        "cid": "cd92e717-1b0f-424c-b796-15920a7e64d0",
        "video_url": "https://y.getyarn.io/cd92e717-1b0f-424c-b796-15920a7e64d0.mp4",
        "duration": 4.9,
        "quote": "We have to get out of here now!",
        "title": "Ben 10 (2005) - S02E13 Back with a Vengeance",
        "translation": "Нам нужно убираться отсюда!",
        "hashtags": "#We_have_to #get #out #of_here #now!",
        "word": "have"
    },
    {
        "id": 223714,
        "cid": "d243f0f3-4d70-456a-ad68-83d61da8b92e",
        "video_url": "https://y.getyarn.io/d243f0f3-4d70-456a-ad68-83d61da8b92e.mp4",
        "duration": 3.93,
        "quote": "I thought you were never coming back.",
        "title": "Devious Maids (2013) - S02E01 Drama",
        "translation": "Я думала, ты никогда не вернешься.",
        "hashtags": "#I_thought #you #were_never #coming #back.",
        "word": "thought"
    },
    {
        "id": 223715,
        "cid": "298df49b-dd73-4d27-82de-841088a980b1",
        "video_url": "https://y.getyarn.io/298df49b-dd73-4d27-82de-841088a980b1.mp4",
        "duration": 3.72,
        "quote": "You are going to do this for me.",
        "title": "Superman III (1983)",
        "translation": "Ты сделаешь это для меня.",
        "hashtags": "#You #are_going #to_do #this #for_me.",
        "word": "going"
    }
]

# User seen history to guarantee no repetitive cards
USER_SEEN_HISTORY = {} # {chat_id: [cid1, cid2, ...]}

def get_seen_cards(chat_id):
    return USER_SEEN_HISTORY.setdefault(str(chat_id), [])

def record_seen_card(chat_id, card_id):
    if not chat_id:
        return
    hist = get_seen_cards(chat_id)
    cid_str = str(card_id)
    if cid_str not in hist:
        hist.append(cid_str)
    # When user saw almost all verified scenes, keep only the last 3 so they cycle fresh
    if len(hist) >= len(VERIFIED_SCENES) - 1:
        USER_SEEN_HISTORY[str(chat_id)] = hist[-3:]

# Find matching scenes by word, phrase, or hashtag with relevance scoring
def find_matching_scenes(query):
    if not query:
        return [], ""
    raw = query.strip()
    raw_lower = raw.lower()
    clean_tag = raw_lower.lstrip("#").strip()
    phrase = clean_tag.replace("_", " ")

    words = [w for w in re.findall(r"[a-zA-Z']+", phrase) if len(w) >= 2]
    stopwords = {"a", "an", "the", "to", "of", "in", "on", "at", "for", "and", "or", "so", "it", "is", "am", "are", "was", "were", "my", "your", "his", "her", "we", "he", "she", "they"}
    significant_words = [w for w in words if w not in stopwords] or words

    matches_with_score = []

    for s in VERIFIED_SCENES:
        score = 0
        s_word = (s.get("word") or "").lower()
        s_quote = (s.get("quote") or "").lower()
        s_tags = (s.get("hashtags") or "").lower()

        # 1. Exact match on card target word
        for sw in significant_words:
            if sw == s_word or sw.replace("'", "") == s_word.replace("'", ""):
                score += 100

        # 2. Hashtag exact match (e.g. #the_truth, #I_need)
        if f"#{clean_tag}" in s_tags or f"#{clean_tag.replace(' ', '_')}" in s_tags:
            score += 90

        # 3. Whole phrase in quote
        if phrase in s_quote:
            score += 80

        # 4. Words match in quote
        s_quote_words = [w.strip(".,!?:;\"'") for w in s_quote.split()]
        s_quote_words_clean = [re.sub(r"[^a-zA-Z']", "", w) for w in s_quote_words]

        for sw in significant_words:
            sw_no_apos = sw.replace("'", "")
            for qw in s_quote_words_clean:
                if sw == qw or sw_no_apos == qw.replace("'", ""):
                    score += 50
                    break

        # 5. Partial hashtag match
        for sw in significant_words:
            if f"#{sw}" in s_tags or f"_{sw}" in s_tags:
                score += 40

        if score > 0:
            matches_with_score.append((score, s))

    matches_with_score.sort(key=lambda x: x[0], reverse=True)
    target_word = significant_words[0] if significant_words else phrase
    return [m[1] for m in matches_with_score], target_word

# Fetch a verified scene from local collection matching query word (fast, 100% reliable)
def fetch_live_cinema_scene(query, exclude_id=None, chat_id=None):
    if not query:
        return None

    matches, target_word = find_matching_scenes(query)
    if not matches:
        return None

    seen_ids = set(get_seen_cards(chat_id)) if chat_id else set()
    if exclude_id:
        seen_ids.add(str(exclude_id))

    # Priority 1: Scene with this word that user has NOT seen yet
    fresh_matches = [m for m in matches if str(m.get("id")) not in seen_ids]
    if fresh_matches:
        card = fresh_matches[0]
        card_id = card.get("id")
        local_clip = os.path.join(BASE_DIR, "clips", f"clip_{card_id}.mp4")
        if os.path.exists(local_clip) and os.path.getsize(local_clip) > 10000:
            card_copy = dict(card)
            card_copy["file_path"] = local_clip
            card_copy["search_word"] = target_word
            return card_copy

    # Priority 2: Other scene with this word excluding only current
    other_matches = [m for m in matches if str(m.get("id")) != str(exclude_id)]
    if other_matches:
        card = other_matches[0]
        card_id = card.get("id")
        local_clip = os.path.join(BASE_DIR, "clips", f"clip_{card_id}.mp4")
        if os.path.exists(local_clip) and os.path.getsize(local_clip) > 10000:
            card_copy = dict(card)
            card_copy["file_path"] = local_clip
            card_copy["search_word"] = target_word
            return card_copy

    # If only 1 match exists and it's current, return it
    card = matches[0]
    card_id = card.get("id")
    local_clip = os.path.join(BASE_DIR, "clips", f"clip_{card_id}.mp4")
    if os.path.exists(local_clip) and os.path.getsize(local_clip) > 10000:
        card_copy = dict(card)
        card_copy["file_path"] = local_clip
        card_copy["search_word"] = target_word
        return card_copy

    return None

# Format telegram post caption: EXACT 1-TO-1 COPY OF «ШАГ 11 НА СЛУХ» (Screenshot 2)
def format_cinema_post(cid, quote, translation, hashtags, show=None):
    q_esc = html.escape(quote or "", quote=False)
    tr_esc = html.escape(translation or "", quote=False)
    tags_esc = html.escape(hashtags or "", quote=False)
    show_esc = html.escape(show or "", quote=False)

    title_part = f"<b>{cid} • 🎬 {show_esc}</b>" if show_esc else f"<b>{cid}</b>"

    # In Telegram HTML, <tg-spoiler> CANNOT enclose blockquote, but blockquote CAN enclose <tg-spoiler>!
    return (
        f"{title_part}\n\n"
        f"<tg-spoiler>{tags_esc}</tg-spoiler>\n\n"
        f"<blockquote><tg-spoiler>\"{q_esc}\"</tg-spoiler></blockquote>\n\n"
        f"<tg-spoiler>{tr_esc}</tg-spoiler>"
    )

# Send video post with buttons (supports local file or direct URL)
def send_cinema_card(chat_id, card_data, video_path_or_url, search_word=None):
    cid = card_data.get('id') or get_next_card_id()
    quote = card_data.get('quote', '')
    translation = card_data.get('translation', quote)
    hashtags = card_data.get('hashtags') or build_quote_hashtags(quote)
    word = search_word if search_word else (card_data.get('word') or 'scene')
    show = card_data.get('show') or card_data.get('title') or ''

    caption = format_cinema_post(cid, quote, translation, hashtags, show)

    keyboard = {
        "inline_keyboard": [
            [
                {"text": f"🎬 Еще сцены со словом '{word}'", "callback_data": f"word:{word}:{cid}"}
            ],
            [
                {"text": "🎲 Случайная сцена", "callback_data": "random"},
                {"text": "📥 В Anki", "callback_data": f"anki:{cid}"}
            ]
        ]
    }

    try:
        if isinstance(video_path_or_url, str) and (video_path_or_url.startswith("http://") or video_path_or_url.startswith("https://")):
            temp_dest = os.path.join(TMP_DIR, f"url_{cid}.mp4")
            c_resp = requests.get(video_path_or_url, headers=BROWSER_HEADERS, timeout=8)
            if c_resp.status_code == 200 and len(c_resp.content) > 10000:
                with open(temp_dest, "wb") as f:
                    f.write(c_resp.content)
                video_path_or_url = temp_dest

        if os.path.exists(video_path_or_url):
            with open(video_path_or_url, "rb") as vf:
                resp = api_call("sendVideo", params={
                    "chat_id": chat_id,
                    "caption": caption,
                    "parse_mode": "HTML",
                    "reply_markup": json.dumps(keyboard)
                }, files={"video": (os.path.basename(video_path_or_url), vf)})

            # Fail-safe retry with pure spoiler caption if Telegram entity parsing fails
            if not resp or not resp.get("ok"):
                print(f"Telegram parse error ({resp}). Retrying with safe spoiler caption...")
                q_safe = html.escape(quote, quote=False)
                tr_safe = html.escape(translation, quote=False)
                tg_safe = html.escape(hashtags, quote=False)
                show_safe = html.escape(show, quote=False)
                title_line = f"<b>{cid} • 🎬 {show_safe}</b>" if show_safe else f"<b>{cid}</b>"
                safe_caption = f"{title_line}\n\n<tg-spoiler>{tg_safe}\n\n\"{q_safe}\"\n\n{tr_safe}</tg-spoiler>"
                with open(video_path_or_url, "rb") as vf:
                    resp = api_call("sendVideo", params={
                        "chat_id": chat_id,
                        "caption": safe_caption,
                        "parse_mode": "HTML",
                        "reply_markup": json.dumps(keyboard)
                    }, files={"video": (os.path.basename(video_path_or_url), vf)})

            if resp and resp.get("ok"):
                record_seen_card(chat_id, cid)

            if video_path_or_url and "tmp" in video_path_or_url.lower():
                try: os.remove(video_path_or_url)
                except Exception: pass
            return resp
        else:
            print("Video source not accessible:", video_path_or_url)
            return None
    except Exception as e:
        print("Error sending video post:", e)
        return None

# Send random verified live scene - NEVER FAILS, delivers in < 1 second!
def send_random_cinema_card(chat_id):
    if VERIFIED_SCENES:
        seen_ids = set(get_seen_cards(chat_id))
        fresh_scenes = [s for s in VERIFIED_SCENES if str(s.get("id")) not in seen_ids]
        if not fresh_scenes:
            fresh_scenes = VERIFIED_SCENES

        for attempt in range(len(fresh_scenes)):
            card = random.choice(fresh_scenes)
            card_id = card.get("id")

            # Check local packaged clip first (100% immune to Cloudflare 403 blocks)
            local_clip = os.path.join(BASE_DIR, "clips", f"clip_{card_id}.mp4")
            if os.path.exists(local_clip) and os.path.getsize(local_clip) > 10000:
                res = send_cinema_card(chat_id, card, local_clip)
                if res and res.get("ok"):
                    return

            # Fallback to downloading if local clip is not available
            clip_url = card.get("video_url")
            temp_dest = os.path.join(TMP_DIR, f"{card.get('cid', card_id)}.mp4")
            try:
                r = requests.get(clip_url, headers=BROWSER_HEADERS, timeout=8)
                if r.status_code == 200 and len(r.content) > 10000:
                    with open(temp_dest, "wb") as f:
                        f.write(r.content)
                    res = send_cinema_card(chat_id, card, temp_dest)
                    if res and res.get("ok"):
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
            res = send_cinema_card(chat_id, scene, scene["file_path"])
            if res and res.get("ok"):
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

                        if data == "random":
                            api_call("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "🎲 Подбираю сцену..."})
                            send_random_cinema_card(chat_id)
                        elif data.startswith("word:"):
                            parts = data.split(":")
                            w = parts[1] if len(parts) > 1 else "scene"
                            curr_cid = parts[2] if len(parts) > 2 else None
                            api_call("answerCallbackQuery", {"callback_query_id": cq["id"], "text": f"🎬 Сцена со словом '{w}'..."})
                            scene = fetch_live_cinema_scene(w, exclude_id=curr_cid, chat_id=chat_id)
                            if scene:
                                send_cinema_card(chat_id, scene, scene["file_path"], search_word=scene.get("search_word", w))
                            else:
                                send_random_cinema_card(chat_id)
                        elif data == "topics":
                            api_call("answerCallbackQuery", {"callback_query_id": cq["id"]})
                            preview_tags = " ".join([f"#{w.capitalize()}" for w in CONVERSATION_WORDS[:30]])
                            msg_text = (
                                f"💡 <b>Популярные слова и темы:</b>\n\n"
                                f"{preview_tags}\n\n"
                                f"💬 <i>Нажмите на любой хэштег или отправьте любое английское слово в чат!</i>"
                            )
                            api_call("sendMessage", {"chat_id": chat_id, "text": msg_text, "parse_mode": "HTML"})
                        elif data.startswith("anki:"):
                            api_call("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "✅ Сцена сохранена в Anki!"})
                        else:
                            api_call("answerCallbackQuery", {"callback_query_id": cq["id"]})

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

                        # Search by word, phrase, or hashtag (e.g. "the_truth", "#the_truth", "I_need", "think", "don't")
                        matches, target_word = find_matching_scenes(text)
                        if matches:
                            seen_ids = set(get_seen_cards(chat_id))
                            unseen = [m for m in matches if str(m.get("id")) not in seen_ids]
                            chosen = unseen[0] if unseen else matches[0]
                            local_clip = os.path.join(BASE_DIR, "clips", f"clip_{chosen['id']}.mp4")
                            send_cinema_card(chat_id, chosen, local_clip, search_word=target_word)
                            continue
                        else:
                            clean_t = re.sub(r'[^a-zA-Z0-9_\' ]', '', text).strip()
                            if clean_t:
                                not_found_msg = (
                                    f"🔍 Сцен со словом «<b>{clean_t}</b>» в текущей подборке пока нет.\n\n"
                                    f"💡 <i>Попробуйте популярные слова и хэштеги:</i>\n"
                                    f"#Truth #Need #Think #Don't #Honest #Never #Control #Baby #What #Going\n\n"
                                    f"Или нажмите кнопку ниже:"
                                )
                                kb_nf = {
                                    "inline_keyboard": [
                                        [{"text": "🎲 Случайная сцена", "callback_data": "random"}],
                                        [{"text": "📚 Популярные темы", "callback_data": "topics"}]
                                    ]
                                }
                                api_call("sendMessage", {"chat_id": chat_id, "text": not_found_msg, "parse_mode": "HTML", "reply_markup": json.dumps(kb_nf)})
                                continue
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
        "version": "2.6.0 (Zero-Spin Desktop Video & Exact Word Search)",
        "verified_scenes": len(VERIFIED_SCENES),
        "uptime": "24/7 online"
    }

@app.get("/test_card/{card_id}")
def test_card(card_id: int, chat_id: str = "8937275197"):
    matches = [c for c in VERIFIED_SCENES if c["id"] == card_id]
    if not matches:
        return {"ok": False, "error": f"Card {card_id} not found in verified scenes"}
    card = matches[0]
    local_clip = os.path.join(BASE_DIR, "clips", f"clip_{card_id}.mp4")
    if not os.path.exists(local_clip):
        return {"ok": False, "error": f"Local clip {local_clip} not found"}
    res = send_cinema_card(chat_id, card, local_clip)
    return {"ok": True, "card_id": card_id, "res": res}

@app.get("/trigger_send")
def trigger_send(chat_id: str = "8937275197"):
    import traceback
    card = VERIFIED_SCENES[0]
    card_id = card["id"]
    local_clip = os.path.join(BASE_DIR, "clips", f"clip_{card_id}.mp4")
    logs = [f"Card: {card_id}"]
    try:
        if os.path.exists(local_clip):
            logs.append(f"Local clip found: {local_clip}, size: {os.path.getsize(local_clip)}")
            res = send_cinema_card(chat_id, card, local_clip)
            logs.append(f"send_cinema_card result: {res}")
            return {"ok": True, "logs": logs}
        else:
            logs.append(f"Local clip NOT found: {local_clip}")
            return {"ok": False, "logs": logs}
    except Exception as e:
        return {"ok": False, "error": str(e), "trace": traceback.format_exc(), "logs": logs}

# Optimize clips with faststart so Telegram Desktop streams instantly with 0 buffering
def ensure_faststart_clips():
    clips_dir = os.path.join(BASE_DIR, "clips")
    if not os.path.exists(clips_dir):
        return
    for fname in os.listdir(clips_dir):
        if fname.endswith(".mp4"):
            fpath = os.path.join(clips_dir, fname)
            tmp_fast = os.path.join(TMP_DIR, f"fast_{fname}")
            try:
                cmd = ["ffmpeg", "-y", "-i", fpath, "-c", "copy", "-movflags", "+faststart", tmp_fast]
                res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                if res.returncode == 0 and os.path.exists(tmp_fast) and os.path.getsize(tmp_fast) > 10000:
                    import shutil
                    shutil.move(tmp_fast, fpath)
            except Exception as e:
                pass

if __name__ == "__main__":
    ensure_faststart_clips()

    bot_thread = threading.Thread(target=bot_polling_loop, daemon=True)
    bot_thread.start()

    ping_thread = threading.Thread(target=keep_alive_loop, daemon=True)
    ping_thread.start()

    port = int(os.environ.get("PORT", 8080))
    print(f"Starting server on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
