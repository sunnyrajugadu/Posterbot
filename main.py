import asyncio
import html
import io
import json
import re
import textwrap
import urllib.parse
from typing import Dict, Any, List, Optional, Tuple
import aiohttp
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

# ==============================================================================
# ⚙️ TOP CONFIGURATION (Edit all Bot, Group, Channel & Timer settings here)
# ==============================================================================
BOT_TOKEN = "PASTE_YOUR_BOT_TOKEN_HERE"  # Paste your Telegram Bot Token here
BOT_USERNAME = "Posterscrappingbot"              # Without @ (auto-updated on startup)
TMDB_API_KEY = "7f43669a428c09611a0518fa9c0bbddb"  # Paste your TMDB API key here
FANART_API_KEY = "PASTE_YOUR_FANART_TV_API_KEY_HERE"  # Fanart.tv client API key

# Group Configuration (Bot only works in this group)
ALLOWED_GROUP_ID = -1004445997227                # Your Official Group Chat ID (-100...)
GROUP_NAME = "POSTER & THUMBNAIL"                # Your Official Group Display Name
GROUP_LINK = "https://t.me/+pGfhWbOLRaQyZmM1"    # Your Official Group Invite Link

# Channel Configuration (Mandatory Force Subscribe check)
FORCE_SUB_ENABLED = True                         # Set False to disable channel join check
CHANNEL_ID = "-1004466843265"                    # Channel ID (-100...)
CHANNEL_USERNAME = "@SAAHO_HUB"                  # Public Channel @username
CHANNEL_LINK = "https://t.me/SAAHO_HUB"          # Channel Invite / Direct Link

# Auto-Delete Timer Configuration (in seconds)
AUTO_DELETE_SECONDS = 1800                       # 1800 seconds = 30 minutes (Set 0 to disable)

# ==============================================================================
# 🌐 CONSTANTS & IN-MEMORY STORES
# ==============================================================================
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG_ORIGINAL = "https://image.tmdb.org/t/p/original"
TMDB_IMG_PREVIEW = "https://image.tmdb.org/t/p/w1280"
TMDB_IMG_LOGO = "https://image.tmdb.org/t/p/w500"

SESSIONS: Dict[str, Dict[str, Any]] = {}
SESSION_OWNERS: Dict[str, int] = {}              # Permanent owner registry per session ID
THUMB_CACHE: Dict[str, bytes] = {}
SESSION_COUNTER = 0
THUMB_COUNTER = 0
STARTED_USERS: set = set()

LANG_MAP = {
    "te": "Telugu",
    "hi": "Hindi",
    "ta": "Tamil",
    "ml": "Malayalam",
    "kn": "Kannada",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "es": "Spanish",
    "fr": "French",
}


def new_session_id() -> str:
    global SESSION_COUNTER
    SESSION_COUNTER += 1
    return str(SESSION_COUNTER)


def new_thumb_id() -> str:
    global THUMB_COUNTER
    THUMB_COUNTER += 1
    return str(THUMB_COUNTER)


# ==============================================================================
# ⏱️ TELEGRAM HTTP HELPERS & AUTO-DELETE SCHEDULER
# ==============================================================================
async def _delete_later(http: aiohttp.ClientSession, chat_id: int, message_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        async with http.post(
            f"{API_BASE}/deleteMessage",
            json={"chat_id": chat_id, "message_id": message_id},
        ) as resp:
            await resp.read()
    except Exception:
        pass


def schedule_auto_delete(http: aiohttp.ClientSession, result: dict, delay: int = AUTO_DELETE_SECONDS):
    if not delay or delay <= 0 or not isinstance(result, dict) or not result.get("ok"):
        return
    msg = result.get("result")
    if isinstance(msg, dict):
        chat_id = msg.get("chat", {}).get("id")
        message_id = msg.get("message_id")
        if chat_id and message_id:
            asyncio.create_task(_delete_later(http, chat_id, message_id, delay))


async def tg_request(
    http: aiohttp.ClientSession,
    method: str,
    payload: Optional[dict] = None,
    auto_delete: bool = True,
) -> dict:
    url = f"{API_BASE}/{method}"
    try:
        async with http.post(url, json=payload or {}) as resp:
            data = await resp.json()
            if auto_delete and method in ("sendMessage", "sendPhoto", "sendDocument", "sendSticker"):
                schedule_auto_delete(http, data)
            return data
    except Exception as e:
        print(f"[TG API Error] {method}: {e}")
        return {"ok": False}


async def tg_upload(
    http: aiohttp.ClientSession,
    method: str,
    form: aiohttp.FormData,
    auto_delete: bool = True,
) -> dict:
    url = f"{API_BASE}/{method}"
    try:
        async with http.post(url, data=form) as resp:
            data = await resp.json()
            if auto_delete:
                schedule_auto_delete(http, data)
            return data
    except Exception as e:
        print(f"[TG Upload Error] {method}: {e}")
        return {"ok": False}


async def download_bytes(http: aiohttp.ClientSession, url: str) -> Optional[bytes]:
    if not url:
        return None
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        async with http.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=18)) as resp:
            if resp.status == 200:
                return await resp.read()
    except Exception:
        pass
    return None


async def upload_public_image_link(http: aiohttp.ClientSession, img_bytes: bytes, fallback_url: str) -> str:
    """Uploads generated image to a fast public host so 'Copy Link' / 'Image Link' has a direct URL."""
    try:
        form = aiohttp.FormData()
        form.add_field("reqtype", "fileupload")
        form.add_field("fileToUpload", img_bytes, filename="thumbnail.jpg", content_type="image/jpeg")
        async with http.post(
            "https://catbox.moe/user/api.php",
            data=form,
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status == 200:
                link = (await resp.text()).strip()
                if link.startswith("http"):
                    return link
    except Exception:
        pass
    return fallback_url


# ==============================================================================
# 🔒 STRICT CHANNEL FORCE-SUBSCRIBE & QUICK SETUP VERIFICATION
# ==============================================================================
async def is_user_verified(http: aiohttp.ClientSession, user_id: int) -> bool:
    if not FORCE_SUB_ENABLED:
        return True

    # Let the configured group owner/admins use the bot even if channel membership
    # cannot be queried for their account. Normal members still need channel access.
    if ALLOWED_GROUP_ID:
        try:
            group_result = await tg_request(
                http, "getChatMember",
                {"chat_id": ALLOWED_GROUP_ID, "user_id": user_id},
                auto_delete=False,
            )
            if group_result.get("ok") and group_result.get("result", {}).get("status") in ("creator", "administrator"):
                return True
        except Exception:
            pass

    targets = []
    if CHANNEL_USERNAME and CHANNEL_USERNAME.startswith("@"):
        targets.append(CHANNEL_USERNAME)
    if CHANNEL_ID:
        targets.append(CHANNEL_ID)

    for target in targets:
        try:
            res = await tg_request(
                http,
                "getChatMember",
                {"chat_id": target, "user_id": user_id},
                auto_delete=False,
            )
            if res.get("ok"):
                member = res.get("result", {})
                status = member.get("status", "")
                if status in ("creator", "administrator", "member"):
                    return True
                if status == "restricted" and member.get("is_member", False):
                    return True
                return False
        except Exception:
            continue
    # Do not silently grant access when Telegram cannot verify membership.
    return False


async def send_quick_setup(http: aiohttp.ClientSession, chat_id: int, reply_to_id: int, user_id: int):
    text = (
        f"🔒 <b>Quick Setup Required</b>\n\n"
        f"<blockquote>1️⃣ Join our official channel {html.escape(CHANNEL_USERNAME)}\n"
        f"2️⃣ Start me in PM once so I can process your requests</blockquote>\n\n"
        f"Complete both steps, then tap ✅ <b>Done</b> below."
    )
    pm_url = f"https://t.me/{BOT_USERNAME.lstrip('@')}?start=setup"
    kb = {
        "inline_keyboard": [
            [{"text": "➕ Join Channel", "url": CHANNEL_LINK, "style": "primary"}],
            [{"text": "🚀 Start Bot in PM", "url": pm_url, "style": "primary"}],
            [{"text": "✅ Done", "callback_data": f"setup_done:{user_id}", "style": "success"}],
        ]
    }
    await tg_request(
        http,
        "sendMessage",
        {
            "chat_id": chat_id,
            "reply_to_message_id": reply_to_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": kb,
        },
    )


# ==============================================================================
# 🔍 TMDB-FIRST + IMDB FULL FALLBACK SCRAPER (POSTERS, BACKDROPS & METADATA)
# ==============================================================================
async def search_tmdb(http: aiohttp.ClientSession, query: str) -> List[Dict[str, Any]]:
    results = []
    if not TMDB_API_KEY or TMDB_API_KEY == "YOUR_TMDB_API_KEY":
        return results
    params = {
        "api_key": TMDB_API_KEY,
        "query": query,
        "include_adult": "false",
    }
    try:
        async with http.get(f"{TMDB_BASE}/search/multi", params=params) as resp:
            if resp.status == 200:
                data = await resp.json()
                for item in data.get("results", []):
                    m_type = item.get("media_type")
                    if m_type not in ("movie", "tv"):
                        continue
                    title = item.get("title") or item.get("name") or "Unknown"
                    date = item.get("release_date") or item.get("first_air_date") or ""
                    year = date[:4] if date else "?"
                    results.append({
                        "source": "tmdb",
                        "media_type": m_type,
                        "id": str(item["id"]),
                        "title": title,
                        "year": year,
                        "poster_path": item.get("poster_path"),
                        "backdrop_path": item.get("backdrop_path"),
                    })
    except Exception:
        pass
    return results[:6]


async def search_imdb(http: aiohttp.ClientSession, query: str) -> List[Dict[str, Any]]:
    results = []
    clean_q = urllib.parse.quote(query.lower().strip())
    if not clean_q:
        return results
    first_char = clean_q[0]
    imdb_url = f"https://v3.sg.media-imdb.com/suggestion/{first_char}/{clean_q}.json"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with http.get(imdb_url, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                for item in data.get("d", []):
                    qid = item.get("qid", "")
                    if qid not in ("movie", "tvSeries", "tvMiniSeries", "feature", "tvMovie"):
                        continue
                    title = item.get("l", "Unknown")
                    year = str(item.get("y", "?"))
                    img_url = item.get("i", {}).get("imageUrl")
                    results.append({
                        "source": "imdb",
                        "media_type": "tv" if "tv" in qid.lower() else "movie",
                        "id": item.get("id", ""),
                        "title": title,
                        "year": year,
                        "poster": img_url,
                    })
    except Exception:
        pass
    return results[:6]


async def search_movies(http: aiohttp.ClientSession, query: str) -> List[Dict[str, Any]]:
    tmdb_task = asyncio.create_task(search_tmdb(http, query))
    imdb_task = asyncio.create_task(search_imdb(http, query))
    tmdb_results, imdb_results = await asyncio.gather(tmdb_task, imdb_task)

    if tmdb_results:
        seen = {f"{r['title'].lower()}_{r['year']}" for r in tmdb_results}
        for ir in imdb_results:
            key = f"{ir['title'].lower()}_{ir['year']}"
            if key not in seen and len(tmdb_results) < 6:
                seen.add(key)
                tmdb_results.append(ir)
        return tmdb_results[:6]
    return imdb_results[:6]


def _format_runtime(minutes: Optional[int]) -> str:
    if not minutes or minutes <= 0:
        return "N/A"
    h = minutes // 60
    m = minutes % 60
    if h and m:
        return f"{h}h {m}m"
    elif h:
        return f"{h}h"
    return f"{m}m"


def _parse_iso_duration(iso_str: str) -> Optional[int]:
    if not iso_str:
        return None
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", iso_str)
    if not m:
        return None
    hours = int(m.group(1) or 0)
    mins = int(m.group(2) or 0)
    total = hours * 60 + mins
    return total if total > 0 else None


async def scrape_imdb_data_and_images(
    http: aiohttp.ClientSession,
    imdb_id: str,
    fallback_poster: Optional[str] = None,
) -> Dict[str, Any]:
    posters: List[Dict[str, str]] = []
    backdrops: List[Dict[str, str]] = []
    seen_p, seen_b = set(), set()
    meta: Dict[str, Any] = {}

    if fallback_poster:
        hi_res = re.sub(r"\._V1_.*\.jpg", "._V1_FMjpg_UX1920_.jpg", fallback_poster)
        prev = re.sub(r"\._V1_.*\.jpg", "._V1_FMjpg_UX1080_.jpg", fallback_poster)
        posters.append({"preview": prev, "original": hi_res})
        seen_p.add(hi_res)

    if not imdb_id or not imdb_id.startswith("tt"):
        return {"posters": posters, "backdrops": backdrops, "meta": meta}

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }

    title_url = f"https://www.imdb.com/title/{imdb_id}/"
    gallery_url = f"https://www.imdb.com/title/{imdb_id}/mediaindex/"

    t_task = http.get(title_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10))
    g_task = http.get(gallery_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10))
    resps = await asyncio.gather(t_task, g_task, return_exceptions=True)

    if not isinstance(resps[0], Exception):
        try:
            async with resps[0] as resp:
                if resp.status == 200:
                    html_text = await resp.text()
                    ld_match = re.search(
                        r'<script type="application/ld\+json">(.*?)</script>',
                        html_text,
                        flags=re.S,
                    )
                    if ld_match:
                        ld = json.loads(ld_match.group(1))
                        if ld.get("description"):
                            meta["overview"] = html.unescape(ld["description"])
                        if ld.get("aggregateRating", {}).get("ratingValue"):
                            meta["rating"] = str(ld["aggregateRating"]["ratingValue"])
                        if ld.get("genre"):
                            g = ld["genre"]
                            meta["genres"] = g if isinstance(g, list) else [g]
                        if ld.get("contentRating"):
                            meta["certification"] = str(ld["contentRating"])
                        dur = _parse_iso_duration(ld.get("duration", ""))
                        if dur:
                            meta["runtime_mins"] = dur
                        if ld.get("image"):
                            img_u = ld["image"]
                            hi = re.sub(r"\._V1_.*\.jpg", "._V1_FMjpg_UX1920_.jpg", img_u)
                            pr = re.sub(r"\._V1_.*\.jpg", "._V1_FMjpg_UX1080_.jpg", img_u)
                            if hi not in seen_p:
                                seen_p.add(hi)
                                posters.insert(0, {"preview": pr, "original": hi})
                        trailer_thumb = ld.get("trailer", {}).get("thumbnailUrl")
                        if trailer_thumb:
                            hi_b = re.sub(r"\._V1_.*\.jpg", "._V1_FMjpg_UX1920_.jpg", trailer_thumb)
                            pr_b = re.sub(r"\._V1_.*\.jpg", "._V1_FMjpg_UX1280_.jpg", trailer_thumb)
                            if hi_b not in seen_b:
                                seen_b.add(hi_b)
                                backdrops.append({"preview": pr_b, "original": hi_b})
        except Exception:
            pass

    if not isinstance(resps[1], Exception):
        try:
            async with resps[1] as resp:
                if resp.status == 200:
                    g_html = await resp.text()
                    matches = re.findall(
                        r'https://m\.media-amazon\.com/images/M/[^"\'\s>]+\.jpg',
                        g_html,
                    )
                    for m in matches:
                        hi = re.sub(r"\._V1_.*\.jpg", "._V1_FMjpg_UX1920_.jpg", m)
                        pr = re.sub(r"\._V1_.*\.jpg", "._V1_FMjpg_UX1280_.jpg", m)
                        is_landscape = False
                        cr_match = re.search(r"CR\d+,\d+,(\d+),(\d+)", m)
                        if cr_match:
                            w_c, h_c = int(cr_match.group(1)), int(cr_match.group(2))
                            if w_c > h_c:
                                is_landscape = True
                        if is_landscape or len(backdrops) < len(posters):
                            if hi not in seen_b and len(backdrops) < 10:
                                seen_b.add(hi)
                                backdrops.append({"preview": pr, "original": hi})
                        else:
                            if hi not in seen_p and len(posters) < 10:
                                seen_p.add(hi)
                                posters.append({"preview": pr, "original": hi})
        except Exception:
            pass

    return {"posters": posters[:10], "backdrops": backdrops[:10], "meta": meta}


