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
                        "page": page_num + 1,
                        "text": page_text
                    })
        except Exception:
            pass
    print(f"[✓] Successfully indexed {len(manual_pages)} pages.")

build_manual_index()

# فهرسة الصور بصرياً باستخدام توقيع البكسلات المصغرة (Thumbnail Signature)
# طريقة خفيفة وسريعة ولا تستهلك رام إطلاقاً
part_images_map = {}
image_signatures = {}

def get_img_sig(img):
    """استخراج بصمة بصرية سريعة من 64 بكسل مع تباين الإضاءة"""
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

# قراءة الشعار المحلي المعتمد logo.png
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
# 3. محرك المطابقة البصرية والبحث الصارم
# ==========================================
def match_uploaded_image(uploaded_img):
    """مقارنة الصورة المرفوعة مع صور المستودع"""
    if uploaded_img is None or not image_signatures:
        return None, None
    try:
        if not isinstance(uploaded_img, Image.Image):
            uploaded_img = Image.fromarray(uploaded_img)
            
        up_sig = get_img_sig(uploaded_img)
        best_part = None
        min_diff = 256 # الحد الأقصى للاختلاف (16x16 = 256)
        
        for part_no, (sig, path) in image_signatures.items():
            # حساب نسبة التطابق بين البصمتين
            diff = sum(c1 != c2 for c1, c2 in zip(up_sig, sig))
            if diff < min_diff:
                min_diff = diff
                best_part = (part_no, path)
                
        # إذا كانت نسبة التشابه مقبولة (أقل من 65 بت اختلاف من أصل 256)
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

def search_engine(query, top_k=5):
    if not manual_pages:
        return [], None
    clean_q = query.strip()
    
    # 1. فحص كود القطعة المكون من 4 مقاطع (نقاط أو مسافات أو شرطات)
    codes_4 = re.findall(r'([A-Za-z0-9]+)[\.\s\-_/]+([A-Za-z0-9]+)[\.\s\-_/]+([A-Za-z0-9]+)[\.\s\-_/]+([A-Za-z0-9]+)', clean_q)
    if codes_4:
        for segs in codes_4:
            pattern = re.escape(segs[0]) + r'[\.\s\-_]+' + re.escape(segs[1]) + r'[\.\s\-_]+' + re.escape(segs[2]) + r'[\.\s\-_]+' + re.escape(segs[3])
            matched = [p for p in manual_pages if re.search(pattern, p["text"], re.IGNORECASE)]
            if matched:
                return matched[:top_k], ".".join(segs)

    # 2. فحص إنذارات الأعطال (مثل E002 أو E02 أو Alarm 02)
    alarms = re.findall(r'\b[A-Za-z]0*\d+\b|\bAlarm\s*\d+\b|\bError\s*\d+\b', clean_q, re.IGNORECASE)
    if alarms:
        for a in alarms:
            m_num = re.search(r'\d+', a)
            if m_num:
                num = int(m_num.group())
                pattern = rf'\b(E|Alarm|Error)\s*0*{num}\b'
                matched = [p for p in manual_pages if re.search(pattern, p["text"], re.IGNORECASE)]
                if matched:
                    if any(k in clean_q for k in ["تغليف", "automac", "fabbri"]):
                        matches_sorted = sorted(matched, key=lambda x: any(k in x["filename"].lower() for k in ["automac", "297", "298"]), reverse=True)
                        return matches_sorted[:top_k], a.upper()
                    return matched[:top_k], a.upper()

    # 3. توجيه الأعطال والمنظومات المحددة بالاسم العربي
    keywords_map = {
        "مايسترو": (["maestro", "eviscerat"], ["infeed", "entry", "positioning", "shackle", "drawing", "guide"]),
        "تغليف": (["automac", "wrapping", "297", "298"], ["tray", "film", "alarm", "infeed", "stop"]),
        "تبريد": (["compressor", "chiller", "refrigeration"], ["temperature", "pressure", "oil", "cooling"]),
        "كمبرسور": (["compressor", "airpol", "atlas"], ["pressure", "filter", "separator", "alarm"]),
        "رياشة": (["plucker", "picking"], ["finger", "belt", "motor"]),
        "سمط": (["scalder", "scalding"], ["temperature", "water", "circulation"])
    }
    
    for ar_word, (cat_filters, terms) in keywords_map.items():
        if ar_word in clean_q:
            pool = [p for p in manual_pages if any(f in p["filename"].lower() for f in cat_filters)]
            if not pool:
                pool = manual_pages
            scored = []
            for p in pool:
                score = sum(1 for t in terms if re.search(r'\b' + re.escape(t) + r'\b', p["text"], re.IGNORECASE))
                if score > 0:
                    scored.append((score, p))
            scored.sort(key=lambda x: x[0], reverse=True)
            if scored:
                return [x[1] for x in scored[:top_k]], ar_word

    # 4. مطابقة مباشرة لأي رمز أو كلمة
    eng_tokens = re.findall(r'[A-Za-z0-9]{3,}', clean_q)
    for tok in eng_tokens:
        pat = r'\b' + re.escape(tok) + r'\b'
        matches = [p for p in manual_pages if re.search(pat, p["text"], re.IGNORECASE)]
        if matches:
            return matches[:top_k], tok

    return [], None

