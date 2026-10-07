import os
import math
import asyncio
import logging
import time
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from aiohttp import web
from dotenv import load_dotenv

from telegram import (
    Update,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)


# ============================================================
# LOAD ENVIRONMENT
# ============================================================

load_dotenv()


# ============================================================
# CONFIGURATION
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

AIRTABLE_TOKEN = os.environ["AIRTABLE_TOKEN"]
AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]

OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

# Weather Underground Station Credentials
WU_STATION_ID = os.getenv("WU_STATION_ID", "IGRIKA1")
WU_SEVASTO_STATION_ID = os.getenv("WU_SEVASTO_STATION_ID", "ISEVAS24")
WU_API_KEY = os.getenv("WU_API_KEY")

# Public dashboard links for the stations (shown to users, not API endpoints)
WU_DASHBOARD_URL = f"https://www.wunderground.com/dashboard/pws/{WU_STATION_ID}"
WU_SEVASTO_DASHBOARD_URL = f"https://www.wunderground.com/dashboard/pws/{WU_SEVASTO_STATION_ID}"

# Weather Posting Channel/Topic Destination Configuration (used for
# group/topic invocations only — private-chat invocations reply in place)
# Target: https://t.me/c/4494497813/75
WEATHER_LOG_CHAT_ID = -1004494497813
WEATHER_LOG_THREAD_ID = 75

AIRTABLE_TABLE_NAME = "Participants"

# Airtable fields
AIRTABLE_TELEGRAM_ID_FIELD = "Telegram_ID"
AIRTABLE_TELEGRAM_USERNAME_FIELD = "Telegram_Username"

# Airtable field containing the participant's single, personalized
# Check-In/Check-Out URL (a "smart" link that routes to whichever form
# applies based on the participant's current status).
CHECKINOUT_URL_FIELD = "Custom_CheckInOut_URL"

# QR code settings
QR_CODE_SIZE = "200x200"

# Temporary group welcome message lifetime
# 5 minutes = 300 seconds
TEMP_MESSAGE_SECONDS = 300

# Weather settings for Paramythia, Greece
PARAMYTHIA_LAT = 39.4686
PARAMYTHIA_LON = 20.5133

# Ground elevation (meters ASL) at each location, used to convert
# Espy's-equation cloudbase (which yields height AGL) into ASL:
#   cloudbase_ASL = cloudbase_AGL + ground_elevation_ASL
PARAMYTHIA_ELEVATION_M = 300
WU_STATION_ELEVATION_M = 250       # /weather_gri
WU_SEVASTO_ELEVATION_M = 150       # /weather_sev

# Webhook / server configuration
# WEBHOOK_URL: the public HTTPS base URL where Telegram delivers updates,
# e.g. "https://parabot-abc123-ew.a.run.app". Cloud Run sets PORT
# automatically; the bot listens on that port.
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")   # optional but recommended
PORT = int(os.getenv("PORT", "8080"))

# Secret token for the /trigger_daily_weather endpoint. GitHub Actions
# sends this in the X-Trigger-Token header to prevent unauthorized calls.
DAILY_TRIGGER_TOKEN = os.getenv("DAILY_TRIGGER_TOKEN", "")

# Live tracking Mini App
# MINI_APP_URL: the public HTTPS URL that Telegram opens as a Web App
# when a user taps /map. Must be the same base URL as WEBHOOK_URL
# (or any HTTPS URL serving the Flask/aiohttp map page).
MINI_APP_URL = os.getenv("MINI_APP_URL", "")

# Local directory used to cache Telegram profile pictures so the map
# can serve them over HTTP without hitting Telegram's CDN on every refresh.
# Use /tmp/avatars as a guaranteed-writable fallback if /app isn't writable
# (e.g. when the container runs as a non-root user without a chown in the image).
_APP_AVATARS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "avatars")
_TMP_AVATARS = "/tmp/avatars"
try:
    os.makedirs(_APP_AVATARS, exist_ok=True)
    # Quick write-test to confirm the directory is actually writable.
    _test = os.path.join(_APP_AVATARS, ".writetest")
    with open(_test, "w") as _f:
        _f.write("ok")
    os.remove(_test)
    AVATAR_DIR = _APP_AVATARS
except OSError:
    os.makedirs(_TMP_AVATARS, exist_ok=True)
    AVATAR_DIR = _TMP_AVATARS

# Print early so it appears in Cloud Run logs before the logger is configured.
print(f"[parabot] AVATAR_DIR={AVATAR_DIR}", flush=True)

# Fallback avatar shown when no profile picture is available.
DEFAULT_AVATAR = "https://cdn-icons-png.flaticon.com/512/149/149071.png"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

logging.getLogger("httpx").setLevel(logging.WARNING)


# ============================================================
# LIVE TRACKING STATE
# ============================================================

# Shared in-memory store: { user_id_str -> location_dict }
active_locations: dict = {}


# ============================================================
# LIVE TRACKING HELPERS
# ============================================================

async def fetch_user_avatar(user, context: ContextTypes.DEFAULT_TYPE) -> str:
    """
    Fetches and caches the user's Telegram profile picture locally.

    Stores the file under AVATAR_DIR/<user_id>.jpg and returns the
    server-relative URL /avatars/<user_id>.jpg.

    If the user has no accessible profile photo (privacy settings or no photo
    set), falls back to a generated initial-letter avatar via ui-avatars.com
    so the map marker always shows something identifiable.
    """
    try:
        photos = await context.bot.get_user_profile_photos(user_id=user.id, limit=1)
        logger.info("Avatar fetch for user %s: total_count=%s", user.id, photos.total_count)

        if photos.total_count > 0:
            file_id = photos.photos[0][0].file_id
            logger.info("Avatar file_id for user %s: %s", user.id, file_id)

            file = await context.bot.get_file(file_id)
            logger.info("Avatar file_path for user %s: %s", user.id, file.file_path)

            if file.file_path:
                avatar_filename = f"{user.id}.jpg"
                avatar_path = os.path.join(AVATAR_DIR, avatar_filename)
                logger.info("Downloading avatar to %s", avatar_path)
                await file.download_to_drive(avatar_path)

                exists = os.path.isfile(avatar_path)
                size = os.path.getsize(avatar_path) if exists else 0
                logger.info("Avatar saved: exists=%s size=%d bytes path=%s", exists, size, avatar_path)

                if exists and size > 0:
                    return f"/avatars/{avatar_filename}"
                else:
                    logger.error("Avatar file missing or empty after download: %s", avatar_path)
            else:
                logger.warning("file.file_path is empty for user %s", user.id)
        else:
            logger.info(
                "User %s has no accessible profile photos (privacy settings or no photo set) — "
                "using initial-letter avatar",
                user.id,
            )

    except Exception as e:
        logger.error("Error fetching profile photo for user %s: %s", user.id, e, exc_info=True)

    # Generate an initial-letter avatar so each person has a unique,
    # identifiable marker colour even without a profile photo.
    initials = (user.first_name or "?")[0].upper()
    return f"https://ui-avatars.com/api/?name={initials}&size=96&rounded=true&bold=true&background=0088cc&color=ffffff"