async def fetch_fanart_tv(http: aiohttp.ClientSession, tmdb_id: Optional[str], tvdb_id: Optional[str], media_type: str) -> List[Dict[str, str]]:
    """Return only poster and backdrop artwork served by Fanart.tv."""
    if not FANART_API_KEY or FANART_API_KEY.startswith("PASTE_YOUR_"):
        return []
    if media_type == "tv" and tvdb_id:
        endpoint = f"https://webservice.fanart.tv/v3/tv/{tvdb_id}"
        keys = ("tvposter", "seasonposter", "showbackground", "tvbanner")
    elif media_type != "tv" and tmdb_id:
        endpoint = f"https://webservice.fanart.tv/v3/movies/{tmdb_id}"
        keys = ("movieposter", "moviebackground", "moviebanner")
    else:
        return []
    headers = {"api-key": FANART_API_KEY, "User-Agent": "MoviePosterBot/1.0"}
    try:
        async with http.get(endpoint, headers=headers, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status != 200:
                return []
            payload = await resp.json(content_type=None)
        found: List[Dict[str, str]] = []
        seen = set()
        for key in keys:
            for entry in payload.get(key, []) or []:
                url = entry.get("url")
                if url and url not in seen:
                    seen.add(url)
                    found.append({"preview": url, "original": url, "kind": "poster" if "poster" in key else "backdrop", "source": "Fanart.tv"})
        return found[:20]
    except Exception:
        return []


async def fetch_media_assets(http: aiohttp.ClientSession, item: Dict[str, Any]) -> Dict[str, Any]:
    backdrops, posters, fanart, logos = [], [], [], []
    title = item["title"]
    year = str(item.get("year", "?"))
    media_type = item.get("media_type", "movie")
    source_label = "🟦 TMDB"
    collection_id = None
    collection_name = ""

    rating = ""
    runtime_mins = 0
    genres: List[str] = []
    overview = ""
    cast_members: List[Dict[str, Any]] = []
    tvdb_id = None
    certification = "Not Rated"
    language = "N/A"
    studio = "N/A"

    tmdb_id = item["id"] if item["source"] == "tmdb" else None
    imdb_id = item["id"] if item["source"] == "imdb" else None

    if not tmdb_id and imdb_id and imdb_id.startswith("tt") and TMDB_API_KEY:
        try:
            async with http.get(
                f"{TMDB_BASE}/find/{imdb_id}",
                params={"api_key": TMDB_API_KEY, "external_source": "imdb_id"},
            ) as f_resp:
                if f_resp.status == 200:
                    fdata = await f_resp.json()
                    if fdata.get("movie_results"):
                        tmdb_id = str(fdata["movie_results"][0]["id"])
                        media_type = "movie"
                    elif fdata.get("tv_results"):
                        tmdb_id = str(fdata["tv_results"][0]["id"])
                        media_type = "tv"
        except Exception:
            pass

    if tmdb_id and TMDB_API_KEY:
        params = {
            "api_key": TMDB_API_KEY,
            "append_to_response": "images,external_ids,release_dates,content_ratings,credits",
            "include_image_language": "en,te,hi,ta,ml,kn,null",
        }
        try:
            async with http.get(f"{TMDB_BASE}/{media_type}/{tmdb_id}", params=params) as resp:
                if resp.status == 200:
                    details = await resp.json()
                    title = details.get("title") or details.get("name") or title
                    r_date = details.get("release_date") or details.get("first_air_date") or ""
                    if r_date:
                        year = r_date[:4]
                    if details.get("belongs_to_collection"):
                        collection_id = details["belongs_to_collection"]["id"]
                        collection_name = details["belongs_to_collection"].get("name") or ""
                    external_ids = details.get("external_ids", {}) or {}
                    tvdb_id = external_ids.get("tvdb_id")
                    if not imdb_id:
                        imdb_id = details.get("imdb_id") or external_ids.get("imdb_id")
                    for person in (details.get("credits", {}).get("cast") or [])[:5]:
                        person_name = (person.get("name") or "").strip()
                        profile_path = person.get("profile_path")
                        if person_name:
                            cast_members.append({
                                "name": person_name,
                                "profile": f"https://image.tmdb.org/t/p/w185{profile_path}" if profile_path else None,
                            })

                    orig_lang = details.get("original_language") or "te"
                    language = LANG_MAP.get(orig_lang, orig_lang.upper())

                    prod_comps = details.get("production_companies") or []
                    if prod_comps and prod_comps[0].get("name"):
                        studio = prod_comps[0]["name"]

                    vote = details.get("vote_average")
                    if vote and float(vote) > 0:
                        rating = f"{float(vote):.1f}"

                    runtime_mins = details.get("runtime") or (
                        details.get("episode_run_time")[0] if details.get("episode_run_time") else 0
                    )
                    genres = [g["name"] for g in details.get("genres", []) if g.get("name")]
                    overview = details.get("overview") or ""

                    img_data = details.get("images", {})
                    for b in img_data.get("backdrops", [])[:12]:
                        backdrops.append({
                            "preview": f"{TMDB_IMG_PREVIEW}{b['file_path']}",
                            "original": f"{TMDB_IMG_ORIGINAL}{b['file_path']}",
                        })
                    for p in img_data.get("posters", [])[:12]:
                        posters.append({
                            "preview": f"{TMDB_IMG_PREVIEW}{p['file_path']}",
                            "original": f"{TMDB_IMG_ORIGINAL}{p['file_path']}",
                        })
                    for lg in img_data.get("logos", [])[:8]:
                        if lg.get("file_path", "").endswith(".png"):
                            logos.append({
                                "preview": f"{TMDB_IMG_LOGO}{lg['file_path']}",
                                "original": f"{TMDB_IMG_ORIGINAL}{lg['file_path']}",
                            })
                    if not backdrops and details.get("backdrop_path"):
                        bp = details["backdrop_path"]
                        backdrops.append({"preview": f"{TMDB_IMG_PREVIEW}{bp}", "original": f"{TMDB_IMG_ORIGINAL}{bp}"})
                    if not posters and details.get("poster_path"):
                        pp = details["poster_path"]
                        posters.append({"preview": f"{TMDB_IMG_PREVIEW}{pp}", "original": f"{TMDB_IMG_ORIGINAL}{pp}"})
                    if not backdrops and item.get("backdrop_path"):
                        bp = item["backdrop_path"]
                        backdrops.append({"preview": f"{TMDB_IMG_PREVIEW}{bp}", "original": f"{TMDB_IMG_ORIGINAL}{bp}"})
                    if not posters and item.get("poster_path"):
                        pp = item["poster_path"]
                        posters.append({"preview": f"{TMDB_IMG_PREVIEW}{pp}", "original": f"{TMDB_IMG_ORIGINAL}{pp}"})
        except Exception:
            pass

    need_imdb = (not backdrops) or (not posters) or (not rating) or (not overview) or (not runtime_mins)
    if need_imdb:
        fallback_poster = item.get("poster")
        if not imdb_id:
            imdb_candidates = await search_imdb(http, f"{title} {year}" if year != "?" else title)
            if imdb_candidates:
                imdb_id = imdb_candidates[0]["id"]
                if not fallback_poster:
                    fallback_poster = imdb_candidates[0].get("poster")

        if imdb_id or fallback_poster:
            imdb_bundle = await scrape_imdb_data_and_images(http, imdb_id or "", fallback_poster)
            if not backdrops and not posters:
                source_label = "🟨 IMDb"
            if not posters and imdb_bundle["posters"]:
                posters = imdb_bundle["posters"]
            if not backdrops and imdb_bundle["backdrops"]:
                backdrops = imdb_bundle["backdrops"]

            im_meta = imdb_bundle["meta"]
            if not rating and im_meta.get("rating"):
                rating = im_meta["rating"]
            if not runtime_mins and im_meta.get("runtime_mins"):
                runtime_mins = im_meta["runtime_mins"]
            if not genres and im_meta.get("genres"):
                genres = im_meta["genres"]
            if not overview and im_meta.get("overview"):
                overview = im_meta.get("overview") or ""
            if im_meta.get("certification"):
                certification = im_meta["certification"]

    # Fanart tab is populated only with real Fanart.tv results for this exact title.
    fanart = await fetch_fanart_tv(http, str(tmdb_id) if tmdb_id else None, str(tvdb_id) if tvdb_id else None, media_type)

    return {
        "title": title,
        "year": year if year != "?" else "N/A",
        "media_type": "SERIES" if media_type == "tv" else "MOVIE",
        "rating": rating or "Not Rated",
        "runtime": _format_runtime(runtime_mins) if runtime_mins else "N/A",
        "genres": genres[:3],
        "certification": certification or "Not Rated",
        "language": language,
        "studio": studio,
        "overview": overview or "Overview unavailable.",
        "source_label": source_label,
        "collection_id": collection_id,
        "collection_name": collection_name,
        "backdrops": backdrops[:10],
        "posters": posters[:10],
        "fanart": fanart[:10],
        "logos": logos[:8],
        "cast": cast_members,
    }


# ==============================================================================
# 🎨 UI BUILDERS (VIEWER, LOGO PICKER, POSITION PICKER, 16 CUSTOM STYLES)
# ==============================================================================
def build_viewer_caption(session: Dict[str, Any]) -> str:
    tab = session["tab"]
    idx = session["index"]
    items = session["data"].get(tab, [])
    total = max(len(items), 1)
    title = html.escape(session["data"]["title"])
    source = "🎨 Fanart.tv" if tab == "fanart" else session["data"]["source_label"]

    if tab == "backdrops":
        folder_line = f"🗂 🏞 <b>Backdrops</b>  ·  {idx + 1} / {total}"
        meta_line = "🏷 Backdrop  ·  🏞 Landscape"
    elif tab == "posters":
        folder_line = f"🗂 🖼 <b>Posters</b>  ·  {idx + 1} / {total}"
        meta_line = "🏷 Poster  ·  🖼 Portrait"
    else:
        folder_line = f"🗂 🎨 <b>Fanart</b>  ·  {idx + 1} / {total}"
        meta_line = "🏷 Fanart  ·  🎨 Artwork"

    return (
        f"<blockquote>🎬 {title}</blockquote>\n"
        f"{folder_line}\n"
        f"<blockquote>{meta_line}\n"
        f"🔗 Source: {source}</blockquote>"
    )


def build_viewer_keyboard(sid: str, session: Dict[str, Any]) -> dict:
    tab = session["tab"]
    idx = session["index"]
    data = session["data"]
    items = data.get(tab, [])
    total = max(len(items), 1)
    current_img = items[idx]["original"] if items else "https://t.me"

    tabs_row = []
    if data.get("backdrops"):
        tabs_row.append({
            "text": ("✅ 🏞 Backdrops" if tab == "backdrops" else "🏞 Backdrops"),
            "callback_data": f"tab:{sid}:backdrops",
            **({"style": "success"} if tab == "backdrops" else {}),
        })
    if data.get("posters"):
        tabs_row.append({
            "text": ("✅ 🖼 Posters" if tab == "posters" else "🖼 Posters"),
            "callback_data": f"tab:{sid}:posters",
            **({"style": "success"} if tab == "posters" else {}),
        })
    if data.get("fanart"):
        tabs_row.append({
            "text": ("✅ 🎨 Fanart" if tab == "fanart" else "🎨 Fanart"),
            "callback_data": f"tab:{sid}:fanart",
            **({"style": "success"} if tab == "fanart" else {}),
        })

    nav_row = []
    if idx > 0:
        nav_row.append({"text": "◀️ Prev", "callback_data": f"nav:{sid}:{idx - 1}"})
    nav_row.append({"text": f"{idx + 1} / {total}", "callback_data": f"noop:{sid}"})
    if idx + 1 < total:
        nav_row.append({"text": "Next ▶️", "callback_data": f"nav:{sid}:{idx + 1}"})

    keyboard = [
        tabs_row,
        nav_row,
        [{"text": "📥 Save Image", "callback_data": f"save:{sid}", "style": "success"}],
        [
            {"text": "🎬 Make Thumbnail", "callback_data": f"thumb:{sid}", "style": "primary"},
            {"text": "🎨 Custom Styles", "callback_data": f"style:{sid}", "style": "primary"},
        ],
        [{"text": "🛠 Manual Edit", "callback_data": f"edit:{sid}"}],
        [
            {"text": "🎬 Sticker", "callback_data": f"sticker:{sid}"},
            {"text": "🗂 Franchise", "callback_data": f"fran:{sid}"},
        ],
        [{"text": "📋 Copy Image Link", "copy_text": {"text": current_img}}],
        [{"text": "✖ Cancel Session", "callback_data": f"close:{sid}", "style": "danger"}],
    ]
    return {"inline_keyboard": keyboard}


def build_logo_picker_ui(sid: str, session: Dict[str, Any]) -> Tuple[str, dict]:
    logos = session["data"].get("logos", [])
    l_idx = session.get("logo_index", 0)
    total = max(len(logos), 1)

    caption = (
        f"🖼 <b>Choose a Logo</b>\n"
        f"<blockquote>Logo {l_idx + 1} / {total}  ·  TMDB</blockquote>\n"
        f"✅ to use this logo for the thumbnail, or skip to use the title as text instead."
    )

    nav_row = []
    if l_idx > 0:
        nav_row.append({"text": "◀️", "callback_data": f"lg_nav:{sid}:{l_idx - 1}"})
    nav_row.append({"text": f"{l_idx + 1} / {total}", "callback_data": f"noop:{sid}"})
    if l_idx + 1 < total:
        nav_row.append({"text": "▶️", "callback_data": f"lg_nav:{sid}:{l_idx + 1}"})

    kb = {
        "inline_keyboard": [
            nav_row,
            [{"text": "✅ Use This Logo", "callback_data": f"lg_use:{sid}:{l_idx}", "style": "success"}],
            [
                {"text": "⚠️ Skip — Use Title", "callback_data": f"lg_skip:{sid}", "style": "primary"},
                {"text": "✖ Cancel", "callback_data": f"lg_back:{sid}", "style": "danger"},
            ],
        ]
    }
    return caption, kb


def build_position_picker_ui(sid: str, session: Dict[str, Any]) -> Tuple[str, dict]:
    chosen_logo = session.get("chosen_logo")
    if chosen_logo is not None:
        status_line = f"🖼 Logo: ✅ Logo #{chosen_logo + 1} selected"
        action_btn_text = "✅ Center Logo — Generate"
    else:
        status_line = "🖼 Logo: ⚠️ Skipped — will use title text"
        action_btn_text = "⚠️ No logo — use title text"

    caption = (
        f"🎬 <b>Make Thumbnail</b>\n\n"
        f"<blockquote>{status_line}</blockquote>\n"
        f"<b>Pick logo position</b>\n"
        f"↖️ ⬆️ ↗️   ·   ◀️ ⬛ ▶️\n"
        f"↙️ ⬇️ ↘️   ·"
    )

    kb = {
        "inline_keyboard": [
            [
                {"text": "↖️", "callback_data": f"pos:{sid}:tl"},
                {"text": "⬆️", "callback_data": f"pos:{sid}:tc"},
                {"text": "↗️", "callback_data": f"pos:{sid}:tr"},
                {"text": "◀️", "callback_data": f"pos:{sid}:cl"},
                {"text": "⬛", "callback_data": f"pos:{sid}:cc"},
                {"text": "▶️", "callback_data": f"pos:{sid}:cr"},
            ],
            [
                {"text": "↙️", "callback_data": f"pos:{sid}:bl"},
                {"text": "⬇️", "callback_data": f"pos:{sid}:bc"},
                {"text": "↘️", "callback_data": f"pos:{sid}:br"},
            ],
            [{"text": action_btn_text, "callback_data": f"pos:{sid}:bc", "style": "primary"}],
            [{"text": "◀️ Back", "callback_data": f"lg_back:{sid}"}],
        ]
    }
    return caption, kb


STYLE_META: Dict[str, Tuple[str, str, str]] = {
    "vertical": ("📱", "Vertical Thumbnail", "vertical"),
    "magic": ("✨", "Magic Thumbnail", "magic"),
    "mood": ("🎨", "Mood Poster Thumbnail", "mood"),
    "premiere": ("🎟", "Premiere Thumbnail", "premiere"),
    "split": ("🌗", "Split-Frame Thumbnail", "split-frame"),
    "tile": ("🏷", "Tile Thumbnail", "tile"),
    "cast": ("🎭", "Cast Thumbnail", "cast"),
    "anime": ("🎬", "Anime Poster Thumbnail", "anime poster"),
    "infocard": ("🗂", "Info Card Thumbnail", "info card"),
    "hero": ("🎥", "Cinematic Hero Thumbnail", "cinematic hero"),
    "ticket": ("🎟", "Ticket Stub Thumbnail", "ticket stub"),
    "dashboard": ("🖥", "Dashboard Thumbnail", "dashboard"),
    "manwa": ("📖", "MANWA Thumbnail", "manwa"),
    "character": ("🧍", "Character Thumbnail", "character"),
    "giant": ("🔠", "Giant Type Thumbnail", "giant type"),
    "breakout": ("🦸", "Breakout Thumbnail", "breakout"),
    "orbit": ("🪐", "Orbit Thumbnail", "orbit"),
    "spotlight": ("🎞", "Spotlight Thumbnail", "spotlight"),
}


def build_custom_styles_keyboard(sid: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "📱 Vertical", "callback_data": f"cstyle:{sid}:vertical", "style": "primary"},
                {"text": "🎞 Spotlight", "callback_data": f"cstyle:{sid}:spotlight", "style": "primary"},
            ],
            [
                {"text": "✨ Magic", "callback_data": f"cstyle:{sid}:magic", "style": "primary"},
                {"text": "🎨 Mood Poster", "callback_data": f"cstyle:{sid}:mood", "style": "primary"},
            ],
            [
                {"text": "🎟 Premiere", "callback_data": f"cstyle:{sid}:premiere", "style": "primary"},
                {"text": "🌗 Split-Frame", "callback_data": f"cstyle:{sid}:split", "style": "primary"},
            ],
            [
                {"text": "🏷 Tile", "callback_data": f"cstyle:{sid}:tile", "style": "primary"},
                {"text": "🎭 Cast", "callback_data": f"cstyle:{sid}:cast", "style": "primary"},
            ],
            [
                {"text": "🎬 Anime Poster", "callback_data": f"cstyle:{sid}:anime", "style": "primary"},
                {"text": "🗂 Info Card", "callback_data": f"cstyle:{sid}:infocard", "style": "primary"},
            ],
            [
                {"text": "🎥 Cinematic Hero", "callback_data": f"cstyle:{sid}:hero", "style": "primary"},
                {"text": "🎟 Ticket Stub", "callback_data": f"cstyle:{sid}:ticket", "style": "primary"},
            ],
            [
                {"text": "🖥 Dashboard", "callback_data": f"cstyle:{sid}:dashboard", "style": "primary"},
                {"text": "📖 MANWA", "callback_data": f"cstyle:{sid}:manwa", "style": "primary"},
            ],
            [
                {"text": "🧍 Character", "callback_data": f"cstyle:{sid}:character", "style": "primary"},
                {"text": "🔠 Giant Type", "callback_data": f"cstyle:{sid}:giant", "style": "primary"},
            ],
            [
                {"text": "🦸 Breakout", "callback_data": f"cstyle:{sid}:breakout", "style": "primary"},
                {"text": "🪐 Orbit", "callback_data": f"cstyle:{sid}:orbit", "style": "primary"},
            ],
            [
                {"text": "✖ Close", "callback_data": f"close_msg:{sid}", "style": "danger"}
            ],
        ]
    }


