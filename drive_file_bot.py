# drive_file_bot.py
import os
import io
import json
import uuid
import threading
import tempfile
from pathlib import Path
from flask import Flask, request, render_template_string
from werkzeug.utils import secure_filename

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from telegram import Update, Bot
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# ---------- Configuration from env ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
GDRIVE_CREDENTIALS = os.getenv("GDRIVE_CREDENTIALS")
GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")
if not GDRIVE_CREDENTIALS:
    raise RuntimeError("GDRIVE_CREDENTIALS not set (paste JSON contents into env)")
if not GDRIVE_FOLDER_ID:
    raise RuntimeError("GDRIVE_FOLDER_ID not set")
if not BASE_URL:
    print("WARNING: BASE_URL not set. For proper upload links set BASE_URL env to your deployed URL.")

# parse credentials
sa_info = json.loads(GDRIVE_CREDENTIALS)
SCOPES = ["https://www.googleapis.com/auth/drive"]

creds = service_account.Credentials.from_service_account_info(sa_info, scopes=SCOPES)
drive_service = build('drive', 'v3', credentials=creds, static_discovery=False)

# Telegram bot object (for sending messages from server)
tg_bot = Bot(token=BOT_TOKEN)

# Flask app for upload page
app = Flask(__name__)
UPLOAD_FOLDER = tempfile.gettempdir()
Path(UPLOAD_FOLDER).mkdir(parents=True, exist_ok=True)

# In-memory token -> user mapping (for demo). For production, persist in DB.
upload_sessions = {}  # token -> {user_id, filename_hint}

# Simple upload HTML
UPLOAD_FORM_HTML = """
<!doctype html>
<title>Upload file to Drive</title>
<h3>Upload file (will be saved to Drive)</h3>
<p>User: {{ user_id }}</p>
<form method=post enctype=multipart/form-data>
  <input type=file name=file required>
  <input type=hidden name=token value="{{ token }}">
  <br><br>
  <button type=submit>Upload</button>
</form>
"""

# ---------- Flask routes ----------
@app.route("/")
def index():
    return "Telegram → Google Drive upload server. Use /upload?token=... to upload."

@app.route("/upload", methods=["GET", "POST"])
def upload():
    token = request.values.get("token")
    if not token or token not in upload_sessions:
        return "Invalid or expired token. Start from the Telegram bot.", 400

    session = upload_sessions[token]
    user_id = session["user_id"]

    if request.method == "GET":
        # show form
        return render_template_string(UPLOAD_FORM_HTML, token=token, user_id=user_id)

    # POST: handle file upload
    if "file" not in request.files:
        return "No file provided.", 400

    file = request.files["file"]
    filename = secure_filename(file.filename) or f"upload_{uuid.uuid4().hex}"
    tmp_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4().hex}_{filename}")

    # Save to disk (streaming) to avoid memory issues
    file.save(tmp_path)

    # Now upload to Google Drive (resumable)
    try:
        file_metadata = {
            "name": filename,
            "parents": [GDRIVE_FOLDER_ID]
        }
        media = MediaFileUpload(tmp_path, resumable=True, chunksize=10 * 1024 * 1024)
        request_drive = drive_service.files().create(body=file_metadata, media_body=media, fields="id, webViewLink, webContentLink")
        response = None
        while response is None:
            status, response = request_drive.next_chunk()
        file_id = response.get("id")

        # Make file public (anyone with link)
        drive_service.permissions().create(
            fileId=file_id,
            body={"role": "reader", "type": "anyone"},
            fields="id"
        ).execute()

        # Get file info
        file_info = drive_service.files().get(fileId=file_id, fields="id, name, webViewLink, webContentLink").execute()
        download_link = file_info.get("webContentLink") or file_info.get("webViewLink")
    except Exception as e:
        # cleanup
        try:
            os.remove(tmp_path)
        except:
            pass
        # notify user on telegram
        try:
            tg_bot.send_message(chat_id=user_id, text=f"❌ Upload failed: {e}")
        except:
            pass
        return f"Upload failed: {e}", 500

    # cleanup temp file
    try:
        os.remove(tmp_path)
    except:
        pass

    # notify user on telegram
    try:
        tg_bot.send_message(chat_id=user_id,
                            text=f"✅ Upload complete!\n\nFile: {filename}\nDownload link:\n{download_link}")
    except Exception as e:
        # still return success page with link
        return f"Upload OK but couldn't notify via Telegram. Link: {download_link}", 200

    # Optionally expire token
    upload_sessions.pop(token, None)

    return f"Upload successful! Link sent to your Telegram.", 200