async def update_user_location(user, location, context: ContextTypes.DEFAULT_TYPE):
    """Upsert a user's live location in the active_locations store."""
    live_period = getattr(location, "live_period", None) or 900
    logger.info("update_user_location called for user %s (%s)", user.id, user.full_name)

    user_id_key = str(user.id)
    existing = active_locations.get(user_id_key, {})

    # Determine whether we need to (re-)fetch the avatar:
    #   1. No URL stored yet.
    #   2. Stored URL is not a successfully-cached local file
    #      (covers ui-avatars.com fallbacks, DEFAULT_AVATAR, or a local
    #      /avatars/ path whose file was wiped by a container restart).
    #   Once a real local file exists we keep it and skip the API call.
    avatar_url = existing.get("avatar_url", "")
    local_file_ok = (
        avatar_url.startswith("/avatars/")
        and os.path.isfile(os.path.join(AVATAR_DIR, os.path.basename(avatar_url)))
    )
    if not local_file_ok:
        avatar_url = await fetch_user_avatar(user, context)

    # Preserve altitude already captured via the Mini App geolocation API.
    existing_altitude = existing.get("altitude")

    active_locations[user_id_key] = {
        "name": user.full_name,
        "username": user.username or user.first_name,
        "avatar_url": avatar_url,
        "lat": location.latitude,
        "lng": location.longitude,
        "heading": getattr(location, "heading", None) or 0,
        "altitude": existing_altitude,
        "last_updated": time.time(),
        "live_period": live_period,
    }


def remove_user_location(user_id, reason: str = "Stopped sharing"):
    user_id_key = str(user_id)
    if user_id_key in active_locations:
        del active_locations[user_id_key]
        logger.info("Removed user %s from live map (%s)", user_id_key, reason)


async def cleanup_stale_locations():
    """
    Async background task: removes users whose location has not been
    updated for more than 3 minutes (180 s). Runs every 10 seconds.
    """
    while True:
        await asyncio.sleep(10)
        now = time.time()
        expired = [
            uid for uid, u in list(active_locations.items())
            if (now - u["last_updated"]) > 180
        ]
        for uid in expired:
            remove_user_location(uid, reason="Timeout")


# ============================================================
# USERNAME NORMALIZATION
# ============================================================

def normalize_username(username):
    """
    Normalize a Telegram username.

    Examples:
        @JohnSmith  -> johnsmith
        JohnSmith   -> johnsmith
        @johnsmith  -> johnsmith
        johnsmith   -> johnsmith
    """
    if not username:
        return None

    username = str(username).strip()

    if username.startswith("@"):
        username = username[1:]

    username = username.strip()

    if not username:
        return None

    return username.lower()


# ============================================================
# COMPASS DIRECTION HELPER
# ============================================================

def degrees_to_cardinal(deg: float) -> str:
    """
    Convert wind direction in degrees to a 16-point compass direction.
    """
    directions = [
        "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
        "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"
    ]
    idx = round(deg / 22.5) % 16
    return directions[idx]


# ============================================================
# DEW POINT / CLOUDBASE HELPERS
# ============================================================

def calculate_dew_point(temp_c: float, humidity_pct: float) -> float:
    """
    Estimate dew point (°C) from temperature (°C) and relative humidity (%)
    using the Magnus-Tetens approximation. Needed because OpenWeather's
    /data/2.5/weather endpoint doesn't return dew point directly.
    """
    a = 17.27
    b = 237.7
    alpha = math.log(humidity_pct / 100.0) + (a * temp_c) / (b + temp_c)
    return (b * alpha) / (a - alpha)


def calculate_cloudbase_espy(temp_c: float, dew_point_c: float) -> float:
    """
    Estimate cloud base height (meters AGL) using Espy's equation:
        h = 125 * (T - Td)
    where T and Td are the surface temperature and dew point in °C,
    and h is in meters. (The commonly used imperial form is
    h_ft = 228 * (T - Td) with T, Td in °F.)
    """
    return 125.0 * (temp_c - dew_point_c)


# ============================================================
# WEATHER POSTING HELPER
# ============================================================

async def post_weather_log(bot, text: str, chat_id: int, thread_id: int = None, reply_markup=None):
    """
    Post weather text and buttons to the given destination.

    Callers decide the destination via get_weather_destination():
    - Private chat invocations -> reply directly in that private chat
      (chat_id=user's chat id, thread_id=None).
    - Group/topic invocations -> post to the configured weather log
      topic (chat_id=WEATHER_LOG_CHAT_ID, thread_id=WEATHER_LOG_THREAD_ID),
      i.e. https://t.me/c/4494497813/75
    """
    try:
        await bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
        logger.info(
            "Successfully posted weather update to chat_id=%s thread_id=%s",
            chat_id, thread_id,
        )
    except Exception as e:
        logger.exception(
            "Failed to post weather update to chat_id=%s thread_id=%s: %s",
            chat_id, thread_id, e,
        )


def get_weather_destination(update: Update):
    """
    Decide where a weather command's reply should go.

    - Invoked in a private chat -> reply directly in that private chat.
    - Invoked anywhere else (group, supergroup, or a topic within one)
      -> post to the configured weather log topic
         (WEATHER_LOG_CHAT_ID / WEATHER_LOG_THREAD_ID).

    Returns (chat_id, thread_id).
    """
    chat = update.effective_chat
    if chat and chat.type == "private":
        return chat.id, None
    return WEATHER_LOG_CHAT_ID, WEATHER_LOG_THREAD_ID


# ============================================================
# AIRTABLE LOOKUP
# ============================================================

async def get_participant(
    telegram_user_id,
    telegram_username,
):
    """
    Find a participant in Airtable.

    Lookup priority:
        1. Telegram_Username
        2. Telegram_ID

    Username matching is case-insensitive and ignores @.
    """
    airtable_url = (
        f"https://api.airtable.com/v0/"
        f"{AIRTABLE_BASE_ID}/"
        f"{AIRTABLE_TABLE_NAME}"
    )

    headers = {
        "Authorization": f"Bearer {AIRTABLE_TOKEN}",
    }

    normalized_username = normalize_username(telegram_username)

    async with httpx.AsyncClient(timeout=15) as client:

        # ====================================================
        # 1. SEARCH BY TELEGRAM USERNAME
        # ====================================================

        if normalized_username:
            username_formula = (
                "LOWER("
                "SUBSTITUTE("
                f"{{{AIRTABLE_TELEGRAM_USERNAME_FIELD}}},"
                "'@',"
                "''"
                ")"
                ")="
                f"'{normalized_username}'"
            )

            logger.info("Searching Airtable by username: %s", normalized_username)

            response = await client.get(
                airtable_url,
                headers=headers,
                params={
                    "filterByFormula": username_formula,
                    "maxRecords": 1,
                },
            )
            response.raise_for_status()

            data = response.json()
            records = data.get("records", [])

            if records:
                fields = records[0].get("fields", {})
                logger.info("Participant found by username: %s", normalized_username)
                return fields

        # ====================================================
        # 2. FALL BACK TO TELEGRAM ID
        # ====================================================

        logger.info(
            "Username lookup failed. Searching Airtable by Telegram ID: %s",
            telegram_user_id,
        )

        id_formula = f"{{{AIRTABLE_TELEGRAM_ID_FIELD}}}='{telegram_user_id}'"

        response = await client.get(
            airtable_url,
            headers=headers,
            params={
                "filterByFormula": id_formula,
                "maxRecords": 1,
            },
        )
        response.raise_for_status()

        data = response.json()
        records = data.get("records", [])

        if records:
            fields = records[0].get("fields", {})
            logger.info("Participant found by Telegram ID: %s", telegram_user_id)
            return fields

    logger.warning(
        "No participant found for Telegram ID=%s username=%s",
        telegram_user_id,
        telegram_username,
    )
    return None