def build_generated_thumb_keyboard(sid: str, thumb_id: str, copy_url: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "📋 Copy Link", "copy_text": {"text": copy_url}}],
            [
                {"text": "🎬 Generate Sticker", "callback_data": f"gen_stk:{sid}:{thumb_id}", "style": "primary"},
                {"text": "✖ Cancel", "callback_data": f"close_msg:{sid}", "style": "danger"},
            ],
        ]
    }


def build_franchise_card_keyboard(sid: str, image_url: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "✖ Close", "callback_data": f"close_msg:{sid}", "style": "danger"}],
            [{"text": "🔗 Image Link", "url": image_url}],
        ]
    }


def group_only_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "🚀 Join Official Group", "url": GROUP_LINK, "style": "primary"}]
        ]
    }


# ==============================================================================
# Poster-derived accent colors keep labels, pills, borders, and badges matched to each title.
def poster_palette(poster_bytes: bytes):
    try:
        with Image.open(io.BytesIO(poster_bytes)) as source:
            thumb = source.convert("RGB").resize((24, 24), Image.Resampling.LANCZOS)
            colors = thumb.getcolors(24 * 24) or []
            colors.sort(key=lambda pair: pair[0], reverse=True)
            candidates = [rgb for _, rgb in colors if max(rgb) - min(rgb) > 28 and max(rgb) > 75]
            accent = candidates[0] if candidates else (104, 93, 220)
            accent = tuple(max(65, min(225, int(c * 1.12))) for c in accent)
            dark = tuple(max(8, int(c * 0.20)) for c in accent)
            light = tuple(min(255, int(c * 0.72 + 80)) for c in accent)
            return accent, dark, light
    except Exception:
        return (104, 93, 220), (20, 18, 48), (190, 185, 255)


# 🖼 HIGH-QUALITY PILLOW RENDERERS (MAGIC, MOOD, PREMIERE, SPLIT, TILE, FRANCHISE)
# ==============================================================================
def _load_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    candidates = (
        ("DejaVuSans-Bold.ttf", "arialbd.ttf", "LiberationSans-Bold.ttf", "FreeSansBold.ttf")
        if bold
        else ("DejaVuSans.ttf", "arial.ttf", "LiberationSans-Regular.ttf", "FreeSans.ttf")
    )
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _draw_telegram_icon(canvas: Image.Image, x: int, y: int, diameter: int = 26):
    scale = 4
    s = diameter * scale
    icon = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(icon)
    d.ellipse([(0, 0), (s - 1, s - 1)], fill=(42, 171, 238, 255))
    p1 = (int(s * 0.19), int(s * 0.49))
    p2 = (int(s * 0.78), int(s * 0.25))
    p3 = (int(s * 0.65), int(s * 0.76))
    p4 = (int(s * 0.45), int(s * 0.58))
    d.polygon([p1, p2, p3, p4], fill=(255, 255, 255, 255))
    d.polygon(
        [
            (int(s * 0.42), int(s * 0.56)),
            (int(s * 0.78), int(s * 0.25)),
            (int(s * 0.48), int(s * 0.62)),
        ],
        fill=(215, 238, 252, 255),
    )
    icon = icon.resize((diameter, diameter), Image.Resampling.LANCZOS)
    canvas.paste(icon, (x, y), icon)