# ---------- Telegram bot handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hi! Send me a file. Small files (<=20MB) will be uploaded automatically. Larger files will require using a browser upload link I'll provide.")

async def get_file_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    file_obj = None

    # determine type
    if message.document:
        file_obj = message.document
    elif message.photo:
        file_obj = message.photo[-1]
    elif message.video:
        file_obj = message.video
    elif message.audio:
        file_obj = message.audio
    elif message.voice:
        file_obj = message.voice

    if not file_obj:
        await update.message.reply_text("Please send a valid file (document/photo/video/audio/voice).")
        return

    # some messages may not have file_size (rare) — handle carefully
    file_size = getattr(file_obj, "file_size", None)

    # If size known and <= 20MB: download and directly upload to Drive
    if file_size is not None and file_size <= 20 * 1024 * 1024:
        await update.message.reply_text("Small file detected — downloading and uploading to Drive...")
        try:
            # download to temp file
            tg_file = await file_obj.get_file()
            tmp_fd, tmp_path = tempfile.mkstemp(prefix="tgdl_")
            os.close(tmp_fd)
            await tg_file.download_to_drive(custom_path=tmp_path)

            # upload to Drive (resumable)
            filename = getattr(file_obj, "file_name", None) or os.path.basename(tmp_path)
            file_metadata = {"name": filename, "parents": [GDRIVE_FOLDER_ID]}
            media = MediaFileUpload(tmp_path, resumable=True, chunksize=10 * 1024 * 1024)
            request_drive = drive_service.files().create(body=file_metadata, media_body=media, fields="id, webViewLink, webContentLink")
            response = None
            while response is None:
                status, response = request_drive.next_chunk()
            file_id = response.get("id")
            # make public
            drive_service.permissions().create(fileId=file_id, body={"role":"reader","type":"anyone"}).execute()
            file_info = drive_service.files().get(fileId=file_id, fields="webContentLink, webViewLink").execute()
            download_link = file_info.get("webContentLink") or file_info.get("webViewLink")
            # cleanup
            try:
                os.remove(tmp_path)
            except:
                pass

            await update.message.reply_text(f"✅ Uploaded to Drive!\n\nDownload link:\n{download_link}")
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to upload automatically: {e}\nYou can use manual upload link instead.")
            # fallthrough to create upload token below to allow manual upload
        else:
            return

    # For large files / fallback: create upload token and give user the upload page link
    token = uuid.uuid4().hex
    upload_sessions[token] = {
        "user_id": update.effective_user.id,
        "filename_hint": getattr(file_obj, "file_name", None) or None
    }

    # Create upload URL
    base = BASE_URL or f"https://{os.getenv('RENDER_SERVICE_ID','your-service')}"
    upload_url = f"{base}/upload?token={token}"

    await update.message.reply_text(
        "❗ This file is large or couldn't be auto-downloaded.\n\n"
        "Please open the following link in your device browser and upload the file directly (this supports very large files):\n\n"
        f"{upload_url}\n\n"
        "After upload completes, I'll send you the Drive download link here."
    )

def main():
    # Start Flask in a separate thread (bind to port from env)
    port = int(os.getenv("PORT", "5000"))
    def run_flask():
        # listen on all interfaces
        app.run(host="0.0.0.0", port=port, threaded=True)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Start Telegram bot (polling)
    app_tg = ApplicationBuilder().token(BOT_TOKEN).build()
    app_tg.add_handler(CommandHandler("start", start))
    app_tg.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), get_file_link))

    print(f"Bot started. Web upload available at /upload. PORT={port}")
    app_tg.run_polling()

if __name__ == "__main__":
    main()
