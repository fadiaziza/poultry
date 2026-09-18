import os
import glob
import re
import io
import base64
import threading
import time
import fitz  # PyMuPDF
import requests
from datetime import datetime
import pytz
from PIL import Image
import torch
from sentence_transformers import SentenceTransformer, util
import gradio as gr

import google.auth
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

PORT = int(os.environ.get("PORT", 8080))
LOCAL_DIR = "/tmp/Maintenance_Manuals"
FOLDER_NAME = "Maintenance_Manuals"
os.makedirs(LOCAL_DIR, exist_ok=True)

# متغيرات النظام العامة
visual_model = None
IMAGE_INDEX = []
IMAGE_PATHS = []
SYSTEM_LOGS = "بدء تشغيل النظام..."
is_syncing = False

def get_visual_model():
    global visual_model
    if visual_model is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
        visual_model = SentenceTransformer('clip-ViT-B-32', device=device)
    return visual_model

def sync_drive_worker():
    global SYSTEM_LOGS, IMAGE_INDEX, IMAGE_PATHS, is_syncing
    is_syncing = True
    SYSTEM_LOGS = "جاري الاتصال بـ Google Drive..."
    print(SYSTEM_LOGS)
    
    try:
        credentials, project = google.auth.default(scopes=['https://www.googleapis.com/auth/drive.readonly'])
        drive_service = build('drive', 'v3', credentials=credentials)

        # البحث عن المجلد الأساسي
        query = f"name = '{FOLDER_NAME}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        res = drive_service.files().list(q=query, fields="files(id, name)").execute()
        folders = res.get('files', [])

        if not folders:
            # محاولة ثانية: البحث في المجلدات المشاركة مع حساب الخدمة
            query_shared = f"mimeType = 'application/vnd.google-apps.folder' and trashed = false and sharedWithMe = true"
            res_shared = drive_service.files().list(q=query_shared, fields="files(id, name)").execute()
            folders = [f for f in res_shared.get('files', []) if f['name'] == FOLDER_NAME]

        if not folders:
            SYSTEM_LOGS = "❌ خطأ: لم يتم العثور على المجلد! تأكد من مشاركته مع حساب الخدمة."
            print(SYSTEM_LOGS)
            is_syncing = False
            return

        folder_id = folders[0]['id']
        SYSTEM_LOGS = f"تم العثور على مجلد الكتالوجات (ID: {folder_id})، جاري سحب الملفات والصور..."
        print(SYSTEM_LOGS)

        def download_recursive(parent_id, target_path):
            os.makedirs(target_path, exist_ok=True)
            page_token = None
            while True:
                response = drive_service.files().list(
                    q=f"'{parent_id}' in parents and trashed = false",
                    fields="nextPageToken, files(id, name, mimeType)",
                    pageToken=page_token
                ).execute()

                for item in response.get('files', []):
                    if item['mimeType'] == 'application/vnd.google-apps.folder':
                        download_recursive(item['id'], os.path.join(target_path, item['name']))
                    else:
                        safe_name = item['name'].replace("/", "-").replace("\\", "-")
                    dest_file = os.path.join(target_path, safe_name)
                    os.makedirs(os.path.dirname(dest_file), exist_ok=True)
                    if not os.path.exists(dest_file):
                        req = drive_service.files().get_media(fileId=item['id'])
                        with open(dest_file, "wb") as f:
                            downloader = MediaIoBaseDownload(f, req)
                            done = False
                            while not done:
                                _, done = downloader.next_chunk()
                                downloader = MediaIoBaseDownload(f, req)
                                done = False
                                while not done:
                                    _, done = downloader.next_chunk()
                page_token = response.get('nextPageToken', None)
                if not page_token:
                    break

        download_recursive(folder_id, LOCAL_DIR)

        # فحص وبناء الفهرس البصري
        valid_exts = ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.PNG", "*.JPG", "*.JPEG")
        all_imgs = []
        for ext in valid_exts:
            all_imgs.extend(glob.glob(os.path.join(LOCAL_DIR, "**", ext), recursive=True))

        target_imgs = [p for p in set(all_imgs) if "logo" not in os.path.basename(p).lower()]
        all_pdfs = glob.glob(os.path.join(LOCAL_DIR, "**", "*.pdf"), recursive=True)

        if target_imgs:
            SYSTEM_LOGS = f"جاري فهرسة {len(target_imgs)} صورة في مصفوفة CLIP..."
            model = get_visual_model()
            embeddings = []
            paths = []
            for p in target_imgs:
                try:
                    img = Image.open(p).convert('RGB')
                    embeddings.append(model.encode(img, convert_to_tensor=True))
                    paths.append(p)
                except Exception:
                    continue
            if embeddings:
                IMAGE_INDEX = torch.stack(embeddings)
                IMAGE_PATHS = paths

        SYSTEM_LOGS = f"✅ اكتملت المزامنة بنجاح! المفهرس: {len(all_pdfs)} كتالوج PDF و {len(IMAGE_PATHS)} صورة قطعة."
        print(SYSTEM_LOGS)

    except Exception as e:
        SYSTEM_LOGS = f"❌ خطأ في الاتصال أو التحميل: {str(e)}"
        print(SYSTEM_LOGS)
    finally:
        is_syncing = False

