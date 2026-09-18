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
# 1. إعدادات النظام والمسارات
# ==========================================
ID_INSTANCE = "710722737613"
API_TOKEN = "8902219901b2411cb1ebfa944bbfc3d7d499d671111c4fe18e"
MY_PHONE = "970599431267"

LOCAL_DIR = "/tmp/Maintenance_Manuals"
REAL_IMAGES_PATH = os.path.join(LOCAL_DIR, "Real_Parts_Images")
os.makedirs(LOCAL_DIR, exist_ok=True)
os.makedirs(REAL_IMAGES_PATH, exist_ok=True)

FOLDER_NAME = "Maintenance_Manuals"

visual_model = None
IMAGE_INDEX = []
IMAGE_PATHS = []
sync_status = "جاري مزامنة الكتالوجات من Google Drive في الخلفية..."

def get_visual_model():
    """تحميل نموذج التعرف البصري عند الحاجة فقط"""
    global visual_model
    if visual_model is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
        visual_model = SentenceTransformer('clip-ViT-B-32', device=device)
    return visual_model

def build_visual_database():
    """فهرسة صور القطع المخزنة للمطابقة البصرية"""
    global IMAGE_INDEX, IMAGE_PATHS
    valid_exts = ("*.png", "*.jpg", "*.jpeg", "*.webp")
    all_imgs = []
    for ext in valid_exts:
        all_imgs.extend(glob.glob(os.path.join(LOCAL_DIR, "**", ext), recursive=True))

    all_imgs = [p for p in set(all_imgs) if "logo" not in os.path.basename(p).lower()]
    if not all_imgs:
        return

    model = get_visual_model()
    temp_index = []
    temp_paths = []
    for path in all_imgs:
        try:
            img = Image.open(path).convert('RGB')
            embedding = model.encode(img, convert_to_tensor=True)
            temp_index.append(embedding)
            temp_paths.append(path)
        except Exception:
            continue

    if temp_index:
        IMAGE_INDEX = torch.stack(temp_index)
        IMAGE_PATHS = temp_paths

def sync_drive_background():
    """تحميل الملفات والمجلدات المتداخلة في مسار منفصل لمنع حظر تشغيل الخادم"""
    global sync_status
    try:
        credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/drive.readonly'])
        drive_service = build('drive', 'v3', credentials=credentials)

        query = f"name = '{FOLDER_NAME}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        res = drive_service.files().list(q=query, fields="files(id, name)").execute()
        folders = res.get('files', [])
        if not folders:
            sync_status = "لم يتم العثور على مجلد Maintenance_Manuals في Google Drive (يرجى مراجعة الصلاحيات)"
            print(sync_status)
            return

        folder_id = folders[0]['id']

        def fetch_files_recursive(parent_id, target_local_path):
            os.makedirs(target_local_path, exist_ok=True)
            page_token = None
            while True:
                f_res = drive_service.files().list(
                    q=f"'{parent_id}' in parents and trashed = false",
                    fields="nextPageToken, files(id, name, mimeType)",
                    pageToken=page_token
                ).execute()
                items = f_res.get('files', [])
                for item in items:
                    if item['mimeType'] == 'application/vnd.google-apps.folder':
                        sub_dir = os.path.join(target_local_path, item['name'])
                        fetch_files_recursive(item['id'], sub_dir)
                    else:
                        file_path = os.path.join(target_local_path, item['name'])
                        os.makedirs(os.path.dirname(file_path), exist_ok=True)
                        if not os.path.exists(file_path):
                            req = drive_service.files().get_media(fileId=item['id'])
                            with open(file_path, "wb") as fh:
                                downloader = MediaIoBaseDownload(fh, req)
                                done = False
                                while not done:
                                    _, done = downloader.next_chunk()
                page_token = f_res.get('nextPageToken', None)
                if not page_token:
                    break

        fetch_files_recursive(folder_id, LOCAL_DIR)
        build_visual_database()
        sync_status = "تمت مزامنة جميع الكتالوجات والصور بنجاح، والمنصة جاهزة للبحث."
        print(sync_status)
    except Exception as e:
        sync_status = f"خطأ أثناء مزامنة Google Drive: {e}"
        print(sync_status)

# تشغيل المزامنة فوراً في الخلفية
threading.Thread(target=sync_drive_background, daemon=True).start()

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
    """إرسال إشعار فوري لعمليات البحث المهمة"""
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
        print(f"WhatsApp Error: {e}")

def get_clean_machine_name(pdf_path):
    fname = os.path.basename(pdf_path)
    clean_name = fname.replace(".pdf", "").replace("pdf.", "").replace("-1", "").strip()
    return f"{clean_name} — [`{fname}`]"

def find_part_by_image(uploaded_image):
    """البحث عن القطعة بمطابقة الملامح البصرية (CLIP)"""
    if len(IMAGE_INDEX) == 0:
        return None, 0.0

    model = get_visual_model()
    uploaded_rgb = uploaded_image.convert('RGB')
    query_emb = model.encode(uploaded_rgb, convert_to_tensor=True)
    cos_scores = util.cos_sim(query_emb, IMAGE_INDEX)[0]
    best_idx = torch.argmax(cos_scores).item()
    best_score = float(cos_scores[best_idx])

    if best_score > 0.65:
        return IMAGE_PATHS[best_idx], best_score
    return None, best_score