def _rounded_poster_card(
    poster_bytes: bytes,
    w: int,
    h: int,
    radius: int = 18,
    border_width: int = 0,
    border_color: Tuple[int, int, int, int] = (255, 255, 255, 255),
) -> Image.Image:
    with Image.open(io.BytesIO(poster_bytes)) as im:
        card = ImageOps.fit(im.convert("RGBA"), (w, h), method=Image.Resampling.LANCZOS)

    mask = Image.new("L", (w, h), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.rounded_rectangle([(0, 0), (w - 1, h - 1)], radius=radius, fill=255)

    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(card, (0, 0), mask)

    if border_width > 0:
        bdraw = ImageDraw.Draw(out)
        bdraw.rounded_rectangle(
            [(border_width // 2, border_width // 2), (w - 1 - border_width // 2, h - 1 - border_width // 2)],
            radius=radius,
            outline=border_color,
            width=border_width,
        )
    return out


def _paste_with_shadow(canvas: Image.Image, card: Image.Image, x: int, y: int, blur: int = 18):
    sw, sh = card.width + blur * 3, card.height + blur * 3
    shadow = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    sdraw.rounded_rectangle(
        [(blur, blur), (blur + card.width, blur + card.height)],
        radius=20,
        fill=(0, 0, 0, 175),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    canvas.paste(shadow, (x - blur + 4, y - blur + 8), shadow)
    canvas.paste(card, (x, y), card)


def render_magic_thumbnail(bg_bytes: bytes, poster_bytes: bytes, meta: Dict[str, Any]) -> bytes:
    accent, dark_accent, light_accent = poster_palette(poster_bytes)
    W, H = 1280, 720
    with Image.open(io.BytesIO(bg_bytes)) as bg:
        canvas = ImageOps.fit(bg.convert("RGBA"), (W, H), method=Image.Resampling.LANCZOS)

    grad = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(grad)
    for x in range(W):
        left_factor = max(0.0, 1.0 - (x / 920.0))
        alpha = int(195 * (left_factor ** 1.25))
        gdraw.line([(x, 0), (x, H)], fill=(8, 6, 4, alpha))
    for y in range(380, H):
        b_alpha = int(160 * ((y - 380) / (H - 380)))
        gdraw.line([(0, y), (W, y)], fill=(5, 4, 3, b_alpha))
    canvas = Image.alpha_composite(canvas, grad)

    p_card = _rounded_poster_card(poster_bytes, 305, 455, radius=16, border_width=4)
    _paste_with_shadow(canvas, p_card, 895, 118, blur=16)

    draw = ImageDraw.Draw(canvas)
    title_str = meta["title"].upper()
    title_size = 58 if len(title_str) <= 14 else (46 if len(title_str) <= 20 else 36)
    f_title = _load_font(title_size, bold=True)

    tx, ty = 72, 200
    draw.text((tx + 2, ty + 3), title_str, font=f_title, fill=(0, 0, 0, 210))
    draw.text((tx, ty), title_str, font=f_title, fill=(255, 255, 255, 255))

    tb = draw.textbbox((tx, ty), title_str, font=f_title)
    star_x = min(tb[2] + 18, 710)
    star_y = tb[1] + (tb[3] - tb[1]) // 2 - 14

    f_rate = _load_font(24, bold=True)
    draw.text((star_x, star_y), "★", font=f_rate, fill=(245, 197, 24, 255))
    draw.text((star_x + 26, star_y), str(meta.get("rating") or "Not Rated"), font=f_rate, fill=(255, 255, 255, 255))
    rb = draw.textbbox((star_x + 26, star_y), str(meta.get("rating") or "Not Rated"), font=f_rate)

    imdb_x = rb[2] + 10
    draw.rounded_rectangle(
        [(imdb_x, star_y + 3), (imdb_x + 48, star_y + 25)],
        radius=5,
        fill=(40, 32, 18, 220),
        outline=(245, 197, 24, 200),
        width=1,
    )
    f_imdb = _load_font(13, bold=True)
    draw.text((imdb_x + 7, star_y + 6), "IMDb", font=f_imdb, fill=(245, 197, 24, 255))

    f_desc = _load_font(20, bold=False)
    lines = textwrap.wrap(meta.get("overview") or "Overview unavailable.", width=62)[:2]
    dy = tb[3] + 24
    for line in lines:
        draw.text((tx, dy), line, font=f_desc, fill=(235, 235, 235, 245))
        dy += 28

    pill_y = dy + 18
    px = tx
    f_pill = _load_font(15, bold=True)

    cert_text = meta.get("certification", "Not Rated")
    cb = draw.textbbox((0, 0), cert_text, font=f_pill)
    cw = (cb[2] - cb[0]) + 22
    draw.rounded_rectangle(
        [(px, pill_y), (px + cw, pill_y + 28)],
        radius=5,
        fill=(20, 20, 20, 140),
        outline=(220, 220, 220, 220),
        width=1,
    )
    draw.text((px + 11, pill_y + 5), cert_text, font=f_pill, fill=(255, 255, 255, 255))
    px += cw + 10

    gold_pills = [str(meta.get("runtime") or "N/A").upper()] + [g.upper() for g in meta.get("genres", [])[:2]] + [str(meta.get("year") or "N/A")]
    for gp in gold_pills:
        gb = draw.textbbox((0, 0), gp, font=f_pill)
        gw = (gb[2] - gb[0]) + 24
        draw.rounded_rectangle(
            [(px, pill_y), (px + gw, pill_y + 28)],
            radius=14,
            fill=(*accent, 235),
        )
        draw.text((px + 12, pill_y + 5), gp, font=f_pill, fill=(255, 255, 255, 255))
        px += gw + 10

    wm_y = pill_y + 46
    bot_handle = f"@{BOT_USERNAME.lstrip('@').upper()}"
    f_wm = _load_font(17, bold=True)
    wb = draw.textbbox((0, 0), bot_handle, font=f_wm)
    wm_w = (wb[2] - wb[0]) + 56

    wm_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    wdraw = ImageDraw.Draw(wm_layer)
    wdraw.rounded_rectangle(
        [(tx, wm_y), (tx + wm_w, wm_y + 38)],
        radius=19,
        fill=(45, 36, 26, 195),
        outline=(255, 255, 255, 65),
        width=1,
    )
    canvas = Image.alpha_composite(canvas, wm_layer)
    _draw_telegram_icon(canvas, tx + 10, wm_y + 6, diameter=26)
    draw = ImageDraw.Draw(canvas)
    draw.text((tx + 44, wm_y + 9), bot_handle, font=f_wm, fill=(255, 255, 255, 255))

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="JPEG", quality=96)
    return out.getvalue()


def render_mood_poster_thumbnail(bg_bytes: bytes, poster_bytes: bytes, meta: Dict[str, Any]) -> bytes:
    W, H = 1280, 720
    accent, dark_accent, light_accent = poster_palette(poster_bytes)
    with Image.open(io.BytesIO(poster_bytes)) as pim:
        small = pim.convert("RGB").resize((1, 1), Image.Resampling.LANCZOS)
        r, g, b = small.getpixel((0, 0))
        base_r = max(35, min(95, int(r * 0.45 + 30)))
        base_g = max(30, min(80, int(g * 0.40 + 22)))
        base_b = max(12, min(45, int(b * 0.22 + 8)))

    canvas = Image.new("RGBA", (W, H), (base_r, base_g, base_b, 255))

    with Image.open(io.BytesIO(bg_bytes)) as bg:
        bg_fit = ImageOps.fit(bg.convert("RGBA"), (W, H), method=Image.Resampling.LANCZOS)
        bg_blur = bg_fit.filter(ImageFilter.GaussianBlur(42))
        canvas = Image.blend(canvas, bg_blur, alpha=0.28)

    grad = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(grad)
    for x in range(W):
        a = int(130 * max(0.2, 1.0 - (x / 1100.0)))
        gdraw.line([(x, 0), (x, H)], fill=(12, 10, 4, a))
    canvas = Image.alpha_composite(canvas, grad)

    p_card = _rounded_poster_card(poster_bytes, 405, 595, radius=18, border_width=0)
    _paste_with_shadow(canvas, p_card, 790, 62, blur=24)

    draw = ImageDraw.Draw(canvas)
    lx = 80

    f_top = _load_font(18, bold=True)
    draw.line([(lx, 108), (lx + 45, 108)], fill=(*accent, 255), width=3)
    top_label = f"{meta['media_type']}   ·   {meta['year']}"
    draw.text((lx + 62, 98), top_label, font=f_top, fill=(*light_accent, 255))

    title_str = meta["title"].upper()
    title_lines = textwrap.wrap(title_str, width=15)[:2]
    f_giant = _load_font(64 if len(title_str) <= 18 else 54, bold=True)
    ty = 190
    for title_line in title_lines:
        draw.text((lx + 3, ty + 4), title_line, font=f_giant, fill=(0, 0, 0, 160))
        draw.text((lx, ty), title_line, font=f_giant, fill=(255, 255, 255, 255))
        ty += 72

    div_y = 496
    draw.line([(lx, div_y), (725, div_y)], fill=(220, 205, 165, 140), width=2)

    f_lbl = _load_font(15, bold=True)
    f_val = _load_font(34, bold=True)
    col_y = 520

    draw.text((lx, col_y), "RATING", font=f_lbl, fill=(205, 195, 160, 230))
    draw.text((lx, col_y + 24), str(meta.get("rating") or "Not Rated"), font=f_val, fill=(255, 255, 255, 255))

    draw.text((lx + 120, col_y), "RUNTIME", font=f_lbl, fill=(205, 195, 160, 230))
    draw.text((lx + 120, col_y + 24), meta["runtime"], font=f_val, fill=(255, 255, 255, 255))

    draw.text((lx + 300, col_y), "YEAR", font=f_lbl, fill=(205, 195, 160, 230))
    draw.text((lx + 300, col_y + 24), str(meta.get("year") or "N/A"), font=f_val, fill=(255, 255, 255, 255))

    f_gen = _load_font(20, bold=False)
    genre_line = "   /   ".join(g.upper() for g in meta.get("genres", [])[:2])
    draw.text((lx, 606), genre_line, font=f_gen, fill=(*accent, 255))

    _draw_telegram_icon(canvas, lx, 650, diameter=24)
    draw = ImageDraw.Draw(canvas)
    f_wm = _load_font(17, bold=True)
    bot_handle = f"@{BOT_USERNAME.lstrip('@').upper()}"
    draw.text((lx + 32, 653), bot_handle, font=f_wm, fill=(245, 245, 245, 240))

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="JPEG", quality=96)
    return out.getvalue()


def render_premiere_thumbnail(bg_bytes: bytes, poster_bytes: bytes, meta: Dict[str, Any]) -> bytes:
    accent, dark_accent, light_accent = poster_palette(poster_bytes)
    W, H = 1280, 720
    with Image.open(io.BytesIO(bg_bytes)) as bg:
        canvas = ImageOps.fit(bg.convert("RGBA"), (W, H), method=Image.Resampling.LANCZOS)

    grad = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(grad)
    for x in range(W):
        lf = max(0.0, 1.0 - (x / 760.0))
        gdraw.line([(x, 0), (x, H)], fill=(5, 5, 5, int(175 * lf)))
    for y in range(440, H):
        bf = (y - 440) / (H - 440)
        gdraw.line([(0, y), (W, y)], fill=(5, 5, 5, int(215 * bf)))
    canvas = Image.alpha_composite(canvas, grad)

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)

    bx1, by1, bx2, by2 = 1042, 30, 1236, 136
    odraw.rounded_rectangle(
        [(bx1, by1), (bx2, by2)],
        radius=14,
        fill=(18, 14, 10, 205),
        outline=(212, 175, 55, 110),
        width=1,
    )

    lx = 64
    title_str = meta["title"].upper()
    f_title = _load_font(42 if len(title_str) <= 18 else 32, bold=True)
    ty = 260

    f_pill = _load_font(15, bold=True)
    pills = (
        [str(meta.get("runtime") or "N/A").upper()]
        + [g.upper() for g in meta.get("genres", [])[:2]]
        + [str(meta.get("year") or "N/A"), meta.get("certification", "NOT RATED").upper()]
    )
    px = lx
    pill_y = ty + 62
    pill_boxes = []
    for p_txt in pills:
        pb = odraw.textbbox((0, 0), p_txt, font=f_pill)
        pw = (pb[2] - pb[0]) + 24
        odraw.rounded_rectangle(
            [(px, pill_y), (px + pw, pill_y + 28)],
            radius=6,
            fill=(18, 18, 18, 195),
            outline=(255, 255, 255, 55),
            width=1,
        )
        pill_boxes.append((px + 12, pill_y + 5, p_txt))
        px += pw + 10

    canvas = Image.alpha_composite(canvas, overlay)
    draw = ImageDraw.Draw(canvas)

    f_b_rate = _load_font(34, bold=True)
    f_b_sub = _load_font(12, bold=True)
    f_b_star = _load_font(15, bold=True)
    rate_label = f"(( {meta['rating']} ))"
    rb = draw.textbbox((0, 0), rate_label, font=f_b_rate)
    rx = bx1 + ((bx2 - bx1) - (rb[2] - rb[0])) // 2
    draw.text((rx, by1 + 10), rate_label, font=f_b_rate, fill=(245, 200, 55, 255))

    ro = draw.textbbox((0, 0), str(meta.get("rating") or "Not Rated"), font=f_b_rate)
    rox = bx1 + ((bx2 - bx1) - (ro[2] - ro[0])) // 2
    draw.text((rox, by1 + 10), str(meta.get("rating") or "Not Rated"), font=f_b_rate, fill=(255, 255, 255, 255))

    sub_txt = "IMDb RATING"
    sb = draw.textbbox((0, 0), sub_txt, font=f_b_sub)
    draw.text((bx1 + ((bx2 - bx1) - (sb[2] - sb[0])) // 2, by1 + 56), sub_txt, font=f_b_sub, fill=(215, 215, 215, 240))

    stars_txt = "★ ★ ★ ★ ★"
    stb = draw.textbbox((0, 0), stars_txt, font=f_b_star)
    draw.text((bx1 + ((bx2 - bx1) - (stb[2] - stb[0])) // 2, by1 + 76), stars_txt, font=f_b_star, fill=(245, 197, 24, 255))

    draw.text((lx + 2, ty + 2), title_str, font=f_title, fill=(0, 0, 0, 210))
    draw.text((lx, ty), title_str, font=f_title, fill=(255, 255, 255, 255))
    for p_tx, p_ty, p_txt in pill_boxes:
        draw.text((p_tx, p_ty), p_txt, font=f_pill, fill=(245, 245, 245, 255))

    f_desc = _load_font(20, bold=False)
    lines = textwrap.wrap(meta.get("overview") or "Overview unavailable.", width=50)[:3]
    dy = pill_y + 44
    for line in lines:
        draw.text((lx + 1, dy + 1), line, font=f_desc, fill=(0, 0, 0, 190))
        draw.text((lx, dy), line, font=f_desc, fill=(240, 240, 240, 250))
        dy += 28

    line_y = 656
    draw.line([(0, line_y), (W, line_y)], fill=(255, 255, 255, 75), width=1)

    f_foot_l = _load_font(15, bold=True)
    f_foot_r = _load_font(16, bold=True)
    left_foot = "STAY CONNECTED"
    right_foot = f"@{BOT_USERNAME.lstrip('@').upper()}"

    lb = draw.textbbox((0, 0), left_foot, font=f_foot_l)
    rb2 = draw.textbbox((0, 0), right_foot, font=f_foot_r)
    total_w = (lb[2] - lb[0]) + 14 + 22 + 8 + (rb2[2] - rb2[0])
    start_x = (W - total_w) // 2
    fy = 674

    draw.text((start_x, fy), left_foot, font=f_foot_l, fill=(200, 200, 200, 235))
    icon_x = start_x + (lb[2] - lb[0]) + 14
    _draw_telegram_icon(canvas, icon_x, fy - 3, diameter=22)
    draw = ImageDraw.Draw(canvas)
    draw.text((icon_x + 30, fy - 1), right_foot, font=f_foot_r, fill=(255, 255, 255, 255))

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="JPEG", quality=96)
    return out.getvalue()


def render_split_frame_thumbnail(bg_bytes: bytes, poster_bytes: bytes, meta: Dict[str, Any]) -> bytes:
    """Renders exact 1280x720 Split-Frame Thumbnail matching Images 4, 5, 6."""
    W, H = 1280, 720
    accent, dark_accent, light_accent = poster_palette(poster_bytes)
    split_x = 584
    canvas = Image.new("RGBA", (W, H), (24, 27, 56, 255))

    # Left half: Poster image fitted cleanly
    with Image.open(io.BytesIO(poster_bytes)) as pim:
        left_img = ImageOps.fit(pim.convert("RGBA"), (split_x, H), method=Image.Resampling.LANCZOS)
    canvas.paste(left_img, (0, 0))

    draw = ImageDraw.Draw(canvas)
    rx = 636

    # Top "MOVIE" / "SERIES" tag
    f_type = _load_font(16, bold=True)
    draw.text((rx, 108), meta.get("media_type", "MOVIE").upper(), font=f_type, fill=(118, 134, 219, 255))

    # Bold White Mixed-Case Movie Title
    title_str = meta["title"]
    t_size = 38 if len(title_str) <= 28 else 30
    f_title = _load_font(t_size, bold=True)
    t_lines = textwrap.wrap(title_str, width=30)[:2]
    ty = 138
    for tl in t_lines:
        draw.text((rx, ty), tl, font=f_title, fill=(255, 255, 255, 255))
        ty += t_size + 10

    # Row 1: ★ Rating + IMDb Gold Tag + Indigo Pills (Year, Runtime, Genres)
    row1_y = ty + 14
    f_rate = _load_font(18, bold=True)
    draw.text((rx, row1_y + 2), "★", font=f_rate, fill=(245, 197, 24, 255))
    rate_txt = str(meta.get("rating") or "Not Rated")
    draw.text((rx + 22, row1_y + 2), rate_txt, font=f_rate, fill=(255, 255, 255, 255))
    rb = draw.textbbox((rx + 22, row1_y + 2), rate_txt, font=f_rate)

    imdb_x = rb[2] + 10
    draw.rounded_rectangle(
        [(imdb_x, row1_y + 3), (imdb_x + 42, row1_y + 23)],
        radius=4,
        fill=(65, 52, 22, 240),
        outline=(245, 197, 24, 210),
        width=1,
    )
    f_imdb = _load_font(11, bold=True)
    draw.text((imdb_x + 6, row1_y + 6), "IMDb", font=f_imdb, fill=(245, 197, 24, 255))

    f_pill = _load_font(14, bold=True)
    px = imdb_x + 54
    py = row1_y
    genres_upper = [g.upper() for g in meta.get("genres", [])]
    all_pills = [str(meta.get("year") or "N/A"), str(meta.get("runtime") or "N/A").upper()] + genres_upper + [meta.get("certification", "NOT RATED").upper()]

    for p_txt in all_pills:
        pb = draw.textbbox((0, 0), p_txt, font=f_pill)
        pw = (pb[2] - pb[0]) + 24
        if px + pw > W - 45:
            px = rx
            py += 38
        draw.rounded_rectangle(
            [(px, py), (px + pw, py + 26)],
            radius=13,
            fill=(*accent, 255),
        )
        draw.text((px + 12, py + 5), p_txt, font=f_pill, fill=(255, 255, 255, 255))
        px += pw + 10

    # Overview Text (2-3 lines)
    f_desc = _load_font(18, bold=False)
    desc_lines = textwrap.wrap(meta.get("overview") or "Overview unavailable.", width=52)[:3]
    dy = py + 48
    for dline in desc_lines:
        draw.text((rx, dy), dline, font=f_desc, fill=(225, 230, 245, 255))
        dy += 28

    # Bottom Watermark on Right Panel: Telegram Icon + @BotUsername
    wm_y = 646
    _draw_telegram_icon(canvas, rx, wm_y, diameter=22)
    draw = ImageDraw.Draw(canvas)
    f_wm = _load_font(16, bold=True)
    draw.text((rx + 30, wm_y + 2), f"@{BOT_USERNAME.lstrip('@').upper()}", font=f_wm, fill=(255, 255, 255, 255))

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="JPEG", quality=96)
    return out.getvalue()


def render_tile_thumbnail(bg_bytes: bytes, poster_bytes: bytes, meta: Dict[str, Any]) -> bytes:
    """Renders exact 1280x720 Tile Thumbnail matching Images 7, 8, 9."""
    accent, dark_accent, light_accent = poster_palette(poster_bytes)
    W, H = 1280, 720
    with Image.open(io.BytesIO(bg_bytes)) as bg:
        canvas = ImageOps.fit(bg.convert("RGBA"), (W, H), method=Image.Resampling.LANCZOS)

    # Bottom dark gradient so bottom-left poster tile & text stand out clearly
    grad = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(grad)
    for y in range(260, H):
        factor = (y - 260) / (H - 260)
        alpha = int(235 * (factor ** 1.15))
        gdraw.line([(0, y), (W, y)], fill=(4, 5, 8, alpha))
    canvas = Image.alpha_composite(canvas, grad)

    # Top-Right Watermark Pill: Telegram Icon + @BotUsername
    bot_handle = f"@{BOT_USERNAME.lstrip('@').upper()}"
    f_wm = _load_font(15, bold=True)
    temp_draw = ImageDraw.Draw(canvas)
    wb = temp_draw.textbbox((0, 0), bot_handle, font=f_wm)
    pill_w = (wb[2] - wb[0]) + 52
    wx = W - pill_w - 36
    wy = 28

    wm_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    wdraw = ImageDraw.Draw(wm_layer)
    wdraw.rounded_rectangle(
        [(wx, wy), (wx + pill_w, wy + 34)],
        radius=17,
        fill=(18, 22, 30, 195),
        outline=(255, 255, 255, 55),
        width=1,
    )
    canvas = Image.alpha_composite(canvas, wm_layer)
    _draw_telegram_icon(canvas, wx + 8, wy + 5, diameter=24)
    draw = ImageDraw.Draw(canvas)
    draw.text((wx + 38, wy + 8), bot_handle, font=f_wm, fill=(255, 255, 255, 255))

    # Bottom-Left Rounded Poster Tile with crisp white border
    tile_card = _rounded_poster_card(
        poster_bytes,
        166,
        244,
        radius=12,
        border_width=2,
        border_color=(230, 230, 235, 240),
    )
    _paste_with_shadow(canvas, tile_card, 48, 424, blur=14)

    draw = ImageDraw.Draw(canvas)
    tx = 242
    ty = 434

    # Bold White Title
    title_str = meta["title"]
    f_title = _load_font(38 if len(title_str) <= 28 else 30, bold=True)
    draw.text((tx + 2, ty + 2), title_str, font=f_title, fill=(0, 0, 0, 210))
    draw.text((tx, ty), title_str, font=f_title, fill=(255, 255, 255, 255))

    # 2-Line Synopsis
    f_desc = _load_font(18, bold=False)
    lines = textwrap.wrap(meta.get("overview") or "Overview unavailable.", width=48)[:2]
    dy = ty + 52
    for line in lines:
        draw.text((tx + 1, dy + 1), line, font=f_desc, fill=(0, 0, 0, 200))
        draw.text((tx, dy), line, font=f_desc, fill=(230, 232, 240, 250))
        dy += 26

    # Bottom Row: ★ Rating + IMDb Gold Tag + Indigo Pills
    py = dy + 14
    f_rate = _load_font(18, bold=True)
    draw.text((tx, py + 2), "★", font=f_rate, fill=(245, 197, 24, 255))
    rate_txt = str(meta.get("rating") or "Not Rated")
    draw.text((tx + 22, py + 2), rate_txt, font=f_rate, fill=(255, 255, 255, 255))
    rb = draw.textbbox((tx + 22, py + 2), rate_txt, font=f_rate)

    imdb_x = rb[2] + 10
    draw.rounded_rectangle(
        [(imdb_x, py + 3), (imdb_x + 42, py + 23)],
        radius=4,
        fill=(65, 52, 22, 240),
        outline=(245, 197, 24, 210),
        width=1,
    )
    f_imdb = _load_font(11, bold=True)
    draw.text((imdb_x + 6, py + 6), "IMDb", font=f_imdb, fill=(245, 197, 24, 255))

    f_pill = _load_font(14, bold=True)
    px = imdb_x + 54
    pills = [str(meta.get("year") or "N/A"), str(meta.get("runtime") or "N/A").upper()] + [g.upper() for g in meta.get("genres", [])[:2]]
    for p_txt in pills:
        pb = draw.textbbox((0, 0), p_txt, font=f_pill)
        pw = (pb[2] - pb[0]) + 24
        draw.rounded_rectangle(
            [(px, py), (px + pw, py + 26)],
            radius=13,
            fill=(*accent, 255),
        )
        draw.text((px + 12, py + 5), p_txt, font=f_pill, fill=(255, 255, 255, 255))
        px += pw + 10

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="JPEG", quality=96)
    return out.getvalue()


def render_franchise_collection_card(
    bg_bytes: bytes,
    poster_bytes_list: List[bytes],
    meta: Dict[str, Any],
    movie_count: int = 1,
    part_years: Optional[List[str]] = None,
) -> bytes:
    """Renders exact 1280x720 Franchise Collection Card matching Images 1, 2, 3."""
    W, H = 1280, 720
    with Image.open(io.BytesIO(bg_bytes)) as bg:
        bg_fit = ImageOps.fit(bg.convert("RGBA"), (W, H), method=Image.Resampling.LANCZOS)
        bg_blur = bg_fit.filter(ImageFilter.GaussianBlur(10))
        bg_dark = ImageEnhance.Brightness(bg_blur).enhance(0.32)

    canvas = bg_dark.convert("RGBA")

    # Warm gold radial glow behind top header & subtle vignette
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    gdraw.ellipse([(370, 5), (910, 215)], fill=(212, 155, 35, 78))
    glow = glow.filter(ImageFilter.GaussianBlur(48))
    canvas = Image.alpha_composite(canvas, glow)

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)

    # 1. Top-Left 4K UHD | HDR10 | DOLBY ATMOS Badge
    odraw.rounded_rectangle(
        [(40, 26), (328, 56)],
        radius=6,
        fill=(18, 14, 10, 210),
        outline=(195, 155, 55, 150),
        width=1,
    )

    # 2. Top-Right Circular Collection Emblem
    cx, cy, cr = 1184, 78, 50
    odraw.ellipse([(cx - cr, cy - cr), (cx + cr, cy + cr)], fill=(18, 12, 12, 225), outline=(195, 45, 45, 220), width=2)
    odraw.ellipse([(cx - cr + 5, cy - cr + 5), (cx + cr - 5, cy + cr - 5)], outline=(212, 175, 55, 170), width=1)
    odraw.rounded_rectangle(
        [(cx - 26, cy + cr + 4), (cx + 26, cy + cr + 24)],
        radius=5,
        fill=(18, 14, 10, 220),
        outline=(212, 175, 55, 150),
        width=1,
    )

    # 3. Top-Center "COMPLETE COLLECTION" Ribbon Box
    rx1, ry1, rx2, ry2 = 458, 96, 822, 128
    odraw.rounded_rectangle(
        [(rx1, ry1), (rx2, ry2)],
        radius=7,
        fill=(48, 18, 16, 235),
        outline=(218, 175, 55, 220),
        width=2,
    )
    odraw.ellipse([(rx1 + 10, ry1 + 12), (rx1 + 18, ry1 + 20)], fill=(245, 197, 24, 255))
    odraw.ellipse([(rx2 - 18, ry1 + 12), (rx2 - 10, ry1 + 20)], fill=(245, 197, 24, 255))

    # 4. 6-Column Bottom Metadata Bar
    bar_x1, bar_y1, bar_x2, bar_y2 = 48, 530, 1232, 586
    odraw.rounded_rectangle(
        [(bar_x1, bar_y1), (bar_x2, bar_y2)],
        radius=8,
        fill=(10, 10, 12, 215),
        outline=(212, 175, 55, 80),
        width=1,
    )
    col_w = (bar_x2 - bar_x1) // 6
    for i in range(1, 6):
        div_x = bar_x1 + i * col_w
        odraw.line([(div_x, bar_y1 + 10), (div_x, bar_y2 - 10)], fill=(255, 255, 255, 45), width=1)

    canvas = Image.alpha_composite(canvas, overlay)
    draw = ImageDraw.Draw(canvas)

    # Populate Top-Left Tech Specs Text
    f_spec = _load_font(11, bold=True)
    draw.text((52, 35), "🎬 4K UHD   |   ✨ HDR10   |   🔊 DOLBY ATMOS", font=f_spec, fill=(235, 215, 165, 255))

    # Populate Top-Right Emblem Text
    f_em1 = _load_font(10, bold=True)
    f_em2 = _load_font(12, bold=True)
    m_lbl = f"{movie_count} MOVIE" if movie_count == 1 else f"{movie_count} MOVIES"
    eb1 = draw.textbbox((0, 0), m_lbl, font=f_em1)
    draw.text((cx - (eb1[2] - eb1[0]) // 2, cy - 18), m_lbl, font=f_em1, fill=(230, 230, 230, 255))
    eb2 = draw.textbbox((0, 0), "COLLECTION", font=f_em2)
    draw.text((cx - (eb2[2] - eb2[0]) // 2, cy - 2), "COLLECTION", font=f_em2, fill=(245, 197, 24, 255))

    yr_str = str(meta.get("year") or "N/A")
    eb3 = draw.textbbox((0, 0), yr_str, font=f_em1)
    draw.text((cx - (eb3[2] - eb3[0]) // 2, cy + cr + 8), yr_str, font=f_em1, fill=(235, 215, 165, 255))

    # Populate Top-Center Title & Banner
    short_title = re.split(r"[:\-–]", meta.get("collection_name") or meta["title"])[0].replace("Collection", "").strip().upper()
    if not short_title:
        short_title = meta["title"].upper()
    f_head = _load_font(40 if len(short_title) <= 14 else 30, bold=True)
    hb = draw.textbbox((0, 0), short_title, font=f_head)
    hx = (W - (hb[2] - hb[0])) // 2
    draw.text((hx + 2, 36), short_title, font=f_head, fill=(0, 0, 0, 220))
    draw.text((hx, 34), short_title, font=f_head, fill=(242, 192, 64, 255))

    f_the = _load_font(10, bold=True)
    tb_the = draw.textbbox((0, 0), "THE", font=f_the)
    draw.text(((W - (tb_the[2] - tb_the[0])) // 2, 82), "THE", font=f_the, fill=(220, 200, 150, 255))

    f_comp = _load_font(16, bold=True)
    comp_txt = "COMPLETE COLLECTION"
    cb_comp = draw.textbbox((0, 0), comp_txt, font=f_comp)
    draw.text(((W - (cb_comp[2] - cb_comp[0])) // 2, ry1 + 7), comp_txt, font=f_comp, fill=(255, 255, 255, 255))

    f_off = _load_font(11, bold=True)
    off_txt = "OFFICIAL MOVIE SERIES"
    ob = draw.textbbox((0, 0), off_txt, font=f_off)
    ox = (W - (ob[2] - ob[0])) // 2
    draw.line([(ox - 55, 144), (ox - 12, 144)], fill=(212, 175, 55, 180), width=1)
    draw.text((ox, 138), off_txt, font=f_off, fill=(220, 205, 165, 240))
    draw.line([(ox + (ob[2] - ob[0]) + 12, 144), (ox + (ob[2] - ob[0]) + 55, 144)], fill=(212, 175, 55, 180), width=1)

    # Center Poster Card(s) with Neon Blue/Indigo Border
    cards_to_show = poster_bytes_list[:4] if poster_bytes_list else []
    num_cards = len(cards_to_show)
    pw, ph = (160, 236) if num_cards == 1 else (142, 210)
    gap = 28
    total_pw = num_cards * pw + max(0, num_cards - 1) * gap
    start_px = (W - total_pw) // 2
    py = 175

    for idx_c, p_bytes in enumerate(cards_to_show):
        cx_card = start_px + idx_c * (pw + gap)
        p_card = _rounded_poster_card(
            p_bytes,
            pw,
            ph,
            radius=10,
            border_width=2,
            border_color=(115, 138, 255, 245),
        )
        _paste_with_shadow(canvas, p_card, cx_card, py, blur=14)
        draw = ImageDraw.Draw(canvas)
        p_yr = part_years[idx_c] if part_years and idx_c < len(part_years) else str(meta.get("year") or "N/A")
        f_pyr = _load_font(13, bold=True)
        yb = draw.textbbox((0, 0), p_yr, font=f_pyr)
        yx = cx_card + (pw - (yb[2] - yb[0])) // 2
        draw.text((yx, py + ph + 8), p_yr, font=f_pyr, fill=(235, 235, 245, 255))
        dot_x = cx_card + pw // 2
        draw.ellipse([(dot_x - 4, py + ph + 28), (dot_x + 4, py + ph + 36)], fill=(115, 138, 255, 255))

    # Populate 6-Column Metadata Bar
    cols_data = [
        (f"★ {meta['rating']}/10", "RATING"),
        (f"🎬 {movie_count}", "MOVIES"),
        (f"🕒 {meta['runtime'].upper()}", "RUNTIME"),
        (f"🎭 {' · '.join(meta['genres'][:2])}", "GENRE"),
        (f"🌐 {meta.get('language', 'Telugu')}", "LANGUAGE"),
        (f"🏛 {meta.get('studio', 'Official')[:15]}", "STUDIO"),
    ]
    f_cval = _load_font(14, bold=True)
    f_clbl = _load_font(10, bold=True)

    for i, (val_str, lbl_str) in enumerate(cols_data):
        cell_x = bar_x1 + i * col_w + 18
        draw.text((cell_x, bar_y1 + 12), val_str, font=f_cval, fill=(255, 255, 255, 255))
        draw.text((cell_x + 18, bar_y1 + 33), lbl_str, font=f_clbl, fill=(175, 175, 185, 230))

    # Bottom Center Tagline: ONE SAGA. ONE CHAPTER.
    f_tag = _load_font(14, bold=True)
    t1 = "ONE SAGA. "
    t2 = "ONE CHAPTER." if movie_count == 1 else f"{movie_count} CHAPTERS."
    b1 = draw.textbbox((0, 0), t1, font=f_tag)
    b2 = draw.textbbox((0, 0), t2, font=f_tag)
    tx_tag = (W - ((b1[2] - b1[0]) + (b2[2] - b2[0]))) // 2
    draw.text((tx_tag, 616), t1, font=f_tag, fill=(255, 255, 255, 255))
    draw.text((tx_tag + (b1[2] - b1[0]), 616), t2, font=f_tag, fill=(245, 197, 24, 255))

    # Bottom-Right Watermark: Telegram Icon + @BotUsername
    bot_handle = f"@{BOT_USERNAME.lstrip('@').upper()}"
    f_wm = _load_font(15, bold=True)
    wb = draw.textbbox((0, 0), bot_handle, font=f_wm)
    wx = W - (wb[2] - wb[0]) - 68
    wy = 642
    _draw_telegram_icon(canvas, wx, wy, diameter=20)
    draw = ImageDraw.Draw(canvas)
    draw.text((wx + 26, wy + 2), bot_handle, font=f_wm, fill=(235, 235, 245, 245))

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="JPEG", quality=96)
    return out.getvalue()


def render_cast_thumbnail(bg_bytes: bytes, poster_bytes: bytes, meta: Dict[str, Any], cast_images: Optional[List[Tuple[str, Optional[bytes]]]] = None) -> bytes:
    """Landscape cast thumbnail with metadata on the left and circular cast portraits below."""
    W, H = 1280, 720
    with Image.open(io.BytesIO(bg_bytes)) as bg:
        canvas = ImageOps.fit(bg.convert("RGBA"), (W, H), method=Image.Resampling.LANCZOS)
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for x in range(W):
        alpha = int(210 * max(0.0, 1.0 - x / 900.0) ** 1.15)
        od.line([(x, 0), (x, H)], fill=(4, 7, 13, alpha))
    canvas = Image.alpha_composite(canvas, overlay)
    draw = ImageDraw.Draw(canvas)
    margin = 58
    draw.text((margin, 48), f"{meta.get('media_type', 'MOVIE')}  ·  {meta.get('year', '')}", font=_load_font(18, bold=True), fill=(230, 195, 105, 255))
    title = str(meta.get("title", "UNKNOWN")).strip()
    title_lines = textwrap.wrap(title, width=24)[:2]
    y = 88
    for line in title_lines:
        draw.text((margin, y), line, font=_load_font(48 if len(title) < 23 else 40, bold=True), fill=(255, 255, 255, 255), stroke_width=1, stroke_fill=(0, 0, 0, 150))
        y += 55
    y += 5
    draw.text((margin, y), f"★ {meta.get('rating', 'N/A')}  ·  {meta.get('runtime', '')}  ·  {meta.get('year', '')}", font=_load_font(20, bold=True), fill=(230, 224, 210, 255))
    y += 35
    genres = " · ".join(str(g) for g in meta.get("genres", [])[:3])
    if genres:
        draw.text((margin, y), genres[:60], font=_load_font(16, bold=True), fill=(140, 207, 222, 255))
        y += 28
    desc = textwrap.wrap(re.sub(r"\s+", " ", str(meta.get("overview", ""))).strip(), width=62)[:2]
    for line in desc:
        draw.text((margin, y), line, font=_load_font(17, bold=True), fill=(245, 245, 245, 245))
        y += 24
    circle, gap, cy = 78, 25, 520
    for index, (name, raw) in enumerate((cast_images or [])[:5]):
        x = margin + index * (circle + gap)
        if x + circle > 760:
            break
        if raw:
            try:
                with Image.open(io.BytesIO(raw)) as actor:
                    actor = ImageOps.fit(actor.convert("RGB"), (circle, circle), method=Image.Resampling.LANCZOS).convert("RGBA")
            except Exception:
                raw = None
        if not raw:
            actor = Image.new("RGBA", (circle, circle), (48, 58, 75, 255))
            ad = ImageDraw.Draw(actor)
            initials = "".join(p[0] for p in name.split()[:2]).upper() or "?"
            af = _load_font(24, bold=True)
            bb = ad.textbbox((0, 0), initials, font=af)
            ad.text(((circle-(bb[2]-bb[0]))//2, (circle-(bb[3]-bb[1]))//2), initials, font=af, fill="white")
        mask = Image.new("L", (circle, circle), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, circle-1, circle-1), fill=255)
        draw.ellipse((x-4, cy-4, x+circle+3, cy+circle+3), fill=(85, 83, 190, 255), outline=(180, 165, 255, 255), width=3)
        canvas.paste(actor, (x, cy), mask)
        label = name if len(name) <= 13 else name.split()[0]
        lf = _load_font(13, bold=True)
        bb = draw.textbbox((0, 0), label, font=lf)
        draw.text((x+(circle-(bb[2]-bb[0]))//2, cy+circle+7), label, font=lf, fill=(245,245,245,255))
    draw.text((W-190, H-35), f"@{BOT_USERNAME.lstrip('@').upper()}", font=_load_font(13, bold=True), fill=(235,235,245,220))
    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="JPEG", quality=96, optimize=True, progressive=True)
    return out.getvalue()


def render_signature_thumbnail(bg_bytes: bytes, poster_bytes: bytes, meta: Dict[str, Any], style_key: str) -> bytes:
    """Render dedicated layouts for Dashboard, Spotlight, Giant Type, Orbit and Vertical."""
    accent, dark, light = poster_palette(poster_bytes)
    if style_key == "vertical":
        W, H = 720, 1280
    else:
        W, H = 1280, 720
    with Image.open(io.BytesIO(bg_bytes)) as bg:
        base = ImageOps.fit(bg.convert("RGB"), (W, H), method=Image.Resampling.LANCZOS)
    base = ImageEnhance.Brightness(base.filter(ImageFilter.GaussianBlur(18))).enhance(0.40).convert("RGBA")
    overlay = Image.new("RGBA", (W, H), (*dark, 175))
    canvas = Image.alpha_composite(base, overlay)
    draw = ImageDraw.Draw(canvas)
    title = str(meta.get("title") or "Untitled").strip()
    rating = str(meta.get("rating") or "Not Rated")
    year = str(meta.get("year") or "N/A")
    runtime = str(meta.get("runtime") or "N/A")
    genres = [str(g) for g in (meta.get("genres") or [])[:3]]
    overview = str(meta.get("overview") or "Overview unavailable.")
    if style_key == "vertical":
        poster = _rounded_poster_card(poster_bytes, 500, 700, radius=24, border_width=4)
        _paste_with_shadow(canvas, poster, 110, 80, blur=18)
        title_font = _load_font(42 if len(title) < 23 else 34, bold=True)
        y = 825
        for line in textwrap.wrap(title.upper(), width=24)[:3]:
            draw.text((48, y), line, font=title_font, fill=(255,255,255,255)); y += 50
        info = f"★ {rating}   •   {year}   •   {runtime}"
        draw.text((48, y + 8), info, font=_load_font(23, bold=True), fill=(*light,255))
        y += 55
        draw.text((48, y), "  /  ".join(genres) if genres else "Genres unavailable", font=_load_font(20, bold=True), fill=(*accent,255))
        for line in textwrap.wrap(overview, width=49)[:4]:
            y += 36; draw.text((48, y), line, font=_load_font(19), fill=(240,240,245,255))
    elif style_key == "dashboard":
        # Poster on the right; dashboard panels and actions on the left.
        poster = _rounded_poster_card(poster_bytes, 390, 570, radius=20, border_width=4)
        _paste_with_shadow(canvas, poster, 820, 75, blur=20)
        draw.rounded_rectangle((55,55,770,665), radius=24, fill=(8,8,18,205), outline=(*accent,220), width=2)
        draw.text((90,80), "✦  FEATURE", font=_load_font(16, bold=True), fill=(*light,255))
        fs = 66 if len(title) <= 10 else 48
        for i,line in enumerate(textwrap.wrap(title.upper(), width=18)[:2]): draw.text((90,120+i*60),line,font=_load_font(fs,bold=True),fill=(255,255,255,255))
        draw.rounded_rectangle((85,255,735,365), radius=18, fill=(0,0,0,130), outline=(255,255,255,90), width=1)
        for i,line in enumerate(textwrap.wrap(overview, width=65)[:3]): draw.text((105,278+i*27),line,font=_load_font(17),fill=(238,238,245,255))
        draw.rounded_rectangle((85,390,735,490), radius=18, fill=(0,0,0,140), outline=(*accent,210), width=1)
        draw.text((110,410), f"★ {rating} / 10", font=_load_font(27,bold=True), fill=(*light,255))
        draw.text((330,410), f"YEAR  {year}", font=_load_font(22,bold=True), fill=(255,255,255,255))
        draw.text((110,455), "GENRES  " + (", ".join(genres) if genres else "Not available"), font=_load_font(18,bold=True), fill=(*accent,255))
        draw.rounded_rectangle((85,525,390,590), radius=20, fill=(*accent,255))
        draw.text((120,544), "▶  WATCH NOW", font=_load_font(22,bold=True), fill=(255,255,255,255))
    elif style_key == "spotlight":
        poster = _rounded_poster_card(poster_bytes, 390, 570, radius=24, border_width=3)
        _paste_with_shadow(canvas, poster, 820, 75, blur=22)
        draw.text((70,70), "● NXT_HUB / FEATURE", font=_load_font(16,bold=True), fill=(*light,255))
        fs = 56 if len(title) <= 15 else 43
        y = 135
        for line in textwrap.wrap(title.upper(), width=20)[:3]: draw.text((65,y),line,font=_load_font(fs,bold=True),fill=(255,255,255,255)); y += fs+7
        draw.text((70,y+10), f"{year}  |  {runtime}", font=_load_font(20,bold=True), fill=(*light,255))
        y += 55
        for line in textwrap.wrap(overview,width=55)[:3]: draw.text((70,y),line,font=_load_font(18),fill=(245,245,250,255)); y+=28
        draw.rounded_rectangle((70,590,420,650), radius=24, fill=(*accent,255))
        draw.text((110,607), f"★ {rating}   •   WATCH NOW", font=_load_font(20,bold=True), fill=(255,255,255,255))
        draw.text((455,610), "  /  ".join(genres) if genres else "Genres unavailable", font=_load_font(17,bold=True), fill=(*light,255))
    elif style_key == "giant":
        poster = _rounded_poster_card(poster_bytes, 350, 570, radius=18, border_width=3)
        _paste_with_shadow(canvas, poster, 865, 75, blur=18)
        fs = 88 if len(title) <= 8 else (65 if len(title) <= 14 else 48)
        y=100
        for line in textwrap.wrap(title.upper(), width=16)[:3]: draw.text((65,y),line,font=_load_font(fs,bold=True),fill=(*light,255),stroke_width=2,stroke_fill=(*dark,255)); y += fs+3
        draw.text((70,430), f"★ {rating}   •   {year}   •   {runtime}", font=_load_font(24,bold=True), fill=(255,255,255,255))
        draw.rounded_rectangle((65,490,800,600), radius=18, fill=(0,0,0,150), outline=(*accent,220), width=2)
        for i,line in enumerate(textwrap.wrap(overview,width=65)[:3]): draw.text((85,510+i*27),line,font=_load_font(17),fill=(245,245,245,255))
        draw.text((70,625), "  /  ".join(genres) if genres else "Genres unavailable", font=_load_font(19,bold=True), fill=(*accent,255))
    else:  # orbit
        cx, cy, radius = 925, 355, 245
        for r, alpha in ((radius+36,75),(radius+20,110),(radius+7,200)):
            draw.ellipse((cx-r,cy-r,cx+r,cy+r), outline=(*accent,alpha), width=4)
        with Image.open(io.BytesIO(poster_bytes)) as pimg:
            circle = ImageOps.fit(pimg.convert("RGB"),(radius*2,radius*2),method=Image.Resampling.LANCZOS).convert("RGBA")
        mask=Image.new("L",circle.size,0); ImageDraw.Draw(mask).ellipse((0,0,circle.width-1,circle.height-1),fill=255)
        circle.putalpha(mask); canvas.alpha_composite(circle,(cx-radius,cy-radius))
        draw=ImageDraw.Draw(canvas)
        draw.text((55,75), "NXT_HUB / FEATURE", font=_load_font(16,bold=True), fill=(*light,255))
        y=170
        for line in textwrap.wrap(title.upper(),width=18)[:3]: draw.text((55,y),line,font=_load_font(48,bold=True),fill=(255,255,255,255)); y+=54
        draw.text((60,390), f"{year}  |  {runtime}", font=_load_font(21,bold=True), fill=(*light,255))
        draw.text((60,440), f"★ {rating}", font=_load_font(26,bold=True), fill=(*accent,255))
        draw.text((60,500), "  /  ".join(genres) if genres else "Genres unavailable", font=_load_font(18,bold=True), fill=(255,255,255,255))
        for i,line in enumerate(textwrap.wrap(overview,width=45)[:3]): draw.text((60,545+i*26),line,font=_load_font(16),fill=(235,235,245,255))
    out=io.BytesIO(); canvas.convert("RGB").save(out,format="JPEG",quality=97,optimize=True)
    return out.getvalue()


def render_custom_style(
    bg_bytes: bytes,
    poster_bytes: bytes,
    meta: Dict[str, Any],
    style_key: str,
) -> bytes:
    if style_key == "magic":
        return render_magic_thumbnail(bg_bytes, poster_bytes, meta)
    if style_key == "mood":
        return render_mood_poster_thumbnail(bg_bytes, poster_bytes, meta)
    if style_key == "premiere":
        return render_premiere_thumbnail(bg_bytes, poster_bytes, meta)
    if style_key == "split":
        return render_split_frame_thumbnail(bg_bytes, poster_bytes, meta)
    if style_key == "tile":
        return render_tile_thumbnail(bg_bytes, poster_bytes, meta)
    if style_key == "cast":
        return render_cast_thumbnail(bg_bytes, poster_bytes, meta)
    if style_key in ("dashboard", "spotlight", "giant", "orbit", "vertical"):
        return render_signature_thumbnail(bg_bytes, poster_bytes, meta, style_key)

    W, H = 1280, 720
    with Image.open(io.BytesIO(bg_bytes)) as im:
        src = im.convert("RGB")
    canvas = ImageOps.fit(src, (W, H), method=Image.Resampling.LANCZOS)

    if style_key == "anime":
        canvas = canvas.filter(ImageFilter.EDGE_ENHANCE_MORE)
        canvas = ImageOps.posterize(canvas, 4)
        canvas = ImageEnhance.Color(canvas).enhance(1.6)
    elif style_key == "manwa":
        gray = ImageOps.grayscale(canvas)
        canvas = ImageEnhance.Contrast(gray).enhance(1.85).convert("RGB")
    elif style_key in ("giant", "breakout", "orbit"):
        canvas = ImageEnhance.Color(canvas).enhance(1.5)
        canvas = ImageEnhance.Contrast(canvas).enhance(1.3)
    else:
        blurred = canvas.filter(ImageFilter.GaussianBlur(18))
        blurred = ImageEnhance.Brightness(blurred).enhance(0.45)
        fg = ImageOps.fit(src, (int(W * 0.84), int(H * 0.78)), method=Image.Resampling.LANCZOS)
        canvas = blurred
        canvas.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))

    rgba = canvas.convert("RGBA")
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    bar_top = int(H * 0.70)
    odraw.rectangle([(0, bar_top), (W, H)], fill=(10, 10, 14, 195))
    rgba = Image.alpha_composite(rgba, overlay)

    draw = ImageDraw.Draw(rgba)
    f_title = _load_font(50, bold=True)
    f_sub = _load_font(20, bold=True)
    title_label = f"{meta['title'].upper()} ({meta['year']})"
    info_label = f"★ {meta['rating']}   ·   {meta['runtime'].upper()}   ·   {' / '.join(g.upper() for g in meta['genres'][:2])}"

    draw.text((55, bar_top + 22), info_label, font=f_sub, fill=(245, 197, 24, 255))
    draw.text((55, bar_top + 56), title_label, font=f_title, fill=(255, 255, 255, 255))

    _draw_telegram_icon(rgba, 55, bar_top + 125, diameter=24)
    draw = ImageDraw.Draw(rgba)
    f_wm = _load_font(17, bold=True)
    draw.text((87, bar_top + 128), f"@{BOT_USERNAME.lstrip('@').upper()}", font=f_wm, fill=(235, 235, 235, 255))

    out = io.BytesIO()
    rgba.convert("RGB").save(out, format="JPEG", quality=95)
    return out.getvalue()


def _calc_xy(canvas_w: int, canvas_h: int, obj_w: int, obj_h: int, pos: str, pad: int = 45) -> Tuple[int, int]:
    if pos.endswith("l"):
        x = pad
    elif pos.endswith("r"):
        x = canvas_w - obj_w - pad
    else:
        x = (canvas_w - obj_w) // 2

    if pos.startswith("t"):
        y = pad
    elif pos.startswith("b"):
        y = canvas_h - obj_h - pad
    else:
        y = (canvas_h - obj_h) // 2
    return max(0, x), max(0, y)


def render_positioned_thumbnail(
    bg_bytes: bytes,
    title: str,
    pos: str = "bc",
    logo_bytes: Optional[bytes] = None,
) -> bytes:
    with Image.open(io.BytesIO(bg_bytes)) as bg:
        canvas = ImageOps.fit(bg.convert("RGBA"), (1280, 720), method=Image.Resampling.LANCZOS)

    overlay = Image.new("RGBA", (1280, 720), (0, 0, 0, 0))
    draw_ov = ImageDraw.Draw(overlay)
    for y in range(420, 720):
        alpha = int(190 * ((y - 420) / 300))
        draw_ov.line([(0, y), (1280, y)], fill=(0, 0, 0, alpha))
    canvas = Image.alpha_composite(canvas, overlay)

    if logo_bytes:
        try:
            with Image.open(io.BytesIO(logo_bytes)) as lg:
                lg = lg.convert("RGBA")
                lg.thumbnail((520, 220), Image.Resampling.LANCZOS)
                lx, ly = _calc_xy(1280, 720, lg.width, lg.height, pos)
                canvas.paste(lg, (lx, ly), lg)
        except Exception:
            logo_bytes = None

    if not logo_bytes:
        draw = ImageDraw.Draw(canvas)
        font = _load_font(68, bold=True)
        clean_title = title.upper()
        bbox = draw.textbbox((0, 0), clean_title, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        tx, ty = _calc_xy(1280, 720, tw, th, pos)
        draw.text((tx + 4, ty + 4), clean_title, font=font, fill=(0, 0, 0, 230))
        draw.text((tx, ty), clean_title, font=font, fill=(255, 215, 0, 255))

    _draw_telegram_icon(canvas, 45, 665, diameter=24)
    draw = ImageDraw.Draw(canvas)
    f_wm = _load_font(16, bold=True)
    draw.text((76, 668), f"@{BOT_USERNAME.lstrip('@').upper()}", font=f_wm, fill=(255, 255, 255, 235))

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="JPEG", quality=95)
    return out.getvalue()


def make_webp_sticker(img_bytes: bytes) -> bytes:
    with Image.open(io.BytesIO(img_bytes)) as im:
        im = im.convert("RGBA")
        im.thumbnail((512, 512), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        im.save(out, format="WEBP", quality=90)
        return out.getvalue()


# ==============================================================================
# 🤖 MESSAGE & CALLBACK HANDLERS
# ==============================================================================
async def send_welcome_group_message(http: aiohttp.ClientSession, chat_id: int, user: dict):
    user_id = user.get("id", 0)
    first_name = html.escape(user.get("first_name", "Member"))
    user_mention = f'<a href="tg://user?id={user_id}">{first_name}</a>'

    text = (
        f"🎉 <b>Welcome to {html.escape(GROUP_NAME)}, {first_name}!</b>\n\n"
        f"<blockquote>🔍 Use <code>/p &lt;movie or series&gt;</code> to grab instant HD posters &amp; backdrops\n"
        f"🎞 Use /p <movie or series name> to search on demand\n"
        f"✨ Try Magic, Mood Poster, Premiere, Split-Frame &amp; Tile thumbnails\n"
        f"📖 Type /help anytime for the complete command guide</blockquote>\n\n"
        f"🍿 Have a great time here, {user_mention}!"
    )
    await tg_request(
        http,
        "sendMessage",
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
    )


async def send_search_results(http: aiohttp.ClientSession, chat_id: int, reply_to_id: int, query: str, user: dict):
    results = await search_movies(http, query)
    if not results:
        await tg_request(
            http,
            "sendMessage",
            {
                "chat_id": chat_id,
                "reply_to_message_id": reply_to_id,
                "text": f"<blockquote>❌ No matching titles found for: <b>{html.escape(query)}</b>\n💡 Check the spelling or try searching with the release year.</blockquote>",
                "parse_mode": "HTML",
            },
        )
        return

    sid = new_session_id()
    owner_uid = user.get("id", 0)
    SESSION_OWNERS[sid] = owner_uid
    SESSIONS[sid] = {
        "type": "search",
        "results": results,
        "query": query,
        "user": user,
        "owner_id": owner_uid,
    }

    buttons = []
    for i, item in enumerate(results):
        label = f"🎬 {item['title']} ({item['year']})"
        buttons.append([{"text": label, "callback_data": f"pick:{sid}:{i}", "style": "primary"}])

    buttons.append([{"text": "✖ Cancel", "callback_data": f"close:{sid}", "style": "danger"}])

    text = f"🔍 <b>Search Results</b>\n\n<i>Query: {html.escape(query)}</i>"
    await tg_request(
        http,
        "sendMessage",
        {
            "chat_id": chat_id,
            "reply_to_message_id": reply_to_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": buttons},
        },
    )


async def handle_message(http: aiohttp.ClientSession, msg: dict):
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type", "")
    text = (msg.get("text") or "").strip()
    user = msg.get("from", {})
    user_id = user.get("id", 0)
    first_name = html.escape(user.get("first_name", "User"))
    user_mention = f'<a href="tg://user?id={user_id}">{first_name}</a>'
    msg_id = msg.get("message_id")

    if chat_id == ALLOWED_GROUP_ID and "new_chat_members" in msg:
        for new_mem in msg["new_chat_members"]:
            if not new_mem.get("is_bot"):
                await send_welcome_group_message(http, chat_id, new_mem)
        return

    if text.startswith("/start"):
        STARTED_USERS.add(user_id)
        if chat_type == "private":
            welcome_text = (
                f"<blockquote>🎉 Welcome, {user_mention}\n"
                f"<b>Movie &amp; Series Posters Hub</b>\n\n"
                f"🔍 Send any movie or series name to get UHD posters &amp; backdrops instantly\n"
                f"🎞 Or drop a video file and I'll generate a matching cover for it\n\n"
                f"🔒 <b>Group Exclusive:</b> Tap the button below to join our official group and start using the bot!</blockquote>"
            )
            await tg_request(
                http,
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": welcome_text,
                    "parse_mode": "HTML",
                    "reply_markup": group_only_keyboard(),
                },
            )
        return

    if text.startswith("/help"):
        if chat_type == "private":
            await tg_request(
                http,
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "reply_to_message_id": msg_id,
                    "text": (
                        f"<blockquote>📖 <b>Bot Help &amp; Access</b>\n\n"
                        f"Hey {user_mention}, all poster scraping &amp; custom thumbnail tools are active exclusively inside our official group!</blockquote>"
                    ),
                    "parse_mode": "HTML",
                    "reply_markup": group_only_keyboard(),
                },
            )
            return

        if chat_id != ALLOWED_GROUP_ID:
            return

        if not await is_user_verified(http, user_id):
            await send_quick_setup(http, chat_id, msg_id, user_id)
            return

        await send_welcome_group_message(http, chat_id, user)
        return

    if re.match(r"^/p(?:@\w+)?(?:\s|$)", text, flags=re.IGNORECASE):
        if chat_type == "private":
            pm_notice = (
                f"<blockquote>🚫 <b>Group Only Command</b>\n\n"
                f"Hey {user_mention}, <code>/p</code> works exclusively inside our official group!\n"
                f"👇 Tap below to join and unlock unlimited UHD Movie &amp; Series Posters.</blockquote>"
            )
            await tg_request(
                http,
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "reply_to_message_id": msg_id,
                    "text": pm_notice,
                    "parse_mode": "HTML",
                    "reply_markup": group_only_keyboard(),
                },
            )
            return

        if chat_id != ALLOWED_GROUP_ID:
            await tg_request(
                http,
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "reply_to_message_id": msg_id,
                    "text": "<blockquote>⚠️ This bot is restricted to our official group! Join below to use all features.</blockquote>",
                    "parse_mode": "HTML",
                    "reply_markup": group_only_keyboard(),
                },
            )
            return

        if not await is_user_verified(http, user_id):
            await send_quick_setup(http, chat_id, msg_id, user_id)
            return

        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await tg_request(
                http,
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "reply_to_message_id": msg_id,
                    "text": (
                        "<blockquote>ℹ️ <b>How to Search Posters:</b>\n"
                        "Send <code>/p &lt;movie or series name&gt;</code>\n"
                        "Example: <code>/p Salaar</code></blockquote>"
                    ),
                    "parse_mode": "HTML",
                },
            )
            return

        await send_search_results(http, chat_id, msg_id, parts[1].strip(), user)
        return

    # Searches start only when a user explicitly sends /p <title> in the official group.
    # Video/document uploads do not trigger background searches.


async def update_viewer_message(http: aiohttp.ClientSession, chat_id: int, message_id: int, sid: str):
    session = SESSIONS.get(sid)
    if not session:
        return
    tab = session["tab"]
    idx = session["index"]
    items = session["data"].get(tab, [])
    if not items:
        return

    img_url = items[idx]["preview"]
    caption = build_viewer_caption(session)
    kb = build_viewer_keyboard(sid, session)

    await tg_request(
        http,
        "editMessageMedia",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "media": {
                "type": "photo",
                "media": img_url,
                "caption": caption,
                "parse_mode": "HTML",
            },
            "reply_markup": kb,
        },
        auto_delete=False,
    )


async def _get_session_bg_and_poster(http: aiohttp.ClientSession, session: Dict[str, Any]) -> Tuple[Optional[bytes], Optional[bytes], str]:
    data = session["data"]
    tab = session["tab"]
    idx = session["index"]

    b_list = data.get("backdrops") or data.get(tab) or data.get("posters")
    p_list = data.get("posters") or data.get(tab) or data.get("backdrops")

    b_item = b_list[min(idx, len(b_list) - 1)] if tab == "backdrops" and b_list else (b_list[0] if b_list else None)
    p_item = p_list[min(idx, len(p_list) - 1)] if tab == "posters" and p_list else (p_list[0] if p_list else None)

    b_url = b_item["original"] if b_item else ""
    b_prev = b_item["preview"] if b_item else ""
    p_prev = p_item["preview"] if p_item else b_prev

    bg_task = asyncio.create_task(download_bytes(http, b_prev))
    po_task = asyncio.create_task(download_bytes(http, p_prev))
    bg_bytes, po_bytes = await asyncio.gather(bg_task, po_task)

    if not bg_bytes and b_url:
        bg_bytes = await download_bytes(http, b_url)
    if not po_bytes and bg_bytes:
        po_bytes = bg_bytes
    if not bg_bytes and po_bytes:
        bg_bytes = po_bytes

    return bg_bytes, po_bytes, (b_url or b_prev or "https://t.me")


async def handle_callback(http: aiohttp.ClientSession, cb: dict):
    cb_id = cb.get("id")
    data = cb.get("data", "")
    msg = cb.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")
    click_user = cb.get("from", {})
    click_uid = click_user.get("id", 0)

    if ":" not in data:
        await tg_request(http, "answerCallbackQuery", {"callback_query_id": cb_id}, auto_delete=False)
        return

    parts = data.split(":")
    action = parts[0]
    sid = parts[1]

    # Each person can complete channel verification for their own Telegram account.
    if action == "setup_done":
        if await is_user_verified(http, click_uid):
            await tg_request(
                http,
                "deleteMessage",
                {"chat_id": chat_id, "message_id": message_id},
                auto_delete=False,
            )
            await tg_request(
                http,
                "answerCallbackQuery",
                {"callback_query_id": cb_id, "text": "✅ Verification complete! Welcome aboard!"},
                auto_delete=False,
            )
            await send_welcome_group_message(http, chat_id, click_user)
        else:
            await tg_request(
                http,
                "answerCallbackQuery",
                {
                    "callback_query_id": cb_id,
                    "text": f"❌ You haven't joined {CHANNEL_USERNAME} yet! Join the channel first, then tap ✅ Done.",
                    "show_alert": True,
                },
                auto_delete=False,
            )
        return

    # Each callback carries its own session ID, so requests remain isolated.
    # Any verified group member (including group/channel admins and the bot owner)
    # may operate the buttons without the misleading "not your buttons" alert.
    session = SESSIONS.get(sid)

    if not session:
        if action in ("close_msg", "close"):
            await tg_request(
                http,
                "deleteMessage",
                {"chat_id": chat_id, "message_id": message_id},
                auto_delete=False,
            )
            await tg_request(http, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "Closed!"}, auto_delete=False)
            return

        await tg_request(
            http,
            "answerCallbackQuery",
            {
                "callback_query_id": cb_id,
                "text": "⚠️ Session expired! Please start a new search with /p",
                "show_alert": True,
            },
            auto_delete=False,
        )
        return

    # Mandatory Force-Subscribe check on group interactions
    if not await is_user_verified(http, click_uid):
        await tg_request(
            http,
            "answerCallbackQuery",
            {
                "callback_query_id": cb_id,
                "text": f"🔒 Please join {CHANNEL_USERNAME} first to use the bot!",
                "show_alert": True,
            },
            auto_delete=False,
        )
        return

    # Close a secondary message (Custom Styles menu, Franchise card, or Generated Thumbnail)
    if action == "close_msg":
        await tg_request(
            http,
            "deleteMessage",
            {"chat_id": chat_id, "message_id": message_id},
            auto_delete=False,
        )
        await tg_request(http, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "Closed!"}, auto_delete=False)
        return

    # Close the entire session (Main Viewer / Search Results)
    if action == "close":
        SESSIONS.pop(sid, None)
        await tg_request(
            http,
            "deleteMessage",
            {"chat_id": chat_id, "message_id": message_id},
            auto_delete=False,
        )
        await tg_request(http, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "Session closed!"}, auto_delete=False)
        return

    # 1. Pick a Movie from Search Results -> Send "🖼 Fetching images..." -> Show Main Viewer
    if action == "pick":
        idx = int(parts[2])
        chosen = session["results"][idx]
        await tg_request(
            http,
            "answerCallbackQuery",
            {"callback_query_id": cb_id, "text": "🖼 Fetching images..."},
            auto_delete=False,
        )

        fetch_msg = await tg_request(
            http,
            "sendMessage",
            {
                "chat_id": chat_id,
                "reply_to_message_id": message_id,
                "text": "🖼 Fetching images...",
            },
            auto_delete=False,
        )
        fetch_msg_id = fetch_msg.get("result", {}).get("message_id")

        assets = await fetch_media_assets(http, chosen)
        if not assets["backdrops"] and not assets["posters"]:
            if fetch_msg_id:
                await tg_request(http, "deleteMessage", {"chat_id": chat_id, "message_id": fetch_msg_id}, auto_delete=False)
            await tg_request(
                http,
                "answerCallbackQuery",
                {
                    "callback_query_id": cb_id,
                    "text": "❌ No posters found on TMDB or IMDb for this title!",
                    "show_alert": True,
                },
                auto_delete=False,
            )
            return

        first_tab = "backdrops" if assets["backdrops"] else "posters"
        req_user = session.get("user") or click_user
        req_owner_id = req_user.get("id", click_uid)
        SESSION_OWNERS[sid] = req_owner_id
        SESSIONS[sid] = {
            "type": "viewer",
            "data": assets,
            "tab": first_tab,
            "index": 0,
            "logo_index": 0,
            "chosen_logo": None,
            "user": req_user,
            "owner_id": req_owner_id,
        }
        first_img = assets[first_tab][0]["preview"]
        caption = build_viewer_caption(SESSIONS[sid])
        kb = build_viewer_keyboard(sid, SESSIONS[sid])

        if fetch_msg_id:
            asyncio.create_task(
                tg_request(http, "deleteMessage", {"chat_id": chat_id, "message_id": fetch_msg_id}, auto_delete=False)
            )
        asyncio.create_task(
            tg_request(http, "deleteMessage", {"chat_id": chat_id, "message_id": message_id}, auto_delete=False)
        )

        await tg_request(
            http,
            "sendPhoto",
            {
                "chat_id": chat_id,
                "photo": first_img,
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": kb,
            },
        )
        return

    # 2. Switch Category Tabs (Backdrops / Posters / Fanart)
    if action == "tab":
        new_tab = parts[2]
        if not session["data"].get(new_tab):
            await tg_request(
                http,
                "answerCallbackQuery",
                {
                    "callback_query_id": cb_id,
                    "text": f"No {new_tab} available for this title!",
                    "show_alert": True,
                },
                auto_delete=False,
            )
            return
        session["tab"] = new_tab
        session["index"] = 0
        await tg_request(http, "answerCallbackQuery", {"callback_query_id": cb_id}, auto_delete=False)
        await update_viewer_message(http, chat_id, message_id, sid)
        return

    # 3. Prev / Next Image Navigation
    if action == "nav":
        new_idx = int(parts[2])
        items = session["data"].get(session["tab"], [])
        if 0 <= new_idx < len(items):
            session["index"] = new_idx
            await tg_request(http, "answerCallbackQuery", {"callback_query_id": cb_id}, auto_delete=False)
            await update_viewer_message(http, chat_id, message_id, sid)
        return

    # 4. Save Full-Resolution Original Image
    if action == "save":
        tab = session["tab"]
        item = session["data"][tab][session["index"]]
        await tg_request(
            http,
            "answerCallbackQuery",
            {"callback_query_id": cb_id, "text": "📥 Sending Original UHD Image..."},
            auto_delete=False,
        )
        req_user = session.get("user") or click_user
        u_mention = f'<a href="tg://user?id={req_user.get("id", 0)}">{html.escape(req_user.get("first_name", "User"))}</a>'
        await tg_request(
            http,
            "sendDocument",
            {
                "chat_id": chat_id,
                "document": item["original"],
                "caption": (
                    f"📥 <b>Original UHD {tab[:-1].title()}</b>\n"
                    f"<blockquote>🎬 {html.escape(session['data']['title'])}</blockquote>\n"
                    f"👤 Requested by: {u_mention}"
                ),
                "parse_mode": "HTML",
            },
        )
        return

    # 5. Make Thumbnail Flow -> Step 1: Choose Logo (or Step 2 if no logos)
    if action == "thumb":
        await tg_request(http, "answerCallbackQuery", {"callback_query_id": cb_id}, auto_delete=False)
        logos = session["data"].get("logos", [])
        if logos:
            session["logo_index"] = 0
            caption, kb = build_logo_picker_ui(sid, session)
            logo_url = logos[0]["preview"]
            await tg_request(
                http,
                "editMessageMedia",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "media": {
                        "type": "photo",
                        "media": logo_url,
                        "caption": caption,
                        "parse_mode": "HTML",
                    },
                    "reply_markup": kb,
                },
                auto_delete=False,
            )
        else:
            session["chosen_logo"] = None
            caption, kb = build_position_picker_ui(sid, session)
            await tg_request(
                http,
                "editMessageCaption",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "caption": caption,
                    "parse_mode": "HTML",
                    "reply_markup": kb,
                },
                auto_delete=False,
            )
        return

    if action == "lg_nav":
        new_l_idx = int(parts[2])
        logos = session["data"].get("logos", [])
        if 0 <= new_l_idx < len(logos):
            session["logo_index"] = new_l_idx
            caption, kb = build_logo_picker_ui(sid, session)
            await tg_request(
                http,
                "editMessageMedia",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "media": {
                        "type": "photo",
                        "media": logos[new_l_idx]["preview"],
                        "caption": caption,
                        "parse_mode": "HTML",
                    },
                    "reply_markup": kb,
                },
                auto_delete=False,
            )
        await tg_request(http, "answerCallbackQuery", {"callback_query_id": cb_id}, auto_delete=False)
        return

    if action in ("lg_use", "lg_skip"):
        session["chosen_logo"] = int(parts[2]) if action == "lg_use" else None
        caption, kb = build_position_picker_ui(sid, session)
        await tg_request(
            http,
            "editMessageCaption",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": kb,
            },
            auto_delete=False,
        )
        await tg_request(http, "answerCallbackQuery", {"callback_query_id": cb_id}, auto_delete=False)
        return

    if action == "lg_back":
        await tg_request(http, "answerCallbackQuery", {"callback_query_id": cb_id}, auto_delete=False)
        await update_viewer_message(http, chat_id, message_id, sid)
        return

    if action == "pos":
        pos_code = parts[2]
        await tg_request(
            http,
            "answerCallbackQuery",
            {"callback_query_id": cb_id, "text": "🎬 Building HD Thumbnail..."},
            auto_delete=False,
        )
        bg_bytes, _, fallback_url = await _get_session_bg_and_poster(http, session)
        if not bg_bytes:
            await tg_request(http, "sendMessage", {"chat_id": chat_id, "text": "❌ Failed to download backdrop."})
            return

        logo_bytes = None
        chosen_logo = session.get("chosen_logo")
        logos = session["data"].get("logos", [])
        if chosen_logo is not None and 0 <= chosen_logo < len(logos):
            logo_bytes = await download_bytes(http, logos[chosen_logo]["original"])

        thumb_bytes = render_positioned_thumbnail(bg_bytes, session["data"]["title"], pos_code, logo_bytes)
        tid = new_thumb_id()
        THUMB_CACHE[tid] = thumb_bytes
        copy_url = await upload_public_image_link(http, thumb_bytes, fallback_url)

        req_user = session.get("user") or click_user
        u_mention = f'<a href="tg://user?id={req_user.get("id", 0)}">{html.escape(req_user.get("first_name", "User"))}</a>'
        cap = (
            f"🎬 <b>HD Thumbnail</b>\n"
            f"<blockquote>🎬 {html.escape(session['data']['title'])}</blockquote>\n"
            f"👤 Requested by: {u_mention}"
        )

        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        form.add_field("caption", cap)
        form.add_field("parse_mode", "HTML")
        form.add_field("reply_markup", json.dumps(build_generated_thumb_keyboard(sid, tid, copy_url)))
        form.add_field("photo", thumb_bytes, filename="thumbnail.jpg", content_type="image/jpeg")
        await tg_upload(http, "sendPhoto", form)
        await update_viewer_message(http, chat_id, message_id, sid)
        return

    # 6. Open Custom Styles Menu (16 Styles) while keeping Main Handler intact
    if action == "style":
        await tg_request(http, "answerCallbackQuery", {"callback_query_id": cb_id}, auto_delete=False)
        req_user = session.get("user") or click_user
        u_id = req_user.get("id", 0)
        u_name = html.escape(req_user.get("first_name", "User"))
        u_mention = f'<a href="tg://user?id={u_id}">{u_name}</a>'
        title = html.escape(session["data"]["title"])
        year = html.escape(str(session["data"].get("year", "N/A")))

        style_text = (
            f"🎨 <b>Custom Thumbnail</b>\n"
            f"<i>Pick a style — each uses this session's title &amp; art</i>\n\n"
            f"<blockquote>🎬 {title} ({year})</blockquote>\n"
            f"👤 Requested by: {u_mention}"
        )
        await tg_request(
            http,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": style_text,
                "parse_mode": "HTML",
                "reply_markup": build_custom_styles_keyboard(sid),
            },
        )
        return

    # 7. Render Chosen Custom Style (Magic, Mood Poster, Premiere, Split-Frame, Tile, etc.)
    if action == "cstyle":
        style_key = parts[2]
        emoji, style_title, toast_name = STYLE_META.get(
            style_key, ("✨", f"{style_key.title()} Thumbnail", style_key)
        )

        await tg_request(
            http,
            "answerCallbackQuery",
            {"callback_query_id": cb_id, "text": f"{emoji} Generating {toast_name} thumbnail..."},
            auto_delete=False,
        )

        build_msg = await tg_request(
            http,
            "sendMessage",
            {
                "chat_id": chat_id,
                "reply_to_message_id": message_id,
                "text": f"{emoji} Building {style_title}...",
            },
            auto_delete=False,
        )
        build_msg_id = build_msg.get("result", {}).get("message_id")

        bg_bytes, po_bytes, fallback_url = await _get_session_bg_and_poster(http, session)
        if not bg_bytes or not po_bytes:
            if build_msg_id:
                await tg_request(http, "deleteMessage", {"chat_id": chat_id, "message_id": build_msg_id}, auto_delete=False)
            await tg_request(http, "sendMessage", {"chat_id": chat_id, "text": "❌ Failed to download image assets."})
            return

        if style_key == "cast":
            cast_meta = session["data"].get("cast", [])[:5]
            cast_downloads = await asyncio.gather(*[
                download_bytes(http, member.get("profile")) if member.get("profile") else asyncio.sleep(0, result=None)
                for member in cast_meta
            ])
            cast_images = [(member.get("name", "Cast"), raw) for member, raw in zip(cast_meta, cast_downloads)]
            styled_bytes = render_cast_thumbnail(bg_bytes, po_bytes, session["data"], cast_images)
        else:
            styled_bytes = render_custom_style(bg_bytes, po_bytes, session["data"], style_key)
        tid = new_thumb_id()
        THUMB_CACHE[tid] = styled_bytes
        copy_url = await upload_public_image_link(http, styled_bytes, fallback_url)

        req_user = session.get("user") or click_user
        u_id = req_user.get("id", 0)
        u_name = html.escape(req_user.get("first_name", "User"))
        u_mention = f'<a href="tg://user?id={u_id}">{u_name}</a>'
        title_html = html.escape(session["data"]["title"])

        caption = (
            f"{emoji} <b>{style_title}</b>\n"
            f"<blockquote>🎬 {title_html}</blockquote>\n"
            f"👤 Requested by: {u_mention}"
        )
        kb = build_generated_thumb_keyboard(sid, tid, copy_url)

        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        form.add_field("caption", caption)
        form.add_field("parse_mode", "HTML")
        form.add_field("reply_markup", json.dumps(kb))
        form.add_field("photo", styled_bytes, filename=f"{style_key}.jpg", content_type="image/jpeg")
        await tg_upload(http, "sendPhoto", form)

        if build_msg_id:
            await tg_request(
                http,
                "deleteMessage",
                {"chat_id": chat_id, "message_id": build_msg_id},
                auto_delete=False,
            )
        return

    # 8. Generate Sticker from a Generated Custom Thumbnail
    if action == "gen_stk":
        tid = parts[2]
        raw = THUMB_CACHE.get(tid)
        await tg_request(
            http,
            "answerCallbackQuery",
            {"callback_query_id": cb_id, "text": "🎬 Generating Sticker..."},
            auto_delete=False,
        )
        if not raw:
            bg_bytes, _, _ = await _get_session_bg_and_poster(http, session)
            raw = bg_bytes
        if raw:
            webp_bytes = make_webp_sticker(raw)
            form = aiohttp.FormData()
            form.add_field("chat_id", str(chat_id))
            form.add_field("sticker", webp_bytes, filename="thumbnail_sticker.webp", content_type="image/webp")
            await tg_upload(http, "sendSticker", form)
        return

    # 9. Sticker Generator from Main Viewer
    if action == "sticker":
        tab = session["tab"]
        item = session["data"][tab][session["index"]]
        await tg_request(
            http,
            "answerCallbackQuery",
            {"callback_query_id": cb_id, "text": "🎬 Creating Sticker..."},
            auto_delete=False,
        )
        raw = await download_bytes(http, item["preview"])
        if raw:
            webp_bytes = make_webp_sticker(raw)
            form = aiohttp.FormData()
            form.add_field("chat_id", str(chat_id))
            form.add_field("sticker", webp_bytes, filename="poster.webp", content_type="image/webp")
            await tg_upload(http, "sendSticker", form)
        return

    # 10. Franchise Collection Card Flow (Matches Images 1, 2, 3)
    if action == "fran":
        await tg_request(
            http,
            "answerCallbackQuery",
            {"callback_query_id": cb_id, "text": "🗂 Building franchise collection card..."},
            auto_delete=False,
        )
        build_msg = await tg_request(
            http,
            "sendMessage",
            {
                "chat_id": chat_id,
                "reply_to_message_id": message_id,
                "text": "🗂 Building franchise collection card...",
            },
            auto_delete=False,
        )
        build_msg_id = build_msg.get("result", {}).get("message_id")

        bg_bytes, po_bytes, fallback_url = await _get_session_bg_and_poster(http, session)
        if not bg_bytes or not po_bytes:
            if build_msg_id:
                await tg_request(http, "deleteMessage", {"chat_id": chat_id, "message_id": build_msg_id}, auto_delete=False)
            await tg_request(http, "sendMessage", {"chat_id": chat_id, "text": "❌ Failed to build franchise card."})
            return

        poster_bytes_list = [po_bytes]
        part_years = [str(session["data"].get("year", "N/A"))]
        movie_count = 1
        col_id = session["data"].get("collection_id")

        base_short = re.split(r"[:\-–]", session["data"]["title"])[0].strip()
        col_display_name = session["data"].get("collection_name") or f"{base_short} Collection"

        if col_id and TMDB_API_KEY:
            try:
                async with http.get(f"{TMDB_BASE}/collection/{col_id}", params={"api_key": TMDB_API_KEY}) as resp:
                    if resp.status == 200:
                        cdata = await resp.json()
                        if cdata.get("name"):
                            col_display_name = cdata["name"]
                        parts_raw = cdata.get("parts") or []
                        if parts_raw:
                            movie_count = len(parts_raw)
                            dl_tasks = []
                            temp_years = []
                            for p_item in parts_raw[:4]:
                                p_path = p_item.get("poster_path")
                                r_yr = (p_item.get("release_date") or "")[:4] or str(session["data"]["year"])
                                temp_years.append(r_yr)
                                if p_path:
                                    dl_tasks.append(download_bytes(http, f"{TMDB_IMG_PREVIEW}{p_path}"))
                            if dl_tasks:
                                fetched_posters = [b for b in await asyncio.gather(*dl_tasks) if b]
                                if fetched_posters:
                                    poster_bytes_list = fetched_posters
                                    part_years = temp_years[:len(fetched_posters)]
            except Exception:
                pass

        fran_bytes = render_franchise_collection_card(
            bg_bytes,
            poster_bytes_list,
            session["data"],
            movie_count=movie_count,
            part_years=part_years,
        )
        img_link = await upload_public_image_link(http, fran_bytes, fallback_url)

        caption = f"<blockquote>🗂 {html.escape(col_display_name)}</blockquote>"
        kb = build_franchise_card_keyboard(sid, img_link)

        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        form.add_field("caption", caption)
        form.add_field("parse_mode", "HTML")
        form.add_field("reply_markup", json.dumps(kb))
        form.add_field("photo", fran_bytes, filename="franchise_collection.jpg", content_type="image/jpeg")
        await tg_upload(http, "sendPhoto", form)

        if build_msg_id:
            await tg_request(
                http,
                "deleteMessage",
                {"chat_id": chat_id, "message_id": build_msg_id},
                auto_delete=False,
            )
        return

    # 11. Manual Edit shortcut
    if action == "edit":
        session["chosen_logo"] = None
        caption, kb = build_position_picker_ui(sid, session)
        await tg_request(
            http,
            "editMessageCaption",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": kb,
            },
            auto_delete=False,
        )
        await tg_request(http, "answerCallbackQuery", {"callback_query_id": cb_id}, auto_delete=False)
        return

    await tg_request(http, "answerCallbackQuery", {"callback_query_id": cb_id}, auto_delete=False)


