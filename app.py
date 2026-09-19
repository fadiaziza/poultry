import os
import glob
import re
import io
import base64
import fitz  # PyMuPDF
import requests
from datetime import datetime
import pytz
from PIL import Image
import torch
from sentence_transformers import SentenceTransformer, util
import gradio as gr
from google.cloud import storage

# ==========================================
# 0. إعدادات السحابة والمنفذ
# ==========================================
PORT = int(os.environ.get("PORT", 8080))
BUCKET_NAME = "aziza-manuals-storage"
BASE_DIR = "/tmp/Maintenance_Manuals"
IMAGE_DIR = os.path.join(BASE_DIR, "Real_Parts_Images")

def sync_data_from_gcs():
    os.makedirs(BASE_DIR, exist_ok=True)
    os.makedirs(IMAGE_DIR, exist_ok=True)
    print(f"[*] Starting download from GCS bucket: {BUCKET_NAME}...")
    try:
        client = storage.Client()
        bucket = client.bucket(BUCKET_NAME)
        blobs = bucket.list_blobs(prefix="Maintenance_Manuals/")
        
        count = 0
        for blob in blobs:
            if blob.name.endswith("/"):
                continue
            relative_path = os.path.relpath(blob.name, "Maintenance_Manuals")
            dest_path = os.path.join(BASE_DIR, relative_path)
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            if not os.path.exists(dest_path):
                blob.download_to_filename(dest_path)
                count += 1
        print(f"[✓] GCS Sync completed. Downloaded {count} new files.")
    except Exception as e:
        print(f"[!] Warning during GCS sync: {e}")

sync_data_from_gcs()

# ==========================================
# 1. إعدادات التنبيهات (Green API)
# ==========================================
ID_INSTANCE = "710722737613"
API_TOKEN_INSTANCE = os.environ.get("GREEN_API_TOKEN", "")
ALERT_GROUP_ID = os.environ.get("ALERT_GROUP_ID", "")

def send_whatsapp_alert(message):
    if not API_TOKEN_INSTANCE or not ALERT_GROUP_ID:
        return
    url = f"https://api.green-api.com/waInstance{ID_INSTANCE}/sendMessage/{API_TOKEN_INSTANCE}"
    payload = {"chatId": ALERT_GROUP_ID, "message": message}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as err:
        print(f"[!] WhatsApp error: {err}")

# ==========================================
# 2. تحميل نماذج الذكاء الاصطناعي وبناء الفهارس
# ==========================================
device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"[*] Loading models on {device}...")

manual_pages = []

def build_manual_index():
    global manual_pages
    manual_pages = []
    pdf_files = glob.glob(os.path.join(BASE_DIR, "**/*.pdf"), recursive=True)
    print(f"[*] Indexing {len(pdf_files)} PDF manuals...")
    for pdf_path in pdf_files:
        filename = os.path.basename(pdf_path)
        try:
            doc = fitz.open(pdf_path)
            for page_num in range(len(doc)):
                page_text = doc[page_num].get_text("text").strip()
                if len(page_text) > 15:
                    manual_pages.append({
                        "filename": filename,
                        "page": page_num + 1,
                        "text": page_text
                    })
        except Exception:
            pass
    print(f"[✓] Indexed {len(manual_pages)} pages.")

build_manual_index()

# خريطة لربط صور المستودع برمز القطعة المكون من 4 مقاطع
part_images_map = {}
if os.path.exists(IMAGE_DIR):
    for f in os.listdir(IMAGE_DIR):
        if f.lower().endswith(('.jpg', '.jpeg', '.png')):
            part_no = os.path.splitext(f)[0]
            clean_key = re.sub(r'[^a-zA-Z0-9]', '', part_no).lower()
            full_path = os.path.join(IMAGE_DIR, f)
            part_images_map[clean_key] = (part_no, full_path)

# قراءة الشعار المعتمد logo.png وتحويله إلى Base64
logo_base64 = ""
for p in ["logo.png", "/app/logo.png"]:
    if os.path.exists(p):
        try:
            with open(p, "rb") as f:
                logo_base64 = base64.b64encode(f.read()).decode("utf-8")
            break
        except Exception:
            pass

# ==========================================
# 3. محرك البحث للأكواد الرباعية والإنذارات
# ==========================================
def extract_4_segment_codes(text):
    """
    استخراج كود القطعة المكون من 4 مقاطع بدقة:
    أمثلة: 0990.AD05.007.00 أو 89.3844.900.0034 أو 89 3608 904 0096
    """
    pattern_4_segments = r'([A-Za-z0-9]+)[\.\s\-_/]+([A-Za-z0-9]+)[\.\s\-_/]+([A-Za-z0-9]+)[\.\s\-_/]+([A-Za-z0-9]+)'
    matches = re.findall(pattern_4_segments, text)
    extracted = []
    for m in matches:
        extracted.append(m)  # (seg1, seg2, seg3, seg4)
    return extracted

