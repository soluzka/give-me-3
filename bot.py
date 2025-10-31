import nextcord
from nextcord.ext import commands, tasks
import logging
import os
import json
from utils import safe_json_dump, safe_json_dumps
from dotenv import load_dotenv  # Import dotenv to load environment variables
import re
import aiohttp
# import requests  # No longer needed
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, session, url_for, jsonify, flash, send_file
import threading  # Import threading to run Flask in a separate thread
import asyncio
from nextcord import Intents
import time
from nextcord import Interaction  # Import Interaction for slash commands
from nextcord.ext.commands import has_permissions  # Import has_permissions for permission checks

# Load environment variables from .env file
load_dotenv()


# Set up logging
logging.basicConfig(level=logging.DEBUG)

# Ensure server_settings.json exists with a default hide_owner_id flag
SERVER_SETTINGS_FILE = os.path.join(os.path.dirname(__file__), 'server_settings.json')
if not os.path.exists(SERVER_SETTINGS_FILE):
    with open(SERVER_SETTINGS_FILE, "w", encoding="utf-8") as f:
        safe_json_dump({"hide_owner_id": False}, f, indent=2)

# Load the Discord token from the environment variable
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')

# Check if the token is loaded correctly
if (DISCORD_TOKEN is None):
    logging.error('DISCORD_TOKEN is not set. Please check your .env file.')
    exit(1)

# Initialize intents
intents = Intents.default()
intents.messages = True  # Enable message intents
# Only enable message_content if explicitly set in env or config
import os
MESSAGE_CONTENT_INTENT = os.getenv('MESSAGE_CONTENT_INTENT', 'true').lower() == 'true'
intents.message_content = MESSAGE_CONTENT_INTENT  # Enable message content intent for guilds if set
intents.members = True  # Enable member intent so the bot can see member and owner info

# Info for user: DM automod/logging always works, guild automod requires message_content intent.
if not MESSAGE_CONTENT_INTENT:
    print("[INFO] MESSAGE CONTENT INTENT is disabled. Guild/server automod and logging will NOT work, but DM automod and logging will still function.")
else:
    print("[INFO] MESSAGE CONTENT INTENT is enabled. Automod and logging will work for both DMs and servers.")

# At the top of your file

def user_has_owner_role(guild_id, user_id, access_token):
    """
    Returns True if the user is the guild owner or has the owner role.
    Logs debug info for troubleshooting.
    """
    import requests
    owner_roles = load_owner_roles().get(guild_id, {})
    owner_role_id = owner_roles.get("role_id")
    owner_id = server_settings.get(guild_id, {}).get("owner_id")
    # Debug logging
    logging.debug(f"[user_has_owner_role] guild_id={guild_id} user_id={user_id} owner_id={owner_id} owner_role_id={owner_role_id}")
    if str(user_id) == str(owner_id):
        logging.debug("[user_has_owner_role] User is guild owner. Access granted.")
        return True
    if owner_role_id:
        url = f'https://discord.com/api/users/@me/guilds/{guild_id}/member'
        headers = {'Authorization': f'Bearer {access_token}'}
        resp = requests.get(url, headers=headers)
        if resp.status_code == 200:
            roles = resp.json().get('roles', [])
            logging.debug(f"[user_has_owner_role] Discord roles for user: {roles}")
            if str(owner_role_id) in [str(r) for r in roles]:
                logging.debug("[user_has_owner_role] User has owner role. Access granted.")
                return True
            logging.debug("[user_has_owner_role] User does not have owner role. Access denied.")
            return False
        else:
            logging.warning(f"[user_has_owner_role] Failed to fetch roles from Discord API: {resp.status_code}")
            return False
    else:
        # No owner_role_id set, fallback to owner_id check (already done above)
        # No owner_role_id set, fallback to owner_id check
        if str(user_id) == str(owner_id):
            logging.debug("[user_has_owner_role] No owner role set, but user is guild owner. Access granted.")
            return True
        logging.debug("[user_has_owner_role] No owner role set, user is not guild owner. Access denied.")
        return False

# Global bot_loop for cross-thread async execution
bot_loop = None

def set_bot_loop(loop):
    global bot_loop
    bot_loop = loop

# ... (logging, env, intents)

BACKUP_DIR = "discord_guild_backups"
os.makedirs(BACKUP_DIR, exist_ok=True)

# Ensure the backup directory exists for compatibility with backup file logic
if not os.path.exists(BACKUP_DIR):
    os.makedirs(BACKUP_DIR)

from flask import Flask
app = Flask(__name__)

# --- Utility: Clean circular references in server_settings.json ---
def clean_server_settings_file():
    filename = SERVER_SETTINGS_FILE
    try:
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
        fields_to_check = ["timeout_enabled", "automod_threshold", "automod_time_window"]
        changed = False
        # Clean global hide_owner_id
        if isinstance(data.get("hide_owner_id"), str) and data["hide_owner_id"].startswith("<circular"):
            data["hide_owner_id"] = True  # Default to True if corrupted
            changed = True
        for gid, settings in data.items():
            if not isinstance(settings, dict):
                continue
            for field in fields_to_check:
                val = settings.get(field)
                # Clean circular references and non-numeric values
                if (
                    (isinstance(val, str) and val.startswith("<circular")) or
                    (field in ["automod_threshold", "automod_time_window"] and not isinstance(val, (int, float)))
                ):
                    if field == "timeout_enabled":
                        # Only reset if not a boolean or is a circular reference
                        if not isinstance(val, bool) or (isinstance(val, str) and val.startswith("<circular")):
                            settings[field] = True
                        changed = True
                    elif field == "automod_threshold":
                        settings[field] = 5
                    elif field == "automod_time_window":
                        settings[field] = 10
                    changed = True
        if changed:
            with open(filename, "w", encoding="utf-8") as f:
                safe_json_dump(data, f, indent=2)
        return changed
    except Exception as e:
        logging.error(f"Error cleaning server_settings: {e}")
        return False

@app.route('/clean_server_settings', methods=['POST'])
def clean_server_settings_route():
    """Route to clean circular references in server_settings.json. Owner-only."""
    discord_user_id = session.get('discord_user_id')
    # Restrict to OWNER_ID from environment
    if not discord_user_id or str(discord_user_id) != str(os.getenv('OWNER_ID')):
        return jsonify({'success': False, 'error': 'Not authorized'}), 403
    changed = clean_server_settings_file()
    return jsonify({'success': True, 'changed': changed})

@app.route('/set_hide_owner_id', methods=['POST'])
def set_hide_owner_id():
    """Set the global hide_owner_id setting to True or False. Owner-only."""
    discord_user_id = session.get('discord_user_id')
    # Restrict to OWNER_ID from environment
    if not discord_user_id or str(discord_user_id) != str(os.getenv('OWNER_ID')):
        return jsonify({'success': False, 'error': 'Not authorized'}), 403

    # Expect JSON: { "hide_owner_id": true } or { "hide_owner_id": false }
    value = request.json.get('hide_owner_id')
    if not isinstance(value, bool):
        return jsonify({'success': False, 'error': 'Value must be boolean'}), 400

    global server_settings
    server_settings = load_server_settings()
    server_settings['hide_owner_id'] = value
    save_server_settings(server_settings)
    return jsonify({'success': True, 'hide_owner_id': server_settings['hide_owner_id']})

@app.route('/update_owner_roles', methods=['POST'])
def update_owner_roles():
    """Update the owner roles dynamically, restricted to the guild owner."""
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))

    try:
        data = request.json
        if not data or 'guild_id' not in data or 'roles' not in data:
            return "Invalid or missing JSON payload. Expected {'guild_id': <id>, 'roles': {...}}.", 400

        guild_id = str(data['guild_id'])
        new_roles = data['roles']

        # Check if the current user is the owner of the guild
        access_token = session.get('access_token')
        headers = {'Authorization': f'Bearer {access_token}'}
        import requests
        user_resp = requests.get('https://discord.com/api/users/@me', headers=headers)
        if user_resp.status_code != 200:
            return "Unable to fetch user info from Discord.", 403
        user_id = user_resp.json().get('id')

        # Load server settings and check owner
        settings = load_server_settings().get(guild_id, {})
        owner_id = str(settings.get('owner_id'))
        if not owner_id or user_id != owner_id:
            return "Only the guild owner can update owner roles.", 403

        # Update owner roles in the JSON file
        owner_roles = load_owner_roles()
        owner_roles[guild_id] = new_roles
        save_owner_roles(owner_roles)
        return "Owner roles updated successfully.", 200
    except Exception as e:
        logging.error(f"Error updating owner roles: {e}")
        return f"An error occurred: {e}", 500


# Initialize bot before any decorators
bot = commands.Bot(command_prefix='/', intents=intents)

templates_creation_result = None

# Define OWNER_ROLES_FILE
OWNER_ROLES_FILE = "owner_roles.json"

@app.route('/get_owner_roles/<guild_id>', methods=['GET'])
def get_owner_roles(guild_id):
    """Retrieve the owner roles for a specific server."""
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))
    try:
        roles = load_owner_roles().get(guild_id, {})
        return {"guild_id": guild_id, "roles": roles}, 200
    except Exception as e:
        logging.error(f"Error retrieving owner roles for guild {guild_id}: {e}")
        return {"error": f"An error occurred: {e}"}, 500

# --- Automatically generate templates for all guilds when the bot is ready ---
# (Moved below bot = ...)

def load_owner_roles():
    """Load owner roles from the JSON file and ensure guild IDs are included."""
    try:
        with open(OWNER_ROLES_FILE, "r") as f:
            owner_roles = json.load(f)
            # Ensure guild IDs are included in the structure
            for guild_id in owner_roles:
                if "guild_id" not in owner_roles[guild_id]:
                    owner_roles[guild_id]["guild_id"] = guild_id
            return owner_roles
    except FileNotFoundError:
        logging.warning(f"{OWNER_ROLES_FILE} not found. Creating a new one.")
        with open(OWNER_ROLES_FILE, "w") as f:
            json.dump({}, f)
        return {}

def save_owner_roles(owner_roles):
    """Save owner roles to the JSON file."""
    try:
        with open(OWNER_ROLES_FILE, "w") as f:
            safe_json_dump(owner_roles, f, indent=2)
        logging.info("Owner roles saved successfully.")
    except Exception as e:
        logging.error(f"Error saving owner roles: {e}")

def clean_circular_references(obj, seen=None, path=None):
    """
    Recursively remove circular references from a dict/list.
    If a circular reference is found, replace it with '<circular_ref>' and log a warning.
    """
    if seen is None:
        seen = set()
    if path is None:
        path = []
    obj_id = id(obj)
    if isinstance(obj, dict):
        if obj_id in seen:
            logging.warning(f"Circular reference detected at {'.'.join(map(str, path))}. Replacing with '<circular_ref>'")
            return '<circular_ref>'
        seen.add(obj_id)
        cleaned = {}
        for k, v in obj.items():
            cleaned[k] = clean_circular_references(v, seen, path + [k])
        seen.remove(obj_id)
        return cleaned
    elif isinstance(obj, list):
        if obj_id in seen:
            logging.warning(f"Circular reference detected at {'.'.join(map(str, path))}. Replacing with '<circular_ref>'")
            return ['<circular_ref>']
        seen.add(obj_id)
        cleaned = [clean_circular_references(i, seen, path + [str(idx)]) for idx, i in enumerate(obj)]
        seen.remove(obj_id)
        return cleaned
    else:
        return obj

def save_server_settings(settings):
    """Save server-specific settings to the JSON file, ensuring valid int or float values. If settings is a flat automod dict, write it as-is (not nested)."""
    try:
        # If settings is a flat automod config (not per-guild dict), write as-is
        automod_keys = {"automod_enabled", "timeout_enabled", "blocked_keywords", "regex_patterns"}
        if set(settings.keys()) <= automod_keys:
            # Clean automod_enabled to always be a bool if present
            if "automod_enabled" in settings and not isinstance(settings["automod_enabled"], bool):
                settings["automod_enabled"] = bool(settings["automod_enabled"])
            # Validate timeout_enabled
            if "timeout_enabled" not in settings or not isinstance(settings["timeout_enabled"], bool):
                settings["timeout_enabled"] = True
            # Validate timeout_duration
            if "timeout_duration" not in settings or not isinstance(settings["timeout_duration"], int):
                settings["timeout_duration"] = 60
            with open(SERVER_SETTINGS_FILE, "w", encoding="utf-8") as f:
                safe_json_dump(settings, f, indent=2)
            logging.info("Flat automod config saved.")
            return
        # Otherwise, clean per-guild settings as before
        changed = False
        # Validate and clean each guild's settings
        for gid, s in settings.items():
            if not isinstance(s, dict):
                continue
            # Validate timeout_enabled
            if "timeout_enabled" not in s or not isinstance(s["timeout_enabled"], bool):
                s["timeout_enabled"] = True
                changed = True
            # Validate timeout_duration
            if "timeout_duration" not in s or not isinstance(s["timeout_duration"], int):
                s["timeout_duration"] = 60
                changed = True
            fields_to_check = ["automod_enabled", "automod_threshold", "automod_time_window", "timeout"]
            # Helper for circular and invalid references
            def sanitize_int_field(val, default):
                if isinstance(val, (int, float)):
                    return int(val)
                if isinstance(val, str) and val.startswith('<circular'):
                    return default
                try:
                    return int(val)
                except Exception:
                    return default

            for field in fields_to_check:
                val = s.get(field)
                if field == "automod_enabled":
                    if not isinstance(val, bool):
                        s[field] = bool(val)
                        changed = True
                elif field == "automod_threshold":
                    sanitized = sanitize_int_field(val, 5)
                    if sanitized != val:
                        s[field] = sanitized
                        changed = True
                elif field == "automod_time_window":
                    sanitized = sanitize_int_field(val, 10)
                    if sanitized != val:
                        s[field] = sanitized
                        changed = True
                elif field == "timeout":
                    sanitized = sanitize_int_field(val, 60)
                    if sanitized != val:
                        s[field] = sanitized
                        changed = True
        with open(SERVER_SETTINGS_FILE, "w", encoding="utf-8") as f:
            safe_json_dump(settings, f, indent=2)
        if changed:
            logging.info("Server settings cleaned before save.")
        clean_server_settings_file()
        logging.info("Server settings saved and cleaned successfully.")
    except Exception as e:
        logging.error(f"Error saving server settings: {e}")


# Ensure bot is initialized with application commands enabled
bot = commands.Bot(command_prefix='/', intents=intents)

# --- Automatically generate templates for all guilds when the bot is ready ---
import threading

# Flask: Run template creation before the first request (compatibility version)
template_init_done = False
@app.before_request
def auto_create_templates_on_flask_start():
    global template_init_done
    if not template_init_done:
        try:
            print("[AUTO-TEMPLATE] Flask startup: Creating templates for all guilds...")
            with app.app_context():
                result = create_templates_for_all_logs(auto_trigger=True)
                print(f"[AUTO-TEMPLATE] {result}")
        except Exception as e:
            print(f"[AUTO-TEMPLATE] Error during Flask startup template creation: {e}")
        template_init_done = True

# Discord bot: Run template creation after bot is ready
# (This function should already exist, but ensure it's present and called)
def generate_templates_on_ready():
    import time
    time.sleep(5)  # Wait a few seconds to ensure bot.guilds is populated
    try:
        print("[AUTO-TEMPLATE] Bot ready: Creating templates for all guilds...")
        with app.app_context():
            result = create_templates_for_all_logs(auto_trigger=True)
            print(f"[AUTO-TEMPLATE] {result}")
    except Exception as e:
        print(f"[AUTO-TEMPLATE] Error during bot ready template creation: {e}")

@bot.event
async def on_ready():
    # ... (existing on_ready logic)
    threading.Thread(target=generate_templates_on_ready, daemon=True).start()

def generate_templates_on_ready():
    import time
    time.sleep(5)  # Wait a few seconds to ensure bot.guilds is populated
    try:
        print("[AUTO-TEMPLATE] Attempting to create templates for all guilds...")
        with app.app_context():
            result = create_templates_for_all_logs(auto_trigger=True)
            print(f"[AUTO-TEMPLATE] {result}")
    except Exception as e:
        print(f"[AUTO-TEMPLATE] Error during automatic template creation: {e}")

def initialize_timeout_settings():
    changed = False
    for guild in bot.guilds:
        gid = str(guild.id)
        if gid not in server_settings:
            server_settings[gid] = {}
        # Timeout settings
        if 'timeout' not in server_settings[gid]:
            server_settings[gid]['timeout'] = DEFAULT_TIMEOUT_DURATION
            changed = True
        if 'timeout_enabled' not in server_settings[gid] or not isinstance(server_settings[gid]['timeout_enabled'], bool):
            server_settings[gid]['timeout_enabled'] = True
            changed = True
        # Automod settings
        if 'automod_threshold' not in server_settings[gid]:
            server_settings[gid]['automod_threshold'] = 5  # Default threshold
            changed = True
        if 'automod_time_window' not in server_settings[gid]:
            server_settings[gid]['automod_time_window'] = 10  # Default time window in seconds
            changed = True
    if changed:
        save_server_settings(server_settings)

@bot.event
async def on_ready():
    global bot_loop
    bot_loop = asyncio.get_running_loop()
    print(f'Bot is ready. Logged in as {bot.user}')
    # Always hide owner ID for all guilds at startup
    for gid in server_settings:
        pass
    server_settings = {str(k): v for k, v in server_settings.items()}
    logging.info(f'Logged in as {bot.user}')
    try:
        # Log all guilds the bot is in
        print("Bot is in the following guilds:")
        for guild in bot.guilds:
            print(f"Guild Name: {guild.name}, Guild ID: {guild.id}")
        # --- Verification Step: Ensure all guilds are in server_settings ---
        missing_guilds = []
        for guild in bot.guilds:
            gid = str(guild.id)
            if gid not in server_settings:
                server_settings[gid] = {
                    "automod_enabled": False,
                    "blocked_keywords": [],
                    "regex_patterns": [],
                }
                missing_guilds.append(f"{guild.name} (ID: {guild.id})")
        if missing_guilds:
            save_server_settings(server_settings)
            print(f"[VERIFY] Added missing guilds to server_settings: {', '.join(missing_guilds)}")
        else:
            print("[VERIFY] All current guilds are present in server_settings.")
        # Synchronize slash commands globally (if needed)
        if hasattr(bot, 'tree'):
            await bot.tree.sync()
            print("Slash commands synchronized globally.")
        # Initialize timeout settings for all guilds
        initialize_timeout_settings()
        # Assign Owner role to all members in all guilds at startup
        for guild in bot.guilds:
            await scan_and_create_owner_role(guild)
        print("Owner roles assigned to all members in all guilds.")
    except Exception as e:
        print(f"Error during on_ready: {e}")
    # Start template generation in a background thread
    threading.Thread(target=generate_templates_on_ready, daemon=True).start()

# Initialize Flask app
app = Flask(__name__)

@app.route('/restore_guild_from_backup', methods=['POST'])
def restore_guild_from_backup():
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))
    guild_id = request.form.get('guild_id')
    if not guild_id:
        flash('Guild ID is required for restore.', 'danger')
        return redirect(url_for('index'))
    backup_path = os.path.join(BACKUP_DIR, f'{guild_id}.json')
    if not os.path.exists(backup_path):
        flash(f'Backup file for guild {guild_id} not found.', 'danger')
        return redirect(url_for('index'))
    try:
        with open(backup_path, 'r', encoding='utf-8') as f:
            settings = json.load(f)
        # Save to server_settings and persist
        server_settings[str(guild_id)] = settings
        save_server_settings(server_settings)
        flash(f'Successfully restored guild {guild_id} from backup.', 'success')
    except Exception as e:
        flash(f'Failed to restore: {e}', 'danger')
    return redirect(url_for('index'))

@app.route('/backup_guild', methods=['POST'])
def backup_guild():
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))
    guild_id = request.form.get('guild_id')
    if not guild_id:
        flash('Guild ID is required for backup.', 'danger')
        return redirect(url_for('index'))
    try:
        settings = server_settings.get(str(guild_id))
        if not settings:
            flash(f'No settings found for guild {guild_id}.', 'danger')
            return redirect(url_for('index'))
        backup_path = os.path.join(BACKUP_DIR, f'{guild_id}.json')
        with open(backup_path, 'w', encoding='utf-8') as f:
            safe_json_dump(settings, f, indent=2, ensure_ascii=False)
        flash(f'Successfully backed up guild {guild_id}.', 'success')
    except Exception as e:
        flash(f'Failed to back up: {e}', 'danger')
    return redirect(url_for('index'))

@app.route('/restore_template', methods=['POST'])
def restore_template():
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))
    guild_id = request.form.get('guild_id')
    template_name = request.form.get('template_name')
    if not guild_id or not template_name:
        flash('Guild ID and Template Name are required.', 'danger')
        return redirect(url_for('index'))
    owner_id = server_settings.get(str(guild_id), {}).get('owner_id')
    if str(discord_user['id']) != str(owner_id):
        flash('Only the guild owner can restore a template.', 'danger')
        return redirect(url_for('index'))
    template_path = os.path.join(TEMPLATES_DIR, f'{template_name}.json')
    if not os.path.exists(template_path):
        flash(f'Template {template_name} not found.', 'danger')
        return redirect(url_for('index'))
    try:
        with open(template_path, 'r', encoding='utf-8') as f:
            settings = json.load(f)
        server_settings[str(guild_id)] = settings
        save_server_settings(server_settings)
        flash(f'Successfully restored guild {guild_id} from template {template_name}.', 'success')
    except Exception as e:
        flash(f'Failed to restore from template: {e}', 'danger')
    return redirect(url_for('index'))

@app.route('/reset_guild_template', methods=['POST'])
def reset_guild_template():
    if 'access_token' not in session:
        return redirect(url_for('login'))
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))
    guild_id = request.form.get('guild_id')
    if not guild_id:
        flash('Guild ID is required.', 'danger')
        return redirect(url_for('portal'))
    owner_id = server_settings.get(str(guild_id), {}).get('owner_id')
    if str(discord_user['id']) != str(owner_id):
        flash('Only the guild owner can reset the template.', 'danger')
        return redirect(url_for('portal'))
    # Reset to default settings
    default_settings = {
        "automod_enabled": True,
        "blocked_keywords": [
            "spam", "scam", "phishing", "free nitro", "giveaway", "discord.gg", "invite", "buy now", "click here", "subscribe", "adult", "nsfw", "crypto", "bitcoin", "porn", "sex", "nude", "robux", "nitro", "airdrop", "token", "password", "login", "credit card", "paypal", "venmo", "cashapp", "gift", "prize", "winner", "claim", "investment", "pump", "dump"
        ],
        "regex_patterns": [
            r'https?://\\S+',
            r'\\b(spam|advertisement|link|buy|free|click here|subscribe)\\b',
            r'discord\\.gg/\\S+',
            r'<@!?\\d{17,20}>',
            r'(.)\\1{3,}',
            r'[^\\f\\n\\r\\t\\v\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]',
            r'^.*([A-Za-z0-9]+( [A-Za-z0-9]+)+).*[A-Za-z]+.*$',
        ],
        "timeout_enabled": True,
        "timeout_duration": 60,
        "spam_threshold": 5,
        "spam_time_window": 10,
    }
    server_settings[str(guild_id)] = default_settings.copy()
    save_server_settings(server_settings)
    flash(f'Reset settings for guild {guild_id} to default.', 'success')
    return redirect(url_for('portal'))

