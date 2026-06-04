import os
import re
import shutil
import asyncio
import zipfile
import html
import gc
import threading
import logging
from concurrent.futures import ThreadPoolExecutor
from xml.etree import ElementTree as ET
from flask import Flask
from docx import Document
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.error import RetryAfter

# ==========================================
# LOGGING & CONFIGURATION
# ==========================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = "7954112414:AAEXxK5PcckCiZChIWUXHsQSRD2XfiasW-Q"
DEFAULT_DOCX_CHUNK = 500          # pages
DEFAULT_EPUB_CHUNK = 1000         # chapters
WORDS_PER_PAGE = 500
MAX_CHUNK_SIZE = 100              # max pages/chapters per chunk (memory safe)
MAX_FILES_PER_REQUEST = 20        # limit number of output files
MAX_LINES_DOCX = 10000            # max lines extracted from DOCX
MAX_LINES_HTML = 5000             # max lines per HTML file in EPUB
MAX_HTML_FILES_EPUB = 100         # max HTML files read from EPUB

# Thread pool for CPU‑intensive tasks
executor = ThreadPoolExecutor(max_workers=2)

# Global queues & state
document_queue = None
user_chunk_sizes = {}
pending_uploads = {}

# ==========================================
# TABLE OF CONTENTS DETECTION & REMOVAL
# ==========================================
CHAPTER_LINE_RE = re.compile(
    r'^\s*(chapter[\s\-:]*[\d]+|अध्याय[\s]*[\d]+|चैप्टर[\s]*[\d]+)',
    re.IGNORECASE
)
VOLUME_LINE_RE = re.compile(
    r'^\s*(volume[\s]*[\d]+|खंड[\s]*[\d]+|भाग[\s]*[\d]+|part[\s]*[\d]+)',
    re.IGNORECASE
)
CONTENT_ANCHOR_RE = re.compile(
    r'^\s*(synopsis|summary|prologue|preface|introduction|foreword|chapter\s*1\b'
    r'|सारांश|भूमिका|प्रस्तावना|परिचय|प्रारंभ|अध्याय\s*1\b|चैप्टर\s*1\b)',
    re.IGNORECASE
)

def is_toc_line(stripped):
    return (not stripped or stripped == '---' or
            bool(CHAPTER_LINE_RE.match(stripped)) or
            bool(VOLUME_LINE_RE.match(stripped)) or
            bool(re.match(r'^:\s*.+', stripped)))

def find_toc_block(lines, min_chapter_hits=20):
    consecutive = 0
    run_start = -1
    toc_start = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        is_chapter = bool(CHAPTER_LINE_RE.match(stripped))
        is_volume = bool(VOLUME_LINE_RE.match(stripped))
        is_blank = not stripped
        is_sep = (stripped == '---')
        if is_chapter or is_volume or is_blank or is_sep:
            if is_chapter or is_volume:
                if run_start == -1:
                    run_start = i
                consecutive += 1
            if consecutive >= min_chapter_hits:
                toc_start = run_start
                break
        else:
            consecutive = 0
            run_start = -1
    if toc_start == -1:
        return -1, -1
    toc_end = toc_start
    for i in range(toc_start, len(lines)):
        if is_toc_line(lines[i].strip()):
            toc_end = i
        else:
            break
    return toc_start, toc_end

def remove_toc(lines):
    if not lines:
        return lines
    toc_start, toc_end = find_toc_block(lines)
    if toc_start == -1:
        return lines
    # TOC at end of file
    if toc_start > len(lines) // 2:
        result = lines[:toc_start]
        while result and (not result[-1].strip() or result[-1].strip() == '---'):
            result.pop()
        return result if result else lines
    # TOC at beginning – find content after it
    content_start = -1
    for i in range(toc_end + 1, min(toc_end + 50, len(lines))):
        stripped = lines[i].strip()
        if stripped and stripped != '---' and not is_toc_line(stripped):
            content_start = i
            break
    if content_start == -1:
        return lines
    return lines[content_start:]