# بدء مسار المزامنة فوراً دون تعطيل المنفذ
threading.Thread(target=sync_drive_worker, daemon=True).start()

def get_clean_machine_name(pdf_path):
    fname = os.path.basename(pdf_path)
    clean_name = fname.replace(".pdf", "").replace("pdf.", "").replace("-1", "").strip()
    return f"{clean_name} — [`{fname}`]"

def search_part_number_in_all_manuals(raw_input):
    clean_input = raw_input.strip()
    alarm_match = re.search(r'\b([Ee]\s*[-_]?\s*\d{2,4})\b', clean_input)
    core_match = re.search(r'(\d{3,4}\.[\w\d]+\.\d{3}\.\d{2})', clean_input)
    if not core_match:
        core_match = re.search(r'(\d{3}\.\d{3}\.\d{2})', clean_input)

    search_terms = set()
    detected_key = clean_input

    if alarm_match:
        raw_code = alarm_match.group(1).replace(" ", "").upper()
        digits = re.sub(r'[^0-9]', '', raw_code)
        int_val = int(digits) if digits else 0
        detected_key = raw_code
        search_terms.update([raw_code, f"E-{digits}", f"E {digits}", f"E{int_val}", f"E-{int_val}", f"E0{int_val}", f"E-0{int_val}", f"Error {int_val}", f"Alarm {int_val}"])
    elif core_match:
        core_number = core_match.group(1)
        detected_key = core_number
        search_terms.update([clean_input, core_number, f"D{core_number}", f"C{core_number}", f"H{core_number}", core_number.replace(".", "")])
    else:
        words = [w for w in re.split(r'[\s,;:_-]+', clean_input) if len(w) >= 3]
        search_terms.update(words)

    valid_targets = [re.escape(t) for t in search_terms if len(t) >= 2]
    if not valid_targets:
        return clean_input, detected_key, []

    search_regex = re.compile(r'(' + '|'.join(valid_targets) + r')', re.IGNORECASE)
    all_pdfs = glob.glob(f"{LOCAL_DIR}/**/*.pdf", recursive=True)
    matches = []

    for pdf_path in all_pdfs:
        doc_name = os.path.basename(pdf_path)
        try:
            doc = fitz.open(pdf_path)
            for page_num in range(len(doc)):
                text = doc[page_num].get_text()
                if search_regex.search(text):
                    lines = [l.strip() for l in text.split("\n") if l.strip()]
                    snippet = []
                    for i, line in enumerate(lines):
                        if search_regex.search(line):
                            start = max(0, i - 1)
                            end = min(len(lines), i + 4)
                            snippet = lines[start:end]
                            break
                    matches.append({
                        "machine_name": get_clean_machine_name(pdf_path),
                        "page": page_num + 1,
                        "details": " | ".join(snippet) if snippet else "مطابقة مسجلة في الدليل الفني."
                    })
                    if len(matches) >= 5:
                        break
            doc.close()
        except Exception:
            continue

    return clean_input, detected_key, matches

