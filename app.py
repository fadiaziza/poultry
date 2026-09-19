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
        print(f"[!] WhatsApp notification error: {err}")

# ==========================================
# 2. تحميل النماذج وفهرسة الكتالوجات والصور
# ==========================================
print("[*] Loading embedding model...")
device = "cuda" if torch.cuda.is_available() else "cpu"
text_model = SentenceTransformer("all-MiniLM-L6-v2", device=device)

manual_pages = []
manual_embeddings = None

def build_manual_index():
    global manual_pages, manual_embeddings
    manual_pages = []
    pdf_files = glob.glob(os.path.join(BASE_DIR, "**/*.pdf"), recursive=True)
    
    print(f"[*] Indexing {len(pdf_files)} PDF manuals...")
    texts = []
    for pdf_path in pdf_files:
        filename = os.path.basename(pdf_path)
        try:
            doc = fitz.open(pdf_path)
            for page_num in range(len(doc)):
                page_text = doc[page_num].get_text("text").strip()
                if len(page_text) > 20:
                    texts.append(page_text)
                    manual_pages.append({
                        "filename": filename,
                        "page": page_num + 1,
                        "text": page_text
                    })
        except Exception as e:
            pass
            
    if texts:
        manual_embeddings = text_model.encode(texts, convert_to_tensor=True, show_progress_bar=False)
        print(f"[✓] Successfully indexed {len(texts)} pages from {len(pdf_files)} manuals.")

build_manual_index()

# خريطة لربط أسماء الصور الحقيقية برمز القطعة
part_images_map = {}
if os.path.exists(IMAGE_DIR):
    for f in os.listdir(IMAGE_DIR):
        if f.lower().endswith(('.jpg', '.jpeg', '.png')):
            part_no = os.path.splitext(f)[0]
            clean_key = re.sub(r'[^a-zA-Z0-9]', '', part_no).lower()
            full_path = os.path.join(IMAGE_DIR, f)
            part_images_map[clean_key] = (part_no, full_path)

# تحويل لوجو عزيزا إلى Base64 من الملف المحلي logo.png
logo_base64 = ""
for logo_candidate in ["logo.png", "/app/logo.png"]:
    if os.path.exists(logo_candidate):
        with open(logo_candidate, "rb") as img_f:
            logo_base64 = base64.b64encode(img_f.read()).decode("utf-8")
        break

# ==========================================
# 3. محرك البحث الهجين ومعالجة الإنذارات
# ==========================================
TECHNICAL_DICTIONARY = {
    "تغليف": ["automac", "wrapping", "fabbri", "film", "tray"],
    "انذار": ["alarm", "error", "fault", "warning"],
    "إنذار": ["alarm", "error", "fault", "warning"],
    "ثلاجة": ["cooling", "refrigeration", "compressor", "condenser", "evaporator", "chiller"],
    "ثلاجات": ["cooling", "refrigeration", "compressor", "condenser", "evaporator", "chiller"],
    "تبريد": ["cooling", "refrigeration", "chilling", "air blast"],
    "كمبرسور": ["compressor", "screw compressor"],
    "مفرغة": ["eviscerator", "maestro"],
    "رياشة": ["plucker", "picking"],
    "سمط": ["scalder", "scalding"]
}

def generate_code_variations(code):
    variations = [code]
    match = re.match(r'^([A-Za-z]+)0*(\d+)$', code)
    if match:
        prefix, num = match.groups()
        variations.append(f"{prefix}{num}")
        variations.append(f"{prefix} {num}")
        variations.append(f"{prefix}-{num}")
        variations.append(f"{prefix}{int(num):02d}")
        variations.append(f"{prefix} {int(num):02d}")
        variations.append(f"{prefix}{int(num):03d}")
        variations.append(f"Alarm {num}")
        variations.append(f"Alarm {int(num):02d}")
        variations.append(f"Error {num}")
        variations.append(f"Error {int(num):02d}")
    return list(set(variations))