# ============================================================
# WEATHER LOOKUPS
# ============================================================

async def get_paramythia_weather():
    """
    Fetch current weather conditions for Paramythia, Greece
    using OpenWeather API.
    """
    if not OPENWEATHER_API_KEY:
        raise ValueError("OPENWEATHER_API_KEY environment variable is missing.")

    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {
        "lat": PARAMYTHIA_LAT,
        "lon": PARAMYTHIA_LON,
        "appid": OPENWEATHER_API_KEY,
        "units": "metric",
    }

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()


async def get_wunderground_weather(station_id: str, api_key: str):
    """
    Fetch current weather conditions for a Weather Underground PWS.
    """
    url = "https://api.weather.com/v2/pws/observations/current"
    params = {
        "stationId": station_id,
        "format": "json",
        "units": "m",  # Metric units (Celsius, km/h, mm)
        "apiKey": api_key,
    }

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()


async def get_wunderground_daily_history(station_id: str, api_key: str, date_str: str = None):
    """
    Fetch the daily summary (min/max/avg) for a Weather Underground PWS
    from the /v2/pws/history/daily endpoint.

    date_str: "YYYYMMDD". Defaults to today's date (local server time).
    Returns the parsed JSON response; the daily stats live under
    each observation's "metric" object as e.g. windspeedHigh, windgustHigh.
    """
    from datetime import datetime
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")

    url = "https://api.weather.com/v2/pws/history/daily"
    params = {
        "stationId": station_id,
        "format": "json",
        "units": "m",  # Metric units (km/h, °C, mm)
        "date": date_str,
        "apiKey": api_key,
    }

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()


# ============================================================
# QR CODE URL
# ============================================================

def create_qr_code_url(data):
    """
    Create a QR Server API URL.
    """
    if not data:
        return None

    encoded_data = quote(str(data), safe="")

    return (
        "https://api.qrserver.com/v1/create-qr-code/"
        f"?size={QR_CODE_SIZE}"
        f"&data={encoded_data}"
    )


# ============================================================
# PRIVATE WELCOME MESSAGE
# ============================================================

