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

# ==========================================
# 1. إعدادات المسارات والمتغيرات
# ==========================================
ID_INSTANCE = "710722737613"
API_TOKEN = "8902219901b2411cb1ebfa944bbfc3d7d499d671111c4fe18e"
MY_PHONE = "970599431267"

LOCAL_DIR = "/tmp/Maintenance_Manuals"
os.makedirs(LOCAL_DIR, exist_ok=True)

FOLDER_NAME = "Maintenance_Manuals"

visual_model = None
IMAGE_INDEX = []
IMAGE_PATHS = []
SYSTEM_LOGS = "بدء تشغيل النظام والاتصال بالسيرفر..."
is_syncing = False

def get_visual_model():
    """تحميل نموذج CLIP عند أول استخدام فقط لتوفير الذاكرة"""
    global visual_model
    if visual_model is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
        visual_model = SentenceTransformer('clip-ViT-B-32', device=device)
    return visual_model

def sync_drive_worker():
    """تحميل متسلسل للملفات والصور مع حماية من تعليق الشبكة ومعالجة أسماء Linux"""
    global SYSTEM_LOGS, IMAGE_INDEX, IMAGE_PATHS, is_syncing
    is_syncing = True
    SYSTEM_LOGS = "جاري الاتصال بـ Google Drive..."
    print(SYSTEM_LOGS, flush=True)

    try:
        credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/drive.readonly'])
        drive_service = build('drive', 'v3', credentials=credentials, cache_discovery=False)

        # البحث عن المجلد الأساسي
        query = f"name = '{FOLDER_NAME}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        res = drive_service.files().list(q=query, fields="files(id, name)").execute()
        folders = res.get('files', [])

        if not folders:
            query_shared = f"mimeType = 'application/vnd.google-apps.folder' and trashed = false and sharedWithMe = true"
            res_shared = drive_service.files().list(q=query_shared, fields="files(id, name)").execute()
            folders = [f for f in res_shared.get('files', []) if f['name'] == FOLDER_NAME]

        if not folders:
            SYSTEM_LOGS = "❌ لم يتم العثور على المجلد! تأكد من مشاركته مع حساب الخدمة."
            print(SYSTEM_LOGS, flush=True)
            is_syncing = False
            return

        folder_id = folders[0]['id']
        SYSTEM_LOGS = "تم الاتصال بالمجلد، جاري مسح وتحميل الكتالوجات والصور..."
        print(SYSTEM_LOGS, flush=True)

        def download_folder_contents(parent_id, target_dir):
            os.makedirs(target_dir, exist_ok=True)
            page_token = None
            while True:
                response = drive_service.files().list(
                    q=f"'{parent_id}' in parents and trashed = false",
                    fields="nextPageToken, files(id, name, mimeType)",
                    pageSize=100,
                    pageToken=page_token
                ).execute()

                for item in response.get('files', []):
                    mtype = item['mimeType']
                    # تنظيف الاسم من الشرطات المائلة والرموز غير المسموحة بنظام Linux
                    clean_name = re.sub(r'[\\/*?:"<>|]', '_', item['name'])
                    local_path = os.path.join(target_dir, clean_name)

                    if mtype == 'application/vnd.google-apps.folder':
                        download_folder_contents(item['id'], local_path)
                    elif any(clean_name.lower().endswith(ext) for ext in ['.pdf', '.png', '.jpg', '.jpeg', '.webp']):
                        if not os.path.exists(local_path):
                            try:
                                req = drive_service.files().get_media(fileId=item['id'])
                                with open(local_path, "wb") as f:
                                    downloader = MediaIoBaseDownload(f, req, chunksize=2*1024*1024)
                                    done = False
                                    while not done:
                                        status, done = downloader.next_chunk()
                                print(f"✅ تم تحميل: {clean_name}", flush=True)
                            except Exception as dl_err:
                                print(f"⚠️ تعذر تحميل {clean_name}: {dl_err}", flush=True)

                page_token = response.get('nextPageToken', None)
                if not page_token:
                    break

        download_folder_contents(folder_id, LOCAL_DIR)

        # فحص الصور المستخرجة وتجهيز الفهرس البصري
        valid_exts = ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.PNG", "*.JPG", "*.JPEG")
        all_imgs = []
        for ext in valid_exts:
            all_imgs.extend(glob.glob(os.path.join(LOCAL_DIR, "**", ext), recursive=True))

        target_imgs = [p for p in set(all_imgs) if "logo" not in os.path.basename(p).lower()]
        all_pdfs = glob.glob(os.path.join(LOCAL_DIR, "**", "*.pdf"), recursive=True)

        if target_imgs:
            SYSTEM_LOGS = f"جاري توليد بصمات CLIP لـ {len(target_imgs)} صورة..."
            print(SYSTEM_LOGS, flush=True)
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
        print(SYSTEM_LOGS, flush=True)

    except Exception as e:
        SYSTEM_LOGS = f"❌ خطأ في عملية المزامنة: {str(e)}"
        print(SYSTEM_LOGS, flush=True)
    finally:
        is_syncing = False