def visual_maintenance_copilot(image_file, text_input):
    global IMAGE_INDEX, IMAGE_PATHS, SYSTEM_LOGS, is_syncing

    # تقرير حالة فورية للمستخدم
    status_prefix = f"> 📡 **حالة السيرفر والملفات:** `{SYSTEM_LOGS}`\n\n---\n\n"

    # 1. مطابقة الصورة
    if image_file is not None:
        if len(IMAGE_INDEX) == 0:
            return status_prefix + "⚠️ **قاعدة بيانات الصور غير متوفرة بعد على السيرفر.** يرجى مراجعة حالة السيرفر بالأعلى للتأكد من اكتمال التنزيل.", None

        model = get_visual_model()
        uploaded_rgb = image_file.convert('RGB')
        query_emb = model.encode(uploaded_rgb, convert_to_tensor=True)
        cos_scores = util.cos_sim(query_emb, IMAGE_INDEX)[0]
        best_idx = torch.argmax(cos_scores).item()
        best_score = float(cos_scores[best_idx])

        if best_score >= 0.40:
            matched_img_path = IMAGE_PATHS[best_idx]
            extracted_code = os.path.splitext(os.path.basename(matched_img_path))[0]
            _, _, manual_matches = search_part_number_in_all_manuals(extracted_code)
            matched_pil = Image.open(matched_img_path)
            percent = int(best_score * 100)

            res = status_prefix + f"### 🎯 نتيجة التعرف البصري على القطعة\n\n"
            res += f"* **نسبة التطابق البصري:** `{percent}%`\n"
            res += f"* **رقم القطعة:** `{extracted_code}`\n\n"
            res += "### 📖 المطابقة في الكتالوجات:\n\n"

            if manual_matches:
                for idx, item in enumerate(manual_matches, 1):
                    res += f"**{idx}. {item['machine_name']}**\n"
                    res += f"* 📄 الصفحة: **{item['page']}**\n"
                    res += f"* ⚙️ التفاصيل: `{item['details']}`\n\n"
            else:
                res += "تم التعرف على شكل القطعة ولكن لم يعثر على الرقم داخل الكتالوجات الحالية.\n"
            return res, matched_pil
        else:
            return status_prefix + f"⚠️ لم يتم العثور على تطابق كافٍ (نسبة الثقة: {int(best_score * 100)}%). تأكد من وضوح الصورة وزاويتها.", None

    # 2. البحث النصي
    user_text = (text_input or "").strip()
    if user_text:
        full_input, detected_key, found_records = search_part_number_in_all_manuals(user_text)
        found_img = None

        # بحث مرن عن الصورة
        clean_key = re.sub(r'[^a-zA-Z0-9]', '', detected_key).lower()
        for p in IMAGE_PATHS:
            fname_clean = re.sub(r'[^a-zA-Z0-9]', '', os.path.splitext(os.path.basename(p))[0]).lower()
            if clean_key and (clean_key in fname_clean or fname_clean in clean_key):
                found_img = Image.open(p)
                break

        if found_records:
            res = status_prefix + f"### 📦 تقرير المطابقة للاستفسار: `{detected_key}`\n\n"
            for idx, item in enumerate(found_records, 1):
                res += f"**{idx}. {item['machine_name']}**\n"
                res += f"* 📄 الصفحة داخل الدليل: **{item['page']}**\n"
                res += f"* ⚙️ التفاصيل الفنية: `{item['details']}`\n\n"
            return res, found_img
        else:
            return status_prefix + f"⚠️ لم يتم العثور على أي تطابق للاستفسار أو الكود `{detected_key}` داخل الكتالوجات المفهرسة.", None

    return status_prefix + "يرجى التقاط صورة للقطعة أو كتابة رقمها/كود الإنذار للبدء.", None

# ==========================================
# واجهة Gradio
# ==========================================
with gr.Blocks(title="منصة الدعم الهندسي - مسلخ شركة دواجن فلسطين") as demo:
    gr.HTML("""
    <div style="border-bottom: 2px solid #2b6cb0; padding-bottom: 10px; margin-bottom: 20px;">
        <h1 style="color: #1a365d; margin: 0; font-size: 24px;">🏢 منصة الدعم الهندسي - مسلخ شركة دواجن فلسطين</h1>
        <p style="color: #4a5568; margin: 5px 0 0 0;">نظام التعرف البصري الذكي على قطع الغيار ومطابقة الكتالوجات (Meyn / Automac)</p>
    </div>
    """)

    with gr.Row():
        with gr.Column(scale=1):
            cam_box = gr.Image(sources=["upload", "webcam"], type="pil", label="📷 صورة القطعة الميكانيكية")
            text_box = gr.Textbox(lines=1, label="📝 أو اكتب رقم القطعة / كود الإنذار (مثال: E002 أو 0098.0020.003.03)", placeholder="أدخل الرقم أو كود الخطأ...")
            search_btn = gr.Button("🔍 فحص ومطابقة بالكتالوجات", variant="primary")

        with gr.Column(scale=1):
            info_output = gr.Markdown(label="📋 التقرير الفني")
            matched_image_display = gr.Image(label="🖼️ القطعة المطابقة من المستودع")

    search_btn.click(
        fn=visual_maintenance_copilot,
        inputs=[cam_box, text_box],
        outputs=[info_output, matched_image_display]
    )

if __name__ == "__main__":
    demo.queue().launch(
        server_name="0.0.0.0",
        server_port=PORT,
        share=False,
        inbrowser=False,
        allowed_paths=["/tmp"]
    )
