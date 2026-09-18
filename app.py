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
# 1. إعدادات النظام وبيئة Google Cloud
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
    global visual_model
    if visual_model is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
        visual_model = SentenceTransformer('clip-ViT-B-32', device=device)
    return visual_model

def build_visual_database():
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
    """تحميل الملفات والمجلدات في مسار منفصل لتجنب تأخير فتح المنفذ"""
    global sync_status
    try:
        credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/drive.readonly'])
        drive_service = build('drive', 'v3', credentials=credentials)

        query = f"name = '{FOLDER_NAME}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        res = drive_service.files().list(q=query, fields="files(id, name)").execute()
        folders = res.get('files', [])
        if not folders:
            sync_status = "لم يتم العثور على مجلد Maintenance_Manuals في Google Drive"
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
        sync_status = "تمت مزامنة جميع الكتالوجات بنجاح والمنصة جاهزة للبحث بالكامل."
        print(sync_status)
    except Exception as e:
        sync_status = f"خطأ أثناء مزامنة Google Drive: {e}"
        print(sync_status)

# إطلاق المزامنة في الخلفية فور تشغيل الكود
threading.Thread(target=sync_drive_background, daemon=True).start()

def get_logo_base64():
    for ext in ["logo.png", "logo.jpg", "logo.jpeg"]:
        for p in glob.glob(os.path.join(LOCAL_DIR, "**", ext), recursive=True):
            if os.path.exists(p):
                with open(p, "rb") as img_file:
                    b64 = base64.b64encode(img_file.read()).decode("utf-8")
                    mime = "image/png" if ext.endswith("png") else "image/jpeg"
                    return f"data:{mime};base64,{b64}"
    return None

def send_whatsapp_alert(query_text, info_summary):
    try:
        url = f"https://7107.api.greenapi.com/waInstance{ID_INSTANCE}/sendMessage/{API_TOKEN}"
        local_tz = pytz.timezone("Asia/Gaza")
        now_str = datetime.now(local_tz).strftime("%I:%M %p")
        payload = {
            "chatId": f"{MY_PHONE}@c.us",
            "message": (
                f"🏭 *مسلخ شركة دواجن فلسطين - فحص ومطابقة*\n"
                f"⏰ الوقت: {now_str}\n"
                f"🔍 الاستفسار: {query_text}\n"
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
    clean_input = raw_input.strip()
    core_match = re.search(r'(\d{3}\.\d{3}\.\d{2})', clean_input)
    core_number = core_match.group(1) if core_match else clean_input

    search_targets = {clean_input, core_number, f"D{core_number}", f"C{core_number}", f"H{core_number}", core_number.replace(".", "")}
    valid_targets = [re.escape(t) for t in search_targets if len(t) >= 4]
    search_regex = re.compile("|".join(valid_targets), re.IGNORECASE)

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
                            snippet = lines[max(0, i - 1):min(len(lines), i + 4)]
                            break
                    matches.append({
                        "manual_file": doc_name,
                        "machine_name": get_clean_machine_name(pdf_path),
                        "page": page_num + 1,
                        "details": " | ".join(snippet) if snippet else "مطابقة مسجلة في جدول الأجزاء."
                    })
                    break
            doc.close()
        except Exception:
            continue
    return clean_input, core_number, matches

def visual_maintenance_copilot(image_file, text_input):
    user_text = (text_input or "").strip()

    all_pdfs = glob.glob(f"{LOCAL_DIR}/**/*.pdf", recursive=True)
    if not all_pdfs:
        return f"⏳ حالة النظام: {sync_status}\nيرجى الانتظار دقيقة لإتمام مزامنة الملفات من Google Drive والمحاولة مجدداً.", None

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
            res += f"* **رقم القطعة المعتمد:** `{extracted_code}`\n\n"
            res += "--- \n### 📖 بيانات الكتالوجات والماكينات المشتركة:\n\n"

            if manual_matches:
                for idx, item in enumerate(manual_matches, 1):
                    res += f"**{idx}. {item['machine_name']}**\n"
                    res += f"* 📄 **الصفحة داخل الدليل:** صفحة **{item['page']}**\n"
                    res += f"* ⚙️ **البيانات والمواصفات:** `{item['details']}`\n\n"
            else:
                res += "تم التعرف على القطعة، ولكن لم يتم العثور على أرقامها في ملفات PDF المتاحة.\n"
            return res, matched_pil
        else:
            return (
                f"⚠️ لم يتم العثور على تطابق كافٍ لشكل هذه القطعة (نسبة الثقة: {int(similarity_score * 100)}%).\n"
                "يرجى تصوير القطعة بزاوية أوضح أو إدخال رقمها نصياً.",
                None
            )

    if user_text:
        full_input_code, core_num, found_records = search_part_number_in_all_manuals(user_text)
        found_img = None
        for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
            for p in glob.glob(os.path.join(LOCAL_DIR, "**", ext), recursive=True):
                if core_num.lower() in os.path.basename(p).lower():
                    found_img = Image.open(p)
                    break
            if found_img:
                break

        summary_info = f"تم إيجاد {len(found_records)} كتالوج مطابق" if found_records else "لا يوجد تطابق"
        send_whatsapp_alert(user_text, summary_info)

        if found_records:
            res = f"### 📦 تقرير مطابقة قطعة الغيار: `{full_input_code}`\n\n"
            for idx, item in enumerate(found_records, 1):
                res += f"**{idx}. {item['machine_name']}**\n"
                res += f"* 📄 **الصفحة داخل الدليل:** صفحة **{item['page']}**\n"
                res += f"* ⚙️ **البيانات والمواصفات:** `{item['details']}`\n\n"
            return res, found_img
        else:
            return f"⚠️ لم يتم العثور على تطابق للرقم `{full_input_code}`.", None

    return "يرجى التقاط صورة للقطعة أو إدخال رقمها في خانة البحث.", None

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
            <p style="margin: 5px 0 0 0; color: #4a5568; font-size: 15px;">نظام التعرف البصري الذكي على قطع الغيار الميكانيكية ومطابقة الكتالوجات (Meyn / Automac)</p>
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
    * **فحص القطعة بالصورة:** التقط صورة واضحة للقطعة الميكانيكية (أو ارفعها من الهاتف)، وسيتعرف النظام على شكلها، يستخرج رقمها المصنعي، ويحدد كافة الماكينات المشتركة ورقم الصفحة.
    * **البحث النصي البديل:** يمكنك أيضاً كتابة رقم القطعة مباشرة إذا كان متوفراً لديك.
    """)

    with gr.Row():
        with gr.Column(scale=1):
            cam_box = gr.Image(sources=["upload", "webcam"], type="pil", label="📷 تصوير القطعة أو رفع صورة من الهاتف")
            text_box = gr.Textbox(lines=1, label="📝 أو اكتب رقم القطعة مباشرة (اختياري)", placeholder="مثال: 0000.D409.003.01")
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
    demo.launch(server_name="0.0.0.0", server_port=PORT)