def maintenance_copilot(query, input_image=None):
    clean_q = query.strip() if query else ""
    matched_image_path = None
    response = []

    # معالجة الصورة المرفوعة والمطابقة البصرية
    if input_image is not None:
        matched_part_no, matched_img = match_uploaded_image(input_image)
        if matched_part_no:
            response.append(f"📸 **تم التعرف بصرياً على صورة القطعة:** `{matched_part_no}`")
            matched_image_path = matched_img
            if not clean_q:
                clean_q = matched_part_no
        else:
            if not clean_q:
                # إشعار الواتساب عند تعذر المطابقة البصرية
                tz = pytz.timezone('Asia/Hebron')
                timestamp = datetime.now(tz).strftime('%Y-%m-%d %I:%M %p')
                fail_msg = f"⚠️ *تنبيه فحص ميداني - مسلخ عزيزا*\n⏰ الوقت: {timestamp}\n📸 تم رفع صورة قطعة لم يتعرف عليها النظام تلقائياً، يرجى التحقق اليدوي."
                send_whatsapp_alert(fail_msg)
                return "❌ لم يتم العثور على صورة متطابقة بصرياً مع قطع المستودع المفهرسة. يرجى إدخال رقم القطعة كتابةً.\n---\n📲 تم إرسال إشعار لطاقم الصيانة بالمتابعة.", None

    if not clean_q:
        return "⚠️ يرجى إدخال رقم القطعة (4 مقاطع)، كود الإنذار (مثل E002)، أو رفع صورة القطعة.", None

    if not clean_q:
        return "⚠️ يرجى إدخال رقم القطعة (4 مقاطع)، كود الإنذار (مثل E002)، أو رفع صورة القطعة.", None

    hits, matched_term = search_engine(clean_q, top_k=4)
    if not matched_image_path:
        matched_image_path = find_image_for_part(matched_term if matched_term else clean_q)

    if hits:
        response.append(f"### ✅ تم العثور على مراجع مطابقة في الكتالوجات:")
        for h in hits:
            response.append(f"- **الملف:** `{h['filename']}` (صفحة {h['page']})")
            text = h['text'].replace("\r", "")
            target = matched_term if matched_term else clean_q
            idx = text.lower().find(target.lower().split()[0])
            if idx != -1:
                start = max(0, idx - 50)
                end = min(len(text), idx + len(target) + 140)
                snippet = text[start:end].replace("\n", " ").strip()
            else:
                words = text.split()
                snippet = " ".join(words[:40])
            response.append(f"  > *\"...{snippet}...\"*\n")
    else:
        response.append(f"❌ لم يتم العثور على أي تطابق لطلبك `{clean_q}` داخل صفحات الكتالوجات.")

    if matched_image_path:
        response.append("\n🖼️ **تم إرفاق صورة القطعة الحقيقية من أرشيف المستودع الميداني أدناه.**")

   # إشعار الواتساب التلقائي (يعمل مع الصور، أرقام القطع، وبلاغات الأعطال)
    tz = pytz.timezone('Asia/Hebron')
    timestamp = datetime.now(tz).strftime('%Y-%m-%d %I:%M %p')

    # تجهيز رسالة التنبيه الشاملة
    alert_msg = f"🔔 *إشعار صيانة ومطابقة - مسلخ عزيزا*\n"
    alert_msg += f"⏰ الوقت: {timestamp}\n"
    alert_msg += f"🔍 الاستعلام / رقم القطعة: `{clean_q}`\n"
    
    if hits:
        alert_msg += f"📖 المرجع الفني: {hits[0]['filename']} (صفحة {hits[0]['page']})\n"
    if matched_image_path:
        alert_msg += f"🖼️ الحالة: تم استخراج صورة مطابقة من أرشيف المستودع."

    # إرسال فوري دون أي شروط مسبقة
    send_whatsapp_alert(alert_msg)
    response.append("\n---\n📲 تم إرسال إشعار فوري لطاقم الصيانة عبر الواتساب.")

    return "\n".join(response), matched_image_path

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
        status_box = gr.Markdown(f"📊 **حالة النظام:** تم تجهيز وفهرسة `{total_manuals}` كتالوج فني ومطابقة صور قطع المستودع الميداني.")
        
    with gr.Row():
        with gr.Column(scale=1):
            query_input = gr.Textbox(
                label="أدخل كود الإنذار / رقم القطعة (4 مقاطع) / وصف العطل",
                placeholder="أمثلة: انذار E002 ماكينة التغليف | مشكله ماكينه المايسترو | 0990.AD05.007.00 | 89 3608 904 0096",
                lines=2
            )
            image_input = gr.Image(type="pil", label="أو ارفع صورة القطعة للتعرف البصري عليها ومطابقتها")
            submit_btn = gr.Button("فحص وتشخيص العطل / مطابقة القطعة 🔍", variant="primary")
            clear_btn = gr.Button("مسح الحقول")
            
        with gr.Column(scale=1):
            output_box = gr.Markdown(label="تقرير الفحص الفني والحلول")
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
        allowed_paths=["/tmp"]
    )