# ==============================================================================
# 🚀 BOT MENU COMMANDS SETUP & MAIN POLLING LOOP
# ==============================================================================
async def setup_bot_profile_and_commands(http: aiohttp.ClientSession):
    global BOT_USERNAME
    me = await tg_request(http, "getMe", auto_delete=False)
    if me.get("ok") and me["result"].get("username"):
        BOT_USERNAME = me["result"]["username"]

    commands = [
        {"command": "p", "description": "Search movie/series posters in the official group"},
        {"command": "help", "description": "View Bot Guide & Channel Verification"},
        {"command": "start", "description": "Start the Bot (PM Only)"},
    ]
    await tg_request(http, "setMyCommands", {"commands": commands}, auto_delete=False)


async def main():
    print(f"🤖 @{BOT_USERNAME} Started (UHD Mode + Strict Request Lock + Auto-Delete)...")
    offset = 0
    connector = aiohttp.TCPConnector(limit=120, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(total=45)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as http:
        await setup_bot_profile_and_commands(http)
        while True:
            try:
                resp = await tg_request(
                    http,
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": 25,
                        "allowed_updates": ["message", "callback_query"],
                    },
                    auto_delete=False,
                )
                if not resp.get("ok"):
                    await asyncio.sleep(2)
                    continue

                for upd in resp.get("result", []):
                    offset = upd["update_id"] + 1
                    if "message" in upd:
                        asyncio.create_task(handle_message(http, upd["message"]))
                    elif "callback_query" in upd:
                        asyncio.create_task(handle_callback(http, upd["callback_query"]))
            except Exception as e:
                print(f"[Polling Error] {e}")
                await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