def generate_variations(token):
    """توليد صيغ الإنذار الشائعة مثل E002 و E02 و Alarm 02"""
    variations = {token, token.replace(" ", "")}
    match = re.match(r'^([A-Za-z]+)0*(\d+)$', token)
    if match:
        prefix, num = match.groups()
        n_int = int(num)
        variations.update([
            f"{prefix}{num}",
            f"{prefix} {num}",
            f"{prefix}-{num}",
            f"{prefix}{n_int:02d}",
            f"{prefix} {n_int:02d}",
            f"{prefix}{n_int:03d}",
            f"Alarm {num}",
            f"Alarm {n_int:02d}",
            f"Error {num}",
            f"Error {n_int:02d}"
        ])
    return list(variations)

def search_manuals(query, top_k=5):
    if not manual_pages:
        return [], None
        
    clean_q = query.strip()
    
    # 1. فحص وجود كود قطعة من 4 مقاطع
    codes_4 = extract_4_segment_codes(clean_q)
    if codes_4:
        for segs in codes_4:
            # مطابقة المقاطع الأربعة سواء كانت مفصولة بنقاط أو بمسافات
            regex_pattern = r'\b' + re.escape(segs[0]) + r'[\.\s\-_]+' + re.escape(segs[1]) + r'[\.\s\-_]+' + re.escape(segs[2]) + r'[\.\s\-_]+' + re.escape(segs[3]) + r'\b'
            matched = []
            for item in manual_pages:
                if re.search(regex_pattern, item["text"], re.IGNORECASE):
                    matched.append(item)
                    if len(matched) >= top_k:
                        break
            if matched:
                reconstructed_code = ".".join(segs)
                return matched, reconstructed_code

    # 2. فحص وجود كود إنذار أو رقم مباشر
    raw_tokens = re.findall(r'[A-Za-z0-9][A-Za-z0-9\.\-_/]+', clean_q)
    for tok in raw_tokens:
        if len(tok) >= 2:
            vars_list = generate_variations(tok)
            for v in vars_list:
                pat = r'(?<![A-Za-z0-9])' + re.escape(v) + r'(?![A-Za-z0-9])'
                matched = []
                for item in manual_pages:
                    if re.search(pat, item["text"], re.IGNORECASE):
                        matched.append(item)
                        if len(matched) >= top_k:
                            break
                if matched:
                    return matched, v
                    
    # 3. مطابقة الكلمات المباشرة لمنظومات محددة
    keywords = {"automac": "ماكينة التغليف Automac", "compressor": "ضاغط/كمبرسور التبريد", "eviscerator": "مفرغة أحشاء Meyn"}
    for kw, label in keywords.items():
        if kw in clean_q.lower() or label in clean_q:
            matched = [item for item in manual_pages if re.search(r'\b' + kw + r'\b', item["text"], re.IGNORECASE)]
            if matched:
                return matched[:top_k], kw

    return [], None

def find_image_by_part_no(part_no):
    if not os.path.exists(IMAGE_DIR) or not part_no:
        return None
    clean_target = re.sub(r'[^a-zA-Z0-9]', '', part_no).lower()
    if clean_target in part_images_map:
        return part_images_map[clean_target][1]
    return None

def maintenance_copilot(query, input_image=None):
    if not query.strip() and input_image is None:
        return "⚠️ يرجى إدخال رقم القطعة المكون من 4 مقاطع أو كود الإنذار.", None
        
    clean_q = query.strip()
    hits, matched_code = search_manuals(clean_q, top_k=5)
    matched_image_path = None
    response = []

    lookup_target = matched_code if matched_code else clean_q
    matched_image_path = find_image_by_part_no(lookup_target)

    if hits:
        response.append(f"### ✅ تم العثور على تطابق دقيق للرمز `{lookup_target}`:")
        for h in hits:
            response.append(f"- **الملف:** `{h['filename']}` (صفحة {h['page']})")
            text = h['text'].replace("\r", "")
            idx = text.lower().find(lookup_target.lower().split('.')[0])
            if idx != -1:
                start = max(0, idx - 60)
                end = min(len(text), idx + len(lookup_target) + 140)
                snippet = text[start:end].replace("\n", " ").strip()
            else:
                snippet = text[:160].replace("\n", " ").strip()
            response.append(f"  > *\"...{snippet}...\"*\n")
    else:
        response.append(f"❌ لم يتم العثور على أي تطابق للرمز أو الإنذار `{clean_q}` داخل الكتالوجات المفهرسة.")

    if matched_image_path:
        response.append("\n🖼️ **تم إرفاق صورة القطعة الحقيقية المطابقة من أرشيف المستودع الميداني.**")

    # إشعار الواتساب عند وجود بلاغات
    tz = pytz.timezone('Asia/Hebron')
    timestamp = datetime.now(tz).strftime('%Y-%m-%d %I:%M %p')
    if any(k in clean_q.lower() for k in ["عطل", "انذار", "إنذار", "تالف", "طلب قطعة", "alarm"]):
        alert_msg = f"⚠️ *إشعار صيانة ومتابعة*\n⏰ الوقت: {timestamp}\n📝 البلاغ: {clean_q}\n"
        if hits:
            alert_msg += f"📖 المرجع: {hits[0]['filename']} (صفحة {hits[0]['page']})"
        send_whatsapp_alert(alert_msg)
        response.append("\n---\n📲 تم إرسال إشعار فوري لمجموعة طاقم الصيانة عبر الواتساب.")

    return "\n".join(response), matched_image_path