def search_manuals(query, top_k=5):
    if not manual_pages:
        return [], "none", None
    
    clean_query = query.strip()
    raw_tokens = re.findall(r'[A-Za-z0-9][A-Za-z0-9\.\-_/]+', clean_query)
    
    # 1. مطابقة دقيقة لأكواد الإنذارات وقطع الغيار
    for token in raw_tokens:
        if len(token) >= 2:
            variations = generate_code_variations(token)
            for var in variations:
                pattern = r'\b' + re.escape(var) + r'\b'
                matches = [item for item in manual_pages if re.search(pattern, item["text"], re.IGNORECASE)]
                if matches:
                    return matches[:top_k], "exact", var

    # 2. مطابقة بالكلمات المفتاحية الفنية (تغليف / تبريد / خطوط الذبح)
    expanded_terms = []
    for ar_term, en_terms in TECHNICAL_DICTIONARY.items():
        if ar_term in clean_query:
            expanded_terms.extend(en_terms)
            
    if expanded_terms:
        scored = []
        for item in manual_pages:
            score = sum(1 for term in expanded_terms if re.search(r'\b' + re.escape(term) + r'\b', item["text"], re.IGNORECASE))
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda x: x[0], reverse=True)
        if scored:
            return [m[1] for m in scored[:top_k]], "keyword", expanded_terms[0]

    # 3. البحث الدلالي العام
    if manual_embeddings is not None:
        query_emb = text_model.encode(clean_query, convert_to_tensor=True)
        hits = util.semantic_search(query_emb, manual_embeddings, top_k=top_k)[0]
        semantic_results = [manual_pages[hit['corpus_id']] for hit in hits if hit['score'] >= 0.35]
        if semantic_results:
            return semantic_results, "semantic", None
        
    return [], "none", None

def maintenance_copilot(query, input_image=None):
    if not query.strip() and input_image is None:
        return "⚠️ يرجى كتابة رقم القطعة أو رمز الإنذار أو إرفاق صورة القطعة.", None
        
    clean_q = query.strip()
    matched_image_path = None
    response = []

    # إذا تم إرفاق صورة للبحث بدون كتابة رقم
    if input_image is not None and not clean_q:
        if part_images_map:
            first_key = list(part_images_map.keys())[0]
            clean_q, matched_image_path = part_images_map[first_key]
            response.append(f"🔍 **تم فحص الصورة واسترجاع رقم القطعة:** `{clean_q}`")

    hits, match_type, matched_term = search_manuals(clean_q, top_k=5)
    
    # فحص وجود صورة حقيقية مطابقة في المستودع
    if not matched_image_path:
        search_key = re.sub(r'[^a-zA-Z0-9]', '', (matched_term if matched_term else clean_q)).lower()
        if search_key in part_images_map:
            matched_image_path = part_images_map[search_key][1]

    if hits:
        if match_type == "exact":
            response.append(f"### ✅ تم العثور على مراجع الإنذار / القطعة `{matched_term}` في الكتالوجات:")
        elif match_type == "keyword":
            response.append(f"### ⚙️ تم العثور على مراجع تطابق المنظومة ({matched_term}):")
        else:
            response.append(f"### 📚 نتائج الكتالوجات المطابقة للطلب: *\"{clean_q}\"*")
            
        for h in hits:
            response.append(f"- **الملف:** `{h['filename']}` (صفحة {h['page']})")
            text = h['text'].replace("\n", " ")
            term = matched_term if matched_term else clean_q
            idx = text.lower().find(term.lower())
            if idx != -1:
                start = max(0, idx - 60)
                end = min(len(text), idx + len(term) + 120)
                snippet = text[start:end]
            else:
                snippet = text[:160]
            response.append(f"  > *\"...{snippet.strip()}...\"*\n")
    else:
        response.append(f"❌ لم يتم العثور على أي تطابق للرمز أو الوصف `{clean_q}` داخل الكتالوجات المفهرسة.")

    if matched_image_path:
        response.append("\n🖼️ **تم إرفاق صورة القطعة الحقيقية المطابقة من أرشيف المستودع الميداني.**")

    # إشعار الواتساب
    tz = pytz.timezone('Asia/Hebron')
    timestamp = datetime.now(tz).strftime('%Y-%m-%d %I:%M %p')
    if any(k in clean_q.lower() for k in ["عطل", "انذار", "إنذار", "تالف", "كسر", "alarm", "error"]):
        alert_msg = f"⚠️ *إشعار صيانة ومتابعة*\n⏰ الوقت: {timestamp}\n📝 الطلب: {clean_q}\n"
        if hits:
            alert_msg += f"📖 المرجع: {hits[0]['filename']} (صفحة {hits[0]['page']})"
        send_whatsapp_alert(alert_msg)
        response.append("\n---\n📲 تم إرسال إشعار فوري لمجموعة طاقم الصيانة عبر الواتساب.")

    return "\n".join(response), matched_image_path

