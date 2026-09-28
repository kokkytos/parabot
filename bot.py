import os
import math
import asyncio
import logging
from datetime import datetime, time as dt_time
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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

# Daily automatic /weather_all posting time (local time, Greece).
# Always posts to WEATHER_LOG_CHAT_ID / WEATHER_LOG_THREAD_ID, since a
# scheduled job has no invoking chat to reply in.
WEATHER_ALL_SCHEDULE_TIMEZONE = "Europe/Athens"
WEATHER_ALL_SCHEDULE_HOUR = 12
WEATHER_ALL_SCHEDULE_MINUTE = 0


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
    silently vanishing. Used by both /weather_all and the daily scheduled
    job so they stay identical.

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
# DAILY SCHEDULED /WEATHER_ALL
# ============================================================

async def scheduled_weather_all_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Runs automatically once a day (see WEATHER_ALL_SCHEDULE_* config /
    main()'s job_queue.run_daily call) and posts the same combined report
    as /weather_all. There's no invoking chat for a scheduled job, so this
    always posts to the configured weather log topic
    (WEATHER_LOG_CHAT_ID / WEATHER_LOG_THREAD_ID).
    """
    try:
        combined_text, dashboard_keyboard = await build_combined_weather_all_message()
        await post_weather_log(
            bot=context.bot,
            text=combined_text,
            chat_id=WEATHER_LOG_CHAT_ID,
            thread_id=WEATHER_LOG_THREAD_ID,
            reply_markup=dashboard_keyboard,
        )
    except Exception as e:
        logger.exception("Scheduled /weather_all job failed: %s", e)


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
# ERROR HANDLER
# ============================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Telegram update error", exc_info=context.error)


# ============================================================
# MAIN
# ============================================================

def main():
    application = (
        Application
        .builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    # Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("codes", codes_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("weather_para", weather_para_command))
    application.add_handler(CommandHandler("weather_gri", weather_gri_command))
    application.add_handler(CommandHandler("weather_sev", weather_sev_command))
    application.add_handler(CommandHandler("weather_all", weather_all_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS,
            new_member,
        )
    )
    application.add_error_handler(error_handler)

    # Daily automatic /weather_all report (see WEATHER_ALL_SCHEDULE_*
    # config near the top of the file). Requires the bot to have been
    # installed with the job-queue extra:
    #     pip install "python-telegram-bot[job-queue]"
    if application.job_queue is not None:
        application.job_queue.run_daily(
            scheduled_weather_all_job,
            time=dt_time(
                hour=WEATHER_ALL_SCHEDULE_HOUR,
                minute=WEATHER_ALL_SCHEDULE_MINUTE,
                tzinfo=ZoneInfo(WEATHER_ALL_SCHEDULE_TIMEZONE),
            ),
            name="daily_weather_all",
        )
        logger.info(
            "Scheduled daily /weather_all at %02d:%02d %s",
            WEATHER_ALL_SCHEDULE_HOUR,
            WEATHER_ALL_SCHEDULE_MINUTE,
            WEATHER_ALL_SCHEDULE_TIMEZONE,
        )
    else:
        logger.warning(
            "JobQueue is not available — daily /weather_all was NOT scheduled. "
            "Install with: pip install \"python-telegram-bot[job-queue]\""
        )

    logger.info("🤖 Telegram Welcome + Airtable Codes Bot is running...")

    application.run_polling()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
