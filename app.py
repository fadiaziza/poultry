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
# 0. إعدادات المنفذ والبيئة السحابية
# ==========================================
PORT = int(os.environ.get("PORT", 8080))

# إعدادات التخزين السحابي GCS
BUCKET_NAME = "aziza-manuals-storage"
BASE_DIR = "/tmp/Maintenance_Manuals"
IMAGE_DIR = os.path.join(BASE_DIR, "Real_Parts_Images")

def sync_data_from_gcs():
    """مزامنة الكتالوجات والصور من Google Cloud Storage إلى الذاكرة السريعة /tmp"""
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

# بدء المزامنة فور تشغيل التطبيق
sync_data_from_gcs()

# ==========================================
# 1. إعدادات النظام وتنبيهات الواتساب
# ==========================================
ID_INSTANCE = "710722737613"
API_TOKEN_INSTANCE = os.environ.get("GREEN_API_TOKEN", "YOUR_GREEN_API_TOKEN_HERE")
ALERT_GROUP_ID = os.environ.get("ALERT_GROUP_ID", "YOUR_GROUP_ID@g.us")

def send_whatsapp_alert(message):
    """إرسال تنبيهات الأعطال والقطع المطلوبة إلى مجموعة الواتساب"""
    if not API_TOKEN_INSTANCE or "YOUR_" in API_TOKEN_INSTANCE:
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
# 2. تحميل نماذج الذكاء الاصطناعي والفهرسة
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
                if len(page_text) > 30:
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
        print(f"[✓] Successfully indexed {len(texts)} manual sections.")
    else:
        print("[!] No manual pages found or indexed.")

build_manual_index()

# ==========================================
# 3. محرك البحث الذكي والمساعد الهندسي
# ==========================================
def search_manuals(query, top_k=3):
    """البحث الدلالي السريع داخل الكتالوجات"""
    if manual_embeddings is None or len(manual_pages) == 0:
        return []
    query_emb = text_model.encode(query, convert_to_tensor=True)
    hits = util.semantic_search(query_emb, manual_embeddings, top_k=top_k)[0]
    results = [manual_pages[hit['corpus_id']] for hit in hits]
    return results

def maintenance_copilot(query, image=None):
    """معالجة استفسارات الصيانة ومطابقة القطع وإرسال التنبيهات"""
    response = []
    
    # البحث في الكتالوجات
    hits = search_manuals(query, top_k=2)
    if hits:
        response.append("### 📚 نتائج الكتالوجات والأدلة الفنية:")
        for h in hits:
            response.append(f"- **الملف:** `{h['filename']}` (صفحة {h['page']})")
            snippet = h['text'][:250].replace("\n", " ")
            response.append(f"  > *\"{snippet}...\"*\n")
    else:
        response.append("⚠️ لم يتم العثور على نتائج مباشرة في كتالوجات الصيانة.")
        
    # فحص إذا كان هناك تنبيه عطل لإرساله للواتساب
    tz = pytz.timezone('Asia/Hebron')
    timestamp = datetime.now(tz).strftime('%Y-%m-%d %I:%M %p')
    
    if any(k in query.lower() for k in ["عطل", "كسر", "تالف", "طلب قطعة", "طارئ", "alarm", "broken"]):
        alert_msg = f"⚠️ *إشعار صيانة ومتابعة*\n⏰ الوقت: {timestamp}\n📝 الطلب: {query}\n"
        if hits:
            alert_msg += f"📖 المرجع: {hits[0]['filename']} (ص {hits[0]['page']})"
        send_whatsapp_alert(alert_msg)
        response.append("\n✅ تم إرسال إشعار فوري لمجموعة طاقم الصيانة عبر الواتساب.")

    return "\n".join(response)

# ==========================================
# 4. واجهة المستخدم عبر Gradio والتشغيل
# ==========================================
demo = gr.Interface(
    fn=maintenance_copilot,
    inputs=[
        gr.Textbox(lines=3, placeholder="اكتب استفسارك الهندسي، رقم القطعة، أو وصف العطل..."),
        gr.Image(type="pil", label="صورة القطعة (اختياري)")
    ],
    outputs=gr.Markdown(label="تقرير المساعد الذكي"),
    title="Palestine Poultry Co. - Maintenance AI Co-Pilot",
    description="نظام الصيانة المتقدم لشركة دواجن فلسطين (مسلخ عزيزا) - مربوط بالحاوية السحابية والكتالوجات المركزية.",
    theme="default"
)

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=PORT)