async def send_private_welcome(bot, user):
    """
    Send the initial private welcome message.
    """
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🎟 My Code",
                callback_data="codes",
            )
        ]
    ])

    first_name = user.first_name or "there"

    await bot.send_message(
        chat_id=user.id,
        text=(
            f"👋 <b>Welcome, {first_name}!</b>\n\n"
            "We're happy to have you in our paragliding community. 🎉\n\n"
            "You can use the button below to retrieve "
            "your personal Check-In/Check-Out code."
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


# ============================================================
# /START
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not user or user.is_bot:
        return

    parameter = context.args[0] if context.args else None

    if parameter == "codes":
        try:
            await send_codes_to_user(bot=context.bot, user=user)
        except Exception as e:
            logger.exception("Could not send codes from /start codes: %s", e)
            await context.bot.send_message(
                chat_id=user.id,
                text="⚠️ I couldn't retrieve your code right now.\n\nPlease try again.",
            )
        return

    if parameter and parameter.startswith("welcome_"):
        try:
            parts = parameter.split("_")
            if len(parts) >= 3:
                group_id = int(parts[1])
                message_id = int(parts[2])
                try:
                    await context.bot.delete_message(
                        chat_id=group_id, message_id=message_id
                    )
                    logger.info(
                        "Deleted temporary welcome message %s from group %s",
                        message_id, group_id
                    )
                except Exception as e:
                    logger.info("Could not delete temporary welcome message: %s", e)
        except (ValueError, IndexError) as e:
            logger.warning("Invalid welcome deep link: %s", e)

    await send_private_welcome(bot=context.bot, user=user)


# ============================================================
# SEND CODE PRIVATELY
# ============================================================

async def send_codes_to_user(bot, user):
    try:
        participant = await get_participant(
            telegram_user_id=user.id,
            telegram_username=user.username,
        )
    except httpx.HTTPStatusError as e:
        logger.exception("Airtable HTTP error: %s", e)
        await bot.send_message(
            chat_id=user.id,
            text=(
                "⚠️ <b>Temporary problem</b>\n\n"
                "I couldn't access the participant database right now.\n\n"
                "Please try again in a moment."
            ),
            parse_mode=ParseMode.HTML,
        )
        return
    except Exception as e:
        logger.exception("Airtable lookup failed: %s", e)
        await bot.send_message(
            chat_id=user.id,
            text="⚠️ Something went wrong while retrieving your code.\n\nPlease try again later.",
        )
        return

    if not participant:
        await bot.send_message(
            chat_id=user.id,
            text=(
                "❌ <b>Participant not found</b>\n\n"
                "I couldn't find a participant record matching your Telegram username or ID.\n\n"
                "Please contact an administrator if you believe this is an error."
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    checkinout_url = participant.get(CHECKINOUT_URL_FIELD)
    checkinout_qr_url = create_qr_code_url(checkinout_url)

    first_name = user.first_name or "there"

    await bot.send_message(
        chat_id=user.id,
        text=(
            f"🎟 <b>Your Personal Code</b>\n\n"
            f"Hello {first_name}!\n\n"
            "Here is your personal Check-In/Check-Out link. It automatically "
            "takes you to the right form (Check-In or Check-Out) based on "
            "your current status.\n\n"
            "⚠️ Please keep this code private."
        ),
        parse_mode=ParseMode.HTML,
    )

    if checkinout_url:
        checkinout_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢🔴 Click here to Check In / Check Out", url=str(checkinout_url))]
        ])
        await bot.send_message(
            chat_id=user.id,
            text="🟢🔴 <b>CHECK-IN / CHECK-OUT</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=checkinout_keyboard,
        )
        if checkinout_qr_url:
            try:
                await bot.send_photo(
                    chat_id=user.id,
                    photo=checkinout_qr_url,
                    caption="📱 Check-In/Check-Out QR Code",
                )
            except Exception as e:
                logger.exception("Could not send Check-In/Check-Out QR: %s", e)
                await bot.send_message(
                    chat_id=user.id,
                    text="⚠️ I couldn't load the QR image. Please use the button above.",
                )
    else:
        await bot.send_message(
            chat_id=user.id,
            text="🟢🔴 <b>CHECK-IN / CHECK-OUT</b>\n\nNo code is currently available.",
            parse_mode=ParseMode.HTML,
        )


# ============================================================
# /CODES COMMAND
# ============================================================

async def codes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not user or user.is_bot:
        return

    # If /codes is called in a private chat,
    # send the code directly in that private chat.
    if update.effective_chat.type == "private":
        await send_codes_to_user(
            bot=context.bot,
            user=user
        )
        return

    # If /codes is called inside a group:
    # DO NOT send anything back to the group.
    #
    # Attempt to send the code directly to the user's
    # private chat.
    try:
        await send_codes_to_user(
            bot=context.bot,
            user=user
        )

    except Exception as e:
        logger.info(
            "Could not DM user directly from group /codes "
            "(bot may not have a private chat with the user): %s",
            e
        )


# ============================================================
# /MAP COMMAND
# ============================================================

async def map_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Send an inline button that opens the live group location map as a
    Telegram Mini App (Web App).

    WebAppInfo buttons only function in private chats. When /map is called
    from a group the button is sent to the user's private DM instead, so it
    actually works. If the bot has never had a private conversation with the
    user it logs the failure silently (same behaviour as /codes).
    """
    if not update.message:
        return

    user = update.effective_user
    if not user or user.is_bot:
        return

    if not MINI_APP_URL:
        await update.message.reply_text(
            "⚠️ The live map is not configured yet. "
            "Please ask an administrator to set MINI_APP_URL."
        )
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            text="🗺️ Open Live Group Map",
            web_app=WebAppInfo(url=MINI_APP_URL),
        )]
    ])

    is_private = update.effective_chat.type == "private"

    if is_private:
        await update.message.reply_text(
            "Tap below to open the interactive live map inside Telegram:",
            reply_markup=keyboard,
        )
    else:
        # Group/topic: send the button privately so WebAppInfo works,
        # then confirm in the group so the user knows where to look.
        try:
            await context.bot.send_message(
                chat_id=user.id,
                text="Tap below to open the interactive live map inside Telegram:",
                reply_markup=keyboard,
            )
            await update.message.reply_text(
                f"🗺️ {user.first_name}, I sent you the map link in a private message!"
            )
        except Exception as e:
            logger.info(
                "Could not DM map button to user %s (no private chat yet): %s",
                user.id, e,
            )
            await update.message.reply_text(
                f"🗺️ {user.first_name}, please start a private chat with me first "
                f"(tap my name → Start), then use /map again."
            )


# ============================================================
# /HELP COMMAND
# ============================================================

HELP_TEXT = (
    "🤖 <b>Bot Commands & How Things Work</b>\n\n"

    "🎟 <b>Code</b>\n"
    "• <code>/codes</code> — Sends your personal Check-In/Check-Out "
    "link + QR code. Always delivered to your <b>private chat</b> with "
    "the bot, even if you run it inside a group. If we've never "
    "messaged privately before, DM the bot once (e.g. tap 'My Code' "
    "in the welcome message) so it's able to message you.\n"
    "• <code>/start</code> — Opens your private chat with the bot and "
    "shows a 'My Code' button. New members get a temporary welcome "
    "message in the group with a link that starts a private chat.\n\n"

    "🌤 <b>Weather</b>\n"
    "• <code>/weather_para</code> — Current weather for Paramythia, "
    "Greece (OpenWeather).\n"
    "• <code>/weather_gri</code> — Current conditions from the Grika "
    "Weather Underground station, plus today's daily max wind/gust and "
    "a link to the live station dashboard.\n"
    "• <code>/weather_sev</code> — Same as above, for the Sevasto "
    "station.\n"
    "• <code>/weather_all</code> — Runs all three of the above and "
    "posts them together in a single message. This combined report is "
    "also posted <b>automatically every day at 12:00 (Greece time)</b> "
    "to the weather log topic.\n\n"

    "Every weather report includes a <b>Cloudbase (Espy)</b> estimate in "
    "meters ASL, calculated from temperature and dew point using Espy's "
    "equation (h = 125 × (T − Td), AGL) plus each location's ground "
    "elevation.\n\n"

    "📍 <b>Live Map</b>\n"
    "• <code>/map</code> — Opens the interactive live group map inside "
    "Telegram. To appear on the map, share your <b>live location</b> "
    "(not a static pin) in the chat:\n"
    "  1. Tap <b>Attachment → Location</b>\n"
    "  2. Choose <b>Share Live Location</b> (not \"Send Current Location\")\n"
    "  3. Pick a duration (15m, 1h, or 8h)\n"
    "The map updates automatically every few seconds as you move. "
    "When used in a group, the map button is sent to your private chat "
    "(the Mini App only works in private messages).\n\n"

    "📍 <b>Where replies go</b>\n"
    "• Run a weather command in your <b>private chat</b> with the bot → "
    "the reply appears right there.\n"
    "• Run a weather command in a <b>group</b> (any topic) → your "
    "command message is deleted and the reply is posted to the "
    "dedicated weather log topic instead, to keep the group tidy.\n\n"

    "❓ <code>/help</code> — Shows this message."
)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    await update.message.reply_text(
        HELP_TEXT,
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# /WEATHER_PARA COMMAND
# ============================================================

async def build_paramythia_weather_text() -> str:
    """
    Fetch Paramythia weather and build the formatted HTML message text,
    including cloudbase (Espy's equation, ASL). Raises on failure so
    callers can decide how to report the error.
    """
    data = await get_paramythia_weather()

    raw_temp = data["main"]["temp"]
    temp = round(raw_temp)
    feels_like = round(data["main"]["feels_like"])
    description = data["weather"][0]["description"].capitalize()
    humidity = data["main"]["humidity"]

    wind_data = data.get("wind", {})
    wind_speed_m_s = wind_data.get("speed", 0)
    wind_speed_kmh = round(wind_speed_m_s * 3.6, 1)

    wind_deg = wind_data.get("deg")
    cardinal_dir = degrees_to_cardinal(wind_deg) if wind_deg is not None else "N/A"
    dir_text = f"{cardinal_dir} ({wind_deg}°)" if wind_deg is not None else "N/A"

    gust_m_s = wind_data.get("gust")
    gust_text = f"• <b>Wind Gusts:</b> {round(gust_m_s * 3.6, 1)} km/h ({gust_m_s} m/s)\n" if gust_m_s is not None else ""

    # Cloudbase (Espy's equation), estimated from temperature and a
    # dew point derived from temperature + humidity (Magnus-Tetens).
    # Espy's equation gives height AGL, so add the ground elevation
    # to report ASL.
    cloudbase_text = ""
    try:
        dew_point_c = calculate_dew_point(raw_temp, humidity)
        cloudbase_agl_m = max(calculate_cloudbase_espy(raw_temp, dew_point_c), 0)
        cloudbase_asl_m = cloudbase_agl_m + PARAMYTHIA_ELEVATION_M
        cloudbase_text = (
            f"• <b>Dew Point:</b> {round(dew_point_c)}°C\n"
            f"• <b>Cloudbase (Espy):</b> ~{round(cloudbase_asl_m)} m ASL\n"
        )
    except Exception as e:
        logger.exception("Could not calculate cloudbase for Paramythia: %s", e)

    return (
        "🌤 <b>Weather in Paramythia, Greece</b>\n\n"
        f"• <b>Condition:</b> {description}\n"
        f"• <b>Temperature:</b> {temp}°C (feels like {feels_like}°C)\n"
        f"• <b>Humidity:</b> {humidity}%\n"
        f"{cloudbase_text}"
        f"• <b>Wind Speed:</b> {wind_speed_kmh} km/h ({wind_speed_m_s} m/s)\n"
        f"{gust_text}"
        f"• <b>Wind Direction:</b> {dir_text}"
    )


async def weather_para_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    is_private = update.effective_chat.type == "private"

    # Delete original command message from the user (group/topic only —
    # bots generally can't delete a user's own messages in a private chat)
    if not is_private:
        try:
            await update.message.delete()
        except Exception as e:
            logger.warning("Could not delete user command message: %s", e)

    target_chat_id, target_thread_id = get_weather_destination(update)

    try:
        weather_text = await build_paramythia_weather_text()

        # Post to the private chat if invoked there, otherwise to the
        # configured weather log topic.
        await post_weather_log(
            bot=context.bot,
            text=weather_text,
            chat_id=target_chat_id,
            thread_id=target_thread_id,
        )

    except Exception as e:
        logger.exception("Could not fetch weather for Paramythia: %s", e)


# ============================================================
# /WEATHER_GRI COMMAND (WEATHER UNDERGROUND STATION)
# ============================================================

async def build_wu_station_weather_text(
    station_id_param: str,
    api_key: str,
    elevation_m: float,
    default_neighborhood: str,
) -> str:
    """
    Fetch current + daily-max conditions for a Weather Underground PWS and
    build the formatted HTML message text, including cloudbase (Espy's
    equation, ASL). Raises on failure so callers can decide how to report
    the error. Returns "" if the station has no current observations.
    """
    data = await get_wunderground_weather(
        station_id=station_id_param,
        api_key=api_key,
    )

    observations = data.get("observations", [])
    if not observations:
        return ""

    obs = observations[0]
    metric = obs.get("metric", {})

    neighborhood = obs.get("neighborhood", default_neighborhood)
    station_id = obs.get("stationID", station_id_param)
    obs_time = obs.get("obsTimeLocal", "N/A")

    temp = metric.get("temp")
    heat_index = metric.get("heatIndex")
    wind_chill = metric.get("windChill")
    humidity = obs.get("humidity")
    dewpt = metric.get("dewpt")

    wind_speed = metric.get("windSpeed", 0)
    wind_gust = metric.get("windGust")
    wind_deg = obs.get("winddir")

    cardinal_dir = degrees_to_cardinal(wind_deg) if wind_deg is not None else "N/A"
    dir_text = f"{cardinal_dir} ({wind_deg}°)" if wind_deg is not None else "N/A"

    gust_text = f"• <b>Wind Gusts:</b> {wind_gust} km/h\n" if wind_gust is not None else ""
    precip_rate = metric.get("precipRate", 0)
    precip_total = metric.get("precipTotal", 0)
    pressure = metric.get("pressure")

    # Cloudbase (Espy's equation): h = 125 * (T - Td), using the
    # station's own temperature and dew point directly. Espy's
    # equation gives height AGL, so add the ground elevation for ASL.
    cloudbase_text = ""
    if temp is not None and dewpt is not None:
        try:
            cloudbase_agl_m = max(calculate_cloudbase_espy(temp, dewpt), 0)
            cloudbase_asl_m = cloudbase_agl_m + elevation_m
            cloudbase_text = f"• <b>Cloudbase (Espy):</b> ~{round(cloudbase_asl_m)} m ASL\n"
        except Exception as e:
            logger.exception("Could not calculate cloudbase for %s: %s", station_id_param, e)

    # Daily max wind speed / gust from the history/daily endpoint
    max_wind_text = ""
    try:
        history_data = await get_wunderground_daily_history(
            station_id=station_id_param,
            api_key=api_key,
        )
        daily_obs = history_data.get("observations", [])
        if daily_obs:
            daily_metric = daily_obs[-1].get("metric", {})
            wind_speed_high = daily_metric.get("windspeedHigh")
            wind_gust_high = daily_metric.get("windgustHigh")

            if wind_speed_high is not None or wind_gust_high is not None:
                max_wind_text = (
                    "\n📊 <b>Today's Daily Max</b>\n"
                    f"• <b>Max Wind Speed:</b> {wind_speed_high} km/h\n"
                    f"• <b>Max Wind Gust:</b> {wind_gust_high} km/h\n"
                )
        else:
            logger.info("No daily history observations returned for %s", station_id_param)
    except Exception as e:
        logger.exception("Could not fetch daily history for %s: %s", station_id_param, e)

    return (
        f"🌤 <b>Weather Station: {neighborhood} ({station_id})</b>\n"
        f"🕒 <i>Observed: {obs_time}</i>\n\n"
        f"• <b>Temperature:</b> {temp}°C\n"
        f"• <b>Feels Like (Heat Index / Wind Chill):</b> {heat_index}°C / {wind_chill}°C\n"
        f"• <b>Humidity:</b> {humidity}%\n"
        f"• <b>Dew Point:</b> {dewpt}°C\n"
        f"{cloudbase_text}"
        f"• <b>Wind Speed:</b> {wind_speed} km/h\n"
        f"{gust_text}"
        f"• <b>Wind Direction:</b> {dir_text}\n"
        f"• <b>Precipitation Rate:</b> {precip_rate} mm/h (Total Today: {precip_total} mm)\n"
        f"• <b>Pressure:</b> {pressure} hPa"
        f"{max_wind_text}"
    )


async def weather_gri_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle /weather_gri in groups, topics, or private chat.

    - Private chat: deletes nothing (can't delete the user's own message
      there) and replies directly in the private chat.
    - Group/topic: deletes the original command message and posts results
      to the configured weather log topic.
    """
    if not update.message:
        return

    is_private = update.effective_chat.type == "private"

    if not is_private:
        try:
            await update.message.delete()
        except Exception as e:
            logger.warning("Could not delete user command message: %s", e)

    target_chat_id, target_thread_id = get_weather_destination(update)

    try:
        weather_text = await build_wu_station_weather_text(
            station_id_param=WU_STATION_ID,
            api_key=WU_API_KEY,
            elevation_m=WU_STATION_ELEVATION_M,
            default_neighborhood="Grika Station",
        )
        if not weather_text:
            return

        dashboard_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📡 Open Station Dashboard", url=WU_DASHBOARD_URL)]
        ])

        # Post to the private chat if invoked there, otherwise to the
        # configured weather log topic.
        await post_weather_log(
            bot=context.bot,
            text=weather_text,
            chat_id=target_chat_id,
            thread_id=target_thread_id,
            reply_markup=dashboard_keyboard,
        )

    except Exception as e:
        logger.exception("Could not fetch Wunderground weather for IGRIKA1: %s", e)


# ============================================================
# /WEATHER_SEV COMMAND (WEATHER UNDERGROUND STATION)
# ============================================================

async def weather_sev_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle /weather_sev in groups, topics, or private chat.

    - Private chat: deletes nothing (can't delete the user's own message
      there) and replies directly in the private chat.
    - Group/topic: deletes the original command message and posts results
      to the configured weather log topic.
    """
    if not update.message:
        return

    is_private = update.effective_chat.type == "private"

    if not is_private:
        try:
            await update.message.delete()
        except Exception as e:
            logger.warning("Could not delete user command message: %s", e)

    target_chat_id, target_thread_id = get_weather_destination(update)

    try:
        weather_text = await build_wu_station_weather_text(
            station_id_param=WU_SEVASTO_STATION_ID,
            api_key=WU_API_KEY,
            elevation_m=WU_SEVASTO_ELEVATION_M,
            default_neighborhood="Sevasto Station",
        )
        if not weather_text:
            return

        dashboard_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📡 Open Station Dashboard", url=WU_SEVASTO_DASHBOARD_URL)]
        ])

        # Post to the private chat if invoked there, otherwise to the
        # configured weather log topic.
        await post_weather_log(
            bot=context.bot,
            text=weather_text,
            chat_id=target_chat_id,
            thread_id=target_thread_id,
            reply_markup=dashboard_keyboard,
        )

    except Exception as e:
        logger.exception("Could not fetch Wunderground weather for %s: %s", WU_SEVASTO_STATION_ID, e)


# ============================================================
# /WEATHER_ALL COMMAND (ALL SOURCES COMBINED)
# ============================================================

async def build_combined_weather_all_message():
    """
    Fetch Paramythia (OpenWeather), Grika (WU) and Sevasto (WU) weather and
    build one combined message + dashboard keyboard. Each source is
    fetched independently, so if one source fails the others are still
    reported; a failed source is shown as an "unavailable" line instead of
    silently vanishing. Used by both /weather_all and the daily trigger
    endpoint so they stay identical.

    Returns (combined_text, dashboard_keyboard).
    """
    sections = []

    try:
        sections.append(await build_paramythia_weather_text())
    except Exception as e:
        logger.exception("Could not fetch weather for Paramythia: %s", e)
        sections.append("🌤 <b>Weather in Paramythia, Greece</b>\n\n⚠️ Unavailable right now.")

    try:
        gri_text = await build_wu_station_weather_text(
            station_id_param=WU_STATION_ID,
            api_key=WU_API_KEY,
            elevation_m=WU_STATION_ELEVATION_M,
            default_neighborhood="Grika Station",
        )
        sections.append(gri_text or "🌤 <b>Weather Station: Grika</b>\n\n⚠️ No current observations.")
    except Exception as e:
        logger.exception("Could not fetch Wunderground weather for IGRIKA1: %s", e)
        sections.append("🌤 <b>Weather Station: Grika</b>\n\n⚠️ Unavailable right now.")

    try:
        sev_text = await build_wu_station_weather_text(
            station_id_param=WU_SEVASTO_STATION_ID,
            api_key=WU_API_KEY,
            elevation_m=WU_SEVASTO_ELEVATION_M,
            default_neighborhood="Sevasto Station",
        )
        sections.append(sev_text or "🌤 <b>Weather Station: Sevasto</b>\n\n⚠️ No current observations.")
    except Exception as e:
        logger.exception("Could not fetch Wunderground weather for %s: %s", WU_SEVASTO_STATION_ID, e)
        sections.append("🌤 <b>Weather Station: Sevasto</b>\n\n⚠️ Unavailable right now.")

    combined_text = "\n\n➖➖➖➖➖➖➖➖➖➖\n\n".join(sections)

    dashboard_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📡 Grika Station Dashboard", url=WU_DASHBOARD_URL)],
        [InlineKeyboardButton("📡 Sevasto Station Dashboard", url=WU_SEVASTO_DASHBOARD_URL)],
    ])

    return combined_text, dashboard_keyboard


async def weather_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle /weather_all in groups, topics, or private chat.

    Fetches Paramythia (OpenWeather), Grika (WU) and Sevasto (WU) weather
    and posts them as one combined message with both station dashboard
    buttons. Each source is fetched independently, so if one source fails
    the others are still reported; a failed source is shown as an
    "unavailable" line instead of silently vanishing.

    - Private chat: deletes nothing (can't delete the user's own message
      there) and replies directly in the private chat.
    - Group/topic: deletes the original command message and posts results
      to the configured weather log topic.
    """
    if not update.message:
        return

    is_private = update.effective_chat.type == "private"

    if not is_private:
        try:
            await update.message.delete()
        except Exception as e:
            logger.warning("Could not delete user command message: %s", e)

    target_chat_id, target_thread_id = get_weather_destination(update)

    combined_text, dashboard_keyboard = await build_combined_weather_all_message()

    # Post to the private chat if invoked there, otherwise to the
    # configured weather log topic.
    await post_weather_log(
        bot=context.bot,
        text=combined_text,
        chat_id=target_chat_id,
        thread_id=target_thread_id,
        reply_markup=dashboard_keyboard,
    )


# ============================================================
# BUTTON HANDLER
# ============================================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not query:
        return

    await query.answer()
    user = query.from_user

    if query.data == "codes":
        try:
            await send_codes_to_user(bot=context.bot, user=user)
        except Exception as e:
            logger.exception("Could not send codes from button: %s", e)
            await context.bot.send_message(
                chat_id=user.id,
                text="⚠️ I couldn't retrieve your code right now. Please try again.",
            )


# ============================================================
# NEW MEMBER HANDLER
# ============================================================

async def new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    if not message or not message.new_chat_members:
        return

    bot_username = context.bot.username
    if not bot_username:
        logger.error("Could not determine bot username.")
        return

    for user in message.new_chat_members:
        if user.is_bot:
            continue

        temporary_message = await message.reply_text(
            f"👋 Welcome {user.mention_html()}!\n\n"
            "We're glad to have you in our paragliding community. 🎉\n\n"
            "Tap the button below to receive your private welcome message.",
            parse_mode=ParseMode.HTML,
        )

        start_parameter = f"welcome_{message.chat.id}_{temporary_message.message_id}"
        start_link = f"https://t.me/{bot_username}?start={start_parameter}"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("👋 Get my private welcome", url=start_link)]
        ])

        try:
            await temporary_message.edit_reply_markup(reply_markup=keyboard)
        except Exception as e:
            logger.exception("Could not add welcome button: %s", e)

        asyncio.create_task(
            delete_later(
                bot=context.bot,
                chat_id=message.chat.id,
                message_id=temporary_message.message_id,
                seconds=TEMP_MESSAGE_SECONDS,
            )
        )