@app.route('/api/portal_guilds')
def api_portal_guilds():
    """
    Returns detailed info for all guilds the bot is in, for dynamic portal updates.
    Includes: id, name, automod status, latest message, owner roles, etc.
    """
    if 'access_token' not in session:
        return jsonify({'error': 'Not authenticated'}), 401

    # Load settings and owner roles
    settings = load_server_settings()
    owner_roles = load_owner_roles()

    # Gather guild info
    guilds_data = []
    for guild in bot.guilds:
        gid = str(guild.id)
        # Hide owner_id in API response (always hidden)
        owner_id = "Hidden"
        guild_info = {
            'id': gid,
            'name': guild.name,
            'owner_id': 'Hidden',
            'automod_enabled': settings.get(gid, {}).get('automod_enabled', False) if isinstance(settings.get(gid, {}), dict) else False,
            'owner_roles': owner_roles.get(gid, {}),
            'owner_name': str(guild.owner) if hasattr(guild, 'owner') else None,
        }
        # Latest message (if available in logs)
        latest_message = None
        log_path = os.path.join('discord_guild_backups', f'{gid}.json')
        if os.path.exists(log_path):
            try:
                with open(log_path, 'r', encoding='utf-8') as f:
                    log_data = json.load(f)
                    messages = log_data.get('messages', [])
                    if messages:
                        last = messages[-1]
                        latest_message = {
                            'author': last.get('author', 'Unknown'),
                            'content': last.get('content', ''),
                            'timestamp': last.get('timestamp', '')
                        }
            except Exception as e:
                latest_message = None
        guild_info['latest_message'] = latest_message
        guilds_data.append(guild_info)
    from utils import sanitize_for_json
    return jsonify({'guilds': sanitize_for_json(guilds_data)})

from flask import send_from_directory

@app.route('/export_messages', methods=['GET'])
def export_messages_route():
    """Export all guilds' messages as a JSON file for download."""
    import json
    SERVER_SETTINGS_FILE = "server_settings.json"
    with open(SERVER_SETTINGS_FILE, "r", encoding="utf-8") as f:
        server_settings = json.load(f)
    just_messages = {
        gid: settings.get("messages", [])
        for gid, settings in server_settings.items()
        if isinstance(settings, dict)
    }
    export_path = "all_guilds_messages.json"
    with open(export_path, "w", encoding="utf-8") as fp:
        safe_json_dump(just_messages, fp, indent=2)
    return send_file(export_path, as_attachment=True)


@app.route('/discord_guild_backups/<path:filename>')
def download_guild_backup(filename):
    """Serve a backup JSON file from the discord_guild_backups directory."""
    return send_from_directory(BACKUP_DIR, filename, as_attachment=True)

@app.route('/backup_all_guilds', methods=['POST'])
def backup_all_guilds():
    """
    Back up all Discord guild settings to JSON files.
    Only includes primitive fields (int, str, float, bool, None) or lists/dicts of primitives.
    This prevents circular references and ensures clean JSON output for every guild.
    """
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))
    try:
        def run_backup():
            import asyncio
            async def backup():
                logging.info("[BackupAllGuilds] Starting backup of all guilds. Total guilds: %d", len(bot.guilds))
                for guild in bot.guilds:
                    try:
                        logging.info(f"[BackupAllGuilds] Starting backup for guild: {guild.name} (ID: {guild.id})")
                        # Build a fully serializable dict for backup, with only primitive fields and no references
                        def safe_primitive(val, default):
                            # Returns val if it's a primitive, else returns default
                            if isinstance(val, (int, float, str, bool)) or val is None:
                                return val
                            return default

                        guild_data = {
                            "id": safe_primitive(getattr(guild, 'id', 0), 0),
                            "name": safe_primitive(getattr(guild, 'name', ''), ''),
                            "owner_id": safe_primitive(getattr(guild, 'owner_id', 0), 0),
                            "owner": safe_primitive(str(getattr(guild, 'owner', None)), None) if getattr(guild, 'owner', None) else None,
                            "icon_url": safe_primitive(str(getattr(getattr(guild, 'icon', None), 'url', None)), None) if getattr(guild, 'icon', None) else None,
                            "features": [safe_primitive(str(f), '') for f in getattr(guild, 'features', [])],
                            "member_count": safe_primitive(getattr(guild, 'member_count', 0), 0),
                            "created_at": safe_primitive(str(getattr(guild, 'created_at', '')), ''),
                            "roles": [
                                {
                                    "id": safe_primitive(getattr(role, 'id', 0), 0),
                                    "name": safe_primitive(getattr(role, 'name', ''), ''),
                                    "permissions": safe_primitive(getattr(getattr(role, 'permissions', None), 'value', 0), 0),
                                    "color": safe_primitive(getattr(getattr(role, 'color', None), 'value', 0), 0),
                                    "position": safe_primitive(getattr(role, 'position', 0), 0),
                                    "mentionable": safe_primitive(getattr(role, 'mentionable', False), False),
                                    "hoist": safe_primitive(getattr(role, 'hoist', False), False),
                                    "managed": safe_primitive(getattr(role, 'managed', False), False)
                                }
                                for role in getattr(guild, 'roles', [])
                            ],
                            "channels": [
                                {
                                    "id": safe_primitive(getattr(channel, 'id', 0), 0),
                                    "name": safe_primitive(getattr(channel, 'name', ''), ''),
                                    "type": safe_primitive(str(getattr(channel, 'type', '')), ''),
                                    "category": safe_primitive(str(getattr(getattr(channel, 'category', None), 'name', '')), None) if getattr(channel, 'category', None) else None,
                                    "position": safe_primitive(getattr(channel, 'position', 0), 0)
                                }
                                for channel in getattr(guild, 'channels', [])
                            ],
                            "emojis": [
                                {
                                    "id": safe_primitive(getattr(emoji, 'id', 0), 0),
                                    "name": safe_primitive(getattr(emoji, 'name', ''), ''),
                                    "animated": safe_primitive(getattr(emoji, 'animated', False), False)
                                }
                                for emoji in getattr(guild, 'emojis', [])
                            ]
                        }  # No references, only primitives, always serializable.

                        # --- Prune old backups for this guild (keep max 10) ---
                        import glob
                        import re
                        guild_backup_pattern = os.path.join(BACKUP_DIR, f"{guild.id}*.json")
                        backup_files = sorted(glob.glob(guild_backup_pattern), key=os.path.getmtime)
                        max_backups = 10
                        if len(backup_files) >= max_backups:
                            for old_file in backup_files[:-max_backups+1]:
                                try:
                                    os.remove(old_file)
                                    logging.info(f"[BackupAllGuilds] Deleted old backup: {old_file}")
                                except Exception as del_exc:
                                    logging.error(f"[BackupAllGuilds] Failed to delete old backup {old_file}: {del_exc}")
                        backup_path = os.path.join(BACKUP_DIR, f"{guild.id}.json")
                        try:
                            with open(backup_path, "w", encoding="utf-8") as f:
                                safe_json_dump(guild_data, f, indent=2, ensure_ascii=False)
                            logging.info(f"[BackupAllGuilds] Successfully backed up guild: {guild.name} (ID: {guild.id}) to {backup_path}")
                        except Exception as guild_exc:
                            logging.error(f"[BackupAllGuilds] Failed to back up guild: {guild.name} (ID: {guild.id}): {guild_exc}")
                    except Exception as outer_exc:
                        logging.error(f"[BackupAllGuilds] Unexpected error for guild: {guild.name} (ID: {guild.id}): {outer_exc}")
                    # End of per-guild backup
                logging.info("[BackupAllGuilds] Finished backup of all guilds.")
            asyncio.run_coroutine_threadsafe(backup(), bot_loop)
        run_backup()
        # After backup, also create templates for all guilds
        try:
            result = create_templates_for_all_logs(auto_trigger=True)
            flash("Backup and template creation started for all Discord guilds. Files will appear in the 'discord_guild_backups' and 'templates' folders.", "success")
        except Exception as template_exc:
            logging.error(f"Error during template creation after backup: {template_exc}")
            flash(f"Backup finished, but template creation failed: {template_exc}", "danger")
    except Exception as e:
        logging.error(f"Error during backup: {e}")
        flash(f"Error during backup: {e}", "danger")
    # Always return a valid Flask response
    return redirect(url_for('portal'))

def list_guild_templates(guild_id):
    """List all templates for a specific guild."""
    import os
    # Fetch owner info
    guild = next((g for g in bot.guilds if str(g.id) == str(guild_id)), None)
    if not guild:
        return render_template('guild_templates.html', guild_id=guild_id, templates=[], guild_owner=None, guild_owner_id=None, is_owner=False, no_templates_message="Guild not found or bot not in guild.")
    owner_id = str(guild.owner_id)
    owner_username = str(guild.owner)
    # Hide owner ID if privacy setting is enabled
    global server_settings
    server_settings = load_server_settings() if 'server_settings' not in globals() else server_settings
    if server_settings.get('hide_owner_id', False):
        owner_id = "Hidden"
    # Get current user ID from session
    user_id = str(session.get('discord_user_id'))
    is_owner = user_id == str(guild.owner_id)  # Compare to real owner_id for permission checks

    # Fetch settings for this guild
    guild_settings = server_settings.get(str(guild_id), {})

    # Load templates for guild_id (existing logic)
    templates = []
    # Check for legacy templates
    try:
        for fname in os.listdir(TEMPLATES_DIR):
            if fname.endswith('.json') and (fname.startswith(f"{guild_id}_") or fname == f"{guild_id}_auto.json"):
                templates.append({'filename': fname})
    except Exception:
        pass
    # Check for new backup file format in discord_guild_backups
    backup_dir = BACKUP_DIR
    backup_filename = f"{guild_id}.json"
    backup_path = os.path.join(backup_dir, backup_filename)
    if os.path.exists(backup_path):
        templates.append({'filename': backup_filename, 'is_backup': True})
    no_templates_message = None
    if not templates:
        no_templates_message = "No templates found for this guild yet."
    return render_template(
        'guild_templates.html',
        guild_id=guild_id,
        templates=templates,
        guild_owner=owner_username,
        guild_owner_id=owner_id,
        is_owner=is_owner,
        no_templates_message=no_templates_message,
        guild_settings=guild_settings
    )
@app.route('/save_template/<guild_id>', methods=['POST'])
def save_template(guild_id):
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))
    try:
        template_name = request.form.get('template_name', 'default').strip()
        import re
        template_name = re.sub(r'[^a-zA-Z0-9_-]', '_', template_name)
        import os, json
        template_dir = os.path.join(os.path.dirname(__file__), 'templates')
        os.makedirs(template_dir, exist_ok=True)
        if template_name == 'default':
            template_path = os.path.join(template_dir, f'template_{guild_id}.json')
        else:
            template_path = os.path.join(template_dir, f'template_{guild_id}_{template_name}.json')
        # Load the latest server settings
        server_settings = load_server_settings()
        settings = server_settings.get(str(guild_id), {})
        # Ensure automod_enabled and timeout_enabled are always True in the template
        settings['automod_enabled'] = True
        settings['timeout_enabled'] = True
        # Sanitize 'timeout' before saving template
        if 'timeout' not in settings or not isinstance(settings['timeout'], int) or (isinstance(settings['timeout'], str) and settings['timeout'].startswith('<circular')):
            try:
                settings['timeout'] = int(settings.get('timeout', 60))
            except Exception:
                settings['timeout'] = 60
        # Ensure spam_threshold and spam_time_window are valid integers before saving template
        if 'spam_threshold' in settings and not isinstance(settings['spam_threshold'], int):
            try:
                settings['spam_threshold'] = int(settings['spam_threshold'])
            except Exception:
                settings['spam_threshold'] = 5
        if 'spam_time_window' in settings and not isinstance(settings['spam_time_window'], int):
            try:
                settings['spam_time_window'] = int(settings['spam_time_window'])
            except Exception:
                settings['spam_time_window'] = 10
        with open(template_path, 'w') as f:
            safe_json_dump(settings, f, indent=2)
        return render_template('back_to_portal.html', message=f'Template "{template_name}" for guild {guild_id} saved successfully!')
    except Exception as e:
        import logging
        logging.error(f"Error saving template for guild {guild_id}: {e}")
        return render_template('back_to_portal.html', error=f'Error saving template: {e}')


@app.route('/templates/<path:filename>')
def serve_template_file(filename):
    return send_from_directory(TEMPLATES_DIR, filename)

# --- Watchdog background task to detect event loop blocking ---
import asyncio
import threading
import logging

@app.route('/download_guild_settings/<guild_id>', methods=['GET'])
def download_guild_settings(guild_id):
    """Download the current server settings for a guild as a JSON file."""
    try:
        server_settings_dict = load_server_settings()
        settings = server_settings_dict.get(str(guild_id))
        if not settings:
            flash(f"No settings found for guild {guild_id}.", "error")
            return redirect(url_for('list_guild_templates', guild_id=guild_id))
        # Clean settings before download
        cleaned_settings = settings.copy()
        # Ensure automod_enabled is always True in the downloaded settings
        cleaned_settings['automod_enabled'] = True
        # Hide owner_id for privacy
        if 'owner_id' in cleaned_settings:
            cleaned_settings['owner_id'] = 'Hidden'
        # Ensure spam_threshold and spam_time_window are valid integers
        if 'spam_threshold' in cleaned_settings and not isinstance(cleaned_settings['spam_threshold'], int):
            try:
                cleaned_settings['spam_threshold'] = int(cleaned_settings['spam_threshold'])
            except Exception:
                cleaned_settings['spam_threshold'] = 5
        if 'spam_time_window' in cleaned_settings and not isinstance(cleaned_settings['spam_time_window'], int):
            try:
                cleaned_settings['spam_time_window'] = int(cleaned_settings['spam_time_window'])
            except Exception:
                cleaned_settings['spam_time_window'] = 10
        # Clean automod_enabled if present
        if "automod_enabled" in cleaned_settings and not isinstance(cleaned_settings["automod_enabled"], bool):
            cleaned_settings["automod_enabled"] = bool(cleaned_settings["automod_enabled"])
        from utils import sanitize_for_json
        cleaned_settings = sanitize_for_json(cleaned_settings)

        # Sanitize automod_threshold, automod_time_window, spam_threshold, spam_time_window, and timeout for download
        def sanitize_int_field(val, default):
            if isinstance(val, (int, float)):
                return int(val)
            if isinstance(val, str) and val.startswith('<circular'):
                return default
            try:
                return int(val)
            except Exception:
                return default
        for field, default in [
            ("automod_threshold", 5),
            ("automod_time_window", 10),
            ("spam_threshold", 5),
            ("spam_time_window", 10),
            ("timeout", 60)
        ]:
            if field in cleaned_settings:
                cleaned_settings[field] = sanitize_int_field(cleaned_settings[field], default)

        from flask import Response
        import json
        response = Response(json.dumps(cleaned_settings, indent=2), mimetype='application/json')
        response.headers['Content-Disposition'] = f'attachment; filename=guild_{guild_id}_settings.json'
        return response
    except Exception as e:
        logging.error(f"Error downloading guild settings for {guild_id}: {e}")
        flash(f"Error downloading settings: {e}", "error")
        return redirect(url_for('list_guild_templates', guild_id=guild_id))


@app.route('/guild/<guild_id>/apply_uploaded_template', methods=['POST'])
def apply_uploaded_template(guild_id):
    """Allow the authenticated guild owner to upload and apply a template JSON file to their guild."""
    # Check owner permissions
    guild = next((g for g in bot.guilds if str(g.id) == str(guild_id)), None)
    if not guild:
        flash(f"Guild {guild_id} not found.", "error")
        return redirect(url_for('list_guild_templates', guild_id=guild_id))
    owner_id = str(guild.owner_id)
    user_id = str(session.get('discord_user_id'))
    if user_id != owner_id:
        flash("Only the guild owner can apply a template from file.", "error")
        return redirect(url_for('list_guild_templates', guild_id=guild_id))
    # Check file
    if 'template_file' not in request.files:
        flash("No file uploaded.", "error")
        return redirect(url_for('list_guild_templates', guild_id=guild_id))
    file = request.files['template_file']
    if file.filename == '' or not file.filename.lower().endswith('.json'):
        flash("Please upload a valid .json file.", "error")
        return redirect(url_for('list_guild_templates', guild_id=guild_id))
    try:
        import tempfile
        import os
        import json
        import asyncio
        # Save file to temp location and load JSON
        with tempfile.NamedTemporaryFile(delete=False, suffix='.json') as tmp:
            file.save(tmp)
            tmp_path = tmp.name
        with open(tmp_path, 'r', encoding='utf-8') as f:
            server_settings = json.load(f)
        os.unlink(tmp_path)
        # Apply template to server (async)
        loop = asyncio.get_event_loop()
        asyncio.run_coroutine_threadsafe(apply_template_to_server(guild, server_settings), loop)
        # Save to server_settings.json
        server_settings_dict = load_server_settings()
        server_settings_dict[str(guild.id)] = server_settings[str(guild.id)]
        save_server_settings(server_settings_dict)
        flash("Template applied from uploaded file!", "success")
        return redirect(url_for('list_guild_templates', guild_id=guild_id))
    except Exception as e:
        logging.error(f"Error applying uploaded template: {e}")
        flash(f"Error applying template: {e}", "error")
        return redirect(url_for('list_guild_templates', guild_id=guild_id))


async def watchdog():
    while True:
        logging.info("[Watchdog] Event loop is alive.")
        await asyncio.sleep(10)

def start_watchdog():
    loop = asyncio.get_event_loop()
    loop.create_task(watchdog())

# Start the watchdog when the bot starts
start_watchdog()

# --- LOGGED GUILDS AGGREGATOR ROUTE ---
@app.route('/logged_guilds')
def logged_guilds():
    import glob, json
    logs_dir = os.path.join(os.getcwd(), 'logs')
    all_guild_messages = []
    logged_guilds = []
    for log_path in glob.glob(os.path.join(logs_dir, '*.json')):
        if log_path.endswith('_users.json'):
            continue
        try:
            with open(log_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    messages = data.get('messages', [])
                elif isinstance(data, list):
                    messages = data
                else:
                    messages = []
                if not messages:
                    continue
                # Infer guild_id from filename
                filename = os.path.basename(log_path)
                guild_id = filename.replace('.json', '')
                # Try to get guild name from server_settings, fallback to guild_id
                server_settings = load_server_settings()
                guild_name = server_settings.get(guild_id, {}).get('guild_name', f'Guild {guild_id}')
                # Show guild_id instead of user_id in the portal
                logged_guilds.append({'name': guild_name, 'id': guild_id, 'guild_id': guild_id, 'message_count': len(messages)})
                for msg in messages:
                    if isinstance(msg, dict):
                        all_guild_messages.append({
                            'guild_name': guild_name,
                            'channel': msg.get('channel', ''),
                            'author': msg.get('author', ''),
                            'content': msg.get('content', ''),
                            'timestamp': msg.get('timestamp', ''),
                            'event': msg.get('event', 'message')
                        })
        except Exception as e:
            print(f"Error loading log {log_path}: {e}")
    return render_template('logged_guilds.html', all_guild_messages=all_guild_messages, logged_guilds=logged_guilds)

# Discord OAuth2 credentials
DISCORD_CLIENT_ID = os.getenv('ClientID')  # Load ClientID from .env

@app.route('/set_timeout/<guild_id>', methods=['POST'])
def set_timeout(guild_id):
    """Set timeout enabled state and duration for a guild from the portal."""
    timeout_enabled = 'timeout_enabled' in request.form
    timeout_duration = request.form.get('timeout_duration', type=int)
    server_settings = load_server_settings()
    if guild_id not in server_settings:
        server_settings[guild_id] = {}
    server_settings[guild_id]['timeout_enabled'] = bool(timeout_enabled)
    if timeout_duration is not None:
        server_settings[guild_id]['timeout_duration'] = timeout_duration
    save_server_settings(server_settings)
    flash(f'Timeout settings updated for guild {guild_id}!')
    return redirect(url_for('portal'))

DISCORD_CLIENT_SECRET = os.getenv('ClientSecret')  # Load ClientSecret from .env
DISCORD_REDIRECT_URI = os.getenv('DISCORD_REDIRECT_URI', 'https://give-me-3.onrender.com/api/auth/discord/redirect')
DISCORD_API_BASE_URL = os.getenv('DISCORD_API_BASE_URL', 'https://discord.com/api')

# Secret key for Flask session
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'supersecretkey')

@app.route('/')
def home():
    # Fetch the Discord user if logged in
    discord_user = get_discord_user() if 'access_token' in session else None
    return render_template('index.html', discord_user=discord_user)  # Pass the user to the template

@app.route('/dashboard')
def dashboard():
    """Dashboard page listing all guilds with links to messages and templates."""
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))

    access_token = session.get('access_token')
    headers = {'Authorization': f'Bearer {access_token}'}
    async def fetch_guilds():
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{DISCORD_API_BASE_URL}/users/@me/guilds", headers=headers) as response:
                guilds_json = await response.json()
                text = await response.text()
                return response.status, guilds_json, text
    future = asyncio.run_coroutine_threadsafe(fetch_guilds(), bot_loop)
    status, guilds_json, text = future.result()

    if status != 200:
        logging.error(f"Failed to fetch user's guilds. Response: {text}")
        return "Failed to fetch guilds", 500

    guilds = []
    for guild in guilds_json:
        guilds.append({
            'id': guild['id'],
            'name': guild['name']
        })

    return render_template('dashboard.html', guilds=guilds)


@app.route("/guild_messages/<int:guild_id>")
def guild_messages(guild_id):
    logging.info(f"[Flask] /guild_messages/{guild_id} route accessed.")
    """Show messages for a guild, applying automod filtering using blocked keywords and regex patterns. Uses global automod_rules as fallback."""
    import re
    server_settings = load_server_settings()

    # Helper to load global automod rules
    def load_global_automod_rules():
        global automod_rules
        return automod_rules

    # Helper to load per-guild automod rules from server_settings
    def load_guild_automod_rules(guild_id):
        settings = server_settings.get(str(guild_id), {})
        return {
            "regex_patterns": settings.get("regex_patterns", []),
            "blocked_keywords": settings.get("blocked_keywords", [])
        }

    # Load messages from log file
    log_file = os.path.join(os.getcwd(), "logs", f"{guild_id}.json")
    messages = []
    if not os.path.exists(log_file):
        with open(log_file, "w") as f:
            json.dump({"messages": []}, f)
    with open(log_file, "r") as f:
        all_messages = json.load(f)
        if isinstance(all_messages, list):
            # Legacy/corrupted format: wrap as dict and fix file
            all_messages = {"messages": all_messages}
            with open(log_file, "w") as fw:
                json.dump(all_messages, fw)
        messages = all_messages.get('messages', [])

    # Load global automod log if present
    global_messages = []
    global_log_path = os.path.join(os.getcwd(), "guild_messages_log.json")
    if os.path.exists(global_log_path):
        try:
            with open(global_log_path, "r") as gf:
                global_log = json.load(gf)
                if isinstance(global_log, list):
                    global_messages = global_log
                elif isinstance(global_log, dict):
                    global_messages = global_log.get('messages', [])
        except Exception as e:
            logging.warning(f"Could not load global automod log: {e}")
    else:
        global_messages = []

    # Load automod settings for this guild, ensure defaults
    if str(guild_id) not in server_settings:
        server_settings[str(guild_id)] = {
            "automod_enabled": True,
            "blocked_keywords": [],
            "regex_patterns": []
        }

    settings = server_settings.get(str(guild_id), {})
    # Ensure automod_enabled is always a boolean
    if "automod_enabled" not in settings or not isinstance(settings["automod_enabled"], bool):
        settings["automod_enabled"] = True
        server_settings[str(guild_id)] = settings
        save_server_settings(server_settings)
    # Ensure timeout_enabled is always a boolean (default True)
    if "timeout_enabled" not in settings or not isinstance(settings["timeout_enabled"], bool):
        settings["timeout_enabled"] = True
        server_settings[str(guild_id)] = settings
        save_server_settings(server_settings)
    automod_enabled = settings["automod_enabled"]
    timeout_enabled = settings["timeout_enabled"]

    # Use global automod_rules as fallback for regex_patterns and blocked_keywords
    global_automod = load_global_automod_rules()
    guild_automod = load_guild_automod_rules(guild_id)
    regex_patterns = guild_automod["regex_patterns"] if guild_automod["regex_patterns"] else global_automod.get("regex_patterns", [])
    blocked_keywords = guild_automod["blocked_keywords"] if guild_automod["blocked_keywords"] else global_automod.get("blocked_keywords", [])

    # Always extract the message list from all_messages
    message_list = all_messages.get('messages', [])
    messages = []
    for msg in message_list:
        blocked = False
        blocked_keywords_found = []
        matched_regexes = []
        if automod_enabled:
            # Check for blocked keywords
            for keyword in blocked_keywords:
                if keyword.lower() in msg.get("content", "").lower():
                    blocked_keywords_found.append(keyword)
                    blocked = True
            # Check for regex patterns
            for pattern in regex_patterns:
                try:
                    if re.search(pattern, msg.get("content", ""), re.IGNORECASE):
                        matched_regexes.append(pattern)
                        blocked = True
                except re.error:
                    continue  # Ignore invalid regex
        if blocked:
            # Always provide both 'blocked_keywords' and 'keywords' for compatibility
            messages.append({**msg, "content": "[Blocked by Automod]", "event": "blocked", "blocked_keywords": blocked_keywords_found, "keywords": blocked_keywords_found, "matched_regexes": matched_regexes})
        else:
            messages.append({**msg, "event": "message"})

    return render_template(
        'guild_messages.html',
        guild_id=guild_id,
        guild_name=f"Test Guild {guild_id}",
        automod_enabled=automod_enabled,
        timeout_enabled=timeout_enabled,
        custom_color=settings.get('custom_color', '#7289da'),
        blocked_keywords=blocked_keywords,
        regex_patterns=regex_patterns,
        messages=messages,  # Pass only the processed list
        global_messages=global_messages  # Add global automod messages
    )

@app.route("/guild/<int:guild_id>/messages")
def guild_messages_alt(guild_id):
    # Support alternate URL pattern used by dashboard
    return guild_messages(guild_id)


@app.route('/')
def index():
    logging.info("[Flask] /index route accessed.")
    discord_user = get_discord_user() if 'access_token' in session else None
    return render_template('index.html', discord_user=discord_user)

@app.route('/login')
def login():
    logging.info("[Flask] /login route accessed.")
    """Redirect the user to Discord's OAuth2 login page."""
    discord_auth_url = (
        f"{DISCORD_API_BASE_URL}/oauth2/authorize"
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={DISCORD_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=identify guilds"
    )
    return redirect(discord_auth_url)

@app.route('/callback')
def callback():
    logging.info("[Flask] /callback route accessed.")
    """Handle the OAuth2 callback and exchange the code for an access token."""
    code = request.args.get('code')
    if not code:
        return "Authorization failed.", 400

    # Exchange code for access token
    token_url = f"{DISCORD_API_BASE_URL}/oauth2/token"
    data = {
        'client_id': DISCORD_CLIENT_ID,
        'client_secret': DISCORD_CLIENT_SECRET,
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': DISCORD_REDIRECT_URI,
    }
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}

    # Log the request data (excluding sensitive information)
    logging.debug(f"OAuth2 Token Request: {data}")

    async def post_token():
        async with aiohttp.ClientSession() as session:
            async with session.post(token_url, data=data, headers=headers) as response:
                token_response = await response.json()
                text = await response.text()
                return response.status, token_response, text

    if bot_loop is None:
        logging.error("Bot loop is not ready yet!")
        return "Bot is not ready. Please try again in a moment.", 503
    future = asyncio.run_coroutine_threadsafe(post_token(), bot_loop)
    status, token_response, text = future.result()
    logging.debug(f"OAuth2 Token Response: {text}")

    if status != 200:
        logging.error(f"Failed to get token: {text}")
        return None
    session['access_token'] = token_response['access_token']
    return redirect(url_for('portal'))  # Redirect to the portal page

@app.route('/api/auth/discord/redirect')
def api_auth_discord_redirect():
    logging.info("[Flask] /api/auth/discord/redirect route accessed.")
    """Alias for the callback route to handle Discord OAuth2 redirect."""
    return callback()

@app.route('/logout')
def logout():
    logging.info("[Flask] /logout route accessed.")
    """Log the user out by clearing the session."""
    session.clear()
    return redirect(url_for('home'))

@app.route('/create_templates_for_all_logs', methods=['POST'])
def create_templates_for_all_logs(auto_trigger=False):
    logging.info("[Flask] /create_templates_for_all_logs POST accessed.")
    """
    Create a template for each server the bot is in, using the backup logic.
    If auto_trigger=True, function can be called internally without rendering the portal.
    """
    logs_dir = os.path.join(os.getcwd(), "logs")
    os.makedirs(logs_dir, exist_ok=True)
    owner_roles = load_owner_roles()
    created_count = 0
    print(f"[TEMPLATE GEN] bot.guilds: {getattr(bot, 'guilds', 'N/A')}")
    # Ensure all timeout and automod settings are initialized before creating templates
    initialize_timeout_settings()
    # Restrict to only guilds owned by the logged-in user (unless auto_trigger)
    discord_user = get_discord_user() if not auto_trigger else None
    user_id = int(discord_user['id']) if discord_user else None
    guilds_to_process = []
    if user_id:
        guilds_to_process = [g for g in getattr(bot, 'guilds', []) if getattr(g, 'owner_id', None) == user_id]
    else:
        guilds_to_process = list(getattr(bot, 'guilds', []))
    if not guilds_to_process and not auto_trigger:
        flash('You do not own any guilds to save templates for.', 'warning')
        return redirect(url_for('portal'))
    for guild in guilds_to_process:
        print(f"[TEMPLATE GEN] Processing guild: {getattr(guild, 'id', 'N/A')} | {getattr(guild, 'name', 'N/A')}")
        guild_id = str(guild.id)
        guild_owner_roles = owner_roles.get(guild_id, {})
        guild_settings = server_settings.get(guild_id, {})
        log_file = os.path.join(logs_dir, f"{guild_id}.json")
        recent_messages = []
        if os.path.exists(log_file):
            try:
                with open(log_file, "r") as f:
                    all_msgs = json.load(f)
                    recent_messages = all_msgs[-100:] if len(all_msgs) > 100 else all_msgs
            except Exception as e:
                logging.warning(f"Could not load messages for backup: {e}")
        server_settings_backup = {
            'name': guild.name,
            'id': guild.id,
            'member_count': guild.member_count,
            'roles': [{'name': role.name, 'permissions': role.permissions.value} for role in guild.roles if not role.managed],
            'categories': [
                {
                    'name': category.name,
                    'channels': [
                        {
                            'name': channel.name,
                            'type': 'text' if isinstance(channel, nextcord.TextChannel) else 'voice',
                            'permissions': {
                                str(target): overwrite._values
                                for target, overwrite in channel.overwrites.items()
                            }
                        }
                        for channel in category.channels
                    ]
                }
                for category in guild.categories
            ],
            'channels': [
                {
                    'name': channel.name,
                    'type': 'text' if isinstance(channel, nextcord.TextChannel) else 'voice',
                    'permissions': {
                        str(target): overwrite._values
                        for target, overwrite in channel.overwrites.items()
                    }
                }
                for channel in guild.channels if channel.category is None
            ],
            'owner_id': guild.owner_id,
            'owner': str(guild.owner),
            'created_at': str(guild.created_at),
            'automod_enabled': guild_settings.get("automod_enabled", True),
            'blocked_keywords': guild_settings.get("blocked_keywords", []),
            'regex_patterns': guild_settings.get("regex_patterns", []),
            'timeout_enabled': guild_settings.get("timeout_enabled", True),
            'timeout': guild_settings.get("timeout", DEFAULT_TIMEOUT_DURATION),
            'automod_threshold': guild_settings.get("automod_threshold", 5),
            'automod_time_window': guild_settings.get("automod_time_window", 10),
            'custom_color': guild_settings.get("custom_color", "#7289da"),
            'owner_roles': guild_owner_roles,
            'recent_messages': recent_messages
        }
        # Force automod_enabled and timeout_enabled to True in the template
        server_settings_backup['automod_enabled'] = True
        server_settings_backup['timeout_enabled'] = True
        template_name = f"{guild_id}_auto"
        template_path = os.path.join(TEMPLATES_DIR, f"{template_name}.json")
        # Backup existing auto template if it exists
        if os.path.exists(template_path):
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_name = f"{guild_id}_auto_{timestamp}.json"
            backup_path = os.path.join(TEMPLATES_DIR, backup_name)
            os.rename(template_path, backup_path)
            print(f"[TEMPLATE GEN] Backed up existing template to: {backup_path}")
        with open(template_path, 'w') as template_file:
            # Ensure spam_threshold and spam_time_window are always present and valid integers
            if 'spam_threshold' not in server_settings_backup or not isinstance(server_settings_backup['spam_threshold'], int):
                try:
                    server_settings_backup['spam_threshold'] = int(server_settings_backup.get('spam_threshold', 5))
                except Exception:
                    server_settings_backup['spam_threshold'] = 5
            if 'spam_time_window' not in server_settings_backup or not isinstance(server_settings_backup['spam_time_window'], int):
                try:
                    server_settings_backup['spam_time_window'] = int(server_settings_backup.get('spam_time_window', 10))
                except Exception:
                    server_settings_backup['spam_time_window'] = 10
            # Sanitize 'timeout' field before sending settings
            if 'timeout' not in server_settings_backup or not isinstance(server_settings_backup['timeout'], int):
                try:
                    server_settings_backup['timeout'] = int(server_settings_backup.get('timeout', 60))
                except Exception:
                    server_settings_backup['timeout'] = 60
            # Remove circular references before sending settings
            from utils import sanitize_for_json, safe_json_dump
            server_settings_backup = sanitize_for_json(server_settings_backup)
            safe_json_dump(server_settings_backup, template_file, indent=2)
        print(f"[TEMPLATE GEN] Created template: {template_path}")
        created_count += 1
    print(f"[TEMPLATE GEN] Created {created_count} templates for all guild logs.")
    # Render the portal page with a result message, unless auto_trigger
    if auto_trigger:
        return f"Created {created_count} templates for all guild logs."
    return redirect(url_for('portal', templates_creation_result=f"Created {created_count} templates for all guild logs."))

def get_discord_user():
    logging.info("[Discord] Fetching Discord user.")
    """Fetch the authenticated user's Discord profile."""
    access_token = session.get('access_token')
    if not access_token:
        return None
    headers = {'Authorization': f'Bearer {access_token}'}
    async def fetch():
        async with aiohttp.ClientSession() as session_http:
            async with session_http.get(f"{DISCORD_API_BASE_URL}/users/@me", headers=headers) as response:
                user = await response.json()
                if response.status == 200:
                    return user
        return None
    if bot_loop is None:
        logging.error("Bot loop is not ready yet!")
        return None
    future = asyncio.run_coroutine_threadsafe(fetch(), bot_loop)
    return future.result()

@app.route('/apply_template_web', methods=['POST'])
def apply_template_web():
    logging.info("[Flask] /apply_template_web POST accessed.")
    """Apply a template through the website."""
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))

    template_name = request.form.get('template_name')
    if not template_name:
        return "Template name is required.", 400

    template_path = os.path.join(TEMPLATES_DIR, f"{template_name}.json")
    if not os.path.exists(template_path):
        return f"Template {template_name} not found.", 404

    try:
        with open(template_path, 'r') as template_file:
            server_settings = json.load(template_file)

        # Create and set an event loop in the current thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.create_task(apply_template_to_server(server_settings))
        loop.run_until_complete(asyncio.sleep(0))  # Run the event loop briefly
        return f"Template {template_name} is being applied successfully!"
    except Exception as e:
        logging.error(f"Error applying template {template_name}: {e}")
        return f"An error occurred: {e}", 500

def run_flask():
    port = int(os.environ.get('PORT', 1500))  # Use PORT environment variable
    app.run(host='0.0.0.0', port=port, debug=True, use_reloader=False)  # Enable debug mode

# Directory to store templates
TEMPLATES_DIR = "templates"
os.makedirs(TEMPLATES_DIR, exist_ok=True)  # Ensure the directory exists

@app.route('/guild_templates_lookup', methods=['POST'])
def list_guild_templates_redirect():
    guild_id = request.form.get('guild_id')
    if not guild_id or not guild_id.isdigit():
        return render_template('error.html', message="Invalid Guild ID.")
    return redirect(url_for('list_guild_templates_portal', guild_id=guild_id))

@app.route('/api/guilds')
def api_guilds():
    guilds_data = []
    for guild in bot.guilds:
        guilds_data.append({
            'id': str(guild.id),
            'name': guild.name,
            'owner': str(guild.owner),
            'member_count': getattr(guild, 'member_count', 0)
        })
    return jsonify(guilds=guilds_data)

@app.route('/guild/<guild_id>/templates')
def list_guild_templates_portal(guild_id):
    """List all templates for a specific guild and allow applying them to that guild's messages channel."""
    templates = []
    for fname in os.listdir(TEMPLATES_DIR):
        if fname.endswith('.json') and (fname.startswith(f"{guild_id}_") or fname == f"{guild_id}_auto.json"):
            path = os.path.join(TEMPLATES_DIR, fname)
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                    templates.append({
                        "filename": fname,
                        "id": data.get("id", ""),  # guild ID
                        "name": data.get("name", fname),
                        "code": data.get("code", ""),  # Discord template code if present
                    })
            except Exception as e:
                logging.error(f"Error reading template file {fname}: {e}")
    logging.info(f"[TEMPLATE LIST] Found templates for guild {guild_id}: {templates}")
    return render_template('guild_templates.html', guild_id=guild_id, templates=templates)


@app.route('/guild/<guild_id>/apply_template/<template_name>', methods=['POST'])
def apply_guild_template(guild_id, template_name):
    """Apply a template to the specified guild's messages channel and update Discord automatically."""
    import asyncio
    template_path = os.path.join(TEMPLATES_DIR, template_name)
    if not os.path.exists(template_path):
        return f"Template {template_name} not found.", 404
    try:
        with open(template_path, 'r') as template_file:
            raw_template_data = json.load(template_file)
        # Clean template data of circular references before applying
        cleaned_template_data = clean_circular_references(raw_template_data)
        # Find the guild by ID
        guild = nextcord.utils.get(bot.guilds, id=int(guild_id))
        if guild is None:
            logging.error(f"[TEMPLATE APPLY] Guild not found: {guild_id}")
            return f"Guild {guild_id} not found.", 404
        # Schedule the coroutine on the bot's event loop
        loop = bot.loop
        logging.info(f"[TEMPLATE APPLY] Applying template {template_name} to guild {guild_id}")
        asyncio.run_coroutine_threadsafe(apply_template_to_server(guild, cleaned_template_data), loop)
        # Update in-memory server_settings and persist to disk for automod and all other settings
        server_settings[str(guild.id)] = cleaned_template_data
        save_server_settings(server_settings)
        return f"Template {template_name} is being applied to guild {guild_id}!"
    except Exception as e:
        logging.error(f"Error applying template {template_name} to guild {guild_id}: {e}")
        return f"An error occurred: {e}", 500


@app.route('/apply_template/<template_name>', methods=['GET'])
def apply_template(template_name):
    """Apply a template to the server automatically."""
    try:
        template_path = os.path.join(TEMPLATES_DIR, f"{template_name}.json")
        if not os.path.exists(template_path):
            return f"Template {template_name} not found.", 404

        # Load the template file
        with open(template_path, 'r') as template_file:
            server_settings = json.load(template_file)

        # Use asyncio to run the bot's restore functionality
        asyncio.run(apply_template_to_server(server_settings))
        return f"Template {template_name} has been applied successfully!"
    except Exception as e:
        logging.error(f"Error applying template {template_name}: {e}")
        return f"An error occurred: {e}", 500

async def apply_template_to_server(guild, server_settings):
    """Restore server settings from the provided template to the specified guild."""
    # Restore server name
    await guild.edit(name=server_settings['name'])

    # Restore roles
    for role_data in server_settings.get('roles', []):
        existing_role = nextcord.utils.get(guild.roles, name=role_data['name'])
        if existing_role is None:
            await guild.create_role(name=role_data['name'], permissions=nextcord.Permissions(role_data['permissions']))
        else:
            if not existing_role.managed:
                await existing_role.edit(permissions=nextcord.Permissions(role_data['permissions']))

    # --- Restore categories and channels ---
    # First, create all categories
    category_map = {}
    for category_data in sorted(server_settings.get('categories', []), key=lambda c: c.get('position', 0)):
        existing_category = nextcord.utils.get(guild.categories, name=category_data['name'])
        if existing_category is None:
            category = await guild.create_category(name=category_data['name'], position=category_data.get('position', 0))
        else:
            category = existing_category
        category_map[category_data['name']] = category

    # Then, create all channels within categories
    for category_data in server_settings.get('categories', []):
        category = category_map.get(category_data['name'])
        for channel_data in sorted(category_data.get('channels', []), key=lambda ch: ch.get('position', 0)):
            existing_channel = nextcord.utils.get(category.channels, name=channel_data['name'])
            overwrites = {}
            for target_id, perms in channel_data.get('permissions', {}).items():
                target = guild.get_role(int(target_id)) or guild.get_member(int(target_id)) or guild.default_role
                if target:
                    overwrites[target] = nextcord.PermissionOverwrite(**perms)
            if existing_channel is None:
                if channel_data['type'] == 'text':
                    await category.create_text_channel(name=channel_data['name'], position=channel_data.get('position', 0), overwrites=overwrites)
                elif channel_data['type'] == 'voice':
                    await category.create_voice_channel(name=channel_data['name'], position=channel_data.get('position', 0), overwrites=overwrites)

    # Restore standalone channels (not in categories)
    for channel_data in sorted(server_settings.get('channels', []), key=lambda ch: ch.get('position', 0)):
        existing_channel = nextcord.utils.get(guild.channels, name=channel_data['name'])
        overwrites = {}
        for target_id, perms in channel_data.get('permissions', {}).items():
            target = guild.get_role(int(target_id)) or guild.get_member(int(target_id)) or guild.default_role
            if target:
                overwrites[target] = nextcord.PermissionOverwrite(**perms)
        if existing_channel is None:
            if channel_data['type'] == 'text':
                await guild.create_text_channel(name=channel_data['name'], position=channel_data.get('position', 0), overwrites=overwrites)
            elif channel_data['type'] == 'voice':
                await guild.create_voice_channel(name=channel_data['name'], position=channel_data.get('position', 0), overwrites=overwrites)


# Define regex patterns for automod globally
patterns = [
    r'https?://\S+',  # Matches any URL
    r'\b(spam|advertisement|link|buy|free|click here|subscribe)\b',  # Matches common spam phrases
    r'discord\.gg/\S+',  # Matches Discord invite links
    r'<@!?\d{17,20}>',  # Matches user mentions
    r'(.)\1{3,}',  # Matches any character repeated 4 or more times
    r'[^\f\n\r\t\v\u0020\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]',  # Your first pattern
    r'^.*([A-Za-z0-9]+( [A-Za-z0-9]+)+).*[A-Za-z]+.*$',  # Your second pattern
    # Add any additional patterns here
]

# Background task example
@tasks.loop(seconds=60)  # Adjust the interval as needed
async def periodic_task():
    # Perform your periodic task here, e.g., cleaning up data or checking status
    print('Periodic task running...')

# Initialize server_settings at the top of the file
SERVER_SETTINGS_FILE = "server_settings.json"

def migrate_server_settings(data):
    """
    Ensure server_settings is a dict of dicts keyed by guild ID.
    If it's a flat dict or contains only global settings, migrate to correct structure.
    """
    if not isinstance(data, dict):
        return {}
    # If it looks like a global settings dict (e.g., only 'hide_owner_id'), migrate to empty
    if all(isinstance(v, (str, bool, int, float, list)) for v in data.values()):
        # Only global settings, not per-guild
        return {}
    # If keys are guild IDs and values are dicts, it's valid
    for k, v in data.items():
        if not isinstance(v, dict):
            data[k] = {}  # Reset any non-dict entry
    return data

def load_server_settings():
    """Load server-specific settings from the JSON file."""
    try:
        with open(SERVER_SETTINGS_FILE, "r") as f:
            data = json.load(f)
        migrated = migrate_server_settings(data)
        # If migration changed the structure, save it back
        if migrated != data:
            with open(SERVER_SETTINGS_FILE, "w") as f:
                json.dump(migrated, f, indent=2)
        return migrated
    except FileNotFoundError:
        logging.warning(f"{SERVER_SETTINGS_FILE} not found. Creating a new one.")
        with open(SERVER_SETTINGS_FILE, "w") as f:
            json.dump({}, f)
        return {}

def save_server_settings(settings):
    """Save server-specific settings to the JSON file, cleaning circular references and fixing known field issues."""
    try:
        with open(SERVER_SETTINGS_FILE, "w") as f:
            safe_json_dump(settings, f, indent=2)
        # Automatically clean circular reference fields after every save
        clean_server_settings_file()
        logging.info("Server settings saved and cleaned successfully.")
    except Exception as e:
        logging.error(f"Error saving server settings: {e}")

# Load server settings at startup
server_settings = load_server_settings()

# Ensure server_settings has a default structure
for guild_id, settings in server_settings.items():
    if isinstance(settings, dict):
        if "members" not in settings:
            settings["members"] = {}  # Add a members key if missing

@bot.event
async def on_message(message):
    # Skip messages from bots
    if message.author.bot:
        return
    # Automod for DMs (works without privileged intents)
    if not message.guild:
        # --- Automod rules for DMs ---
        blocked_keywords = [
            "spam", "scam", "phishing", "free nitro", "giveaway", "discord.gg", "invite", "buy now", "click here", "subscribe", "adult", "nsfw", "crypto", "bitcoin", "porn", "sex", "nude", "robux", "nitro", "airdrop", "token", "password", "login", "credit card", "paypal", "venmo", "cashapp", "gift", "prize", "winner", "claim", "investment", "pump", "dump"
        ]
        regex_patterns = [
            r'https?://\\S+',
            r'\\b(spam|advertisement|link|buy|free|click here|subscribe)\\b',
            r'discord\\.gg/\\S+',
            r'<@!?\\d{17,20}>',
            r'(.)\\1{3,}',
            r'[^\\f\\n\\r\\t\\v\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]',
            r'^.*([A-Za-z0-9]+( [A-Za-z0-9]+)+).*[A-Za-z]+.*$'
        ]
        content = message.content.lower()
        blocked = any(keyword in content for keyword in blocked_keywords)
        blocked_regex = any(re.search(pattern, content) for pattern in regex_patterns)
        if blocked or blocked_regex:
            await message.reply("Your message was blocked by automod (DM). Please do not send spam or prohibited content.")
            return
        # Optionally log the DM or take other action
        return
    # Only process guild automod if message_content intent is enabled
    if not intents.message_content:
        # Still process commands
        await bot.process_commands(message)
        return

    logs_dir = os.path.join(os.getcwd(), "logs")
    os.makedirs(logs_dir, exist_ok=True)
    log_file = os.path.join(logs_dir, f"{message.guild.id}.json")
    users_file = os.path.join(logs_dir, f"{message.guild.id}_users.json")
    # Prepare message data
    msg_data = {
        "author": str(message.author),
        "content": message.content,
        "channel": str(message.channel.name),
        "timestamp": str(message.created_at)
    }
    # Load existing log or start new
    try:
        if os.path.exists(log_file):
            with open(log_file, "r") as f:
                messages = json.load(f)
            if not isinstance(messages, list):
                messages = []
        else:
            messages = []
    except Exception as e:
        logging.warning(f"Could not load log file {log_file}: {e}")
        messages = []
    messages.append(msg_data)
    # Optionally, limit log size (e.g., last 1000 messages)
    if len(messages) > 1000:
        messages = messages[-1000:]
    with open(log_file, "w") as f:
        json.dump(messages, f, indent=2)

    # Log all Discord users for this guild
    try:
        users_list = []
        async for member in message.guild.fetch_members(limit=None):
            users_list.append({
                "id": str(member.id),
                "name": member.name,
                "discriminator": member.discriminator,
                "display_name": member.display_name,
                "bot": member.bot
            })
        with open(users_file, "w") as uf:
            json.dump(users_list, uf, indent=2)
    except Exception as e:
        logging.warning(f"Could not log users for guild {message.guild.id}: {e}")

    # --- Automod: Blocked Keywords & Regex Patterns ---
    # Only apply automod logic AFTER logging the message, so all messages are always logged
    server_settings = load_server_settings()
    guild_id = str(message.guild.id)
    settings = server_settings.get(guild_id, {})
    blocked_keywords = settings.get("blocked_keywords", [])
    regex_patterns = settings.get("regex_patterns", [])
    automod_enabled = settings.get("automod_enabled", True)

    if automod_enabled:
        blocked_keywords_found = [kw for kw in blocked_keywords if kw.lower() in message.content.lower()]
        import re
        matched_regexes = [pattern for pattern in regex_patterns if re.search(pattern, message.content, re.IGNORECASE)]
        if blocked_keywords_found or matched_regexes:
            try:
                await message.delete()
                logging.info(f"[Automod] Blocked message from {message.author} in {message.guild.name}: {message.content}")
                logging.info(f"[Automod] Blocked Keywords: {blocked_keywords_found}, Matched Regexes: {matched_regexes}")
                # Optionally, notify the user (comment out if not wanted)
                # await message.channel.send(f"{message.author.mention}, your message was blocked by automod.", delete_after=10)
            except Exception as e:
                logging.error(f"[Automod] Failed to delete message: {e}")
            return  # Do not process commands for blocked messages

    # Let commands still work
    await bot.process_commands(message)


@bot.event
async def on_ready():
    """Triggered when the bot is ready."""
    set_bot_loop(asyncio.get_running_loop())
    logging.info(f'Logged in as {bot.user}')
    try:
        # Synchronize slash commands globally
        await bot.tree.sync()
        logging.info("Slash commands synchronized globally.")
        # Ensure default settings for all guilds the bot is in
        for guild in bot.guilds:
            guild_id = str(guild.id)
            if guild_id not in server_settings:
                server_settings[guild_id] = {
                    "automod_enabled": True,
                    "blocked_keywords": [],
                    "regex_patterns": [],
                    "spam_threshold": 5,
                    "spam_time_window": 10,
                    "owner_id": (getattr(guild, 'owner', None).id if getattr(guild, 'owner', None) else None),
                    "owner_name": (getattr(guild, 'owner', None).name if getattr(guild, 'owner', None) else "Unknown"),
                }

        # Save updated settings to ensure persistence
        save_server_settings(server_settings)

        # Log all guilds the bot is in
        logging.info("Bot is in the following guilds:")
        for guild in bot.guilds:
            logging.info(f"Guild Name: {guild.name}, Guild ID: {guild.id}")

            # Ensure default settings for each guild
            guild_id = str(guild.id)
            if guild_id not in server_settings:
                server_settings[guild_id] = {
                    "automod_enabled": True,
                    "blocked_keywords": [],
                    "regex_patterns": [],
                }
        save_server_settings(server_settings)

        # Synchronize slash commands globally
        await bot.tree.sync()
        logging.info("Slash commands synchronized globally.")
    except Exception as e:
        logging.error(f"Error during on_ready: {e}")

@bot.event
async def on_guild_join(guild):
    """Triggered when the bot joins a new guild."""
    try:
        # Automatically load and assign roles for the new guild
        await load_and_assign_roles(guild)

        # Notify the owner in the system channel (if available)
        if guild.system_channel:
            owner = getattr(guild, 'owner', None)
            owner_mention = owner.mention if owner and hasattr(owner, 'mention') else ''
            await guild.system_channel.send(
                f"Hello {owner_mention or 'Server Owner'}, thank you for adding me to **{guild.name}**! "
                f"Roles have been automatically loaded and assigned. Use the portal to customize settings."
            )

        owner = getattr(guild, 'owner', None)
        owner_name = owner.name if owner and hasattr(owner, 'name') else 'Unknown'
        owner_id = owner.id if owner and hasattr(owner, 'id') else 'Unknown'
        logging.info(f"Bot added to guild: {guild.name} (ID: {guild.id}), owned by {owner_name} (ID: {owner_id}).")
    except Exception as e:
        logging.error(f"Error in on_guild_join for guild {guild.name} (ID: {guild.id}): {e}")

async def load_and_assign_roles(guild):
    """
    Load roles from owner_roles.json, assign them to members, and move the bot's role to the top.
    """
    try:
        guild_id = str(guild.id)
        bot_member = guild.me  # The bot's member object

        # Load owner roles from the JSON file
        owner_roles = load_owner_roles()  # Ensure owner_roles is loaded here
        owner_roles_data = owner_roles.get(guild_id, {})
        if owner_roles_data:
            role_type = owner_roles_data.get("type")
            role_id = owner_roles_data.get("role_id")
            roles = owner_roles_data.get("roles", {})

            if role_type == "server" and role_id:
                # Assign the server-wide owner role to all members
                owner_role = nextcord.utils.get(guild.roles, id=role_id)
                if (owner_role):
                    for member in guild.members:
                        if not member.bot and owner_role not in member.roles:
                            await member.add_roles(owner_role, reason="Automatically assigned server-wide owner role")
                            logging.info(f"Assigned server-wide owner role '{owner_role.name}' to {member.name} (ID: {member.id}).")
            elif role_type == "member" and roles:
                # Assign unique roles to individual members
                for member_id, role_id in roles.items():
                    member = guild.get_member(int(member_id))
                    unique_role = nextcord.utils.get(guild.roles, id=role_id)
                    if member and unique_role and unique_role not in member.roles:
                        await member.add_roles(unique_role, reason="Automatically assigned unique owner role")
                        logging.info(f"Assigned unique role '{unique_role.name}' to {member.name} (ID: {member.id}).")

        # Ensure the bot's role is always on top
        bot_role = bot_member.top_role
        await bot_role.edit(position=guild.roles[-1].position)
        logging.info(f"Bot's role '{bot_role.name}' moved to the top in guild {guild.name} (ID: {guild.id}).")
    except Exception as e:
        logging.error(f"An error occurred: {e}")
        logging.error(f"Error in load_and_assign_roles for guild {guild.name} (ID: {guild.id}): {e}")

@app.route('/portal')
def portal():
    global server_settings  # must be at the top before any use
    # Always reload server_settings and hide owner ID for every guild for privacy
    server_settings = load_server_settings()
    for gid in server_settings:
        pass
        pass
    # --- CANONICAL GUILD ID LOGIC ---
    # The canonical source of guild IDs is bot.guilds, as populated on_ready and updated on_guild_join.
    # All portal logic below will only use guilds present in bot.guilds.
    # This guarantees that the portal always reflects the actual servers the bot is in, and prevents mismatches.
    logging.debug(f"Canonical guild IDs (bot.guilds): {[str(guild.id) for guild in bot.guilds]}")
    templates_creation_result = request.args.get('templates_creation_result')
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))

    try:
        # Fetch the authenticated user's guilds
        access_token = session.get('access_token')
        headers = {'Authorization': f'Bearer {access_token}'}
        # Fetch the authenticated user's guilds asynchronously using aiohttp
        async def fetch_guilds():
            async with aiohttp.ClientSession() as session_http:
                async with session_http.get(f"{DISCORD_API_BASE_URL}/users/@me/guilds", headers=headers) as response:
                    guilds_json = await response.json()
                    text = await response.text()
                    return response.status, guilds_json, text
        if bot_loop is None:
            logging.error("Bot loop is not ready yet!")
            return "Bot is not ready. Please try again in a moment.", 503
        status, user_guilds, text = asyncio.run_coroutine_threadsafe(fetch_guilds(), bot_loop).result()

        if status != 200:
            logging.error(f"Failed to fetch user's guilds. Response: {text}")
            return render_template('error.html', message="Failed to fetch your guilds. Please try logging in again.")

        # Log all user guilds
        logging.info("User's guilds fetched from Discord API:")
        for guild in user_guilds:
            logging.info(f"Guild Name: {guild['name']}, Guild ID: {guild['id']}")

        # Load owner roles and server settings
        owner_roles = load_owner_roles()

        # Pre-load server IDs and related information into memory
        memory_cache = {}
        for guild in bot.guilds:
            guild_id = str(guild.id)
            default_settings = {
                "automod_enabled": False,
                "blocked_keywords": [],
                "regex_patterns": [],
                "spam_threshold": 5,
                "spam_time_window": 10,
                "owner_id": (getattr(guild, 'owner', None).id if getattr(guild, 'owner', None) else None),
                "owner_name": (getattr(guild, 'owner', None).name if getattr(guild, 'owner', None) else "Unknown"),
            }
            if guild_id not in server_settings or not isinstance(server_settings[guild_id], dict):
                server_settings[guild_id] = default_settings.copy()

            # Cache guild information in memory
            memory_cache[guild_id] = {
                "name": guild.name,
                "owner_name": (getattr(guild, 'owner', None).name if getattr(guild, 'owner', None) else "Unknown"),
                "owner_id": guild.owner.id if guild.owner else "Unknown",
                "automod_enabled": server_settings[guild_id].get("automod_enabled", False),
                "blocked_keywords": server_settings[guild_id].get("blocked_keywords", []),
                "regex_patterns": server_settings[guild_id].get("regex_patterns", []),
                "spam_threshold": server_settings[guild_id].get("spam_threshold", 5),
                "spam_time_window": server_settings[guild_id].get("spam_time_window", 10),
                "bot_role_top": server_settings[guild_id].get("bot_role_top", False),
            }

        # Save updated settings to ensure persistence
        save_server_settings(server_settings)

        # Separate guilds into those the bot is in and those it can be added to
        bot_guild_ids = {str(guild.id) for guild in bot.guilds}
        user_guilds_with_permissions = []

        for guild in user_guilds:
            guild_id = guild.get('id')
            if not guild_id:
                continue
            if guild_id not in bot_guild_ids and guild.get('permissions', 0) & 0x20:  # Check for 'Manage Server' permission
                owner_role = owner_roles.get(guild_id, {}).get("role_name", f"Role ID: {owner_roles.get(guild_id, {}).get('role_id', 'Unknown')}")
                user_guilds_with_permissions.append({
                    'id': guild_id,
                    'name': guild.get('name', 'Unknown'),
                    'icon': guild.get('icon', None),
                    'owner_role': owner_role or 'Unknown'
                })

        # Get the list of guilds the bot is already in
        bot_guilds = []
        for guild_id, data in memory_cache.items():
            # Fetch owner role from owner_roles.json if available
            owner_role_name = None
            if guild_id in owner_roles:
                owner_role_name = owner_roles[guild_id].get("role_name")
            # Determine if the current user is the owner
            user_is_owner = str(discord_user.get("id")) == str(data.get("owner_id", ""))
            # Determine if the current user has the owner role (API call, fallback to owner check)
            try:
                user_has_owner_role = user_has_owner_role(guild_id, discord_user.id, session.get('access_token'))
            except Exception as e:
                user_has_owner_role = False
            bot_guilds.append({
                'id': guild_id,
                'name': data.get("name", "Unknown"),
                'owner_name': data.get("owner_name", "Unknown"),
                'owner_id': data.get("owner_id", "Unknown"),
                'automod_enabled': data.get("automod_enabled", True),
                'blocked_keywords': data.get("blocked_keywords", []),
                'regex_patterns': data.get("regex_patterns", []),
                'spam_threshold': data.get("spam_threshold", 5),
                'spam_time_window': data.get("spam_time_window", 10),
                'bot_role_top': data.get("bot_role_top", False),
                'owner_role_name': owner_role_name or 'Not Set',
                'user_is_owner': user_is_owner,
                'user_has_owner_role': user_has_owner_role,
            })

        # Fetch the Discord user
        discord_user = get_discord_user()
        if not discord_user:
            return redirect(url_for('login'))

        # Build a list of all logged guilds from server_settings (even if not in bot_guilds)
        logged_guilds = []
        for gid, guild_data in server_settings.items():
            if not isinstance(guild_data, dict):
                continue  # Skip non-dict entries
            if not str(gid).isdigit():
                continue  # Skip non-numeric keys (not real guilds)
            logged_guilds.append({
                'id': gid,
                'name': guild_data.get('name', f'Guild {gid}'),
                'owner_name': guild_data.get('owner_name', 'Unknown'),
                'owner_id': guild_data.get('owner_id', 'Unknown'),
                'automod_enabled': guild_data.get('automod_enabled', True),
                'blocked_keywords': guild_data.get('blocked_keywords', []),
                'regex_patterns': guild_data.get('regex_patterns', []),
                'spam_threshold': guild_data.get('spam_threshold', 5),
                'spam_time_window': guild_data.get('spam_time_window', 10),
                'latest_message': guild_data.get('latest_message', {}),
            })

        return render_template(
            'portal.html',
            bot_guilds=bot_guilds,
            user_guilds_with_permissions=user_guilds_with_permissions,
            discord_user=discord_user,
            server_settings=server_settings,  # Pass server_settings to the template
            templates_creation_result=templates_creation_result,
            logged_guilds=logged_guilds
        )
    except Exception as e:
        import traceback
        logging.error(f"Error loading portal: {e}\n{traceback.format_exc()}")
        return render_template('error.html', message="An unexpected error occurred. Please try again later.")

async def scan_and_create_owner_role(guild):
    """
    Automatically scan for existing owner roles, create them if missing, and assign to all members.
    """
    try:
        guild_id = str(guild.id)
        bot_member = guild.me  # The bot's member object

        # Check if an owner role already exists
        owner_role_name = "Owner"
        existing_role = nextcord.utils.get(guild.roles, name=owner_role_name)

        if not existing_role:
            # Create the owner role if it doesn't exist
            owner_role = await guild.create_role(
                name=owner_role_name,
                permissions=nextcord.Permissions(administrator=True),
                reason="Automatically created owner role"
            )
            logging.info(f"Created owner role '{owner_role.name}' in guild {guild.name} (ID: {guild.id}).")
        else:
            owner_role = existing_role
            logging.info(f"Owner role '{owner_role.name}' already exists in guild {guild.name} (ID: {guild.id}).")

        # Assign the owner role to all members in the guild
        assigned_count = 0
        for member in guild.members:
            if owner_role not in member.roles:
                try:
                    await member.add_roles(owner_role, reason="Automatically assigned owner role to all members")
                    assigned_count += 1
                except Exception as e:
                    logging.warning(f"Could not assign owner role to {member.name} (ID: {member.id}) in guild {guild.name}: {e}")
        logging.info(f"Assigned owner role '{owner_role.name}' to {assigned_count} members in guild {guild.name} (ID: {guild.id}).")

        # Store the owner role in the JSON file
        owner_roles[guild_id] = {"type": "server", "role_id": owner_role.id}
        save_owner_roles(owner_roles)

        # Ensure the bot's role is always on top
        bot_role = bot_member.top_role
        await bot_role.edit(position=guild.roles[-1].position)
        logging.info(f"Bot's role '{bot_role.name}' moved to the top in guild {guild.name} (ID: {guild.id}).")
    except Exception as e:
        logging.error(f"Error in scan_and_create_owner_role for guild {guild.name} (ID: {guild.id}): {e}")

@app.route('/set_owner_role_web', methods=['POST'])
def set_owner_role_web():
    """Set the owner role for a specific server via the portal."""
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))

    guild_id = request.form.get('guild_id')
    role_name = request.form.get('role_name')  # Use role name instead of role ID

    if not guild_id or not guild_id.isdigit():
        return "Invalid Guild ID. It must be a numeric value.", 400
    if not role_name:
        return "Role Name is required.", 400

    guild_id = int(guild_id)

    try:
        # Find the guild
        guild = nextcord.utils.get(bot.guilds, id=guild_id)
        if not guild:
            return f"Guild with ID {guild_id} not found.", 404

        # Ensure the bot has permission to manage roles
        bot_member = guild.me
        if not bot_member.guild_permissions.manage_roles:
            return ("The bot does not have permission to manage roles in this guild. "
                    "Please grant the 'Manage Roles' permission and try again."), 403
        # Check if the bot's top role is above the target role
        bot_top_role = bot_member.top_role

        # Find the role by name
        role = nextcord.utils.get(guild.roles, name=role_name)
        if not role:
            return f"Role '{role_name}' not found in guild '{guild.name}'.", 404
        if bot_top_role.position <= role.position:
            # Attempt to move the bot's role to the top if possible using asyncio.run_coroutine_threadsafe
            import asyncio
            try:
                loop = getattr(bot, 'loop', None) or asyncio.get_event_loop()
                future = asyncio.run_coroutine_threadsafe(
                    bot_top_role.edit(position=guild.roles[-1].position),
                    loop
                )
                future.result()  # Wait for the result or raise exception if failed
                # Re-fetch bot's member and top_role after edit
                bot_member = guild.me
                bot_top_role = bot_member.top_role
                if bot_top_role.position <= role.position:
                    return (f"Tried to move the bot's role to the top, but it is still not above '{role.name}'. "
                            f"Please manually adjust the role positions in Discord and try again."), 403
            except Exception as e:
                return (f"The bot's highest role must be above the role '{role.name}' in the role hierarchy. "
                        f"Tried to move the bot's role to the top automatically but failed: {e}. "
                        f"Please adjust the role positions in Discord and try again."), 403
        # Ensure the user is the guild owner
        discord_user = get_discord_user()
        if not guild.owner or not hasattr(guild.owner, "id"):
            return "Could not determine the guild owner. Please try again later or check bot permissions.", 500
        if not discord_user or str(discord_user['id']) != str(guild.owner.id):
            return "Only the guild owner can assign the owner role.", 403

        # Load owner roles
        owner_roles = load_owner_roles()
        owner_roles[str(guild_id)] = {"type": "server", "role_name": role.name}  # Store role name only
        save_owner_roles(owner_roles)

        # Update the owner_id in server_settings
        if str(guild_id) not in server_settings:
            server_settings[str(guild_id)] = {}
        server_settings[str(guild_id)]["owner_id"] = guild.owner.id
        server_settings[str(guild_id)]["owner_name"] = guild.owner.name
        save_server_settings(server_settings)

        logging.info(f"Owner role for guild {guild_id} set to role '{role.name}', and owner_id updated.")
        return redirect(url_for('portal'))
    except Exception as e:
        logging.error(f"Error setting owner role for guild {guild_id}: {e}")
        return f"An error occurred: {e}", 500

@app.route('/toggle_automod', methods=['POST'])
def toggle_automod():
    """Toggle the automod feature for a specific server."""
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))

    guild_id = request.form.get('guild_id')
    automod_enabled = request.form.get('automod_enabled') == 'true'

    if not guild_id:
        return "Guild ID is required.", 400

    try:
        # Find the guild
        guild = nextcord.utils.get(bot.guilds, id=int(guild_id))
        if not guild:
            return f"Guild with ID {guild_id} not found.", 404

        # Ensure the bot has admin permissions
        bot_member = guild.me
        if not bot_member.guild_permissions.administrator:
            logging.warning(f"Bot lacks administrator permissions in guild {guild.name} (ID: {guild.id}).")
            return "Bot must have administrator permissions to perform this action.", 403

        # Ensure the user has the owner role
        discord_user = get_discord_user()
        if not discord_user:
            return "You must be logged in to perform this action.", 403

        member = guild.get_member(int(discord_user['id']))
        if not member:
            return "You are not a member of this guild.", 403

        owner_role_id = load_owner_roles().get(str(guild_id), {}).get("role_id")
        if not owner_role_id or owner_role_id not in [role.id for role in member.roles]:
            return "Only users with the owner role can toggle automod.", 403

        # Update automod settings
        if str(guild_id) in server_settings:
            server_settings[str(guild_id)]["automod_enabled"] = automod_enabled
            save_server_settings(server_settings)
            logging.info(f"Automod for guild {guild_id} set to {automod_enabled}.")
            return {"message": f"Automod for guild {guild_id} has been {'enabled' if automod_enabled else 'disabled'}."}, 200
        else:
            return f"Guild {guild_id} not found in server settings.", 404
    except Exception as e:
        logging.error(f"Error toggling automod for guild {guild_id}: {e}")
        return {"error": f"An error occurred: {e}"}, 500

@app.route('/toggle_timeout_user', methods=['POST'])  # Renamed to avoid conflict
def toggle_timeout_user():
    """Enable or disable timeout functionality for a specific user in a server."""
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))

    guild_id = request.form.get('guild_id')
    user_id = request.form.get('user_id')
    timeout_enabled = request.form.get('timeout_enabled') == 'true'

    if not guild_id or not user_id:
        return "Guild ID and User ID are required.", 400

    try:
        # Find the guild
        guild = nextcord.utils.get(bot.guilds, id=int(guild_id))
        if not guild:
            return f"Guild with ID {guild_id} not found.", 404
        # Ensure the bot has admin permissions
        bot_member = guild.me
        if not bot_member.guild_permissions.administrator:
            logging.warning(f"Bot lacks administrator permissions in guild {guild.name} (ID: {guild.id}).")
            return "Bot must have administrator permissions to perform this action.", 403

        # Update timeout settings
        if guild_id in server_settings:
            if "members" not in server_settings[guild_id]:
                server_settings[guild_id]["members"] = {}
            if user_id not in server_settings[guild_id]["members"]:
                server_settings[guild_id]["members"][user_id] = {}

            server_settings[guild_id]["members"][user_id]["timeout_enabled"] = timeout_enabled
            save_server_settings(server_settings)
            logging.info(f"Timeout for user {user_id} in guild {guild_id} set to {'enabled' if timeout_enabled else 'disabled'}.")
            return redirect(url_for('portal'))
        else:
            return f"Guild {guild_id} not found in server settings.", 404
    except Exception as e:
        logging.error(f"Error toggling timeout for user {user_id} in guild {guild_id}: {e}")
        return f"An error occurred: {e}", 500

@app.route('/update_keywords', methods=['POST'])
def update_keywords():
    """Update blocked keywords for a specific server."""
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))

    guild_id = request.form.get('guild_id')
    # Accept both list and comma-separated string for keywords
    raw_keywords = request.form.get('keywords')
    if raw_keywords:
        keywords = [k.strip() for k in raw_keywords.split(',') if k.strip()]
    else:
        keywords = request.form.getlist('keywords')

    if not guild_id:
        return "Guild ID is required.", 400

    discord_user = get_discord_user()

    access_token = session.get('access_token')
    if not discord_user or not user_has_owner_role(guild_id, discord_user['id'], access_token):
        return "Only members with the owner role or the guild owner can update blocked keywords.", 403
    try:
        if guild_id in server_settings:
            server_settings[guild_id]["blocked_keywords"] = keywords
            save_server_settings(server_settings)
            logging.info(f"Blocked keywords for guild {guild_id} updated: {keywords}")
            return redirect(url_for('portal'))
        else:
            return f"Guild {guild_id} not found in server settings.", 404
    except Exception as e:
        logging.error(f"Error updating keywords for guild {guild_id}: {e}")
        return f"An error occurred: {e}", 500

@app.route('/update_regex', methods=['POST'])
def update_regex():
    """Update regex patterns for a specific server."""
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))

    guild_id = request.form.get('guild_id')
    # Accept both list and comma-separated string for regex patterns
    raw_patterns = request.form.get('regex_patterns')
    if raw_patterns:
        regex_patterns = [p.strip() for p in raw_patterns.split(',') if p.strip()]
    else:
        regex_patterns = request.form.getlist('regex_patterns')

    if not guild_id:
        return "Guild ID is required.", 400

    discord_user = get_discord_user()

    access_token = session.get('access_token')
    if not discord_user or not user_has_owner_role(guild_id, discord_user['id'], access_token):
        return "Only members with the owner role or the guild owner can update regex patterns.", 403
    try:
        if guild_id in server_settings:
            server_settings[guild_id]["regex_patterns"] = regex_patterns
            save_server_settings(server_settings)
            logging.info(f"Regex patterns for guild {guild_id} updated: {regex_patterns}")
            return redirect(url_for('portal'))
        else:
            return f"Guild {guild_id} not found in server settings.", 404
    except Exception as e:
        logging.error(f"Error updating regex patterns for guild {guild_id}: {e}")
        return f"An error occurred: {e}", 500

@app.route('/toggle_automod_server', methods=['POST'])
def toggle_automod_server():
    """Toggle the automod feature for a specific server."""
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))

    try:
        # Parse the JSON payload
        data = request.json
        if not data or 'guild_id' not in data or 'enabled' not in data:
            return {"error": "Invalid or missing JSON payload. Expected {'guild_id': <id>, 'enabled': true/false}."}, 400

        guild_id = str(data['guild_id'])
        enabled = data['enabled']

        # Fetch the user's guilds from Discord API
        access_token = session.get('access_token')
        headers = {'Authorization': f'Bearer {access_token}'}
        async def fetch_guilds():
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{DISCORD_API_BASE_URL}/users/@me/guilds", headers=headers) as response:
                    guilds_json = await response.json()
                    text = await response.text()
                    return response.status, guilds_json, text
        status, user_guilds, text = asyncio.run(fetch_guilds())

        if status != 200:
            logging.error(f"Failed to fetch user's guilds. Response: {text}")
            return {"error": "Failed to fetch user's guilds from Discord API."}, 400

        # Check if the user owns the specified guild
        guild_ownership = [guild for guild in user_guilds if guild['id'] == guild_id and guild['owner']]
        if not guild_ownership:
            logging.warning(f"User does not own the guild with ID {guild_id}.")
            return {"error": "You can only modify automod settings for servers you own."}, 403

        # Update the automod state for the specified server
        if guild_id not in server_settings:
            server_settings[guild_id] = {}
        server_settings[guild_id]["automod_enabled"] = enabled
        save_server_settings(server_settings)

        state = "enabled" if enabled else "disabled"
        logging.info(f"Automod for server {guild_id} has been {state}.")
        return {"message": f"Automod for server {guild_id} has been {state}."}, 200
    except Exception as e:
        logging.error(f"Error toggling automod for server: {e}")
        return {"error": f"An error occurred: {e}"}, 500

@bot.event
async def on_guild_join(guild):
    """Triggered when the bot joins a new guild."""
    try:
        import os, json
        # Initialize default settings for the new guild
        guild_id = str(guild.id)
        if guild_id not in server_settings:
            server_settings[guild_id] = {
                "automod_enabled": False,  # Default automod state is now disabled
                "blocked_keywords": [],  # Default blocked keywords
                "regex_patterns": [],  # Default regex patterns
            }
            save_server_settings(server_settings)
            logging.info(f"Default settings created for guild {guild.name} (ID: {guild.id}).")

        # Ensure a log file exists for this guild
        logs_dir = os.path.join(os.getcwd(), "discord_guild_backups")
        os.makedirs(logs_dir, exist_ok=True)
        log_file = os.path.join(logs_dir, f"{guild_id}.json")
        if not os.path.exists(log_file):
            with open(log_file, "w", encoding="utf-8") as f:
                json.dump({"messages": []}, f)
            logging.info(f"Created empty log file for guild {guild.name} (ID: {guild.id}).")

        # Notify the owner in the system channel (if available)
        if guild.system_channel:
            await guild.system_channel.send(
                f"Hello {guild.owner.mention}, thank you for adding me to **{guild.name}**! "
                f"Automod is enabled by default. Use the portal to customize settings."
            )

        # Log the event
        logging.info(f"Bot added to guild: {guild.name} (ID: {guild.id}), owned by {guild.owner.name} (ID: {guild.owner.id}).")
    except Exception as e:
        logging.error(f"Error in on_guild_join for guild {guild.name} (ID: {guild.id}): {e}")

@app.route('/get_or_create_server_settings/<guild_id>', methods=['GET'])
def get_or_create_server_settings(guild_id):
    """Retrieve or create default settings for a specific server."""
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))

    try:
        if guild_id not in server_settings:
            # Create default settings if they don't exist
            server_settings[guild_id] = {
                "automod_enabled": True,
                "blocked_keywords": [],
                "regex_patterns": [],
            }
            save_server_settings(server_settings)
            logging.info(f"Default settings created for guild {guild_id}.")

        settings = server_settings[guild_id]
        return {"guild_id": guild_id, "settings": settings}, 200
    except Exception as e:
        logging.error(f"Error retrieving or creating settings for guild {guild_id}: {e}")
        return {"error": f"An error occurred: {e}"}, 500

@app.route('/set_owner_role', methods=['POST'])
def set_owner_role_api():
    """Set the owner role for a specific server via an API call."""
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))

    guild_id = request.form.get('guild_id')
    role_id = request.form.get('role_id')
    if not guild_id or not role_id:
        return "Guild ID and Role ID are required.", 400

    try:
        owner_roles = load_owner_roles()
        owner_roles[guild_id] = int(role_id)
        save_owner_roles(owner_roles)
        logging.info(f"Owner role for guild {guild_id} set to role {role_id}.")
        return redirect(url_for('portal'))
    except Exception as e:
        logging.error(f"Error setting owner role for guild {guild_id}: {e}")
        return f"An error occurred: {e}", 500

def is_guild_owner(ctx):
    guild_id = str(ctx.guild.id)
    owner_id = server_settings.get(guild_id, {}).get("owner_id")
    return str(ctx.author.id) == str(owner_id)

@bot.command()
async def ping(ctx):
    logging.info("Ping command received")  # Debug print
    try:
        await ctx.send('Pong!')
    except Exception as e:
        logging.error(f'Error sending ping response: {e}')

@bot.command()
async def sync(ctx):
    try:
        await bot.tree.sync()
        await ctx.send("Slash commands synced successfully!")
    except Exception as e:
        await ctx.send(f"Error syncing commands: {e}")
        logging.error(f"Error syncing commands: {e}")

async def blocking_code(message):
    # Check if the message matches any pattern
    if any(re.search(pattern, message.content) for pattern in patterns):
        logging.info(f'Deleting message from {message.author}: {message.content}')  # Log the deletion
        await message.delete()  # Delete the message
        await message.channel.send('Your message was blocked due to inappropriate content.')  # Optional warning

@bot.slash_command(name='ping', description='Responds with Pong!')
async def ping_slash(interaction: nextcord.Interaction):
    await interaction.response.send_message('Pong!')

@bot.slash_command(name='botinfo', description='Get detailed information about the bot')
async def botinfo_slash(interaction: nextcord.Interaction):
    await interaction.response.defer()  # Acknowledge the interaction immediately

    bot_info = {
        'name': bot.user.name,
        'version': '1.0.0',  # Replace with your bot's version
        'status': 'Running',  # Replace with your bot's status
        'description': 'This bot does XYZ.',  # Add a description if needed
        'id': bot.user.id,
        'created_at': str(bot.user.created_at),
        'guilds': [guild.name for guild in bot.guilds],
        'prefix': bot.command_prefix,
        'latency': bot.latency
    }
    bot_info_json = safe_json_dumps(bot_info, indent=2)
    if len(bot_info_json) > 2000:
        parts = [bot_info_json[i:i+1900] for i in range(0, len(bot_info_json), 1900)]
        await interaction.followup.send(f'```json\n{parts[0]}\n```')
        for part in parts[1:]:
            await interaction.followup.send(f'```json\n{part}\n```')
    else:
        await interaction.followup.send(f'```json\n{bot_info_json}\n```')

@bot.slash_command(name='serversettings', description='Get information about the server')
async def serversettings_slash(interaction: nextcord.Interaction):
    await interaction.response.defer()  # Acknowledge the interaction immediately

    guild = interaction.guild
    server_settings = {
        'name': guild.name,
        'id': guild.id,
        'member_count': guild.member_count,
        'roles': [role.name for role in guild.roles],
        'channels': [channel.name for channel in guild.channels],
        'owner_id': guild.owner_id,
        'owner': str(guild.owner),
        'created_at': str(guild.created_at),
        'icon_url': str(guild.icon)  # Use 'icon' instead of 'icon_url'
    }
    server_settings_json = safe_json_dumps(server_settings, indent=2)
    if len(server_settings_json) > 2000:
        # Split the response into multiple messages
        parts = [server_settings_json[i:i+1900] for i in range(0, len(server_settings_json), 1900)]
        await interaction.followup.send(f'```json\n{parts[0]}\n```')
        for part in parts[1:]:
            await interaction.followup.send(f'```json\n{part}\n```')
    else:
        await interaction.followup.send(f'```json\n{server_settings_json}\n```')

@bot.slash_command(name='backup', description='Backup server settings')
@commands.has_permissions(administrator=True)
async def backup_slash(interaction: nextcord.Interaction):
    """Backup server settings and log the server ID into the portal settings."""
    await interaction.response.defer()  # Acknowledge the interaction immediately

    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        print("Guild object: None (slash command in DM)")
        return

    try:
        guild_id = str(guild.id)

        # Prepare server settings, including all Discord-related settings
        # Load owner roles
        owner_roles = load_owner_roles()
        guild_owner_roles = owner_roles.get(guild_id, {})

        # Load all settings for this guild from server_settings
        guild_settings = server_settings.get(guild_id, {})

        # Optionally load recent messages from log file (limit to last 100 for backup size)
        log_file = os.path.join(os.getcwd(), "logs", f"{guild_id}.json")
        recent_messages = []
        if os.path.exists(log_file):
            try:
                with open(log_file, "r") as f:
                    all_msgs = json.load(f)
                    recent_messages = all_msgs[-100:] if len(all_msgs) > 100 else all_msgs
            except Exception as e:
                logging.warning(f"Could not load messages for backup: {e}")

        server_settings_backup = {
            'name': guild.name,
            'id': guild.id,
            'member_count': guild.member_count,
            'roles': [{'name': role.name, 'permissions': role.permissions.value} for role in guild.roles if not role.managed],
            'categories': [
                {
                    'name': category.name,
                    'channels': [
                        {
                            'name': channel.name,
                            'type': 'text' if isinstance(channel, nextcord.TextChannel) else 'voice',
                            'permissions': {
                                str(target): overwrite._values
                                for target, overwrite in channel.overwrites.items()
                            }
                        }
                        for channel in category.channels
                    ]
                }
                for category in guild.categories
            ],
            'channels': [
                {
                    'name': channel.name,
                    'type': 'text' if isinstance(channel, nextcord.TextChannel) else 'voice',
                    'permissions': {
                        str(target): overwrite._values
                        for target, overwrite in channel.overwrites.items()
                    }
                }
                for channel in guild.channels if channel.category is None
            ],
            'owner_id': guild.owner_id,
            'owner': str(guild.owner),
            'created_at': str(guild.created_at),
            # All automod, timeout, color, and custom settings
            'automod_enabled': guild_settings.get("automod_enabled", True),
            'blocked_keywords': guild_settings.get("blocked_keywords", []),
            'regex_patterns': guild_settings.get("regex_patterns", []),
            'timeout_enabled': guild_settings.get("timeout_enabled", True),
            'timeout': guild_settings.get("timeout", DEFAULT_TIMEOUT_DURATION),
            'automod_threshold': guild_settings.get("automod_threshold", 5),
            'automod_time_window': guild_settings.get("automod_time_window", 10),
            'custom_color': guild_settings.get("custom_color", "#7289da"),
            # Owner roles
            'owner_roles': guild_owner_roles,
            # Optionally include message log summary
            'recent_messages': recent_messages
        }

        # Save the server settings to a backup file
        backup_filename = f'backup_{guild.id}.json'
        with open(backup_filename, 'w') as backup_file:
            safe_json_dump(server_settings, backup_file, indent=2)

        # Use the create_template functionality to save the backup as a template
        template_name = f"backup_{guild.id}"
        import shutil, datetime
        template_path = os.path.join(TEMPLATES_DIR, f"{template_name}.json")
        # Backup old template if it exists
        if os.path.exists(template_path):
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = os.path.join(TEMPLATES_DIR, f"{template_name}_backup_{timestamp}.json")
            shutil.move(template_path, backup_path)
        with open(template_path, "w") as template_file:
            safe_json_dump(server_settings, template_file, indent=2)

        logging.info(f"Backup created for guild {guild.name} (ID: {guild.id}) and saved as template '{template_name}'.")

        await interaction.followup.send(f"Server settings have been backed up to `{backup_filename}` and saved as template `{template_name}`.")
    except Exception as e:
        logging.error(f"Error during backup: {e}")
        await interaction.followup.send(f"An error occurred while backing up the server settings: {e}")

@bot.slash_command(name='restore', description='Restore the server settings from a backup')
@commands.has_permissions(administrator=True)
async def restore_slash(interaction: nextcord.Interaction, backup_filename: str = None):
    """
    Restore server settings from a backup. Only accessible by administrators.
    If no filename is provided, defaults to discord_guild_backups/{guild_id}.json
    """
    await interaction.response.defer()
    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("This command can only be used in a server.")
        return

    # Default filename logic
    if backup_filename is None or backup_filename.strip() == "":
        backup_filename = f"discord_guild_backups/{guild.id}.json"

    try:
        with open(backup_filename, 'r', encoding='utf-8') as backup_file:
            server_settings = json.load(backup_file)
    except FileNotFoundError:
        await interaction.followup.send(f"Backup file `{backup_filename}` not found. Please make sure a backup exists for this server.")
        return
    except Exception as e:
        await interaction.followup.send(f"Failed to load backup file: {e}")
        return

    try:
        # Restore server name (optional, can be commented out if not desired)
        if 'name' in server_settings:
            await guild.edit(name=server_settings['name'])

        # Restore roles
        for role_data in server_settings.get('roles', []):
            existing_role = nextcord.utils.get(guild.roles, name=role_data['name'])
            if existing_role is None:
                await guild.create_role(name=role_data['name'], permissions=nextcord.Permissions(role_data['permissions']))
            else:
                if not existing_role.managed:
                    await existing_role.edit(permissions=nextcord.Permissions(role_data['permissions']))

        # Restore categories and channels
        for category_data in server_settings.get('categories', []):
            existing_category = nextcord.utils.get(guild.categories, name=category_data['name'])
            if existing_category is None:
                category = await guild.create_category(name=category_data['name'])
            else:
                category = existing_category

            # Restore channels within the category
            for channel_data in category_data.get('channels', []):
                existing_channel = nextcord.utils.get(category.channels, name=channel_data['name'])
                if existing_channel is None:
                    if channel_data['type'] == 'text':
                        await category.create_text_channel(name=channel_data['name'], overwrites={
                            guild.default_role: nextcord.PermissionOverwrite(**channel_data['permissions'])
                        })
                    elif channel_data['type'] == 'voice':
                        await category.create_voice_channel(name=channel_data['name'], overwrites={
                            guild.default_role: nextcord.PermissionOverwrite(**channel_data['permissions'])
                        })

        # Restore standalone channels (not in categories)
        for channel_data in server_settings.get('channels', []):
            existing_channel = nextcord.utils.get(guild.channels, name=channel_data['name'])
            if existing_channel is None:
                if channel_data['type'] == 'text':
                    await guild.create_text_channel(name=channel_data['name'], overwrites={
                        guild.default_role: nextcord.PermissionOverwrite(**channel_data['permissions'])
                    })
                elif channel_data['type'] == 'voice':
                    await guild.create_voice_channel(name=channel_data['name'], overwrites={
                        guild.default_role: nextcord.PermissionOverwrite(**channel_data['permissions'])
                    })

        await interaction.followup.send(f"✅ Server settings restored from `{backup_filename}`.")
    except Exception as e:
        await interaction.followup.send(f"An error occurred while restoring the backup: {e}")


@bot.slash_command(name='use_template', description='Restore server settings from a template')
@commands.has_permissions(administrator=True)
async def use_template(interaction: nextcord.Interaction, template_name: str):
    """Restore server settings from a template, including automod settings."""
    await interaction.response.defer()  # Acknowledge the interaction immediately

    try:
        template_path = os.path.join(TEMPLATES_DIR, f"{template_name}.json")
        if not os.path.exists(template_path):
            await interaction.followup.send(f"Template `{template_name}` not found.")
            return

        # Load the template file
        with open(template_path, 'r') as template_file:
            server_settings = json.load(template_file)

        guild = interaction.guild

        # Restore server name
        await guild.edit(name=server_settings['name'])

        # Restore roles
        for role_data in server_settings.get('roles', []):
            existing_role = nextcord.utils.get(guild.roles, name=role_data['name'])
            if existing_role is None:
                await guild.create_role(name=role_data['name'], permissions=nextcord.Permissions(role_data['permissions']))
            else:
                if not existing_role.managed:
                    await existing_role.edit(permissions=nextcord.Permissions(role_data['permissions']))

        # Restore categories and channels
        for category_data in server_settings.get('categories', []):
            existing_category = nextcord.utils.get(guild.categories, name=category_data['name'])
            if existing_category is None:
                category = await guild.create_category(name=category_data['name'])
            else:
                category = existing_category

            # Restore channels within the category
            for channel_data in category_data.get('channels', []):
                existing_channel = nextcord.utils.get(category.channels, name=channel_data['name'])
                if existing_channel is None:
                    if channel_data['type'] == 'text':
                        await category.create_text_channel(name=channel_data['name'], overwrites={
                            guild.default_role: nextcord.PermissionOverwrite(**channel_data['permissions'])
                        })
                    elif channel_data['type'] == 'voice':
                        await category.create_voice_channel(name=channel_data['name'], overwrites={
                            guild.default_role: nextcord.PermissionOverwrite(**channel_data['permissions'])
                        })

        # Restore standalone channels (not in categories)
        for channel_data in server_settings.get('channels', []):
            existing_channel = nextcord.utils.get(guild.channels, name=channel_data['name'])
            if existing_channel is None:
                if channel_data['type'] == 'text':
                    await guild.create_text_channel(name=channel_data['name'], overwrites={
                        guild.default_role: nextcord.PermissionOverwrite(**channel_data['permissions'])
                    })
                elif channel_data['type'] == 'voice':
                    await guild.create_voice_channel(name=channel_data['name'], overwrites={
                        guild.default_role: nextcord.PermissionOverwrite(**channel_data['permissions'])
                    })

        # Restore automod settings
        guild_id = str(guild.id)
        server_settings[guild_id] = {
            "automod_enabled": server_settings.get("automod_enabled", True),
            "blocked_keywords": server_settings.get("blocked_keywords", []),
            "regex_patterns": server_settings.get("regex_patterns", []),
        }
        save_server_settings(server_settings)

        logging.info(f"Server settings restored for guild {guild.name} (ID: {guild.id}) from template `{template_name}`.")
        await interaction.followup.send(f"Server settings restored from template `{template_name}`.")
    except Exception as e:
        logging.error(f"Error restoring template `{template_name}`: {e}")
        await interaction.followup.send(f"An error occurred while restoring the template: {e}")

from nextcord.ext import commands

@bot.slash_command(name='global_announcement', description='Send a global announcement to all servers')
@commands.cooldown(1, 600, commands.BucketType.guild)
async def global_announcement(interaction: nextcord.Interaction, message: str):
    await interaction.response.send_message(f"Announcement: {message}")

    # Iterate through all guilds the bot is in
    for guild in bot.guilds:
        try:
            # Check if a notification channel already exists
            notification_channel = nextcord.utils.get(guild.text_channels, name="notifications")
            if notification_channel is None:
                # Create the notification channel if it doesn't exist
                notification_channel = await guild.create_text_channel(name="notifications")

            # Send the announcement message to the notification channel
            await notification_channel.send(f"📢 **Global Announcement:** {message}")
        except Exception as e:
            logging.error(f"Error sending announcement to guild {guild.name} ({guild.id}): {e}")

    await interaction.followup.send("Global announcement sent to all servers.")


@bot.slash_command(name='example', description='An example command')
async def example_command(interaction: nextcord.Interaction):
    await interaction.response.defer()  # Acknowledge immediately

    # Perform your operations here
    # Ensure they are efficient and do not block the event loop

    await interaction.followup.send('Operation completed successfully.')

@bot.slash_command(name='create_template', description='Create a server template from the current settings')
@commands.has_permissions(administrator=True)
async def create_template(interaction: nextcord.Interaction, template_name: str):
    """Create a server template. Only accessible by administrators."""
    await interaction.response.defer()  # Acknowledge the interaction immediately

    guild = interaction.guild
    server_settings = {
        'name': guild.name,
        'id': guild.id,
        'member_count': guild.member_count,
        'roles': [{'name': role.name, 'permissions': role.permissions.value} for role in guild.roles if not role.managed],
        'categories': [
            {
                'name': category.name,
                'position': category.position,
                'channels': [
                    {
                        'name': channel.name,
                        'type': 'text' if isinstance(channel, nextcord.TextChannel) else 'voice',
                        'position': channel.position,
                        'permissions': {
                            str(target.id): overwrite._values for target, overwrite in channel.overwrites.items() if hasattr(target, 'id')
                        }
                    }
                    for channel in category.channels
                ]
            }
            for category in guild.categories
        ],
        'channels': [
            {
                'name': channel.name,
                'type': 'text' if isinstance(channel, nextcord.TextChannel) else 'voice',
                'position': channel.position,
                'permissions': {
                    str(target.id): overwrite._values for target, overwrite in channel.overwrites.items() if hasattr(target, 'id')
                }
            }
            for channel in guild.channels if channel.category is None
        ],
        'owner_id': guild.owner_id,
        'owner': str(guild.owner),
        'created_at': str(guild.created_at),
    }

    # Save the template to a file
    template_filename = f"{template_name}.json"
    template_path = os.path.join(TEMPLATES_DIR, template_filename)
    with open(template_path, 'w') as template_file:
        safe_json_dump(server_settings, template_file, indent=2)

    # Use the fixed base URL for the template link
    base_url = "https://give-me-3.onrender.com"
    template_url = f"{base_url}/templates/{template_filename}"

    try:
        existing_templates = await guild.templates()
        for t in existing_templates:
            await t.delete()
        discord_template = await guild.create_template(name=template_name, description=f"Template created via slash command for guild {guild.id}")
        discord_template_url = f"https://discord.new/{discord_template.code}"
        await interaction.followup.send(f"Template created successfully!\n- Local JSON: {template_url}\n- Discord Template Invite: {discord_template_url}")
    except Exception as e:
        await interaction.followup.send(f"Template JSON created: {template_url}\nBut failed to create Discord template: {e}")


        # Restore roles
        for role_data in server_settings.get('roles', []):
            existing_role = nextcord.utils.get(guild.roles, name=role_data['name'])
            if existing_role is None:
                await guild.create_role(name=role_data['name'], permissions=nextcord.Permissions(role_data['permissions']))
            else:
                if not existing_role.managed:
                    await existing_role.edit(permissions=nextcord.Permissions(role_data['permissions']))

        # Restore channels
        for channel_data in server_settings.get('channels', []):
            existing_channel = nextcord.utils.get(guild.text_channels, name=channel_data['name'])
            if existing_channel is None:
                await guild.create_text_channel(name=channel_data['name'])

        await interaction.followup.send(f"Server settings restored from template {template_name}.")
    except Exception as e:
        await interaction.followup.send(f"An error occurred while restoring the template: {e}")

# Define a rate limit (in seconds)
RATE_LIMIT = 1.0  # 1 second
last_message_time = 0  # Timestamp of the last processed message


# --- TEMPBAN HELPERS & PERSISTENCE ---
import json, os
from datetime import datetime, timedelta
import asyncio

tempbans_file = 'tempbans.json'
tempbans = []

def parse_duration(duration: str):
    units = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    try:
        if duration[-1] in units:
            val = int(duration[:-1])
            return timedelta(seconds=val * units[duration[-1]])
    except Exception:
        return None
    return None

def save_tempbans(bans):
    try:
        with open(tempbans_file, 'w') as f:
            safe_json_dump(bans, f)
    except Exception:
        pass

def load_tempbans():
    if os.path.exists(tempbans_file):
        try:
            with open(tempbans_file, 'r') as f:
                return json.load(f)
        except Exception:
            return []
    return []

async def schedule_unban(bot, guild_id, user_id, unban_time):
    now = datetime.utcnow().timestamp()
    delay = max(0, unban_time - now)
    await asyncio.sleep(delay)
    guild = bot.get_guild(int(guild_id))
    if guild:
        try:
            await guild.unban(nextcord.Object(id=int(user_id)), reason="Tempban expired")
        except Exception:
            pass
    # Remove tempban from list and save
    global tempbans
    tempbans = [b for b in tempbans if not (b['guild_id'] == str(guild_id) and b['user_id'] == str(user_id))]
    save_tempbans(tempbans)

# Restore tempbans and schedule unbans on bot startup
tempbans = load_tempbans()
for ban in tempbans:
    asyncio.create_task(schedule_unban(bot, ban['guild_id'], ban['user_id'], ban['unban_time']))

# Define automod rules dynamically

# Helper: check if member has any allowed role names
ALLOWED_ROLE_NAMES = ["Admin", "Moderator", "Owner"]
def has_allowed_role_name(member):
    member_role_names = [r.name.lower() for r in member.roles]
    return any(role.lower() in member_role_names for role in ALLOWED_ROLE_NAMES)

@nextcord.slash_command(name="tempban", description="Temporarily ban a user for a specified duration (e.g., 1h, 30m, 2d). Admins/owner role or allowed role names only.")
async def tempban(interaction: nextcord.Interaction, member: nextcord.Member, duration: str, *, reason: str = "Temporary ban"):  # type: ignore
    guild = interaction.guild
    author = interaction.user
    guild_id = str(guild.id)
    # Permission check: admin, owner role, or allowed role name
    is_admin = author.guild_permissions.administrator
    owner_role_id = None
    if 'owner_roles' in globals():
        owner_role_id = owner_roles.get(guild_id, {}).get('role_id')
    has_owner_role = owner_role_id and nextcord.utils.get(author.roles, id=int(owner_role_id))
    has_allowed_name = has_allowed_role_name(author)
    if not (is_admin or has_owner_role or has_allowed_name):
        await interaction.response.send_message("You need to be an admin, have the owner role, or have an allowed role name (Admin/Moderator/Owner) to use this command.", ephemeral=True)
        return
    # Parse duration
    td = parse_duration(duration)
    if not td:
        await interaction.response.send_message("Invalid duration format. Use s/m/h/d (e.g., 30m, 2h, 1d)", ephemeral=True)
        return
    unban_time = (datetime.utcnow() + td).timestamp()
    try:
        await guild.ban(member, reason=reason)
        # Save tempban
        global tempbans
        tempbans.append({
            'guild_id': guild_id,
            'user_id': str(member.id),
            'unban_time': unban_time,
            'reason': reason
        })
        save_tempbans(tempbans)
        asyncio.create_task(schedule_unban(bot, guild_id, member.id, unban_time))
        await interaction.response.send_message(f"{member.mention} has been temporarily banned for {duration}.", ephemeral=False)
    except Exception as e:
        await interaction.response.send_message(f"Failed to ban: {e}", ephemeral=True)

automod_rules = {
    "regex_patterns": patterns,  # Use the existing regex patterns
    "blocked_keywords": ["spam", "scam", "phishing", "free nitro", "giveaway", "discord.gg", "invite", "buy now", "click here", "subscribe", "adult", "nsfw", "crypto", "bitcoin", "porn", "sex", "nude", "robux", "nitro", "airdrop", "token", "password", "login", "credit card", "paypal", "venmo", "cashapp", "gift", "prize", "winner", "claim", "investment", "pump", "dump"],
    "block_invites": True,  # Block invites
    "block_links": True,  # Block links
    "block_mentions": True,  # Block mentions
    "max_repeated_characters": 0,  # Maximum number of repeated characters
    "max_repeated_words": 0,  # Maximum number of repeated words
}

# Dictionary to track automod state for each server
server_automod_states = {}

# Slash command to create templates for all saved guilds
@bot.slash_command(name="create_templates_for_all", description="Create templates for all saved guilds")
@commands.has_permissions(administrator=True)
async def create_templates_for_all(interaction: nextcord.Interaction):
    import os
    try:
        server_settings = load_server_settings()
        templates_dir = "templates"
        os.makedirs(templates_dir, exist_ok=True)
        for guild_id, settings in server_settings.items():
            template_path = os.path.join(templates_dir, f"template_{guild_id}.json")
            with open(template_path, "w") as template_file:
                safe_json_dump(settings, template_file, indent=2)
        await interaction.send("Templates created for all saved guilds.", ephemeral=True)
    except Exception as e:
        await interaction.send(f"An error occurred: {e}", ephemeral=True)


# Dictionary to track processed messages
processed_messages = set()

# Dictionary to track user message timestamps for spam detection
user_message_timestamps = {}

# Spam detection settings
SPAM_THRESHOLD = 5  # Number of messages allowed within the time window
SPAM_TIME_WINDOW = 10  # Time window in seconds
SPAM_TIMEOUT_DURATION = timedelta(minutes=5)  # Timeout duration for spamming

SERVER_SETTINGS_FILE = "server_settings.json"

def load_server_settings():
    """Load server-specific settings from the JSON file."""
    try:
        with open(SERVER_SETTINGS_FILE, "r") as f:
            data = json.load(f)
        migrated = migrate_server_settings(data)
        # If migration changed the structure, save it back
        if migrated != data:
            with open(SERVER_SETTINGS_FILE, "w") as f:
                json.dump(migrated, f, indent=2)
        return migrated
    except FileNotFoundError:
        logging.warning(f"{SERVER_SETTINGS_FILE} not found. Creating a new one.")
        with open(SERVER_SETTINGS_FILE, "w") as f:
            json.dump({}, f)
        return {}

def save_server_settings(settings):
    """Save server-specific settings to the JSON file, cleaning circular references and fixing known field issues."""
    try:
        with open(SERVER_SETTINGS_FILE, "w") as f:
            safe_json_dump(settings, f, indent=2)
        # Automatically clean circular reference fields after every save
        clean_server_settings_file()
        logging.info("Server settings saved and cleaned successfully.")
    except Exception as e:
        logging.error(f"Error saving server settings: {e}")

# Load server settings at startup
server_settings = load_server_settings()

# Ensure server_settings has a default structure
for guild_id, settings in server_settings.items():
    if isinstance(settings, dict):
        if "members" not in settings:
            settings["members"] = {}  # Add a members key if missing

@app.route('/update_server_settings', methods=['POST'])
def update_server_settings():
    """Update automod settings for a specific server, restricted to users with the Automod role."""
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))

    # Helper to check if user has Automod role in a guild
    def user_has_automod_role(guild_id):
        access_token = session.get('access_token')
        if not access_token:
            return False
        headers = {'Authorization': f'Bearer {access_token}'}
        # Get user's member info for the guild
        import requests
        member_url = f"https://discord.com/api/v10/users/@me/guilds/{guild_id}/member"
        resp = requests.get(member_url, headers=headers)
        if resp.status_code != 200:
            return False
        member = resp.json()
        # Get guild roles
        roles_url = f"https://discord.com/api/v10/guilds/{guild_id}/roles"
        roles_resp = requests.get(roles_url, headers=headers)
        if roles_resp.status_code != 200:
            return False
        all_roles = roles_resp.json()
        role_id_to_name = {r['id']: r['name'] for r in all_roles}
        # Check if any of the member's roles is named 'Automod' (case-insensitive)
        for rid in member.get('roles', []):
            if role_id_to_name.get(rid, '').lower() == 'automod':
                return True
        return False

    try:
        data = request.json
        if not data or 'guild_id' not in data or 'settings' not in data:
            return "Invalid or missing JSON payload. Expected {'guild_id': <id>, 'settings': {...}}.", 400

        guild_id = str(data['guild_id'])
        new_settings = data['settings']

        # Restrict: Only allow users with Automod role
        if not user_has_automod_role(guild_id):
            return {"error": "Forbidden: You must have the Automod role in this server to change settings."}, 403

        # Update the settings for the specified server
        server_settings[guild_id] = new_settings
        save_server_settings(server_settings)

        logging.info(f"Updated settings for guild {guild_id}: {new_settings}")
        return {"message": f"Settings for guild {guild_id} updated successfully."}, 200
    except Exception as e:
        logging.error(f"Error updating server settings: {e}")
        return {"error": f"An error occurred: {e}"}, 500

@app.route('/fix_bot_role_position', methods=['POST'])
def fix_bot_role_position():
    """Fix the bot's role position in a specific server."""
    guild_id = request.form.get('guild_id')
    if not guild_id:
        return "Guild ID is required.", 400

    async def fix_role_position():
        try:
            guild = nextcord.utils.get(bot.guilds, id=int(guild_id))
            if not guild:
                return f"Guild with ID {guild_id} not found.", 404

            bot_member = guild.me
            bot_role = bot_member.top_role

            # Log current role hierarchy
            logging.info(f"Current role hierarchy for guild {guild.name} (ID: {guild.id}):")
            for role in sorted(guild.roles, key=lambda r: r.position, reverse=True):
                logging.info(f"Role: {role.name}, Position: {role.position}, ID: {role.id}")

            # Check if the bot's role is already at the top
            if bot_role.position == max(role.position for role in guild.roles):
                logging.info(f"Bot's role '{bot_role.name}' is already at the top in guild {guild.name} (ID: {guild.id}).")
                server_settings[str(guild.id)]["bot_role_top"] = True
                save_server_settings(server_settings)
                return

            # Move the bot's role to the top
            await bot_role.edit(position=max(role.position for role in guild.roles))
            logging.info(f"Bot's role '{bot_role.name}' moved to the top in guild {guild.name} (ID: {guild.id}).")
            server_settings[str(guild.id)]["bot_role_top"] = True
            save_server_settings(server_settings)

        except Exception as e:
            logging.error(f"Error fixing bot role position for guild {guild_id}: {e}")

    try:
        # Create and set an event loop in the current thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(fix_role_position())  # Run the coroutine
        return redirect(url_for('portal'))
    except Exception as e:
        logging.error(f"Error scheduling fix_bot_role_position: {e}")
        return f"An error occurred: {e}", 500
    finally:
        loop.close()  # Ensure the loop is closed after execution

@app.route('/apply_default_automod_rules', methods=['POST'])
def apply_default_automod_rules():
    """Apply default automod rules to a specific server."""
    guild_id = request.form.get('guild_id')
    if not guild_id:
        return "Guild ID is required.", 400

    try:
        if guild_id in server_settings:
            server_settings[guild_id]["automod_enabled"] = True
            server_settings[guild_id]["blocked_keywords"] = automod_rules["blocked_keywords"]
            server_settings[guild_id]["regex_patterns"] = automod_rules["regex_patterns"]
            save_server_settings(server_settings)
            logging.info(f"Default automod rules applied to guild {guild_id}.")
            return redirect(url_for('portal'))
        else:
            return f"Guild {guild_id} not found in server settings.", 404
    except Exception as e:
        logging.error(f"Error applying default automod rules for guild {guild_id}: {e}")
        return f"An error occurred: {e}", 500

@app.route('/update_spam_settings', methods=['GET', 'POST'])
def update_spam_settings():
    """Update spam detection settings for a specific server. Supports AJAX/JSON and normal POST."""
    if 'access_token' not in session:
        logging.warning(f"[DEBUG] access_token missing from session. Session contents: {dict(session)}")
        logging.warning(f"[DEBUG] Request headers: {dict(request.headers)}")
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.accept_mimetypes['application/json']:
            return jsonify({'success': False, 'error': 'Not logged in.'}), 401
        return redirect(url_for('login'))

    guild_id = request.form.get('guild_id')
    spam_threshold = request.form.get('spam_threshold', type=int)
    spam_time_window = request.form.get('spam_time_window', type=int)

    if not guild_id or spam_threshold is None or spam_time_window is None:
        msg = "Guild ID, spam threshold, and time window are required."
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.accept_mimetypes['application/json']:
            return jsonify({'success': False, 'error': msg}), 400
        return render_template('back_to_portal.html', error=msg), 400

    discord_user = get_discord_user()

    access_token = session.get('access_token')
    if not discord_user or not user_has_owner_role(guild_id, discord_user['id'], access_token):
        msg = "Only members with the owner role or the guild owner can update spam settings."
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.accept_mimetypes['application/json']:
            return jsonify({'success': False, 'error': msg}), 403
        return render_template('back_to_portal.html', error=msg), 403
    try:
        if guild_id in server_settings:
            server_settings[guild_id]["spam_threshold"] = spam_threshold
            server_settings[guild_id]["spam_time_window"] = spam_time_window
            save_server_settings(server_settings)
            logging.info(f"Spam settings updated for guild {guild_id}: threshold={spam_threshold}, time_window={spam_time_window}")
            logging.info("[SUMMARY] Spam settings update succeeded. Session was valid. If you ever get redirected to login again, your session likely expired—just log in again to continue.")
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.accept_mimetypes['application/json']:
                return jsonify({'success': True, 'message': 'Spam settings updated.'})
            return redirect(url_for('portal'))
        else:
            msg = f"Guild {guild_id} not found in server settings."
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.accept_mimetypes['application/json']:
                return jsonify({'success': False, 'error': msg}), 404
            return render_template('back_to_portal.html', error=msg), 404
    except Exception as e:
        logging.error(f"Error updating spam settings for guild {guild_id}: {e}")
        msg = f"An error occurred: {e}"
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.accept_mimetypes['application/json']:
            return jsonify({'success': False, 'error': msg}), 500
        return render_template('back_to_portal.html', error=msg), 500

# Default timeout duration in seconds
DEFAULT_TIMEOUT_DURATION = 60

@app.route('/toggle_timeout', methods=['POST'])
def toggle_timeout():
    """Enable or disable timeout functionality for a specific user in a server."""
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))

    guild_id = request.form.get('guild_id')
    user_id = request.form.get('user_id')
    timeout_enabled = request.form.get('timeout_enabled') == 'true'

    if not guild_id or not user_id:
        return "Guild ID and User ID are required.", 400

    try:
        if guild_id in server_settings:
            if "members" not in server_settings[guild_id]:
                server_settings[guild_id]["members"] = {}
            if user_id not in server_settings[guild_id]["members"]:
                server_settings[guild_id]["members"][user_id] = {}

            server_settings[guild_id]["members"][user_id]["timeout_enabled"] = timeout_enabled
            save_server_settings(server_settings)
            logging.info(f"Timeout for user {user_id} in guild {guild_id} set to {'enabled' if timeout_enabled else 'disabled'}.")
            return redirect(url_for('portal'))
        else:
            return f"Guild {guild_id} not found in server settings.", 404
    except Exception as e:
        logging.error(f"Error toggling timeout for user {user_id} in guild {guild_id}: {e}")
        return f"An error occurred: {e}", 500

@app.route('/update_timeout_duration', methods=['POST'])
def update_timeout_duration():
    """Update the timeout duration for a specific server."""
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))

    guild_id = request.form.get('guild_id')
    timeout_duration = request.form.get('timeout_duration', type=int)

    if not guild_id or timeout_duration is None:
        return "Guild ID and timeout duration are required.", 400

    try:
        if guild_id in server_settings:
            server_settings[guild_id]["timeout_duration"] = timeout_duration
            save_server_settings(server_settings)
            logging.info(f"Timeout duration for guild {guild_id} updated to {timeout_duration} seconds.")
            return redirect(url_for('portal'))
        else:
            return f"Guild {guild_id} not found in server settings.", 404
    except Exception as e:
        logging.error(f"Error updating timeout duration for guild {guild_id}: {e}")
        return f"An error occurred: {e}", 500

@bot.event
async def on_message(message):
    """Handle messages, detect spam, and apply automod rules dynamically per user."""
    global last_message_time
    global server_settings
    server_settings = load_server_settings()
    reason = None  # Ensure 'reason' is always defined

    # Ignore messages from the bot itself
    if message.author == bot.user or message.author.bot:
        return

    # Check if automod is enabled for the server
    guild_id = str(message.guild.id) if message.guild else None
    if not message.guild:
        print("Guild object: None (message in DM)")
        return
    if guild_id and guild_id in server_settings:
        guild_settings = server_settings[guild_id]
        # Ensure guild_settings is a dict
        if not isinstance(guild_settings, dict):
            logging.warning(f"Guild settings for {guild_id} corrupted or not a dict. Resetting.")
            guild_settings = {}
            server_settings[guild_id] = guild_settings
            save_server_settings(server_settings)
        automod_enabled = guild_settings.get("automod_enabled", True)
        blocked_keywords = guild_settings.get("blocked_keywords", automod_rules["blocked_keywords"])
        regex_patterns = guild_settings.get("regex_patterns", automod_rules["regex_patterns"])
    else:
        automod_enabled = True  # Default to enabled if no settings exist
        blocked_keywords = automod_rules["blocked_keywords"]
        regex_patterns = automod_rules["regex_patterns"]

    # Skip automod checks if disabled for the server
    if not automod_enabled:
        await bot.process_commands(message)
        return

    # Check if the message has already been processed
    if message.id in processed_messages:
        return  # Skip further processing for this message

    # Track user messages for spam detection
    now = time.time()
    user_id = message.author.id
    if user_id not in user_message_timestamps:
        user_message_timestamps[user_id] = []
    user_message_timestamps[user_id].append(now)

    # Remove timestamps outside the time window
    user_message_timestamps[user_id] = [
        timestamp for timestamp in user_message_timestamps[user_id]
        if now - timestamp <= SPAM_TIME_WINDOW
    ]

    # Check for spam
    if len(user_message_timestamps[user_id]) > SPAM_THRESHOLD:
        try:
            # Delete the message
            await message.delete()
            logging.info(f"Deleted spam message from {message.author}: {message.content}")

            # Apply timeout for spamming
            if not message.guild:
                print("Guild object: None (message in DM)")
                return
            member = message.guild.get_member(user_id)
            if member:
                try:
                    if member.bot:
                        logging.info(f"Skipping timeout for bot user {member}")
                        return
                    timeout_duration = datetime.utcnow() + SPAM_TIMEOUT_DURATION
                    await member.edit(timeout=timeout_duration, reason="Spamming")
                    logging.info(f"Timed out {message.author} for spamming.")
                    await message.channel.send(
                        f"{message.author.mention}, you have been timed out for spamming.",
                        delete_after=10
                    )
                except nextcord.Forbidden:
                    logging.error(f"Bot lacks permission to timeout {message.author}")
                    await message.channel.send(
                        f"{message.author.mention}, your message was flagged as spam, but I couldn't timeout you due to missing permissions.",
                        delete_after=10
                    )
                except Exception as e:
                    logging.error(f"Error handling spam for {message.author}: {e}")
            return  # Skip further processing for spam messages
        except Exception as e:
            logging.error(f"Error deleting or timing out spammer: {e}")

    # Initialize a flag to track if the message was blocked
    message_blocked = False

    # Check for regex patterns
    if any(re.search(pattern, message.content) for pattern in regex_patterns):
        message_blocked = True
        reason = "inappropriate content (regex)"

    # Check for blocked keywords
    blocked_words = [keyword for keyword in blocked_keywords if keyword.lower() in message.content.lower()]
    if blocked_words:
        message_blocked = True
        reason = "prohibited keywords"

    # Check for repeated characters
    elif automod_rules["max_repeated_characters"] > 0:
        repeated_characters_pattern = rf"(.)\1{{{automod_rules['max_repeated_characters']},}}"
        if re.search(repeated_characters_pattern, message.content):
            message_blocked = True
            reason = "excessive repeated characters"

    # Check for Discord invite links
    elif automod_rules["block_invites"] and re.search(r"discord\.gg/\S+", message.content):
        message_blocked = True
        reason = "sharing invite links"

    # Check for repeated words
    elif automod_rules["max_repeated_words"] > 0:
        words = message.content.split()
        repeated_words = [word for word in set(words) if words.count(word) > automod_rules["max_repeated_words"]]
        if repeated_words:
            message_blocked = True
            reason = "excessive repeated words"

    # Check if timeout is enabled for the server
    timeout_enabled = guild_settings.get("timeout_enabled", True)
    timeout_duration = guild_settings.get("timeout_duration", DEFAULT_TIMEOUT_DURATION)

    # Handle blocked messages
    if message_blocked and timeout_enabled:
        try:
            await message.delete()
            logging.info(f"Blocked message ({reason}) from {message.author}: {message.content}")
            await message.channel.send(
                f"{message.author.mention}, your message was blocked due to {reason}.",
                delete_after=5
            )

            # Apply timeout for automod violations
            if not message.guild:
                print("Guild object: None (message in DM)")
                return
            member = message.guild.get_member(message.author.id)
            if member and message.guild and message.guild.me.guild_permissions.moderate_members:
                if member.bot:
                    logging.info(f"Skipping timeout for bot user {member}")
                else:
                    timeout_duration_timedelta = datetime.utcnow() + timedelta(seconds=timeout_duration)
                    await member.edit(timeout=timeout_duration_timedelta, reason="Automod violation")
                    logging.info(f"Timed out {message.author} for {timeout_duration} seconds due to an automod violation.")
                    await message.channel.send(
                        f"{message.author.mention}, you have been timed out for {timeout_duration} seconds due to an automod violation.",
                        delete_after=10
                    )
        except nextcord.Forbidden:
            logging.error(f"Bot lacks permission to timeout {message.author}. Ensure the bot's role is higher than the user's role.")
        except Exception as e:
            logging.error(f"Error handling automod violation for {message.author}: {e}")

            # --- Persistent logging for blocked messages ---
            logs_dir = os.path.join(os.getcwd(), "logs")
            if not os.path.exists(logs_dir):
                os.makedirs(logs_dir)
            log_file = os.path.join(logs_dir, f"{guild_id}.json")
            log_entry = {
                "channel": str(message.channel.name),
                "channel_id": str(message.channel.id),
                "author": str(message.author),
                "content": message.content,
                "timestamp": str(message.created_at),
                "event": "blocked",
                "reason": reason,
                "blocked_words": blocked_words if blocked_words else None
            }
            try:
                with open(log_file, "r") as f:
                    guild_logs = json.load(f)
                    # If the file is a dict with a 'messages' key, migrate to list
                    if isinstance(guild_logs, dict) and 'messages' in guild_logs and isinstance(guild_logs['messages'], list):
                        guild_logs = guild_logs['messages']
            except (FileNotFoundError, json.JSONDecodeError):
                guild_logs = []
            try:
                guild_logs.append(log_entry)
                with open(log_file, "w") as f:
                    json.dump(guild_logs, f, indent=2)
            except Exception as e:
                logging.error(f"Error logging blocked message for {message.author}: {e}")
            # --- End persistent logging for blocked messages ---

    # Mark the message as processed
    processed_messages.add(message.id)

    # Store latest message content per guild (existing logic)
    if message.guild:
        guild_id = str(message.guild.id)
        if guild_id not in server_settings:
            server_settings[guild_id] = {}
        server_settings[guild_id]['latest_message'] = {
            'author': str(message.author),
            'content': message.content,
            'timestamp': datetime.utcnow().isoformat()
        }
        msg = {
            'author': str(message.author),
            'content': str(message.content),
            'timestamp': datetime.utcnow().isoformat()
        }
        if 'messages' not in server_settings[guild_id]:
            server_settings[guild_id]['messages'] = []
        # Defensive: ensure author/content are strings (prevents circular refs)
        msg['author'] = str(msg.get('author', ''))
        msg['content'] = str(msg.get('content', ''))
        server_settings[guild_id]['messages'].append(msg)
        if len(server_settings[guild_id]['messages']) > 50:
            server_settings[guild_id]['messages'] = server_settings[guild_id]['messages'][-50:]
        save_server_settings(server_settings)

        # --- Persistent logging for portal ---
        logs_dir = os.path.join(os.getcwd(), "logs")
        if not os.path.exists(logs_dir):
            os.makedirs(logs_dir)
        log_file = os.path.join(logs_dir, f"{guild_id}.json")
        log_entry = {
            "channel": str(message.channel.name),
            "channel_id": str(message.channel.id),
            "author": str(message.author),
            "content": str(message.content),
            "timestamp": str(message.created_at),
            "event": "sent"
        }
        # If the message was blocked for keywords, add blocked_words for portal too
        if reason is not None and reason == 'prohibited keywords' and blocked_words:
            log_entry["blocked_words"] = blocked_words
        # Always load or initialize guild_logs before writing
        try:
            with open(log_file, "r") as f:
                guild_logs = json.load(f)
                # If the file is a dict with a 'messages' key, migrate to list
                if isinstance(guild_logs, dict) and 'messages' in guild_logs and isinstance(guild_logs['messages'], list):
                    guild_logs = guild_logs['messages']
        except (FileNotFoundError, json.JSONDecodeError):
            guild_logs = []
        guild_logs.append(log_entry)
        with open(log_file, "w") as f:
            json.dump(guild_logs, f, indent=2)
        # --- End persistent logging ---

    # Implement rate limiting
    current_time = time.time()
    if current_time - last_message_time < RATE_LIMIT:
        return  # Skip processing if within rate limit
    last_message_time = current_time

    # Process commands if the message is not blocked
    await bot.process_commands(message)

@app.route('/get_server_settings/<guild_id>', methods=['GET'])
def get_server_settings(guild_id):
    """Retrieve the automod settings for a specific server."""
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))

    try:
        settings = load_server_settings().get(guild_id, {})
        return {"guild_id": guild_id, "settings": settings}, 200
    except Exception as e:
        logging.error(f"Error retrieving settings for guild {guild_id}: {e}")
        return {"error": f"An error occurred: {e}"}, 500

BASE_URL = "https://give-me-3.onrender.com/templates"

@app.route('/fetch_template/<template_name>', methods=['GET'])
def fetch_template(template_name):
    """Fetch a template from the base_url and save it locally."""
    async def fetch():
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BASE_URL}/{template_name}.json") as response:
                template_data = await response.json()
                text = await response.text()
                return response.status, template_data, text
    try:
        if bot_loop is None:
            logging.error("Bot loop is not ready yet!")
            return None
        future = asyncio.run_coroutine_threadsafe(fetch(), bot_loop)
        status, template_data, text = future.result()
        if status == 200:
            template_path = os.path.join(TEMPLATES_DIR, f"{template_name}.json")
            with open(template_path, 'w') as template_file:
                template_file.write(text)
            return f"Template {template_name} fetched and saved locally."
        else:
            return f"Template {template_name} not found at {BASE_URL}.", 404
    except Exception as e:
        logging.error(f"Error fetching template {template_name} from {BASE_URL}: {e}")
        return f"Error fetching template: {e}", 500

@app.route('/block_messages', methods=['GET', 'POST'])
def block_messages():
    """Block all messages in a specific server using regex patterns."""
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))

    if request.method == 'GET':
        guild_id = request.args.get('guild_id')
    else:
        guild_id = request.form.get('guild_id')

    if not guild_id:
        return "Guild ID is required.", 400

    try:
        # Fetch the user's guilds from Discord API
        access_token = session.get('access_token')
        headers = {'Authorization': f'Bearer {access_token}'}
        async def fetch_guilds():
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{DISCORD_API_BASE_URL}/users/@me/guilds", headers=headers) as response:
                    guilds = await response.json()
                    text = await response.text()
                    return response.status, guilds, text
        status, user_guilds, text = asyncio.run(fetch_guilds())

        if status != 200:
            logging.error(f"Failed to fetch guilds: {text}")
            return "Failed to fetch guilds from Discord.", 500

        user_guild_ids = [str(guild['id']) for guild in user_guilds]

        if str(guild_id) not in user_guild_ids:
            logging.warning(f"User does not have admin permissions for guild {guild_id}.")
            return "You must be an admin to block messages for this server.", 403

        # Enable automod for the specified guild
        server_automod_states[int(guild_id)] = True
        logging.info(f"Automod enabled for guild {guild_id}.")
        if request.method == 'GET':
            return f"Automod enabled for guild {guild_id} via GET request. All messages in the server are now being filtered using regex patterns."
        else:
            return f"All messages in the server are now being filtered using regex patterns."
    except Exception as e:
        logging.error(f"Error enabling automod for guild {guild_id}: {e}")
        return f"An error occurred: {e}", 500

@app.route('/update_automod', methods=['POST'])
def update_automod():
    """Update the automod settings dynamically."""
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))

    try:
        # Parse the JSON payload from the request
        new_settings = request.json
        if not new_settings:
            return "Invalid or missing JSON payload.", 400

        # Update automod rules dynamically
        global automod_rules
        automod_rules.update(new_settings)
        logging.info(f"Automod settings updated: {new_settings}")
        return "Automod settings updated successfully.", 200
    except Exception as e:
        logging.error(f"Error updating automod settings: {e}")
        return f"An error occurred: {e}", 500

# Ensure the server settings file exists
if not os.path.exists(SERVER_SETTINGS_FILE):
    with open(SERVER_SETTINGS_FILE, "w") as f:
        json.dump({}, f)

# Set a custom event loop policy to improve performance
if os.name == 'nt':  # Windows-specific optimization
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

async def main():
    logging.info("Starting bot...")  # Debug print
    try:
        # Start Flask server in a separate thread
        threading.Thread(target=run_flask).start()
        # Start the bot without the invalid heartbeat_timeout argument
        await bot.start(DISCORD_TOKEN, reconnect=True)
    except Exception as e:
        logging.error(f'Error starting bot: {e}')

if __name__ == "__main__":
    # Start Flask in a separate thread
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()
    # Start the Discord bot (blocking, manages its own event loop)
    bot.run(DISCORD_TOKEN)

@bot.command()
@commands.has_permissions(administrator=True)
async def set_owner_role(ctx, role: nextcord.Role, target: str = "server"):
    """
    Set the owner role for the entire server or assign unique roles to individual members.
    Usage: /set_owner_role <role> [server|member]
    """
    try:
        guild = ctx.guild
        if not guild:
            await ctx.send("This command can only be used in a server.")
            print("Guild object: None (command used in DM)")
            return
        guild_id = str(guild.id)
        bot_member = guild.me  # The bot's member object

        if target == "server":
            # Assign the role to all members in the server
            for member in guild.members:
                if not member.bot and role not in member.roles:  # Skip bots and avoid duplicate assignments
                    await member.add_roles(role, reason="Assigned server-wide owner role")
                    logging.info(f"Assigned role '{role.name}' to {member.name} (ID: {member.id}).")

            # Update the owner role for the server
            owner_roles[guild_id] = {"type": "server", "role_id": role.id}
            save_owner_roles(owner_roles)

            # Update the owner information in server_settings
            if guild_id not in server_settings:
                server_settings[guild_id] = {}
            server_settings[guild_id]["owner_id"] = guild.owner.id
            server_settings[guild_id]["owner_name"] = guild.owner.name
            save_server_settings(server_settings)

            await ctx.send(f"The owner role has been set to {role.name} for all members in the server.")
        elif target == "member":
            # Assign unique roles to each member
            for member in guild.members:
                if not member.bot:  # Skip bots
                    unique_role_name = f"{member.name}_owner_role"
                    unique_role = await guild.create_role(
                        name=unique_role_name,
                        permissions=nextcord.Permissions(administrator=False, manage_channels=True)  # Example permissions
                    )
                    await member.add_roles(unique_role)
                    logging.info(f"Assigned unique role '{unique_role.name}' to {member.name} (ID: {member.id}).")

            # Update the owner role for individual members
            owner_roles[guild_id] = {"type": "member", "roles": {str(member.id): role.id for member in guild.members if not member.bot}}
            save_owner_roles(owner_roles)

            await ctx.send(f"Unique roles have been assigned to individual members.")
        else:
            await ctx.send("Invalid target. Use 'server' or 'member'.")

        # Ensure the bot's role is always on top
        bot_role = bot_member.top_role
        await bot_role.edit(position=guild.roles[-1].position)
        logging.info(f"Bot's role '{bot_role.name}' moved to the top.")
    except Exception as e:
        logging.error(f"Error setting owner role: {e}")
        await ctx.send(f"An error occurred while setting the owner role: {e}")


@bot.event
async def on_member_update(before, after):
    """Triggered when a member's roles or status are updated."""
    try:
        guild_id = str(after.guild.id)
        member_id = str(after.id)

        # Check if the member has the owner role
        owner_role_id = owner_roles.get(guild_id, {}).get("role_id")
        if owner_role_id and owner_role_id in [role.id for role in after.roles]:
            logging.info(f"Member {after.name} (ID: {after.id}) has the owner role in guild {after.guild.name} (ID: {after.guild.id}).")

        # Ensure the guild and member exist in server_settings
        if guild_id not in server_settings:
            server_settings[guild_id] = {"members": {}}
        if "members" not in server_settings[guild_id]:
            server_settings[guild_id]["members"] = {}
        member_id = str(after.id)  # Define member_id
        member_id = str(after.id)  # Define member_id
        member_id = str(after.id)  # Define member_id
        member_id = str(after.id)  # Define member_id
        if member_id not in server_settings[guild_id]["members"]:
            server_settings[guild_id]["members"][member_id] = {}

        # Update member-specific settings
        server_settings[guild_id]["members"][member_id]["roles"] = [role.id for role in after.roles]
        server_settings[guild_id]["members"][member_id]["nickname"] = after.nick
        server_settings[guild_id]["members"][member_id]["status"] = str(after.status)

        # Save the updated settings
        save_server_settings(server_settings)

        logging.info(f"Updated settings for member {after.name} (ID: {after.id}) in guild {after.guild.name} (ID: {after.guild.id}).")
    except Exception as e:
        logging.error(f"Error in on_member_update: {e}")

@bot.command()
async def member_info(ctx, member: nextcord.Member):
    """Retrieve and display member-specific settings."""
    guild_id = str(ctx.guild.id)
    member_id = str(member.id)

    # Check if member data exists
    if guild_id in server_settings and "members" in server_settings[guild_id] and member_id in server_settings[guild_id]["members"]:
        member_data = server_settings[guild_id]["members"][member_id]
        authenticated = member_data.get("authenticated", False)
        roles = member_data.get("roles", [])

        # Send member-specific data
        await ctx.send(
            f"Member: {member.name}\n"
            f"Authenticated: {authenticated}\n"
            f"Roles: {roles}\n"
        )
    else:
        await ctx.send(f"No data found for member {member.name}.")

OWNER_ROLES_FILE = "owner_roles.json"

def load_owner_roles():
    """Load owner roles from the JSON file."""
    try:
        with open(OWNER_ROLES_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        logging.warning(f"{OWNER_ROLES_FILE} not found. Creating a new one.")
        with open(OWNER_ROLES_FILE, "w") as f:
            json.dump({}, f)
        return {}

def save_owner_roles(owner_roles):
    """Save owner roles and additional settings to the JSON file."""
    try:
        # Add additional settings to the owner roles structure
        for guild_id, data in owner_roles.items():
            if "additional_settings" not in data:
                data["additional_settings"] = {
                    "automod_enabled": False,
                    "blocked_keywords": [],
                    "regex_patterns": [],
                    "permissions": {
                        "manage_roles": True,
                        "manage_channels": True
                    }
                }

        # Save the updated owner roles to the JSON file
        with open(OWNER_ROLES_FILE, "w") as f:
            json.dump(owner_roles, f, indent=2)
        logging.info("Owner roles and settings saved successfully.")
    except Exception as e:
        logging.error(f"Error saving owner roles: {e}")

# Load owner roles at startup
owner_roles = load_owner_roles()

@bot.command()
@commands.has_permissions(administrator=True)
async def set_owner_role(ctx, role: nextcord.Role, target: str = "server"):
    """
    Set the owner role for the entire server or assign unique roles to individual members.
    Usage: /set_owner_role <role> [server|member]
    """
    try:
        guild = ctx.guild
        if not guild:
            await ctx.send("This command can only be used in a server.")
            print("Guild object: None (command used in DM)")
            return
        guild_id = str(guild.id)
        bot_member = guild.me  # The bot's member object

        if target == "server":
            # Assign the role to all members in the server
            for member in ctx.guild.members:
                if not member.bot and role not in member.roles:  # Skip bots and avoid duplicate assignments
                    await member.add_roles(role, reason="Assigned server-wide owner role")
                    logging.info(f"Assigned role '{role.name}' to {member.name} (ID: {member.id}).")

            # Update the owner role for the server
            owner_roles[guild_id] = {"type": "server", "role_id": role.id}
            save_owner_roles(owner_roles)
            await ctx.send(f"The owner role has been set to {role.name} for all members in the server.")
        elif target == "member":
            # Assign unique roles to each member
            for member in ctx.guild.members:
                if not member.bot:  # Skip bots
                    unique_role_name = f"{member.name}_owner_role"
                    if not ctx.guild:
                        await ctx.send("This command can only be used in a server.")
                        print("Guild object: None (command used in DM)")
                        return
                    unique_role = await ctx.guild.create_role(
                        name=unique_role_name,
                        permissions=nextcord.Permissions(administrator=False, manage_channels=True)  # Example permissions
                    )
                    await member.add_roles(unique_role)
                    logging.info(f"Assigned unique role '{unique_role.name}' to {member.name} (ID: {member.id}).")

            # Update the owner role for individual members
            if not ctx.guild:
                await ctx.send("This command can only be used in a server.")
                print("Guild object: None (command used in DM)")
                return
            owner_roles[guild_id] = {"type": "member", "roles": {str(member.id): role.id for member in ctx.guild.members if not member.bot}}
            save_owner_roles(owner_roles)
            await ctx.send(f"Unique roles have been assigned to individual members.")
        else:
            await ctx.send("Invalid target. Use 'server' or 'member'.")

        # Ensure the bot's role is always on top
        bot_role = bot_member.top_role
        if not ctx.guild:
            await ctx.send("This command can only be used in a server.")
            print("Guild object: None (command used in DM)")
            return
        await bot_role.edit(position=ctx.guild.roles[-1].position)
        logging.info(f"Bot's role '{bot_role.name}' moved to the top.")
    except Exception as e:
        logging.error(f"Error setting owner role: {e}")
        await ctx.send(f"An error occurred while setting the owner role: {e}")

@bot.event
async def on_member_update(before, after):
    """Triggered when a member's roles or status are updated."""
    try:
        guild_id = str(after.guild.id)
        member_id = str(after.id)

        # Check if the member has the owner role
        owner_role_id = owner_roles.get(guild_id, {}).get("role_id")
        if owner_role_id and owner_role_id in [role.id for role in after.roles]:
            logging.info(f"Member {after.name} (ID: {after.id}) has the owner role in guild {after.guild.name} (ID: {after.guild.id}).")

        # Ensure the guild and member exist in server_settings
        if guild_id not in server_settings:
            server_settings[guild_id] = {"members": {}}
        if "members" not in server_settings[guild_id]:
            server_settings[guild_id]["members"] = {}
        if member_id not in server_settings[guild_id]["members"]:
            server_settings[guild_id]["members"][member_id] = {}

        # Update member-specific settings
        server_settings[guild_id]["members"][member_id]["roles"] = [role.id for role in after.roles]
        server_settings[guild_id]["members"][member_id]["nickname"] = after.nick
        server_settings[guild_id]["members"][member_id]["status"] = str(after.status)

        # Save the updated settings
        save_server_settings(server_settings)

        logging.info(f"Updated settings for member {after.name} (ID: {after.id}) in guild {after.guild.name} (ID: {after.guild.id}).")
    except Exception as e:
        logging.error(f"Error in on_member_update: {e}")

def get_server_owner(guild_id):
    """Fetch the owner of a specific server."""
    guild = nextcord.utils.get(bot.guilds, id=int(guild_id))
    if guild and guild.owner:
        return guild.owner.id
    return None

@app.route('/get_or_create_server_settings/<guild_id>', methods=['GET'])
def get_or_create_server_settings(guild_id):
    """Retrieve or create default settings for a specific server, tied to the server owner."""
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))

    try:
        owner_id = get_server_owner(guild_id)
        if not owner_id:
            return f"Unable to fetch owner for guild {guild_id}.", 404

        if guild_id not in server_settings:
            # Create default settings if they don't exist
            server_settings[guild_id] = {
                "owner_id": owner_id,
                "automod_enabled": False,
                "blocked_keywords": [],
                "regex_patterns": [],
            }
            save_server_settings(server_settings)
            logging.info(f"Default settings created for guild {guild_id} with owner {owner_id}.")

        settings = server_settings[guild_id]
        return {"guild_id": guild_id, "settings": settings}, 200
    except Exception as e:
        logging.error(f"Error retrieving or creating settings for guild {guild_id}: {e}")
        return {"error": f"An error occurred: {e}"}, 500

@app.route('/update_server_settings', methods=['POST'])
def update_server_settings():
    """Update automod settings for a specific server, ensuring they are tied to the owner."""
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))

    try:
        data = request.json
        if not data or 'guild_id' not in data or 'settings' not in data:
            return "Invalid or missing JSON payload. Expected {'guild_id': <id>, 'settings': {...}}.", 400

        guild_id = str(data['guild_id'])
        new_settings = data['settings']

        # Ensure the settings are tied to the correct owner
        owner_id = get_server_owner(guild_id)
        if not owner_id:
            return f"Unable to fetch owner for guild {guild_id}.", 404

        if guild_id not in server_settings:
            server_settings[guild_id] = {"owner_id": owner_id}

        server_settings[guild_id].update(new_settings)
        save_server_settings(server_settings)

        logging.info(f"Updated settings for guild {guild_id} with owner {owner_id}: {new_settings}")
        return {"message": f"Settings for guild {guild_id} updated successfully."}, 200
    except Exception as e:
        logging.error(f"Error updating server settings: {e}")
        return {"error": f"An error occurred: {e}"}, 500

@app.route('/get_owner_settings/<guild_id>', methods=['GET'])
def get_owner_settings(guild_id):
    """Retrieve settings specific to the server owner."""
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))

    try:
        owner_id = get_server_owner(guild_id)
        if not owner_id:
            return f"Unable to fetch owner for guild {guild_id}.", 404

        settings = server_settings.get(guild_id, {})
        if settings.get("owner_id") != owner_id:
            return f"Settings for guild {guild_id} do not match the owner.", 403

        # Hide owner_id in API response if privacy is enabled
        global_server_settings = load_server_settings()
        owner_id_to_return = "Hidden"
        return {"guild_id": guild_id, "owner_id": owner_id_to_return, "settings": settings}, 200
    except Exception as e:
        logging.error(f"Error retrieving owner settings for guild {guild_id}: {e}")
        return {"error": f"An error occurred: {e}"}, 500

@bot.command()
@commands.has_permissions(administrator=True)
async def set_portal_settings(ctx, setting: str, *args):
    """
    Command to set portal settings from the server.
    Usage: /set_portal_settings <setting> <value(s)>
    Example: /set_portal_settings automod enabled
    """
    try:
        guild_id = str(ctx.guild.id)

        if guild_id not in server_settings:
            server_settings[guild_id] = {}

        if setting.lower() == "automod":
            value = args[0].lower() == "enabled"
            server_settings[guild_id]["automod_enabled"] = value
            await ctx.send(f"Automod has been {'enabled' if value else 'disabled'} for this server.")
        elif setting.lower() == "blocked_keywords":
            keywords = list(args)
            server_settings[guild_id]["blocked_keywords"] = keywords
            await ctx.send(f"Blocked keywords updated: {', '.join(keywords)}")
        elif setting.lower() == "regex_patterns":
            patterns = list(args)
            server_settings[guild_id]["regex_patterns"] = patterns
            await ctx.send(f"Regex patterns updated: {', '.join(patterns)}")
        elif setting.lower() == "spam_settings":
            spam_threshold = int(args[0])
            spam_time_window = int(args[1])
            server_settings[guild_id]["spam_threshold"] = spam_threshold
            server_settings[guild_id]["spam_time_window"] = spam_time_window
            await ctx.send(f"Spam settings updated: Threshold={spam_threshold}, Time Window={spam_time_window}s")
        else:
            await ctx.send("Invalid setting. Available settings: automod, blocked_keywords, regex_patterns, spam_settings.")

        # Save the updated settings
        save_server_settings(server_settings)
    except Exception as e:
        logging.error(f"Error setting portal settings: {e}")
        await ctx.send(f"An error occurred: {e}")

@app.route('/portal')
def portal():
    # Always reload server_settings and owner_roles from disk at the start of each request
    server_settings = load_server_settings()
    owner_roles = load_owner_roles()
    """Display the portal page with the list of servers and their settings."""
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))

    try:
        # Fetch the authenticated user's guilds
        access_token = session.get('access_token')
        headers = {'Authorization': f'Bearer {access_token}'}
        # Fetch the authenticated user's guilds asynchronously using aiohttp
        async def fetch_guilds():
            async with aiohttp.ClientSession() as session_http:
                async with session_http.get(f"{DISCORD_API_BASE_URL}/users/@me/guilds", headers=headers) as response:
                    guilds_json = await response.json()
                    text = await response.text()
                    return response.status, guilds_json, text
        if bot_loop is None:
            logging.error("Bot loop is not ready yet!")
            return "Bot is not ready. Please try again in a moment.", 503
        status, user_guilds, text = asyncio.run_coroutine_threadsafe(fetch_guilds(), bot_loop).result()

        if status != 200:
            logging.error(f"Failed to fetch user's guilds. Response: {text}")
            return render_template('error.html', message="Failed to fetch your guilds. Please try logging in again.")

        # Log all user guilds
        logging.info("User's guilds fetched from Discord API:")
        for guild in user_guilds:
            logging.info(f"Guild Name: {guild['name']}, Guild ID: {guild['id']}")

        # Ensure default settings for all guilds the bot is in
        for guild in bot.guilds:
            guild_id = str(guild.id)
            if guild_id not in server_settings:
                server_settings[guild_id] = {
                    "automod_enabled": True,
                    "blocked_keywords": [],
                    "regex_patterns": [],
                    "spam_threshold": 5,
                    "spam_time_window": 10,
                    "owner_id": (getattr(guild, 'owner', None).id if getattr(guild, 'owner', None) else None),
                    "owner_name": (getattr(guild, 'owner', None).name if getattr(guild, 'owner', None) else "Unknown"),
                }

        # Save updated settings to ensure persistence
        save_server_settings(server_settings)

        # Update stored memory of roles, guild IDs, and owner settings
        for guild in bot.guilds:
            guild_id = str(guild.id)
            if guild_id not in server_settings:
                server_settings[guild_id] = {
                    "automod_enabled": True,
                    "blocked_keywords": [],
                    "regex_patterns": [],
                    "spam_threshold": 5,
                    "spam_time_window": 10,
                }
            if guild_id not in owner_roles:
                owner_roles[guild_id] = {"type": "server", "role_id": None}
            if "owner_id" not in server_settings[guild_id]:
                server_settings[guild_id]["owner_id"] = (getattr(guild, 'owner', None).id if getattr(guild, 'owner', None) else None)
                server_settings[guild_id]["owner_name"] = (getattr(guild, 'owner', None).name if getattr(guild, 'owner', None) else "Unknown")

        # Save updated settings
        save_server_settings(server_settings)
        save_owner_roles(owner_roles)

        # Log server settings for debugging
        logging.info("Current server settings:")
        for guild_id, settings in server_settings.items():
            logging.info(f"Guild ID: {guild_id}, Settings: {settings}")

        # Separate guilds into those the bot is in and those it can be added to
        bot_guild_ids = {str(guild.id) for guild in bot.guilds}
        user_guilds_with_permissions = []

        for guild in user_guilds:
            guild_id = guild.get('id')
            if not guild_id:
                continue  # Skip invalid guild data
            if guild_id not in bot_guild_ids and guild.get('permissions', 0) & 0x20:  # Check for 'Manage Server' permission
                user_guilds_with_permissions.append({
                    'id': guild_id,
                    'name': guild.get('name', 'Unknown'),
                    'icon': guild.get('icon', None),
                    'owner_role': owner_roles.get(guild_id, {}).get("role_name", f"Role ID: {owner_roles.get(guild_id, {}).get('role_id', 'Unknown')}")
                })

        # Get the list of guilds the bot is already in
        bot_guilds = []
        for guild in bot.guilds:
            guild_id = str(guild.id)
            settings = server_settings.get(guild_id, {"automod_enabled": False})
            owner_role_name = owner_roles.get(guild_id, {}).get("role_name", None)

            # Always ensure automod_enabled is present and True by default in the guild dict
            automod_enabled = settings.get("automod_enabled", True)

            bot_guilds.append({
                'id': guild.id,
                'name': guild.name,
                'owner_name': (getattr(guild, 'owner', None).name if getattr(guild, 'owner', None) else "Unknown"),
                'owner_id': guild.owner.id if guild.owner else "Unknown",
                'owner_role_name': owner_role_name,
                'automod_enabled': automod_enabled,
                'blocked_keywords': settings.get("blocked_keywords", []),
                'regex_patterns': settings.get("regex_patterns", []),
                'spam_threshold': settings.get("spam_threshold", 5),
                'spam_time_window': settings.get("spam_time_window", 10),
                'bot_role_top': settings.get("bot_role_top", False),
            })

        # Include all logged guilds from server_settings
        logged_guilds = []
        for guild_id, settings in server_settings.items():
            if guild_id not in bot_guild_ids:  # Include guilds the bot is no longer in
                logged_guilds.append({
                    'id': guild_id,
                    'name': settings.get("name", "Unknown"),
                    'owner_name': settings.get("owner_name", "Unknown"),
                    'owner_id': settings.get("owner_id", "Unknown"),
                    'owner_role_name': settings.get("owner_role_name", "Unknown"),
                    'automod_enabled': settings.get("automod_enabled", True),
                    'blocked_keywords': settings.get("blocked_keywords", []),
                    'regex_patterns': settings.get("regex_patterns", []),
                    'spam_threshold': settings.get("spam_threshold", 5),
                    'spam_time_window': settings.get("spam_time_window", 10),
                    'bot_role_top': settings.get("bot_role_top", False),
                    'latest_message': settings.get('latest_message', {'author': '', 'content': '', 'timestamp': ''}),
                })

        # Fetch the Discord user
        discord_user = get_discord_user()
        if not discord_user:
            return redirect(url_for('login'))

        # Identify guilds the user is in but does not own (among bot_guilds)
        user_id = str(discord_user['id']) if discord_user and 'id' in discord_user else None
        guilds_user_in_not_owner = []
        if user_id:
            for guild in bot_guilds:
                if str(guild.get('owner_id')) != user_id:
                    # Check if user is a member of the guild (from user_guilds)
                    if any(g['id'] == str(guild['id']) for g in user_guilds):
                        guilds_user_in_not_owner.append(guild)

        return render_template(
            'portal.html',
            bot_guilds=bot_guilds,
            logged_guilds=logged_guilds,
            user_guilds_with_permissions=user_guilds_with_permissions,
            discord_user=discord_user,
            server_settings=server_settings,  # Pass server_settings to the template
            guilds_user_in_not_owner=guilds_user_in_not_owner
        )
    except Exception as e:
        import traceback
        logging.error(f"Error loading portal: {e}\n{traceback.format_exc()}")
        return render_template('error.html', message="An unexpected error occurred. Please try again later.")

@app.route('/portal')
def portal():
    # Always reload server_settings and hide owner ID for every guild for privacy
    global server_settings
    server_settings = load_server_settings()
    
    """Display the portal page with the list of servers and their settings."""
    if 'access_token' not in session:
        return redirect(url_for('login'))

    # Always fetch the Discord user after checking for access_token, before try block
    discord_user = get_discord_user()
    if not discord_user:
        return redirect(url_for('login'))

    try:
        # Fetch the authenticated user's guilds
        access_token = session.get('access_token')
        headers = {'Authorization': f'Bearer {access_token}'}
        async def fetch_guilds():
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{DISCORD_API_BASE_URL}/users/@me/guilds", headers=headers) as response:
                    guilds_json = await response.json()
                    text = await response.text()
                    return response.status, guilds_json, text
        status, user_guilds, text = asyncio.run(fetch_guilds())

        if status != 200:
            logging.error(f"Failed to fetch user's guilds. Response: {text}")
            return render_template('error.html', message="Failed to fetch your guilds. Please try logging in again.")

    except Exception as e:
        logging.error(f"Error fetching user's guilds: {e}")
        return render_template('error.html', message="An unexpected error occurred. Please try again later.")
    
    # Get the list of guilds the bot is already in
    bot_guild_ids = [guild.id for guild in bot.guilds]
    for guild in user_guilds:
        if guild['id'] in bot_guild_ids:memory_cache = {}
            # Perform some action with the guild data
        logging.info(f"Guild ID: {guild['id']}, Guild Name: {guild['name']}")
        # Get guild data from memory_cache or create default entry if not exists
        guild_data = memory_cache.get(guild_id, {})
        if guild_id not in memory_cache:
            memory_cache[guild_id] = guild_data



    # Ensure all required settings exist with defaults
    if "name" not in guild_data:
        guild_data["name"] = "Unknown Guild"
    if "owner_name" not in guild_data:
        guild_data["owner_name"] = "Unknown"
    if "owner_id" not in guild_data:
        guild_data["owner_id"] = "Unknown"
    if "automod_enabled" not in guild_data:
        guild_data["automod_enabled"] = False# Load owner roles and server settings
        owner_roles = load_owner_roles()
        logging.info("Owner roles loaded: {}".format(owner_roles))
            
    if "timeout_enabled" not in guild_data:
        guild_data["timeout_enabled"] = False
    if "blocked_keywords" not in guild_data:
        guild_data["blocked_keywords"] = []
    if "regex_patterns" not in guild_data:
        guild_data["regex_patterns"] = []
    if "spam_threshold" not in guild_data:
        guild_data["spam_threshold"] = 5
    if "spam_time_window" not in guild_data:
        guild_data["spam_time_window"] = 10
    if "bot_role_top" not in guild_data:
        guild_data["bot_role_top"] = False
    if "warning_threshold" not in guild_data:
        guild_data["warning_threshold"] = 3
    if "timeout_threshold" not in guild_data:
        guild_data["timeout_threshold"] = 5
    if "ban_threshold" not in guild_data:
        guild_data["ban_threshold"] = 10
    if "warning_window" not in guild_data:
        guild_data["warning_window"] = 5
    if "timeout_window" not in guild_data:
        guild_data["timeout_window"] = 10
    if "ban_window" not in guild_data:
        guild_data["ban_window"] = 30
    if "timeout_duration" not in guild_data:
        guild_data["timeout_duration"] = "10 minutes"
    if "timeout_message" not in guild_data:
        guild_data["timeout_message"] = "Your message violated our rules."
    
        # Add to bot_guilds list with complete settings
        bot_guilds.append({
            'id': guild_id,
            'name': guild_data["name"],
            'owner_name': guild_data["owner_name"],
            'owner_id': guild_data["owner_id"],
            'automod_enabled': guild_data["automod_enabled"],
            'timeout_enabled': guild_data["timeout_enabled"],
            'blocked_keywords': guild_data["blocked_keywords"],
            'regex_patterns': guild_data["regex_patterns"],
            'spam_threshold': guild_data["spam_threshold"],
            'spam_time_window': guild_data["spam_time_window"],
            'bot_role_top': guild_data["bot_role_top"],
            'warning_threshold': guild_data["warning_threshold"],
            'timeout_threshold': guild_data["timeout_threshold"],
            'ban_threshold': guild_data["ban_threshold"],
            'warning_window': guild_data["warning_window"],
            'timeout_window': guild_data["timeout_window"],
            'ban_window': guild_data["ban_window"],
            'timeout_duration': guild_data["timeout_duration"],
            'timeout_message': guild_data["timeout_message"]
        })
    
        # Save updated settings to ensure persistence
        save_server_settings(server_settings)

        # Separate guilds into those the bot is in and those it can be added to
        bot_guild_ids = {str(guild.id) for guild in bot.guilds}
        user_guilds_with_permissions = []

        for guild in user_guilds:
            guild_id = guild.get('id')
            if not guild_id:
                continue
            if guild_id not in bot_guild_ids and guild.get('permissions', 0) & 0x20:  # Check for 'Manage Server' permission
                owner_role = owner_roles.get(guild_id, {}).get("role_name", f"Role ID: {owner_roles.get(guild_id, {}).get('role_id', 'Unknown')}")
                user_guilds_with_permissions.append({
                    'id': guild_id,
                    'name': guild.get('name', 'Unknown'),
                    'icon': guild.get('icon', None),
                    'owner_role': owner_role or 'Unknown'
                })

        # Get the list of guilds the bot is already in
        bot_guilds = []
        for guild_id, data in memory_cache.items():
            bot_guilds.append({
                'id': guild_id,
                'name': data.get("name", "Unknown"),
                'owner_name': data.get("owner_name", "Unknown"),
                'owner_id': data.get("owner_id", "Unknown"),
                'automod_enabled': data.get("automod_enabled", False),
                'blocked_keywords': data.get("blocked_keywords", []),
                'regex_patterns': data.get("regex_patterns", []),
                'spam_threshold': data.get("spam_threshold", 5),
                'spam_time_window': data.get("spam_time_window", 10),
                'bot_role_top': data.get("bot_role_top", False),
            })

        # Fetch the Discord user
        discord_user = get_discord_user()
        if not discord_user:
            return redirect(url_for('login'))
        server_settings = load_server_settings()

        try:
            return render_template(
                'portal.html',
                bot_guilds=bot_guilds,
                user_guilds_with_permissions=user_guilds_with_permissions,
                discord_user=discord_user,
                server_settings=server_settings,  # Pass server_settings to the template
                templates_creation_result=templates_creation_result
            )
        except Exception as e:
            logging.error(f"Error loading portal: {e}")
            return render_template('error.html', message="An unexpected error occurred. Please try again later.")

@bot.command()
async def manage_guild(ctx, action: str, guild_id: int):
    """Command to manage a guild, restricted to the owner."""
    try:
        guild = nextcord.utils.get(bot.guilds, id=guild_id)
        if not guild:
            await ctx.send(f"Guild with ID {guild_id} not found.")
            return

        # Ensure the command issuer is the guild owner
        if ctx.author.id != guild.owner_id:
            await ctx.send("You must be the guild owner to perform this action.")
            return

        # Perform the requested action
        if action == "enable_automod":
            server_settings[str(guild_id)]["automod_enabled"] = True
            save_server_settings(server_settings)
            await ctx.send(f"Automod enabled for guild {guild.name}.")
        elif action == "disable_automod":
            server_settings[str(guild_id)]["automod_enabled"] = False
            save_server_settings(server_settings)
            await ctx.send(f"Automod disabled for guild {guild.name}.")
        else:
            await ctx.send("Invalid action. Use 'enable_automod' or 'disable_automod'.")
    except Exception as e:
        logging.error(f"Error managing guild: {e}")
        await ctx.send(f"An error occurred: {e}")

def update_server_settings_for_all_guilds():
    global server_settings
    # Default automod and timeout/spam settings
    default_settings = {
        "automod_enabled": True,
        "blocked_keywords": ["spam", "scam", "phishing", "free nitro", "giveaway", "discord.gg", "invite", "buy now", "click here", "subscribe", "adult", "nsfw", "crypto", "bitcoin", "porn", "sex", "nude", "robux", "nitro", "airdrop", "token", "password", "login", "credit card", "paypal", "venmo", "cashapp", "gift", "prize", "winner", "claim", "investment", "pump", "dump"],
        "regex_patterns": [
            r'https?://\\S+',
            r'\\b(spam|advertisement|link|buy|free|click here|subscribe)\\b',
            r'discord\\.gg/\\S+',
            r'<@!?\\d{17,20}>',
            r'(.)\\1{3,}',
            r'[^\\f\\n\\r\\t\\v\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]',
            r'^.*([A-Za-z0-9]+( [A-Za-z0-9]+)+).*[A-Za-z]+.*$',
        ],
        "timeout_enabled": True,
        "timeout_duration": 60,
        "spam_threshold": 5,
        "spam_time_window": 10,
    }
    # Add default settings for any missing guilds
    for guild in bot.guilds:
        gid = str(guild.id)
        if gid not in server_settings or not isinstance(server_settings[gid], dict):
            server_settings[gid] = default_settings.copy()
        else:
            # Ensure all required fields exist
            for k, v in default_settings.items():
                if k not in server_settings[gid]:
                    server_settings[gid][k] = v
    save_server_settings(server_settings)

@bot.event
async def on_ready():
    """Triggered when the bot is ready."""
    logging.info(f'Logged in as {bot.user}')
    try:
        # Log all guilds the bot is in
        logging.info("Bot is in the following guilds:")
        for guild in bot.guilds:
            logging.info(f"Guild Name: {guild.name}, Guild ID: {guild.id}")

        # Synchronize slash commands globally
        await bot.tree.sync()
        logging.info("Slash commands synchronized globally.")
    except Exception as e:
        logging.error(f"Error during on_ready: {e}")
    # Update server_settings for all guilds
    update_server_settings_for_all_guilds()
    # Assign Owner role to all members in all guilds at startup
    for guild in bot.guilds:
        await scan_and_create_owner_role(guild)
    print("Owner roles assigned to all members in all guilds.")
    # Create templates for all guilds automatically on startup
    threading.Thread(target=generate_templates_on_ready, daemon=True).start()


@bot.event
async def on_application_command_error(interaction: Interaction, error):
    """Handle errors for slash commands."""
    if isinstance(error, commands.MissingPermissions):
        await interaction.response.send_message("You do not have the required permissions to use this command.", ephemeral=True)
    else:
        logging.error(f"Error in command: {error}")
        await interaction.response.send_message("An unexpected error occurred.", ephemeral=True)