def search_part_number_in_all_manuals(raw_input):
    """البحث في ملفات الكتالوجات عن أرقام القطع وأكواد الإنذارات والأعطال"""
    clean_input = raw_input.strip()
    
    # 1. استخراج كود الإنذار/العطل (مثلاً: E002, E-02, Error 02, Alarm 002)
    alarm_match = re.search(r'\b([Ee]\s*[-_]?\s*\d{2,4})\b', clean_input)
    
    # 2. استخراج رقم القطعة الميكانيكية القياسي (مثلاً: 0098.0020.003.03 أو 0000.D409.003.01)
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
        # إضافة كافة صيغ كتابة كود الإنذار في الكتالوجات
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
        # إذا أدخل المستخدم كلمات أو أرقام غير مطابقة للصيغ أعلاه
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
                        "manual_file": doc_name,
                        "machine_name": get_clean_machine_name(pdf_path),
                        "page": page_num + 1,
                        "details": " | ".join(snippet) if snippet else "مطابقة مسجلة في الدليل الفني."
                    })
                    if len(matches) >= 6:
                        break
            doc.close()
        except Exception:
            continue

    return clean_input, detected_key, matches

def visual_maintenance_copilot(image_file, text_input):
    user_text = (text_input or "").strip()

    all_pdfs = glob.glob(f"{LOCAL_DIR}/**/*.pdf", recursive=True)
    if not all_pdfs:
        return f"⏳ حالة النظام: {sync_status}\nيرجى الانتظار قليلاً لإتمام مزامنة الملفات من Google Drive والمحاولة مجدداً.", None

    # مسار التعرف البصري بالصورة
    if image_file is not None:
        matched_img_path, similarity_score = find_part_by_image(image_file)
        if matched_img_path:
            img_filename = os.path.basename(matched_img_path)
            extracted_code = os.path.splitext(img_filename)[0]

            clean_input, core_num, manual_matches = search_part_number_in_all_manuals(extracted_code)
            matched_pil = Image.open(matched_img_path)
            percent = int(similarity_score * 100)
            send_whatsapp_alert(f"مطابقة بصرية لصورة قطعة ({percent}%)", f"رقم القطعة: {extracted_code}")

            res = f"### 🎯 نتيجة التعرف البصري على القطعة\n\n"
            res += f"* **نسبة التطابق البصري:** `{percent}%`\n"
            res += f"* **رقم القطعة المستخرج:** `{extracted_code}`\n\n"
            res += "--- \n### 📖 المطابقة في الكتالوجات والماكينات المشتركة:\n\n"

            if manual_matches:
                for idx, item in enumerate(manual_matches, 1):
                    res += f"**{idx}. {item['machine_name']}**\n"
                    res += f"* 📄 **الصفحة داخل الدليل:** صفحة **{item['page']}**\n"
                    res += f"* ⚙️ **البيانات الفنية:** `{item['details']}`\n\n"
            else:
                res += "تم التعرف على شكل القطعة، ولكن لم يتم العثور على أرقامها في ملفات PDF المفهرسة حالياً.\n"
            return res, matched_pil
        else:
            return (
                f"⚠️ لم يتم العثور على تطابق كافٍ لشكل هذه القطعة (نسبة الثقة: {int(similarity_score * 100)}%).\n"
                "يرجى تصوير القطعة بوضوح أو إدخال رقمها نصياً.",
                None
            )

    # مسار البحث النصي (أرقام قطع أو أكواد أعطال وإنذارات)
    if user_text:
        full_input_code, detected_key, found_records = search_part_number_in_all_manuals(user_text)
        found_img = None
        
        # البحث المرن عن صورة القطعة المطابقة
        clean_search_key = re.sub(r'[^a-zA-Z0-9]', '', detected_key).lower()
        full_clean_input = re.sub(r'[^a-zA-Z0-9]', '', full_input_code).lower()

        for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
            for p in glob.glob(os.path.join(LOCAL_DIR, "**", ext), recursive=True):
                fname = os.path.splitext(os.path.basename(p))[0].lower()
                clean_fname = re.sub(r'[^a-zA-Z0-9]', '', fname)
                
                if "logo" in clean_fname:
                    continue
                
                if (clean_search_key and (clean_search_key in clean_fname or clean_fname in clean_search_key)) or \
                   (len(clean_fname) >= 4 and clean_fname in full_clean_input):
                    try:
                        found_img = Image.open(p)
                        break
                    except Exception:
                        continue
            if found_img:
                break

        summary_info = f"تم إيجاد {len(found_records)} دليل/كتالوج مطابق" if found_records else "لا يوجد تطابق"
        send_whatsapp_alert(user_text, summary_info)

        if found_records:
            res = f"### 📦 تقرير المطابقة للاستفسار: `{detected_key}`\n\n"
            for idx, item in enumerate(found_records, 1):
                res += f"**{idx}. {item['machine_name']}**\n"
                res += f"* 📄 **الصفحة داخل الدليل:** صفحة **{item['page']}**\n"
                res += f"* ⚙️ **التفاصيل الفنية والحلول:** `{item['details']}`\n\n"
            return res, found_img
        else:
            return f"⚠️ لم يتم العثور على تطابق في الكتالوجات للرمز/الرقم `{detected_key}`.\nتأكد من كتابة كود الإنذار بدقة (مثل E002 أو E-02) أو رقم القطعة.", None

    return "يرجى التقاط صورة للقطعة أو إدخال رقمها/كود الإنذار في خانة البحث.", None

# ==========================================
# 2. الواجهة الرسومية الرسمية
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
            matched_image_display = gr.Image(label="🖼️ القطعة المطابقة من أرشيف المستودع", visible=True)

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