# ============================================================
# DELETE TEMPORARY MESSAGE
# ============================================================

async def delete_later(bot, chat_id, message_id, seconds):
    await asyncio.sleep(seconds)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info("Deleted expired temporary message %s", message_id)
    except Exception as e:
        logger.info("Temporary message %s already deleted: %s", message_id, e)


# ============================================================
# LOCATION HANDLERS (LIVE TRACKING)
# ============================================================

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the initial live-location share or a one-off location pin."""
    msg = update.message
    logger.info("handle_location fired: msg=%s location=%s", msg is not None, msg.location if msg else None)
    if msg and msg.location and msg.from_user:
        await update_user_location(msg.from_user, msg.location, context)


async def handle_live_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle edited-message location updates (live location ticks)."""
    msg = update.edited_message
    logger.info("handle_live_update fired: msg=%s", msg is not None)
    if not msg or not msg.from_user:
        return
    if msg.location:
        # live_period == 0 means the user tapped "Stop sharing"
        if getattr(msg.location, "live_period", None) == 0:
            remove_user_location(msg.from_user.id, reason="Manually Stopped")
        else:
            await update_user_location(msg.from_user, msg.location, context)


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Telegram update error", exc_info=context.error)


# ============================================================
# LIVE MAP HTML PAGE
# ============================================================