# ==========================================
# 4. واجهة Gradio
# ==========================================
total_manuals = len(glob.glob(os.path.join(BASE_DIR, "**/*.pdf"), recursive=True))

logo_html = f'<img src="data:image/png;base64,{logo_base64}" style="width: 100%; height: 100%; object-fit: contain;">' if logo_base64 else '<span style="font-size: 20px; font-weight: 900; color: #1b5e20;">عزيزا</span>'

HEADER_HTML = f"""
<div style="background: linear-gradient(135deg, #0b3d20 0%, #1b5e20 100%); padding: 18px 25px; border-radius: 14px; color: white; margin-bottom: 20px; box-shadow: 0 4px 15px rgba(0,0,0,0.18); direction: rtl; text-align: right; border-bottom: 4px solid #ffcc00;">
    <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 15px;">
        <div style="display: flex; align-items: center; gap: 20px;">
            <div style="background: #ffffff; border-radius: 50%; padding: 4px; box-shadow: 0 4px 12px rgba(0,0,0,0.3); display: flex; align-items: center; justify-content: center; width: 85px; height: 85px; border: 3px solid #ffcc00; overflow: hidden;">
                {logo_html}
            </div>
            <div>
                <h1 style="margin: 0; font-size: 23px; font-weight: 800; color: #ffffff;">شركة دواجن فلسطين - مسلخ عزيزا المركزي</h1>
                <p style="margin: 4px 0 0 0; font-size: 14px; color: #e8f5e9;">نظام الصيانة والتشخيص الهندسي الدقيق (خطوط Meyn • ماكينات التغليف Automac • منظومات التبريد)</p>
            </div>
        </div>
        <div style="border-right: 2px solid rgba(255,255,255,0.25); padding-right: 20px;">
            <span style="font-size: 12px; color: #c8e6c9; display: block;">إعداد وتطوير النظام:</span>
            <span style="font-size: 16px; font-weight: bold; color: #ffeb3b;">م. فادي محمود</span>
            <span style="font-size: 12px; color: #e8f5e9; display: block;">مسؤول قسم الصيانة والأتمتة</span>
        </div>
    </div>
</div>
"""

with gr.Blocks(title="منصة الصيانة الهندسية - مسلخ عزيزا") as demo:
    gr.HTML(HEADER_HTML)
    
    with gr.Row():
        status_box = gr.Markdown(f"📊 **حالة النظام:** مفهرس `{total_manuals}` كتالوج بالكامل ومطابقة أرقام القطع المكونة من 4 مقاطع.")
        
    with gr.Row():
        with gr.Column(scale=1):
            query_input = gr.Textbox(
                label="أدخل رقم القطعة (4 مقاطع) أو كود الإنذار",
                placeholder="أمثلة: 0990.AD05.007.00 أو 89.3844.900.0034 أو 89 3608 904 0096 أو E002",
                lines=2
            )
            image_input = gr.Image(type="pil", label="صورة القطعة الميدانية (اختياري)")
            submit_btn = gr.Button("فحص وتشخيص العطل / مطابقة القطعة 🔍", variant="primary")
            clear_btn = gr.Button("مسح الحقول")
            
        with gr.Column(scale=1):
            output_box = gr.Markdown(label="تقرير الفحص الفني والمطابقة")
            matched_img_output = gr.Image(type="filepath", label="صورة القطعة المطابقة من أرشيف المستودع")
            
    submit_btn.click(
        fn=maintenance_copilot,
        inputs=[query_input, image_input],
        outputs=[output_box, matched_img_output]
    )
    clear_btn.click(
        lambda: ("", None, "", None),
        outputs=[query_input, image_input, output_box, matched_img_output]
    )

if __name__ == "__main__":
    demo.launch(
        server_name="0.0.0.0",
        server_port=PORT,
        allowed_paths=["/tmp", "."]
    )