# بدء مسار المزامنة فوراً دون تأخير الخادم
threading.Thread(target=sync_drive_worker, daemon=True).start()

def get_logo_base64():
    """استخراج شعار الشركة في حال توفره بالمجلد"""
    for ext in ["logo.png", "logo.jpg", "logo.jpeg"]:
        for p in glob.glob(os.path.join(LOCAL_DIR, "**", ext), recursive=True):
            if os.path.exists(p):
                with open(p, "rb") as img_file:
                    b64 = base64.b64encode(img_file.read()).decode("utf-8")
                    mime = "image/png" if ext.endswith("png") else "image/jpeg"
                    return f"data:{mime};base64,{b64}"
    return None

def send_whatsapp_alert(query_text, info_summary):
    """إشعار واتساب لعمليات البحث"""
    try:
        url = f"https://7107.api.greenapi.com/waInstance{ID_INSTANCE}/sendMessage/{API_TOKEN}"
        local_tz = pytz.timezone("Asia/Gaza")
        now_str = datetime.now(local_tz).strftime("%I:%M %p")
        payload = {
            "chatId": f"{MY_PHONE}@c.us",
            "message": (
                f"🏭 *مسلخ شركة دواجن فلسطين - استفسار فني*\n"
                f"⏰ الوقت: {now_str}\n"
                f"🔍 الاستفسار/القطعة: {query_text}\n"
                f"📋 النتيجة: {info_summary}"
            )
        }
        requests.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=4)
    except Exception as e:
        print(f"WhatsApp Error: {e}", flush=True)

def get_clean_machine_name(pdf_path):
    fname = os.path.basename(pdf_path)
    clean_name = fname.replace(".pdf", "").replace("pdf.", "").replace("-1", "").strip()
    return f"{clean_name} — [`{fname}`]"

def search_part_number_in_all_manuals(raw_input):
    """البحث المرن عن أرقام القطع والإنذارات داخل الكتالوجات"""
    clean_input = raw_input.strip()
    
    # 1. استخراج كود الإنذار/العطل (مثل E002 أو E-02)
    alarm_match = re.search(r'\b([Ee]\s*[-_]?\s*\d{2,4})\b', clean_input)
    
    # 2. استخراج رقم القطعة الميكانيكية
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
        search_terms.update([
            raw_code,
            f"E-{digits}",
            f"E {digits}",
            f"E{int_val}",
            f"E-{int_val}",
            f"E0{int_val}",
            f"E-0{int_val}",
            f"Error {int_val}",
            f"Alarm {int_val}",
            f"Error {digits}",
            f"Alarm {digits}"
        ])
    elif core_match:
        core_number = core_match.group(1)
        detected_key = core_number
        search_terms.update([
            clean_input,
            core_number,
            f"D{core_number}",
            f"C{core_number}",
            f"H{core_number}",
            core_number.replace(".", "")
        ])
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

    status_prefix = f"> 📡 **حالة السيرفر والملفات:** `{SYSTEM_LOGS}`\n\n---\n\n"

    # 1. البحث البصري بالصورة
    if image_file is not None:
        if len(IMAGE_INDEX) == 0:
            return status_prefix + "⚠️ **قاعدة بيانات الصور غير متوفرة بعد على السيرفر.** يرجى متابعة حالة السيرفر بالأعلى حتى اكتمال التنزيل.", None

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

            send_whatsapp_alert(f"مطابقة بصرية لصورة قطعة ({percent}%)", f"رقم القطعة: {extracted_code}")

            res = status_prefix + f"### 🎯 نتيجة التعرف البصري على القطعة\n\n"
            res += f"* **نسبة التطابق البصري:** `{percent}%`\n"
            res += f"* **رقم القطعة المستخرج:** `{extracted_code}`\n\n"
            res += "### 📖 المطابقة في الكتالوجات:\n\n"

            if manual_matches:
                for idx, item in enumerate(manual_matches, 1):
                    res += f"**{idx}. {item['machine_name']}**\n"
                    res += f"* 📄 الصفحة: **{item['page']}**\n"
                    res += f"* ⚙️ التفاصيل الفنية: `{item['details']}`\n\n"
            else:
                res += "تم التعرف على شكل القطعة ولكن لم يتم العثور على أرقامها في ملفات الكتالوجات المفهرسة حالياً.\n"
            return res, matched_pil
        else:
            return status_prefix + f"⚠️ لم يتم العثور على تطابق كافٍ (نسبة الثقة: {int(best_score * 100)}%). يرجى تصوير القطعة بوضوح أو إدخال كودها.", None

    # 2. البحث النصي / أكواد الإنذار
    user_text = (text_input or "").strip()
    if user_text:
        full_input, detected_key, found_records = search_part_number_in_all_manuals(user_text)
        found_img = None

        clean_key = re.sub(r'[^a-zA-Z0-9]', '', detected_key).lower()
        full_clean = re.sub(r'[^a-zA-Z0-9]', '', full_input).lower()

        for p in IMAGE_PATHS:
            fname_clean = re.sub(r'[^a-zA-Z0-9]', '', os.path.splitext(os.path.basename(p))[0]).lower()
            if (clean_key and (clean_key in fname_clean or fname_clean in clean_key)) or (len(fname_clean) >= 4 and fname_clean in full_clean):
                found_img = Image.open(p)
                break

        summary_info = f"تم إيجاد {len(found_records)} كتالوج مطابق" if found_records else "لا يوجد تطابق"
        send_whatsapp_alert(user_text, summary_info)

        if found_records:
            res = status_prefix + f"### 📦 تقرير المطابقة للاستفسار: `{detected_key}`\n\n"
            for idx, item in enumerate(found_records, 1):
                res += f"**{idx}. {item['machine_name']}**\n"
                res += f"* 📄 الصفحة داخل الدليل: **{item['page']}**\n"
                res += f"* ⚙️ التفاصيل الفنية والحلول: `{item['details']}`\n\n"
            return res, found_img
        else:
            return status_prefix + f"⚠️ لم يتم العثور على تطابق في الكتالوجات للرمز/الرقم `{detected_key}`.\nتأكد من كتابة كود الإنذار بدقة (مثل E002 أو E-02) أو رقم القطعة.", None

    return status_prefix + "يرجى التقاط صورة للقطعة أو كتابة رقمها/كود الإنذار للبدء.", None