MAP_HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
    <title>Paragliding Live Map</title>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        body, html { margin: 0; padding: 0; height: 100%; width: 100%; overflow: hidden;
                     font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        #map { height: 100vh; width: 100vw; }

        .avatar-marker {
            width: 48px; height: 48px; border-radius: 50%;
            border: 3px solid #0088cc;
            box-shadow: 0px 3px 8px rgba(0,0,0,0.4);
            background-size: cover; background-position: center; background-color: #fff;
            transition: all 0.3s ease;
        }
        .popup-card { text-align: center; padding: 4px; }
        .popup-name { font-size: 15px; font-weight: bold; color: #222; margin-bottom: 2px; }
        .popup-username { font-size: 12px; color: #666; margin-bottom: 6px; }
        .popup-altitude {
            font-size: 12px; font-weight: 500; color: #2e7d32;
            background-color: #e8f5e9; padding: 4px 8px;
            border-radius: 4px; margin-bottom: 10px; display: inline-block;
        }
        .gmaps-btn {
            display: inline-block; background-color: #4285F4; color: white !important;
            text-decoration: none; padding: 8px 12px; font-size: 12px; font-weight: 600;
            border-radius: 6px; box-shadow: 0 2px 4px rgba(0,0,0,0.2);
        }
        .gmaps-btn:hover { background-color: #3367D6; }
    </style>
</head>
<body>
    <div id="map"></div>
    <script>
        const tg = window.Telegram.WebApp;
        tg.ready();
        tg.expand();

        const map = L.map('map').setView([39.47, 20.51], 12);

        // Base layers
        const osmLayer = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            maxZoom: 19,
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
        });

        const terrainLayer = L.tileLayer('https://tiles.stadiamaps.com/tiles/stamen_terrain/{z}/{x}/{y}{r}.png', {
            maxZoom: 18,
            attribution: '&copy; <a href="https://stadiamaps.com/" target="_blank">Stadia Maps</a> &copy; <a href="https://stamen.com/" target="_blank">Stamen Design</a> &copy; <a href="https://openmaptiles.org/" target="_blank">OpenMapTiles</a> &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
        });

        // Add terrain as the default base layer
        terrainLayer.addTo(map);

        // Layer control to switch between base maps
        const baseLayers = {
            "Terrain": terrainLayer,
            "OpenStreetMap": osmLayer
        };
        L.control.layers(baseLayers).addTo(map);

        let markers = {};
        let boundsSet = false;

        // Push device altitude to the server so the map popup can show it.
        if ("geolocation" in navigator && tg.initDataUnsafe && tg.initDataUnsafe.user) {
            navigator.geolocation.watchPosition(
                (position) => {
                    const altitude = position.coords.altitude !== null
                        ? Math.round(position.coords.altitude) : null;
                    fetch('/api/update_location', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            user_id:  tg.initDataUnsafe.user.id,
                            name:     (tg.initDataUnsafe.user.first_name + ' ' +
                                       (tg.initDataUnsafe.user.last_name || '')).trim(),
                            username: tg.initDataUnsafe.user.username || '',
                            lat:      position.coords.latitude,
                            lng:      position.coords.longitude,
                            altitude: altitude
                        })
                    }).catch(err => console.error("Altitude push error:", err));
                },
                (err) => console.warn("Geolocation error:", err.message),
                { enableHighAccuracy: true }
            );
        }

        function createAvatarIcon(avatarUrl) {
            return L.divIcon({
                className: 'custom-leaflet-icon',
                html: `<div class="avatar-marker" style="background-image: url('${avatarUrl}');"></div>`,
                iconSize: [48, 48], iconAnchor: [24, 24], popupAnchor: [0, -26]
            });
        }

        function createPopupContent(user) {
            const googleMapsUrl = `https://www.google.com/maps?q=${user.lat},${user.lng}`;
            const altitudeText = (user.altitude !== null && user.altitude !== undefined)
                ? `⛰️ Altitude: <b>${user.altitude} m</b>`
                : `⛰️ Altitude: <i>N/A</i>`;
            return `
                <div class="popup-card">
                    <div class="popup-name">${user.name}</div>
                    <div class="popup-username">@${user.username}</div>
                    <div class="popup-altitude">${altitudeText}</div>
                    <a href="${googleMapsUrl}" target="_blank" class="gmaps-btn">
                        📍 Follow on Google Maps
                    </a>
                </div>`;
        }

        async function fetchLocations() {
            try {
                const response = await fetch('/api/locations');
                const users = await response.json();
                const activeIds = new Set(Object.keys(users));

                for (const uid in markers) {
                    if (!activeIds.has(uid)) {
                        map.removeLayer(markers[uid]);
                        delete markers[uid];
                    }
                }

                const bounds = [];
                for (const [userId, user] of Object.entries(users)) {
                    const latLng = [user.lat, user.lng];
                    bounds.push(latLng);
                    const popupHTML = createPopupContent(user);
                    if (markers[userId]) {
                        markers[userId].setLatLng(latLng);
                        markers[userId].getPopup().setContent(popupHTML);
                    } else {
                        const icon = createAvatarIcon(user.avatar_url || '');
                        markers[userId] = L.marker(latLng, { icon })
                            .addTo(map).bindPopup(popupHTML);
                    }
                }

                if (!boundsSet && bounds.length > 0) {
                    map.fitBounds(bounds, { padding: [40, 40], maxZoom: 15 });
                    boundsSet = true;
                }
            } catch (err) {
                console.error("Map refresh error:", err);
            }
        }

        setInterval(fetchLocations, 3000);
        fetchLocations();
    </script>
