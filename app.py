import os
import glob
import re
import io
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
# 0. إعدادات المنفذ والبيئة السحابية
# ==========================================
PORT = int(os.environ.get("PORT", 8080))

BUCKET_NAME = "aziza-manuals-storage"
BASE_DIR = "/tmp/Maintenance_Manuals"
IMAGE_DIR = os.path.join(BASE_DIR, "Real_Parts_Images")

def sync_data_from_gcs():
    """مزامنة الكتالوجات والصور من Google Cloud Storage إلى /tmp"""
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

# مزامنة الملفات من السحابة عند بدء التشغيل
sync_data_from_gcs()

# ==========================================
# 1. إعدادات النظام وتنبيهات الواتساب
# ==========================================
ID_INSTANCE = "710722737613"
API_TOKEN_INSTANCE = os.environ.get("GREEN_API_TOKEN", "")
ALERT_GROUP_ID = os.environ.get("ALERT_GROUP_ID", "")

def send_whatsapp_alert(message):
    """إرسال تنبيهات الأعطال والقطع المطلوبة إلى مجموعة الواتساب"""
    if not API_TOKEN_INSTANCE or not ALERT_GROUP_ID:
        return
    url = f"https://api.green-api.com/waInstance{ID_INSTANCE}/sendMessage/{API_TOKEN_INSTANCE}"
    payload = {
        "chatId": ALERT_GROUP_ID,
        "message": message
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as err:
        print(f"[!] WhatsApp notification error: {err}")

# ==========================================
# 2. تحميل نموذج الذكاء الاصطناعي وفهرسة الكتالوجات
# ==========================================
print("[*] Loading embedding model...")
device = "cuda" if torch.cuda.is_available() else "cpu"
text_model = SentenceTransformer("all-MiniLM-L6-v2", device=device)

manual_pages = []
manual_embeddings = None

def build_manual_index():
    """قراءة وفهرسة صفحات كتالوجات PDF من المجلد المحلي"""
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
            print(f"Error reading {filename}: {e}")
            
    if texts:
        manual_embeddings = text_model.encode(texts, convert_to_tensor=True, show_progress_bar=False)
        print(f"[✓] Successfully indexed {len(texts)} pages from {len(pdf_files)} manuals.")
    else:
        print("[!] No manual pages found or indexed.")

build_manual_index()

# ==========================================
# 3. محرك البحث الهجين ومطابقة صور القطع
# ==========================================
def find_part_image(part_number):
    """البحث عن صورة حقيقية مطابقة لرقم القطعة في المجلد"""
    if not os.path.exists(IMAGE_DIR):
        return None
        
    clean_num = part_number.strip()
    
    # 1. مطابقة مباشرة لاسم الملف مع الامتداد
    for ext in [".jpg", ".jpeg", ".png", ".JPG", ".PNG"]:
        exact_path = os.path.join(IMAGE_DIR, f"{clean_num}{ext}")
        if os.path.exists(exact_path):
            return exact_path
            
    # 2. مطابقة مرنة تتجاهل النقاط والشرطات وحالة الأحرف
    clean_flat = re.sub(r'[^a-zA-Z0-9]', '', clean_num).lower()
    if clean_flat and len(clean_flat) >= 4:
        for fname in os.listdir(IMAGE_DIR):
            base, ext = os.path.splitext(fname)
            fname_flat = re.sub(r'[^a-zA-Z0-9]', '', base).lower()
            if clean_flat in fname_flat or fname_flat in clean_flat:
                return os.path.join(IMAGE_DIR, fname)
                
    return None

def search_manuals(query, top_k=5):
    """البحث الدقيق للأرقام والأكواد، والدلالي للجمل التقنية"""
    if not manual_pages:
        return [], "none"
    
    clean_query = query.strip()
    pattern = re.escape(clean_query)
    
    # أولاً: البحث الحرفي الدقيق للأرقام والأكواد
    exact_results = []
    for item in manual_pages:
        if re.search(pattern, item["text"], re.IGNORECASE):
            exact_results.append(item)
            if len(exact_results) >= top_k:
                break
                
    if exact_results:
        return exact_results, "exact"

    # ثانياً: البحث الدلالي للجمل والوصف (كلمتين فأكثر) مع تصفية الدرجات الضعيفة
    if manual_embeddings is not None and len(clean_query.split()) > 1:
        query_emb = text_model.encode(clean_query, convert_to_tensor=True)
        hits = util.semantic_search(query_emb, manual_embeddings, top_k=top_k)[0]
        semantic_results = [manual_pages[hit['corpus_id']] for hit in hits if hit['score'] >= 0.40]
        if semantic_results:
            return semantic_results, "semantic"
        
    return [], "none"

def maintenance_copilot(query, input_image=None):
    if not query.strip():
        return "⚠️ يرجى إدخال رقم القطعة أو رمز الإنذار أو وصف العطل.", None
        
    clean_q = query.strip()
    hits, match_type = search_manuals(clean_q, top_k=5)
    matched_image_path = find_part_image(clean_q)
    
    response = []
    
    if hits:
        if match_type == "exact":
            response.append(f"### ✅ تم العثور على تطابق دقيق للرمز `{clean_q}` في الكتالوجات:")
        else:
            response.append(f"### 📚 نتائج الكتالوجات المطابقة للوصف: *\"{clean_q}\"*")
            
        for h in hits:
            response.append(f"- **الملف:** `{h['filename']}` (صفحة {h['page']})")
            
            # اقتطاع سياق ظهور الرقم أو النص
            text = h['text'].replace("\n", " ")
            idx = text.lower().find(clean_q.lower())
            if idx != -1:
                start = max(0, idx - 50)
                end = min(len(text), idx + len(clean_q) + 80)
                snippet = text[start:end]
            else:
                snippet = text[:150]
            response.append(f"  > *\"{snippet.strip()}\"*\n")
    else:
        response.append(f"❌ لم يتم العثور على أي تطابق للرمز أو النص `{clean_q}` داخل صفحات الكتالوجات المفهرسة.")

    if matched_image_path:
        response.append("\n🖼️ **تم العثور على صورة القطعة الحقيقية من قاعدة بيانات المستودع (انظر لوحة الصورة أدناه).**")

    # تنبيه الواتساب عند وجود بلاغات أعطال
    tz = pytz.timezone('Asia/Hebron')
    timestamp = datetime.now(tz).strftime('%Y-%m-%d %I:%M %p')
    
    if any(k in clean_q.lower() for k in ["عطل", "كسر", "تالف", "طلب قطعة", "alarm", "broken"]):
        alert_msg = f"⚠️ *إشعار صيانة ومتابعة*\n⏰ الوقت: {timestamp}\n📝 الطلب: {clean_q}\n"
        if hits:
            alert_msg += f"📖 المرجع: {hits[0]['filename']} (صفحة {hits[0]['page']})"
        send_whatsapp_alert(alert_msg)
        response.append("\n---\n📲 تم إرسال إشعار فوري لمجموعة طاقم الصيانة عبر الواتساب.")

    return "\n".join(response), matched_image_path

# ==========================================
# 4. بناء واجهة المستخدم Gradio مع الترويسة والشعار
# ==========================================
total_manuals = len(glob.glob(os.path.join(BASE_DIR, "**/*.pdf"), recursive=True))

HEADER_HTML = """
<div style="background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%); padding: 25px; border-radius: 12px; color: white; margin-bottom: 20px; box-shadow: 0 4px 15px rgba(0,0,0,0.15); direction: rtl; text-align: right;">
    <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 15px;">
        <div style="display: flex; align-items: center; gap: 20px;">
            <div style="background: white; border-radius: 10px; padding: 8px 14px; box-shadow: 0 2px 8px rgba(0,0,0,0.1);">
                <span style="font-size: 26px; font-weight: 900; color: #d32f2f; letter-spacing: 1px;">AZIZA</span>
                <span style="font-size: 13px; font-weight: bold; color: #1e3c72; display: block; text-align: center;">عـزيـزة</span>
            </div>
            <div>
                <h1 style="margin: 0; font-size: 24px; font-weight: 800; color: #ffffff;">شركة دواجن فلسطين - مسلخ عزيزا</h1>
                <p style="margin: 4px 0 0 0; font-size: 15px; color: #e0e0e0;">المنصة الهندسية الذكية لمطابقة كتالوجات الصيانة وقطع الغيار (Meyn / Automac)</p>
            </div>
        </div>
        <div style="border-right: 2px solid rgba(255,255,255,0.3); padding-right: 20px;">
            <span style="font-size: 13px; color: #cfd8dc; display: block;">إعداد وتصميم النظام:</span>
            <span style="font-size: 16px; font-weight: bold; color: #ffeb3b;">م. فادي محمود</span>
            <span style="font-size: 12px; color: #b0bec5; display: block;">إدارة الصيانة والتشغيل</span>
        </div>
    </div>
</div>
"""

with gr.Blocks(title="منصة الدعم الهندسي - مسلخ عزيزا") as demo:
    gr.HTML(HEADER_HTML)
    
    with gr.Row():
        status_box = gr.Markdown(f"📊 **حالة النظام:** تم تحميل وفهرسة `{total_manuals}` كتالوج فني بالكامل من الحاوية السحابية ومزامنة صور المستودع.")
        
    with gr.Row():
        with gr.Column(scale=1):
            query_input = gr.Textbox(
                label="أدخل رقم القطعة / كود الإنذار / وصف العطل",
                placeholder="مثال: 0115.D276.000.07 أو 0990.WA02.080.00 أو Scalder...",
                lines=2
            )
            image_input = gr.Image(type="pil", label="صورة فوتوغرافية من الموقع (اختياري للتحليل)")
            submit_btn = gr.Button("فحص ومطابقة القطعة بالكتالوجات 🔍", variant="primary")
            clear_btn = gr.Button("مسح الحقول")
            
        with gr.Column(scale=1):
            output_box = gr.Markdown(label="تقرير المطابقة الهندسي")
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
    demo.queue().launch(
        server_name="0.0.0.0",
        server_port=PORT
    )