# ==========================================
# 2. واجهة التطبيق
# ==========================================
logo_data_url = get_logo_base64()
logo_html = f'<img src="{logo_data_url}" style="height: 75px; margin-left: 20px; border-radius: 8px; vertical-align: middle;">' if logo_data_url else ''

header_markdown = f"""
<div style="display: flex; align-items: center; justify-content: space-between; border-bottom: 2px solid #2b6cb0; padding-bottom: 12px; margin-bottom: 20px;">
    <div style="display: flex; align-items: center;">
        {logo_html}
        <div>
            <h1 style="margin: 0; color: #1a365d; font-size: 26px;">منصة الدعم الهندسي في مسلخ شركة دواجن فلسطين</h1>
            <p style="margin: 5px 0 0 0; color: #4a5568; font-size: 15px;">نظام التعرف البصري الذكي على قطع الغيار الميكانيكية ومطابقة الكتالوجات وحلول الأعطال (Meyn / Automac)</p>
        </div>
    </div>
</div>
"""

footer_markdown = """
<div style="margin-top: 40px; padding-top: 15px; border-top: 1px solid #e2e8f0; text-align: center; color: #718096; font-size: 14px;">
    ⚙️ تم تصميم وتطوير المنصة بواسطة <strong>م. فادي محمود</strong> | قسم الصيانة والدعم الهندسي — مسلخ شركة دواجن فلسطين
</div>
"""

with gr.Blocks(title="منصة الدعم الهندسي - التعرف البصري على القطع") as demo:
    gr.HTML(header_markdown)
    gr.Markdown("""
    * **فحص القطعة بالصورة:** التقط صورة واضحة للقطعة الميكانيكية، وسيتعرف النظام على شكلها، ويستخرج رقمها المصنعي، ويحدد كافة الماكينات المشتركة ورقم الصفحة.
    * **البحث النصي / أكواد الأعطال:** يمكنك إدخال رقم القطعة مباشرة، أو كتابة كود الإنذار (مثل **E002**) لمطابقة العطل في أدلة التشغيل والصيانة.
    """)

    with gr.Row():
        with gr.Column(scale=1):
            cam_box = gr.Image(sources=["upload", "webcam"], type="pil", label="📷 تصوير القطعة أو رفع صورة من الهاتف")
            text_box = gr.Textbox(lines=1, label="📝 أو اكتب رقم القطعة / كود الإنذار (اختياري)", placeholder="مثال: 0098.0020.003.03 أو E002")
            search_btn = gr.Button("🔍 فحص ومطابقة القطعة بالكتالوجات", variant="primary")

        with gr.Column(scale=1):
            info_output = gr.Markdown(label="📋 التقرير الفني والمطابقة")
            matched_image_display = gr.Image(label="🖼️ القطعة المطابقة من أرشيف المستودع")

    search_btn.click(
        fn=visual_maintenance_copilot,
        inputs=[cam_box, text_box],
        outputs=[info_output, matched_image_display]
    )

    gr.HTML(footer_markdown)

if __name__ == "__main__":
    demo.queue().launch(
        server_name="0.0.0.0",
        server_port=PORT,
        share=False,
        inbrowser=False,
        allowed_paths=["/tmp"]
    )