# ==========================================
# MEMORY‑EFFICIENT PARSERS & SAVERS
# ==========================================
def fast_read_docx(input_path):
    lines = []
    try:
        with zipfile.ZipFile(input_path, 'r') as docx_zip:
            xml_content = docx_zip.read('word/document.xml')
            tree = ET.fromstring(xml_content)
            ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
            for para in tree.findall('.//w:p', namespaces=ns):
                texts = [t.text for t in para.findall('.//w:t', namespaces=ns) if t.text]
                if texts:
                    lines.append("".join(texts).strip())
                if len(lines) > MAX_LINES_DOCX:
                    break
    except Exception as e:
        logger.error(f"DOCX extraction failed: {e}")
    return lines

def fast_html_to_text(raw_html):
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', raw_html)
    text = re.sub(r'<(script|style|head)[^>]*>.*?</\1>', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'</?(p|div|h[1-6]|br|tr|li)[^>]*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    return lines[:MAX_LINES_HTML]

def natural_sort_key(s):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r'(\d+)', s)]

def save_chunk(lines, path, fmt):
    logger.info(f"Saving {os.path.basename(path)} ...")
    try:
        if fmt == 'txt':
            with open(path, 'w', encoding='utf-8') as f:
                f.write('\n\n'.join(lines))
        else:
            # Create minimal DOCX with direct XML injection
            doc = Document()
            doc.save(path)
            xml_paras = []
            for line in lines:
                safe_text = html.escape(line)
                xml_paras.append(f'<w:p><w:r><w:t>{safe_text}</w:t></w:r></w:p>')
            body_xml = "".join(xml_paras)
            temp_path = path + ".tmp"
            with zipfile.ZipFile(path, 'r') as zin:
                with zipfile.ZipFile(temp_path, 'w', compression=zipfile.ZIP_DEFLATED) as zout:
                    for item in zin.infolist():
                        if item.filename == 'word/document.xml':
                            orig_xml = zin.read(item.filename).decode('utf-8')
                            new_xml = orig_xml.replace('<w:sectPr', body_xml + '<w:sectPr')
                            zout.writestr(item, new_xml.encode('utf-8'))
                        else:
                            zout.writestr(item, zin.read(item.filename))
            os.replace(temp_path, path)
    except Exception as e:
        logger.error(f"Save failed: {e}")
    finally:
        gc.collect()

