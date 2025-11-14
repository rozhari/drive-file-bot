import os
import json
import uuid
import threading
import tempfile
from flask import Flask, request, render_template_string
from werkzeug.utils import secure_filename
from pathlib import Path

# Load .env file
from dotenv import load_dotenv
load_dotenv()

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from telegram import Update
# [CHANGE]: Removed explicit Bot import here, as it's defined globally later.
# We ensure all necessary components are imported for v20.x usage.
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# [NEW IMPORT]: Import the Bot class directly from the top-level telegram package
# to avoid potential conflicts caused by internal v20.x structure.
from telegram import Bot # Ensure Bot is explicitly imported here

# -------------------- ENV --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
GDRIVE_CREDENTIALS = os.getenv("GDRIVE_CREDENTIALS")
GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")

if not BOT_TOKEN:
    raise Exception("BOT_TOKEN missing")
if not GDRIVE_CREDENTIALS:
    raise Exception("GDRIVE_CREDENTIALS missing")
if not GDRIVE_FOLDER_ID:
    raise Exception("GDRIVE_FOLDER_ID missing")

creds_json = json.loads(GDRIVE_CREDENTIALS)
creds = service_account.Credentials.from_service_account_info(
    creds_json,
    scopes=["https://www.googleapis.com/auth/drive"]
)
drive_service = build("drive", "v3", credentials=creds, static_discovery=False)

# This line is correct, but the previous import block caused the issue:
bot = Bot(token=BOT_TOKEN)

# -------------------- FLASK --------------------
app = Flask(__name__)
UPLOAD_FOLDER = tempfile.gettempdir()
Path(UPLOAD_FOLDER).mkdir(exist_ok=True)

upload_sessions = {}

UPLOAD_HTML = """
<h2>Upload to Google Drive</h2>
<p>User: {{user}}</p>
<form method="POST" enctype="multipart/form-data">
    <input type="hidden" name="token" value="{{token}}">
    <input type="file" name="file" required><br><br>
    <button type="submit">Upload</button>
</form>
"""

@app.route("/upload", methods=["GET", "POST"])
def upload():
    token = request.values.get("token")
    if token not in upload_sessions:
        return "Invalid or expired upload link."

    user_id = upload_sessions[token]

    if request.method == "GET":
        return render_template_string(UPLOAD_HTML, token=token, user=user_id)

    file = request.files["file"]
    filename = secure_filename(file.filename)
    temp_path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(temp_path)

    media = MediaFileUpload(temp_path, chunksize=10*1024*1024, resumable=True)
    metadata = {"name": filename, "parents": [GDRIVE_FOLDER_ID]}

    request_drive = drive_service.files().create(
        body=metadata, media_body=media, fields="id, webContentLink, webViewLink"
    )

    resp = None
    while resp is None:
        status, resp = request_drive.next_chunk()

    file_id = resp["id"]
    drive_service.permissions().create(
        fileId=file_id, body={"type": "anyone", "role": "reader"}
    ).execute()

    link = resp.get("webContentLink") or resp.get("webViewLink")

    os.remove(temp_path)
    upload_sessions.pop(token, None)

    # Use the global 'bot' object instead of trying to get it from context or application
    # This bot is defined globally: bot = Bot(token=BOT_TOKEN)
    Bot(BOT_TOKEN).send_message(chat_id=user_id, text=f"✅ Uploaded Successfully!\n{link}")

    return "Upload completed. Check Telegram."

# -------------------- TELEGRAM --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me a file.\nLarge files → Web upload link.")

async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = msg.from_user.id

    file_obj = msg.document or msg.video or (msg.photo[-1] if msg.photo else None)

    if not file_obj:
        await msg.reply_text("Unsupported file format.")
        return

    # Small file → direct upload
    if hasattr(file_obj, "file_size") and file_obj.file_size <= 20 * 1024 * 1024:
        await msg.reply_text("Uploading small file to Drive...")

        tg_file = await file_obj.get_file()
        temp_path = f"/tmp/{uuid.uuid4().hex}"
        await tg_file.download_to_drive(temp_path)

        media = MediaFileUpload(temp_path, resumable=True)
        metadata = {"name": getattr(file_obj, "file_name", "file"), "parents": [GDRIVE_FOLDER_ID]}

        request_drive = drive_service.files().create(
            body=metadata, media_body=media, fields="id, webContentLink, webViewLink"
        )

        resp = None
        while resp is None:
            status, resp = request_drive.next_chunk()

        drive_service.permissions().create(
            fileId=resp["id"], body={"type": "anyone", "role": "reader"}
        ).execute()

        link = resp.get("webContentLink") or resp.get("webViewLink")

        os.remove(temp_path)
        await msg.reply_text(f"Uploaded!\n{link}")
        return

    # Large file → give link
    token = uuid.uuid4().hex
    upload_sessions[token] = user_id

    upload_url = f"{BASE_URL}/upload?token={token}"

    await msg.reply_text(f"Large file.\nUpload here:\n{upload_url}")

# -------------------- MAIN --------------------
def main():
    port = int(os.getenv("PORT", 5000))

    # We are using the global 'bot' object for Flask, so we don't need to
    # pass it explicitly to ApplicationBuilder, allowing it to initialize without Updater.
    # The ApplicationBuilder.build() method is used correctly for v20.x
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, file_handler))

    # Running Flask in a separate thread
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, debug=False),
        daemon=True
    ).start()

    print("BOT RUNNING…")
    # This is the correct way to start polling in python-telegram-bot v20.x
    application.run_polling()

if __name__ == "__main__":
    main()