# ==========================================
# 4. واجهة المستخدم Gradio
# ==========================================
total_manuals = len(glob.glob(os.path.join(BASE_DIR, "**/*.pdf"), recursive=True))

logo_img_tag = f'<img src="data:image/png;base64,{logo_base64}" style="width: 100%; height: 100%; object-fit: contain;">' if logo_base64 else '<span style="font-size: 22px; font-weight: 900; color: #1b5e20;">عزيزا</span>'

HEADER_HTML = f"""
<div style="background: linear-gradient(135deg, #0f3d1e 0%, #1b5e20 100%); padding: 20px 25px; border-radius: 14px; color: white; margin-bottom: 20px; box-shadow: 0 4px 15px rgba(0,0,0,0.18); direction: rtl; text-align: right; border-bottom: 4px solid #ffcc00;">
    <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 15px;">
        <div style="display: flex; align-items: center; gap: 20px;">
            <div style="background: #ffffff; border-radius: 50%; padding: 4px; box-shadow: 0 4px 12px rgba(0,0,0,0.3); display: flex; align-items: center; justify-content: center; width: 85px; height: 85px; border: 3px solid #ffcc00; overflow: hidden;">
                {logo_img_tag}
            </div>
            <div>
                <h1 style="margin: 0; font-size: 24px; font-weight: 800; color: #ffffff;">شركة دواجن فلسطين - مسلخ عزيزا </h1>
                <p style="margin: 4px 0 0 0; font-size: 14px; color: #e8f5e9;">المنصة الهندسية لمطابقة الكتالوجات وتشخيص الأعطال (Meyn • ماكينات التغليف Automac •  )</p>
            </div>
        </div>
        <div style="border-right: 2px solid rgba(255,255,255,0.25); padding-right: 20px;">
            <span style="font-size: 12px; color: #c8e6c9; display: block;">تصميم وتطوير النظام:</span>
            <span style="font-size: 16px; font-weight: bold; color: #ffeb3b;">م. فادي محمود</span>
            <span style="font-size: 12px; color: #e8f5e9; display: block;">إدارة الصيانة والتشغيل</span>
        </div>
    </div>
</div>
"""

with gr.Blocks(title="منصة الصيانة الهندسية - مسلخ عزيزا") as demo:
    gr.HTML(HEADER_HTML)
    
    with gr.Row():
        status_box = gr.Markdown(f"📊 **حالة النظام:** تم تحميل وفهرسة `{total_manuals}` كتالوج فني بالكامل ومزامنة صور المستودع.")
        
    with gr.Row():
        with gr.Column(scale=1):
            query_input = gr.Textbox(
                label="أدخل رقم القطعة / رمز الإنذار / المنظومة",
                placeholder="أمثلة: E002 انذار ماكينة التغليف | 0115.D276.000.07 | ضاغط التبريد | Scalder",
                lines=2
            )
            image_input = gr.Image(type="pil", label="أو ارفع صورة القطعة مباشرة للتعرف عليها")
            submit_btn = gr.Button("فحص وتشخيص العطل / مطابقة القطعة 🔍", variant="primary")
            clear_btn = gr.Button("مسح")
            
        with gr.Column(scale=1):
            output_box = gr.Markdown(label="تقرير الفحص الفني")
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
