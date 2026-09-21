import os
import glob
import re
import io
import base64
import fitz  # PyMuPDF
import requests
from datetime import datetime
import pytz
from PIL import Image, ImageStat
import gradio as gr
from google.cloud import storage
from google import genai

# ==========================================
# 0. إعدادات السحابة والمنفذ والذكاء الاصطناعي
# ==========================================
PORT = int(os.environ.get("PORT", 8080))
BUCKET_NAME = "aziza-manuals-storage"
BASE_DIR = "/tmp/Maintenance_Manuals"
IMAGE_DIR = os.path.join(BASE_DIR, "Real_Parts_Images")

# إعداد عميل Gemini API
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
ai_client = None
if GEMINI_API_KEY:
    try:
        ai_client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"[!] Warning initializing Gemini API: {e}")

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
        print(f"[✓] GCS Sync completed. Downloaded {count} files.")
    except Exception as e:
        print(f"[!] Warning during GCS sync: {e}")

sync_data_from_gcs()

# ==========================================
# 1. إعدادات تنبيهات الواتساب (Green-API)
# ==========================================
ID_INSTANCE = "710722737613"
API_TOKEN_INSTANCE = "8902219901b2411cb1ebfa944bbfc3d7d499d671111c4fe18e"
ALERT_GROUP_ID = "970599431267@c.us"

def send_whatsapp_alert(message):
    if not API_TOKEN_INSTANCE or "YOUR_GREEN_API" in API_TOKEN_INSTANCE:
        return
    if not ALERT_GROUP_ID or "YOUR_PHONE" in ALERT_GROUP_ID:
        return

    url = f"https://api.green-api.com/waInstance{ID_INSTANCE}/sendMessage/{API_TOKEN_INSTANCE}"
    payload = {"chatId": ALERT_GROUP_ID, "message": message}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as err:
        print(f"[!] WhatsApp notification error: {err}")

# ==========================================
# 2. فهرسة صفحات الكتالوجات وبصمات صور المستودع
# ==========================================
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
                        "filepath": pdf_path,
                        "page": page_num + 1,
                        "text": page_text
                    })
        except Exception:
            pass
    print(f"[✓] Successfully indexed {len(manual_pages)} pages.")

build_manual_index()

part_images_map = {}
image_signatures = {}

def get_img_sig(img):
    img_gray = img.convert('L').resize((16, 16), Image.Resampling.BILINEAR)
    pixels = list(img_gray.getdata())
    avg = sum(pixels) / len(pixels)
    return [1 if p > avg else 0 for p in pixels]

def build_image_index():
    global part_images_map, image_signatures
    part_images_map = {}
    image_signatures = {}
    if not os.path.exists(IMAGE_DIR):
        return
    valid_exts = ('.jpg', '.jpeg', '.png', '.JPG', '.PNG')
    for f in os.listdir(IMAGE_DIR):
        if f.endswith(valid_exts):
            part_no = os.path.splitext(f)[0]
            clean_k = re.sub(r'[^a-zA-Z0-9]', '', part_no).lower()
            img_path = os.path.join(IMAGE_DIR, f)
            part_images_map[clean_k] = (part_no, img_path)
            try:
                with Image.open(img_path) as im:
                    image_signatures[part_no] = (get_img_sig(im), img_path)
            except Exception:
                pass
    print(f"[✓] Indexed {len(image_signatures)} part images for visual comparison.")

build_image_index()

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
# 3. محرك استخراج صور الكتالوجات والبحث والمساعد الذكي
# ==========================================
def render_pdf_page_to_image(filepath, page_num):
    """تحويل صفحة الـ PDF المحددة إلى صورة واضحة بدقة 150 DPI"""
    try:
        doc = fitz.open(filepath)
        page = doc[page_num - 1]
        pix = page.get_pixmap(dpi=150)
        out_img_path = f"/tmp/page_{os.path.basename(filepath)}_{page_num}.png"
        pix.save(out_img_path)
        return out_img_path
    except Exception as e:
        print(f"[!] Error rendering PDF page to image: {e}")
        return None

