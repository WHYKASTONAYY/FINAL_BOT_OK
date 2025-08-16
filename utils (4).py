import sqlite3
import time
import os
import logging
import json
import shutil
import tempfile
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import requests
from collections import Counter, defaultdict # Moved higher up

# --- Telegram Imports ---
from telegram import Update, Bot
from telegram.constants import ParseMode
import telegram.error as telegram_error
from telegram.ext import ContextTypes
from telegram import helpers
# -------------------------

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Render Disk Path Configuration ---
RENDER_DISK_MOUNT_PATH = '/mnt/data'
DATABASE_PATH = os.path.join(RENDER_DISK_MOUNT_PATH, 'shop.db')
MEDIA_DIR = os.path.join(RENDER_DISK_MOUNT_PATH, 'media')
BOT_MEDIA_JSON_PATH = os.path.join(RENDER_DISK_MOUNT_PATH, 'bot_media.json')

# Ensure the base media directory exists on the disk when the script starts
try:
    os.makedirs(MEDIA_DIR, exist_ok=True)
    logger.info(f"Ensured media directory exists: {MEDIA_DIR}")
except OSError as e:
    logger.error(f"Could not create media directory {MEDIA_DIR}: {e}")

logger.info(f"Using Database Path: {DATABASE_PATH}")
logger.info(f"Using Media Directory: {MEDIA_DIR}")
logger.info(f"Using Bot Media Config Path: {BOT_MEDIA_JSON_PATH}")


# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: if ADMIN_ID is set but PRIMARY_ADMIN_IDS is empty, use ADMIN_ID ---
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: 
        ADMIN_ID = int(ADMIN_ID_RAW)
        if not PRIMARY_ADMIN_IDS:  # Only use ADMIN_ID if PRIMARY_ADMIN_IDS is empty
            PRIMARY_ADMIN_IDS = [ADMIN_ID]
    except (ValueError, TypeError): 
        logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# If PRIMARY_ADMIN_IDS is set and ADMIN_ID is not set, use the first primary admin as ADMIN_ID for backward compatibility
if PRIMARY_ADMIN_IDS and ADMIN_ID is None:
    ADMIN_ID = PRIMARY_ADMIN_IDS[0]

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: 
    logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); 
    raise SystemExit("TOKEN not set.")

# Enhanced token validation
if ':' not in TOKEN:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid (missing colon). Token: {TOKEN[:10]}...")
    raise SystemExit("TOKEN format is invalid.")

token_parts = TOKEN.split(':')
if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
    logger.critical(f"CRITICAL ERROR: TOKEN format is invalid. Expected format: 'bot_id:secret_key'")
    raise SystemExit("TOKEN format is invalid.")

logger.info(f"TOKEN validation passed. Bot ID: {token_parts[0]}")

if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
if not NOWPAYMENTS_IPN_SECRET: logger.info("NOWPayments webhook signature verification is disabled by configuration.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs set. Admin features will be disabled.")
else: logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Some legacy features may not work.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")

# --- Helper Functions ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is any type of admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "").strip()  # Strip whitespace
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# --- Parse Primary Admin IDs ---
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: 
        PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: 
        logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# --- Backward compatibility: