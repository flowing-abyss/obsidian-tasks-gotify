import configparser
import hashlib
import re
import sqlite3
from datetime import datetime
from pathlib import Path
import requests
import pytz

# Fix for DeprecationWarning in Python 3.12+
def adapt_datetime_iso(val):
    return val.isoformat()

sqlite3.register_adapter(datetime, adapt_datetime_iso)

CONFIG_FILE = "config.ini"
DB_FILE = "sent_notifications.db"

DATE_REGEX = re.compile(r"📅\s*(?P<date>\d{4}-\d{2}-\d{2})")
TIME_REGEX = re.compile(r"⏰\s*(?P<time>\d{2}:\d{2})")
TAG_REGEX = re.compile(r"(#\S+)")
TASK_MARKER_REGEX = re.compile(r"^\s*-\s*\[\s*\]\s*")

def parse_task_line(line):
    if not TASK_MARKER_REGEX.match(line):
        return None

    original_text = TASK_MARKER_REGEX.sub(" ", line).strip()

    date_match = DATE_REGEX.search(original_text)
    time_match = TIME_REGEX.search(original_text)

    date_str = date_match.group("date") if date_match else None
    time_str = time_match.group("time") if time_match else None

    clean_text = original_text
    if date_match:
        clean_text = clean_text.replace(date_match.group(0), "")
    if time_match:
        clean_text = clean_text.replace(time_match.group(0), "")

    tags = TAG_REGEX.findall(clean_text)
    clean_text = TAG_REGEX.sub("", clean_text).strip()
    clean_text = " ".join(clean_text.split())

    return {"text": clean_text, "date": date_str, "time": time_str, "tags": tags}

def cleanup_database():
    try:
        if Path(DB_FILE).exists():
            Path(DB_FILE).unlink()
            print("Database cleaned up.")
    except Exception as e:
        print(f"Database cleanup error: {e}")

def setup_database():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS sent_notifications (
                    task_id TEXT PRIMARY KEY,
                    sent_at TIMESTAMP
                )
            """
            )
            conn.commit()
    except sqlite3.Error as e:
        print(f"Database setup error: {e}")
        raise

def get_config():
    config = configparser.ConfigParser()
    if not Path(CONFIG_FILE).exists():
        raise FileNotFoundError(f"Configuration file {CONFIG_FILE} not found.")
    config.read(CONFIG_FILE)
    return config

def generate_task_id(file_path, task_text):
    return hashlib.sha1(f"{file_path}:{task_text}".encode("utf-8")).hexdigest()

def is_notification_sent(task_id):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM sent_notifications WHERE task_id = ?", (task_id,)
            )
            return cursor.fetchone() is not None
    except sqlite3.Error as e:
        print(f"Database check error: {e}")
        return True

def mark_notification_as_sent(task_id):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO sent_notifications (task_id, sent_at) VALUES (?, ?)",
                (task_id, datetime.now()),
            )
            conn.commit()
    except sqlite3.Error as e:
        print(f"Database write error: {e}")

def format_notification(task_text, original_task_time, tags, file_name):
    title = "✅️ New task"
    clean_file_name = Path(file_name).stem
    tags_str = ", ".join(tags) if tags else "No"

    message_parts = [f"📝 {task_text.strip()}"]

    if original_task_time:
        message_parts.append(f"⏰ {original_task_time}")

    message_parts.append(f"🏷️ {tags_str}")
    message_parts.append(f"📄 {clean_file_name}")

    message = "\n".join(message_parts)
    return title, message

def send_gotify_notification(server_url, token, title, message):
    try:
        url = f"{server_url}/message?token={token}"
        response = requests.post(url, data={"title": title, "message": message})
        response.raise_for_status()
        print(f"Notification '{title}' sent successfully.")
        return True
    except requests.exceptions.RequestException as e:
        print(f"Gotify notification error: {e}")
        return False

def find_and_process_tasks(config):
    vault_path = Path(config["obsidian"]["vault_path"])
    default_time_str = config["settings"]["default_notification_time"]
    timezone_str = config.get("settings", "timezone", fallback="UTC")
    timezone = pytz.timezone(timezone_str)

    exclude_dirs = {
        d.strip() for d in config["obsidian"]["exclude_dirs"].split(",") if d.strip()
    }
    now = datetime.now(timezone)

    all_md_files = list(vault_path.rglob("*.md"))
    filtered_files = []
    
    for md_file in all_md_files:
        relative_path = md_file.relative_to(vault_path)
        file_dir_path = str(relative_path.parent) if relative_path.parent != Path('.') else ''
        
        should_exclude = False
        for exclude_dir in exclude_dirs:
            if file_dir_path == exclude_dir or file_dir_path.startswith(exclude_dir + '/'):
                should_exclude = True
                break
        
        if not should_exclude:
            filtered_files.append(md_file)

    for md_file in filtered_files:
        with open(md_file, "r", encoding="utf-8") as f:
            for line in f:
                parsed_data = parse_task_line(line)
                if not parsed_data or not parsed_data["date"]:
                    continue

                task_text = parsed_data["text"]
                task_id = generate_task_id(str(md_file), task_text)

                if is_notification_sent(task_id):
                    continue

                task_time_str = parsed_data["time"] or default_time_str
                due_datetime_str = f'{parsed_data["date"]} {task_time_str}'
                naive_due_datetime = datetime.strptime(due_datetime_str, "%Y-%m-%d %H:%M")
                due_datetime = timezone.localize(naive_due_datetime)

                if now >= due_datetime:
                    title, message = format_notification(
                        task_text,
                        parsed_data["time"],
                        parsed_data["tags"],
                        md_file.name,
                    )

                    if send_gotify_notification(
                        config["gotify"]["server_url"],
                        config["gotify"]["token"],
                        title,
                        message,
                    ):
                        mark_notification_as_sent(task_id)

def main():
    try:
        print("Starting Obsidian task watcher...")
        config = get_config()
        
        default_time_str = config["settings"]["default_notification_time"]
        timezone_str = config.get("settings", "timezone", fallback="UTC")
        timezone = pytz.timezone(timezone_str)
        now = datetime.now(timezone)
        
        default_time = datetime.strptime(default_time_str, "%H:%M").time()
        current_time = now.time()
        
        if current_time < default_time:
            print(f"Current time {current_time} is before notification time {default_time}. Cleaning up database and exiting.")
            cleanup_database()
            return
        
        setup_database()
        find_and_process_tasks(config)
        print("Watcher run complete.")
    except (FileNotFoundError, KeyError, configparser.Error) as e:
        print(f"Configuration error: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")

if __name__ == "__main__":
    main()