def ask_gemini_engineer(user_query, context_text):
    """صياغة خطوات الفحص الهندسي باللغة الإنجليزية التقنية الصارمة من الكتالوج"""
    if not ai_client or not context_text:
        return ""
    prompt = f"""
You are a Lead Automation & Industrial Maintenance Engineer at a poultry processing plant, highly specialized in Meyn evisceration lines, Automac stretch film wrapping machines (Fabbri Group), and industrial refrigeration.
Based STRICTLY on the technical manual excerpt below, provide a professional, structured troubleshooting procedure in English for the field maintenance technician.

Fault / Query:
"{user_query}"

Technical Manual Page Content:
\"\"\"{context_text[:3500]}\"\"\"

Response Format Requirements:
- Header: Alarm/Warning Code and Official Description.
- Root Cause: One concise sentence explaining the root cause according to the manual.
- Corrective Actions / Checklist: Numbered, direct, practical technical steps (e.g. 1. Check photocell sensor alignment, 2. Verify pneumatic pressure >= 6 bar, 3. Inspect microswitch contacts, 4. Reset thermal overload).
- Strictly in English. No introductory chatter or generic closing phrases. Keep it direct and professional.
"""
    try:
        response = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        return response.text.strip()
    except Exception as err:
        print(f"[!] Gemini API Error: {err}")
        return ""

def match_uploaded_image(uploaded_img):
    if uploaded_img is None or not image_signatures:
        return None, None
    try:
        if not isinstance(uploaded_img, Image.Image):
            uploaded_img = Image.fromarray(uploaded_img)
            
        up_sig = get_img_sig(uploaded_img)
        best_part = None
        min_diff = 256
        
        for part_no, (sig, path) in image_signatures.items():
            diff = sum(c1 != c2 for c1, c2 in zip(up_sig, sig))
            if diff < min_diff:
                min_diff = diff
                best_part = (part_no, path)
                
        if min_diff <= 65:
            return best_part[0], best_part[1]
    except Exception as e:
        print(f"[!] Vision matching error: {e}")
    return None, None

def find_image_for_part(query_text):
    if not query_text or not part_images_map:
        return None
    clean_target = re.sub(r'[^a-zA-Z0-9]', '', query_text).lower()
    if clean_target in part_images_map:
        return part_images_map[clean_target][1]

    tokens = re.findall(r'[A-Za-z0-9]{4,}', query_text)
    for tok in tokens:
        c_tok = tok.lower()
        if c_tok in part_images_map:
            return part_images_map[c_tok][1]

    for k, v in part_images_map.items():
        if len(k) >= 6 and (k in clean_target or clean_target in k):
            return v[1]
    return None

def search_engine(query, top_k=3):
    if not manual_pages:
        return [], None, None
    clean_q = query.strip()

    # 1. التمييز بين الإنذارات (E) والتحذيرات (W) لماكينات التغليف
    alarm_match = re.search(r'\b([EWew])\s*0*(\d+)\b', clean_q)
    if alarm_match:
        prefix = alarm_match.group(1).upper()
        num = int(alarm_match.group(2))
        std_code = f"{prefix}{num:03d}"
        
        pattern = rf'\b{prefix}\s*0*{num}\b'
        matched = [p for p in manual_pages if re.search(pattern, p["text"], re.IGNORECASE)]
        if matched:
            matched_sorted = sorted(
                matched, 
                key=lambda x: any(k in x["filename"].lower() for k in ["automac", "297", "298", "wrapping", "fabbri"]), 
                reverse=True
            )
            return matched_sorted[:top_k], std_code, "alarm"
        return [], std_code, "alarm"

    # 2. مطابقة كود القطعة الصريح الرباعي (Article nr)
    code_match = re.search(r'([A-Za-z0-9]{2,4})\.([A-Za-z0-9]{4})\.([A-Za-z0-9]{3,4})\.([A-Za-z0-9]{2,4})', clean_q)
    if code_match:
        full_code = code_match.group(0)
        spaced_code = " ".join(code_match.groups())
        
        matched = [
            p for p in manual_pages 
            if full_code.lower() in p["text"].lower() or spaced_code.lower() in p["text"].lower()
        ]
        if matched:
            return matched[:top_k], full_code, "part"
        return [], full_code, "part"

    # 3. توجيه المنظومات بالاسم الصريح
    keywords_map = {
        "مايسترو": ["maestro", "eviscerat"],
        "تغليف": ["automac", "wrapping", "297", "298"],
        "تبريد": ["compressor", "chiller", "refrigeration"],
        "كمبرسور": ["compressor", "airpol", "atlas"],
        "رياشة": ["plucker", "picking"],
        "سمط": ["scalder", "scalding"],
        "قوانص": ["gizzard", "peeler"]
    }
    for ar_word, cat_filters in keywords_map.items():
        if ar_word in clean_q:
            matched = [p for p in manual_pages if any(f in p["filename"].lower() for f in cat_filters)]
            if matched:
                return matched[:top_k], ar_word, "keyword"

    return [], None, None

def maintenance_copilot(query, input_image=None):
    clean_q = query.strip() if query else ""
    matched_warehouse_image = None
    matched_catalog_page_img = None
    response = []

    # معالجة الصورة المرفوعة والمطابقة البصرية
    if input_image is not None:
        matched_part_no, matched_img = match_uploaded_image(input_image)
        if matched_part_no:
            response.append(f"📸 **تم التعرف بصرياً على صورة القطعة:** `{matched_part_no}`")
            matched_warehouse_image = matched_img
            if not clean_q:
                clean_q = matched_part_no
        else:
            if not clean_q:
                tz = pytz.timezone('Asia/Hebron')
                timestamp = datetime.now(tz).strftime('%Y-%m-%d %I:%M %p')
                fail_msg = f"⚠️ *تنبيه فحص ميداني - مسلخ عزيزا*\n⏰ الوقت: {timestamp}\n📸 تم رفع صورة قطعة لم يتعرف عليها النظام تلقائياً، يرجى التحقق اليدوي."
                send_whatsapp_alert(fail_msg)
                return "❌ لم يتم العثور على صورة متطابقة بصرياً مع قطع المستودع المفهرسة. يرجى إدخال رقم القطعة أو كود الإنذار كتابةً.\n---\n📲 تم إرسال إشعار لطاقم الصيانة بالمتابعة.", None, None

    if not clean_q:
        return "⚠️ يرجى إدخال كود الإنذار/التنبيه (مثل E002 أو W015)، رقم القطعة (4 مقاطع)، أو رفع صورة القطعة.", None, None

    hits, matched_term, hit_type = search_engine(clean_q, top_k=3)
    if not matched_warehouse_image and hit_type != "alarm":
        matched_warehouse_image = find_image_for_part(matched_term if matched_term else clean_q)

    # ==========================================
    # أولاً: معالجة الإنذارات والتنبيهات (E / W)
    # ==========================================
    if hit_type == "alarm" and matched_term:
        prefix = matched_term[0]
        alarm_status = "CRITICAL ALARM (MACHINE STOPPED)" if prefix == "E" else "WARNING ALERT (PREVENTIVE)"
        
        # 1. وضع خطوات الفحص الإنجليزية في قمة التقرير
        if hits:
            ai_insight = ask_gemini_engineer(clean_q, hits[0]['text'])
            if ai_insight:
                response.append(f"## 🛠️ {matched_term} - {alarm_status}\n")
                response.append(ai_insight)
                response.append("\n" + "="*55 + "\n")
            
            # 2. المرجع الفني الرسمي من الكتالوج
            response.append(f"📖 **Technical Manual Reference:** `{hits[0]['filename']}` (Page {hits[0]['page']})")
            matched_catalog_page_img = render_pdf_page_to_image(hits[0]['filepath'], hits[0]['page'])
        else:
            response.append(f"⚠️ No direct catalog page found for `{matched_term}` in current indexed manuals.")

    # ==========================================
    # ثانياً: معالجة أرقام القطع والمنظومات
    # ==========================================
    else:
        if hits:
            ai_insight = ask_gemini_engineer(clean_q, hits[0]['text'])
            if ai_insight:
                response.append("## 🔧 Technical Inspection & Part Details:\n")
                response.append(ai_insight)
                response.append("\n" + "="*55 + "\n")

            response.append("### ✅ Catalog References Found:")
            for h in hits:
                response.append(f"- **Manual:** `{h['filename']}` (Page {h['page']})")
                text = h['text'].replace("\r", "")
                target = matched_term if matched_term else clean_q
                idx = text.lower().find(target.lower().split()[0])
                if idx != -1:
                    start = max(0, idx - 40)
                    end = min(len(text), idx + len(target) + 120)
                    snippet = text[start:end].replace("\n", " ").strip()
                else:
                    words = text.split()
                    snippet = " ".join(words[:30])
                response.append(f"  > *\"...{snippet}...\"*\n")
            
            matched_catalog_page_img = render_pdf_page_to_image(hits[0]['filepath'], hits[0]['page'])
        else:
            response.append(f"❌ لم يتم العثور على أي تطابق لطلبك `{clean_q}` داخل صفحات الكتالوجات.")

    if matched_warehouse_image:
        response.append("\n🖼️ **تم إرفاق صورة القطعة الحقيقية من أرشيف المستودع الميداني.**")
    if matched_catalog_page_img:
        response.append("📖 **تم استخراج صورة صفحة الكتالوج والمخطط الفني أدناه.**")

    # إشعار الواتساب التلقائي
    tz = pytz.timezone('Asia/Hebron')
    timestamp = datetime.now(tz).strftime('%Y-%m-%d %I:%M %p')
    alert_msg = f"🔔 *إشعار صيانة وتشخيص - مسلخ عزيزا*\n"
    alert_msg += f"⏰ الوقت: {timestamp}\n"
    alert_msg += f"🔍 الاستعلام: `{clean_q}`\n"
    if hits:
        alert_msg += f"📖 المرجع: {hits[0]['filename']} (صفحة {hits[0]['page']})\n"
    if matched_warehouse_image:
        alert_msg += f"🖼️ الحالة: تم استخراج صورة مطابقة من المستودع."

    send_whatsapp_alert(alert_msg)
    response.append("\n---\n📲 تم إرسال إشعار فوري لطاقم الصيانة عبر الواتساب.")

    return "\n".join(response), matched_warehouse_image, matched_catalog_page_img

# ==========================================
# 4. واجهة Gradio الرسمية
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
                <h1 style="margin: 0; font-size: 23px; font-weight: 800; color: #ffffff;">شركة دواجن فلسطين - مسلخ عزيزا</h1>
                <p style="margin: 4px 0 0 0; font-size: 14px; color: #e8f5e9;">نظام الصيانة والتشخيص الهندسي الذكي المدعوم بـ AI (خطوط Meyn • ماكينات التغليف Automac • منظومات التبريد)</p>
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

with gr.Blocks(title="منصة الصيانة الهندسية الذكية - مسلخ عزيزا") as demo:
    gr.HTML(HEADER_HTML)
    
    with gr.Row():
        status_box = gr.Markdown(f"📊 **حالة النظام:** تم تجهيز وفهرسة `{total_manuals}` كتالوج فني، وربط المساعد الذكي مع أرشيف المستودع الميداني.")
        
    with gr.Row():
        with gr.Column(scale=1):
            query_input = gr.Textbox(
                label="أدخل كود الإنذار (E002) أو التنبيه (W015) / رقم القطعة (4 مقاطع) / وصف العطل",
                placeholder="أمثلة: E002 | W010 | عطل في ذراع سحب المايسترو | 0990.AD05.007.00 | 89.3500.160.0085",
                lines=2
            )
            image_input = gr.Image(type="pil", label="أو ارفع صورة القطعة للتعرف البصري عليها ومطابقتها")
            submit_btn = gr.Button("تشخيص العطل ومطابقة القطعة 🔍", variant="primary")
            clear_btn = gr.Button("مسح الحقول")
            
        with gr.Column(scale=1):
            output_box = gr.Markdown(label="تقرير الفحص الفني والحلول")
            with gr.Row():
                matched_warehouse_img_output = gr.Image(type="filepath", label="صورة القطعة من المستودع الميداني")
                matched_catalog_page_output = gr.Image(type="filepath", label="مخطط وصفحة الكتالوج الفني / دليل العطل")
            
    submit_btn.click(
        fn=maintenance_copilot,
        inputs=[query_input, image_input],
        outputs=[output_box, matched_warehouse_img_output, matched_catalog_page_output]
    )
    clear_btn.click(
        lambda: ("", None, "", None, None),
        outputs=[query_input, image_input, output_box, matched_warehouse_img_output, matched_catalog_page_output]
    )

if __name__ == "__main__":
    demo.launch(
        server_name="0.0.0.0",
        server_port=PORT,
        allowed_paths=["/tmp"]
    )