</body>
</html>"""


# ============================================================
# AIOHTTP ROUTES — LIVE MAP
# ============================================================

async def handle_map_index(request: web.Request) -> web.Response:
    """Serve the Mini App HTML page."""
    return web.Response(text=MAP_HTML_PAGE, content_type="text/html")


async def handle_api_locations(request: web.Request) -> web.Response:
    """Return all active locations as JSON."""
    import json
    return web.Response(
        text=json.dumps(active_locations),
        content_type="application/json",
    )


async def handle_api_update_location(request: web.Request) -> web.Response:
    """
    POST /api/update_location

    Called by the Mini App's JavaScript to push the browser's
    geolocation (including altitude) back to the server so the map
    can display it alongside the Telegram live-location data.
    """
    import json
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text='{"error": "Invalid JSON"}',
                            content_type="application/json")

    if not data or "user_id" not in data:
        return web.Response(status=400, text='{"error": "Missing user_id"}',
                            content_type="application/json")

    user_id_key = str(data["user_id"])
    existing = active_locations.get(user_id_key, {})

    active_locations[user_id_key] = {
        "name":         data.get("name") or existing.get("name") or "Telegram User",
        "username":     data.get("username") or existing.get("username") or "",
        "avatar_url":   existing.get("avatar_url") or DEFAULT_AVATAR,
        "lat":          data["lat"],
        "lng":          data["lng"],
        "altitude":     data.get("altitude"),
        "heading":      existing.get("heading", 0),
        "last_updated": time.time(),
        "live_period":  existing.get("live_period", 900),
    }
    return web.Response(text='{"status": "success"}', content_type="application/json")


async def handle_avatar(request: web.Request) -> web.Response:
    """
    Serve cached avatar images from AVATAR_DIR.

    The file is written to disk by fetch_user_avatar() when the user first
    shares their location. If it is missing (e.g. after a container restart
    on Cloud Run) we return a redirect to DEFAULT_AVATAR so the map always
    shows something rather than a broken image.
    """
    filename = request.match_info["filename"]
    filepath = os.path.join(AVATAR_DIR, filename)
    logger.info("Avatar request: %s — exists=%s", filepath, os.path.isfile(filepath))
    if os.path.isfile(filepath):
        return web.FileResponse(filepath)
    # File not on disk yet — redirect to the fallback avatar.
    logger.warning("Avatar not found, redirecting to DEFAULT_AVATAR: %s", filepath)
    raise web.HTTPFound(DEFAULT_AVATAR)


# ============================================================
# AIOHTTP ROUTES
# ============================================================

async def handle_health(request: web.Request) -> web.Response:
    """Liveness / readiness probe for Cloud Run."""
    return web.Response(text="ok")


async def handle_daily_trigger(request: web.Request) -> web.Response:
    """
    POST /trigger_daily_weather

    Called by the GitHub Actions scheduled workflow to fire the daily
    combined weather report. Authenticates via the X-Trigger-Token header.
    The ptb Application is stored in app["ptb_app"] by main().
    """
    if DAILY_TRIGGER_TOKEN:
        token = request.headers.get("X-Trigger-Token", "")
        if token != DAILY_TRIGGER_TOKEN:
            logger.warning("Daily trigger: unauthorized attempt")
            return web.Response(status=401, text="Unauthorized")

    ptb_app: Application = request.app["ptb_app"]

    try:
        combined_text, dashboard_keyboard = await build_combined_weather_all_message()
        await post_weather_log(
            bot=ptb_app.bot,
            text=combined_text,
            chat_id=WEATHER_LOG_CHAT_ID,
            thread_id=WEATHER_LOG_THREAD_ID,
            reply_markup=dashboard_keyboard,
        )
        logger.info("Daily weather trigger: report posted successfully")
        return web.Response(text="ok")
    except Exception as e:
        logger.exception("Daily weather trigger: failed to post report: %s", e)
        return web.Response(status=500, text="internal error")


# ============================================================
# MAIN
# ============================================================

async def main():
    # Build the ptb Application (no job_queue needed)
    ptb_app = (
        Application
        .builder()
        .token(TELEGRAM_BOT_TOKEN)
        .updater(None)          # disable the built-in polling updater
        .build()
    )

    # Register handlers
    ptb_app.add_handler(CommandHandler("start", start_command))
    ptb_app.add_handler(CommandHandler("codes", codes_command))
    ptb_app.add_handler(CommandHandler("help", help_command))
    ptb_app.add_handler(CommandHandler("map", map_command))
    ptb_app.add_handler(CommandHandler("weather_para", weather_para_command))
    ptb_app.add_handler(CommandHandler("weather_gri", weather_gri_command))
    ptb_app.add_handler(CommandHandler("weather_sev", weather_sev_command))
    ptb_app.add_handler(CommandHandler("weather_all", weather_all_command))
    ptb_app.add_handler(CallbackQueryHandler(button_handler))
    ptb_app.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS,
            new_member,
        )
    )
    # Live location: initial share (new message with location)
    ptb_app.add_handler(
        MessageHandler(
            filters.LOCATION & ~filters.UpdateType.EDITED_MESSAGE,
            handle_location,
        )
    )
    # Live location: periodic ticks and stop events (edited message)
    ptb_app.add_handler(
        MessageHandler(
            filters.LOCATION & filters.UpdateType.EDITED_MESSAGE,
            handle_live_update,
        )
    )
    ptb_app.add_error_handler(error_handler)

    # Build the aiohttp web application
    web_app = web.Application()
    web_app["ptb_app"] = ptb_app

    # Webhook path uses the bot token as a secret path segment so that
    # only Telegram (which knows the full URL) can POST updates to it.
    webhook_path = f"/webhook/{TELEGRAM_BOT_TOKEN}"
    webhook_full_url = WEBHOOK_URL.rstrip("/") + webhook_path

    web_app.router.add_get("/health", handle_health)
    web_app.router.add_post("/trigger_daily_weather", handle_daily_trigger)

    # Live map routes
    web_app.router.add_get("/", handle_map_index)
    web_app.router.add_get("/api/locations", handle_api_locations)
    web_app.router.add_post("/api/update_location", handle_api_update_location)
    web_app.router.add_get("/avatars/{filename}", handle_avatar)

    # Wire Telegram updates through ptb's webhook handler
    async def handle_telegram_update(request: web.Request) -> web.Response:
        # Verify the optional secret token header Telegram sends
        if WEBHOOK_SECRET:
            secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if secret != WEBHOOK_SECRET:
                logger.warning("Webhook: invalid secret token")
                return web.Response(status=403, text="Forbidden")

        data = await request.json()
        update = Update.de_json(data, ptb_app.bot)
        await ptb_app.process_update(update)
        return web.Response(text="ok")

    web_app.router.add_post(webhook_path, handle_telegram_update)

    # Initialize and start the ptb application (connects bot, sets up context)
    await ptb_app.initialize()
    await ptb_app.start()

    # Register the bot command menu visible in the Telegram UI
    await ptb_app.bot.set_my_commands([
        BotCommand("start",        "Open your private chat with the bot"),
        BotCommand("codes",        "Get your personal Check-In/Check-Out code"),
        BotCommand("map",          "Open the live group location map"),
        BotCommand("weather_para", "Current weather for Paramythia (OpenWeather)"),
        BotCommand("weather_gri",  "Current conditions – Grika WU station"),
        BotCommand("weather_sev",  "Current conditions – Sevasto WU station"),
        BotCommand("weather_all",  "All three weather reports combined"),
        BotCommand("help",         "Show all commands and instructions"),
    ])
    logger.info("Bot command menu registered.")

    # Register the webhook with Telegram (skipped if WEBHOOK_URL is not set,
    # which can happen on the very first Cloud Run deploy before the URL is known)
    if WEBHOOK_URL:
        await ptb_app.bot.set_webhook(
            url=webhook_full_url,
            secret_token=WEBHOOK_SECRET if WEBHOOK_SECRET else None,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        logger.info("Webhook registered at %s", webhook_full_url)
    else:
        logger.warning("WEBHOOK_URL is not set — skipping webhook registration. "
                       "The bot will not receive Telegram updates until WEBHOOK_URL is configured.")

    # Start the aiohttp server
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info("🤖 Parabot is running on port %d (webhook mode)", PORT)

    # Start the background task that evicts stale live-location entries
    asyncio.create_task(cleanup_stale_locations())

    # Keep running until interrupted
    try:
        await asyncio.Event().wait()
    finally:
        logger.info("Shutting down…")
        await ptb_app.bot.delete_webhook()
        await ptb_app.stop()
        await ptb_app.shutdown()
        await runner.cleanup()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    asyncio.run(main())