# ==========================================
# SPLITTER LOGIC (DOCX / TXT by pages)
# ==========================================
def split_text_based_logic(input_path, output_dir, chunk_size_pages, output_format, is_txt_file=False):
    os.makedirs(output_dir, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    generated_files = []

    if is_txt_file:
        with open(input_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [line.strip() for line in f if line.strip()]
    else:
        lines = fast_read_docx(input_path)

    lines = remove_toc(lines)
    if not lines:
        return []

    max_words = chunk_size_pages * WORDS_PER_PAGE
    collector = []
    word_count = 0
    part_num = 1

    for text in lines:
        wc = len(text.split())
        if word_count + wc > max_words and collector:
            ext = 'txt' if output_format == 'txt' else 'docx'
            out_path = os.path.join(output_dir, f"Part_{part_num}_{base_name}.{ext}")
            save_chunk(collector, out_path, output_format)
            generated_files.append(out_path)
            collector = []
            word_count = 0
            part_num += 1
            if part_num > MAX_FILES_PER_REQUEST:
                break
        collector.append(text)
        word_count += wc

    if collector and part_num <= MAX_FILES_PER_REQUEST:
        ext = 'txt' if output_format == 'txt' else 'docx'
        out_path = os.path.join(output_dir, f"Part_{part_num}_{base_name}.{ext}")
        save_chunk(collector, out_path, output_format)
        generated_files.append(out_path)

    del lines
    gc.collect()
    return generated_files

def split_docx_logic(input_path, output_dir, chunk_size, output_format):
    return split_text_based_logic(input_path, output_dir, chunk_size, output_format, is_txt_file=False)

def split_txt_logic(input_path, output_dir, chunk_size, output_format):
    return split_text_based_logic(input_path, output_dir, chunk_size, output_format, is_txt_file=True)

# ==========================================
# SPLITTER LOGIC (EPUB by chapters)
# ==========================================
def split_epub_logic(input_path, output_dir, chunk_size, output_format):
    os.makedirs(output_dir, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    clean_name = re.sub(r'[^\w\-_]', '_', base_name)
    generated_files = []

    try:
        all_lines = []
        with zipfile.ZipFile(input_path, 'r') as epub_zip:
            html_files = [f for f in epub_zip.namelist() if f.lower().endswith(('.html', '.xhtml', '.htm'))]
            html_files.sort(key=natural_sort_key)
            html_files = html_files[:MAX_HTML_FILES_EPUB]
            for fname in html_files:
                try:
                    content = epub_zip.read(fname).decode('utf-8', errors='ignore')
                    lines = fast_html_to_text(content)
                    if lines:
                        all_lines.extend(lines)
                        all_lines.append("---")
                except Exception as e:
                    logger.warning(f"Skipping {fname}: {e}")
                if len(all_lines) > 20000:  # safety
                    break

        all_lines = remove_toc(all_lines)
        if not all_lines:
            return []

        buffer = []
        chapter_cnt = 0
        part_num = 1
        for line in all_lines:
            buffer.append(line)
            if line == "---":
                chapter_cnt += 1
            if chapter_cnt >= chunk_size:
                ext = 'txt' if output_format == 'txt' else 'docx'
                out_path = os.path.join(output_dir, f"Part_{part_num}_{clean_name}.{ext}")
                save_chunk(buffer, out_path, output_format)
                generated_files.append(out_path)
                buffer = []
                chapter_cnt = 0
                part_num += 1
                if part_num > MAX_FILES_PER_REQUEST:
                    break
                gc.collect()

        if buffer and part_num <= MAX_FILES_PER_REQUEST:
            ext = 'txt' if output_format == 'txt' else 'docx'
            out_path = os.path.join(output_dir, f"Part_{part_num}_{clean_name}.{ext}")
            save_chunk(buffer, out_path, output_format)
            generated_files.append(out_path)

        del all_lines
        gc.collect()
        return generated_files

    except Exception as e:
        logger.error(f"EPUB processing failed: {e}")
        return []

# ==========================================
# BACKGROUND WORKER
# ==========================================
async def queue_worker():
    while True:
        job = await document_queue.get()
        status_msg = job['status_msg']
        try:
            loop = asyncio.get_running_loop()
            if job['type'] == 'docx':
                await status_msg.edit_text(f"📄 Processing DOCX `{job['file_name']}` ...")
                files = await loop.run_in_executor(
                    executor, split_docx_logic,
                    job['input_path'], job['output_dir'],
                    job['chunk_size'], job['format']
                )
            elif job['type'] == 'txt':
                await status_msg.edit_text(f"📝 Processing TXT `{job['file_name']}` ...")
                files = await loop.run_in_executor(
                    executor, split_txt_logic,
                    job['input_path'], job['output_dir'],
                    job['chunk_size'], job['format']
                )
            else:  # epub
                await status_msg.edit_text(f"📚 Processing EPUB `{job['file_name']}` ...")
                files = await loop.run_in_executor(
                    executor, split_epub_logic,
                    job['input_path'], job['output_dir'],
                    job['chunk_size'], job['format']
                )

            if not files:
                await status_msg.edit_text("⚠️ No readable content found.")
                continue

            await status_msg.edit_text(f"✅ Sending {len(files)} file(s)...")
            for f in files:
                with open(f, 'rb') as doc:
                    try:
                        await status_msg.reply_document(document=doc, filename=os.path.basename(f))
                        await asyncio.sleep(0.3)
                    except RetryAfter as e:
                        await asyncio.sleep(e.retry_after)
                    except Exception as e:
                        logger.error(f"Send error {os.path.basename(f)}: {e}")
            await status_msg.reply_text("🎉 All done! Send another file.")
        except Exception as e:
            await status_msg.edit_text(f"❌ Error: {str(e)[:200]}")
        finally:
            if os.path.exists(job['temp_dir']):
                shutil.rmtree(job['temp_dir'], ignore_errors=True)
            document_queue.task_done()
            gc.collect()

# ==========================================
# BOT HANDLERS
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name
    await update.message.reply_text(
        f"👋 Hello {name}!\n\n"
        f"📁 Send me a **.docx**, **.txt** or **.epub** file.\n"
        f"⚙️ `/set 50` — change chunk size (pages for DOCX/TXT, chapters for EPUB).\n"
        f"📖 Auto‑removes Table of Contents (English / Hindi)."
    )

async def set_chunk_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_size = min(int(context.args[0]), MAX_CHUNK_SIZE)
        user_chunk_sizes[update.effective_user.id] = new_size
        await update.message.reply_text(f"✅ Chunk size set to **{new_size}**.")
    except:
        await update.message.reply_text(f"⚠️ Usage: `/set 1-{MAX_CHUNK_SIZE}`")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    file_name = doc.file_name.lower()
    msg_id = update.message.message_id

    if not (file_name.endswith('.docx') or file_name.endswith('.epub') or file_name.endswith('.txt')):
        await update.message.reply_text("❌ Only .docx, .txt or .epub files allowed.")
        return

    # Quick file size check
    if doc.file_size and doc.file_size > 50 * 1024 * 1024:
        await update.message.reply_text("❌ File too large (max 50 MB).")
        return

    pending_uploads[msg_id] = {
        'document': doc,
        'user_id': update.effective_user.id,
        'file_name': doc.file_name
    }

    if file_name.endswith('.docx'):
        keyboard = [[
            InlineKeyboardButton("📄 DOCX", callback_data=f"docx|docx|{msg_id}"),
            InlineKeyboardButton("📝 TXT",  callback_data=f"docx|txt|{msg_id}")
        ]]
        await update.message.reply_text("DOCX detected. Save chunks as:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif file_name.endswith('.txt'):
        keyboard = [[
            InlineKeyboardButton("📄 DOCX", callback_data=f"txt|docx|{msg_id}"),
            InlineKeyboardButton("📝 TXT",  callback_data=f"txt|txt|{msg_id}")
        ]]
        await update.message.reply_text("TXT detected. Save chunks as:", reply_markup=InlineKeyboardMarkup(keyboard))
    else:  # epub
        keyboard = [[
            InlineKeyboardButton("📄 DOCX", callback_data=f"epub|docx|{msg_id}"),
            InlineKeyboardButton("📝 TXT",  callback_data=f"epub|txt|{msg_id}")
        ]]
        await update.message.reply_text("EPUB detected. Save chunks as:", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("|")
    job_type, output_format, msg_id = data[0], data[1], int(data[2])

    if msg_id not in pending_uploads:
        await query.edit_message_text("❌ Session expired. Please upload again.")
        return

    job_info = pending_uploads.pop(msg_id)
    document = job_info['document']
    user_id = job_info['user_id']
    file_name = job_info['file_name']

    # Use /tmp for Render's ephemeral storage
    temp_dir = f"/tmp/bot_{user_id}_{msg_id}"
    input_path = os.path.join(temp_dir, file_name)
    output_dir = os.path.join(temp_dir, "output")
    os.makedirs(output_dir, exist_ok=True)

    await query.edit_message_text(f"📥 Downloading `{file_name}`...")
    try:
        file = await document.get_file()
        await file.download_to_drive(input_path)
    except Exception as e:
        await query.edit_message_text(f"❌ Download failed: {str(e)[:100]}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return

    chunk_size = user_chunk_sizes.get(user_id,
                                      DEFAULT_DOCX_CHUNK if job_type != 'epub' else DEFAULT_EPUB_CHUNK)
    chunk_size = min(chunk_size, MAX_CHUNK_SIZE)

    job_data = {
        'type': job_type,
        'format': output_format,
        'chunk_size': chunk_size,
        'status_msg': query.message,
        'temp_dir': temp_dir,
        'input_path': input_path,
        'output_dir': output_dir,
        'file_name': file_name
    }
    await document_queue.put(job_data)
    await query.edit_message_text(f"⏳ Queued (position: ~{document_queue.qsize()})")

# ==========================================
# FLASK KEEP‑ALIVE SERVER (for Render)
# ==========================================
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return "Bot is alive and running!", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

# ==========================================
# MAIN ENTRY POINT
# ==========================================
async def start_background_tasks(app: Application):
    global document_queue
    document_queue = asyncio.Queue(maxsize=10)
    asyncio.create_task(queue_worker())

def main():
    logger.info("🤖 Starting bot on Render...")
    # Start Flask keep-alive thread
    threading.Thread(target=run_flask, daemon=True).start()

    # Build bot application
    app = Application.builder().token(TOKEN).post_init(start_background_tasks).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("set", set_chunk_size))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(CallbackQueryHandler(button_callback))

    # Use polling – Render's free tier works fine with long polling
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
