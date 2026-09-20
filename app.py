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
        requests.post(url, json=payload, timeout=6)
    except Exception as err:
        print(f"[!] WhatsApp notification error: {err}")

# ==========================================
# 2. جداول كشف الأعطال والإنذارات الفنية بالعربية
# ==========================================
TROUBLESHOOTING_KB = {
    "E002": {
        "title": "إنذار E002 - انحشار / عدم تغذية صواني التغليف (Tray Infeed Jam)",
        "machine": "ماكينة التغليف Automac 75 / 297",
        "causes": [
            "اتساخ أو انحراف محاذاة حساس دخول الصواني الفوتوسيل (Photocell).",
            "انحشار صينية عند بوابة السحب أو وصول صواني غير متباعدة بانتظام.",
            "خلل في شوط أو توقيت دافع الصواني الميكانيكي (Pusher)."
        ],
        "remedy": [
            "تنظيف عدسة حساس الدخول بقطعة قماش جافة والتأكد من محاذاة العاكس.",
            "إزالة الصينية العالقة والتحقق من سلاسة حركة سير التغذية الناقل.",
            "إعادة ضبط الحساس ومراقبة إشارة الاستشعار، ثم تصفير الإنذار من الشاشة الرئيسية."
        ]
    },
    "E004": {
        "title": "إنذار E004 - انتهاء أو انقطاع فيلم التغليف (Film Reel Empty / Broken)",
        "machine": "ماكينة التغليف Automac",
        "causes": [
            "نفاد رول فيلم التغليف بالكامل من الحامل السفلي.",
            "تمزق الفيلم نتيجة شد زائد على بكرات التوجيه أو وجود شوائب.",
            "عدم إغلاق مشبك ذراع تثبيت الرول بإحكام."
        ],
        "remedy": [
            "تركيب رول فيلم جديد وتمريره وفق المسار الهندسي المحدد بالملصق.",
            "فحص مرونة دوران بكرات الشد وضبط عيار الشداد لتفادي القطع المفاجئ."
        ]
    },
    "E014": {
        "title": "إنذار E014 - خلل حرارة حزام اللحام السفلي (Sealing Belt Temp Fault)",
        "machine": "ماكينة التغليف Automac",
        "causes": [
            "تلف مقاومة التسخين السفلية أو قراءة غير دقيقة للثرموكابل (Thermocouple).",
            "فصل القاطع الحراري أو فيوز قدرة وحدة التسخين داخل لوحة الكهرباء."
        ],
        "remedy": [
            "قياس حرارة سطح اللحام بجهاز خارجي ومقارنتها بقراءة الشاشة.",
            "فحص التوصيلات الكهربائية لفيوزات وحدة التسخين وإعادة تشغيل المنظومة."
        ]
    },
    "مايسترو": {
        "title": "استكشاف أعطال جهاز فتح البطن وتفريغ الأحشاء (Meyn Maestro Eviscerator)",
        "machine": "خط التجهيز وتفريغ الأحشاء Meyn Maestro",
        "causes": [
            "تمزق الكبد أو المرارة: عدم تناسب ارتفاع شوكة الاستخراج (Drawing Spoon) مع متوسط أوزان القطيع.",
            "عدم ثبات الطيور: تآكل أو اتساخ مرابط التعليق (Shackles) أو ميلان سكة التوجيه المركزية.",
            "خلل في زمن الفتح: ضعف نوابض الترجيع (Springs) أو تآكل عجلات الكامة (Cam Followers)."
        ],
        "remedy": [
            "إعادة معايرة الارتفاع المركزي لوحدات Maestro وفق جدول أوزان القطيع الفعلي اليومي.",
            "فحص نوابض الترجيع وعجلات الكامات واستبدال الأجزاء المستهلكة لضمان الحركة المتزنة.",
            "التأكد من انتظام ضغط خط غسيل وتزييت الشوكات أثناء الدوران."
        ]
    },
    "رياشة": {
        "title": "مشاكل نتف وترييش الدواجن (Plucker / Picker)",
        "machine": "قسم الذبح والترييش Meyn",
        "causes": [
            "بقاء الريش: تآكل أصابع النتف المطاطية، أو انخفاض حرارة حوض السمط (Scalder).",
            "تمزق الجلد أو كسر الأجنحة: تقارب مفرط لبنوك الأصابع أو سرعة دوران زائدة."
        ],
        "remedy": [
            "استبدال الأصابع المطاطية المكسورة والمتآكلة في جميع الديسكات.",
            "معايرة حرارة مياه السمط وثبات دورة الماء.",
            "ضبط مسافة بنوك الترييش لتتلامس أطراف الأصابع مع الريش فقط دون صدم الطير."
        ]
    }
}

# ==========================================
# 3. فهرسة صفحات الكتالوجات وتحويلها لصور
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

def render_pdf_page_as_image(filepath, page_num):
    """تحويل صفحة الكتالوج الأصلية لصورة عالية الوضوح لعرضها للفني"""
    try:
        doc = fitz.open(filepath)
        page = doc[page_num - 1]
        pix = page.get_pixmap(dpi=150)
        output_image_path = f"/tmp/page_{page_num}_{os.path.splitext(os.path.basename(filepath))[0]}.png"
        pix.save(output_image_path)
        return output_image_path
    except Exception as e:
        print(f"[!] PDF page rendering error: {e}")
        return None

# ==========================================
# 4. محرك البصمة البصرية المستقر للمستودع
# ==========================================
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

# قراءة الشعار المعتمد
logo_base64 = ""
for p in ["logo.png", "/app/logo.png"]:
    if os.path.exists(p):
        try:
            with open(p, "rb") as f:
                logo_base64 = base64.b64encode(f.read()).decode("utf-8")
            break
        except Exception:
            pass

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
                
        if min_diff <= 85:
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
    
    # 1. المطابقة الصارمة لكود القطعة المكون من 4 مقاطع (فقط الكود الكامل)
    codes_4 = re.findall(r'([A-Za-z0-9]+)[\.\s\-_/]+([A-Za-z0-9]+)[\.\s\-_/]+([A-Za-z0-9]+)[\.\s\-_/]+([A-Za-z0-9]+)', clean_q)
    if codes_4:
        for segs in codes_4:
            pattern = re.escape(segs[0]) + r'[\.\s\-_]+' + re.escape(segs[1]) + r'[\.\s\-_]+' + re.escape(segs[2]) + r'[\.\s\-_]+' + re.escape(segs[3])
            matched = [p for p in manual_pages if re.search(pattern, p["text"], re.IGNORECASE)]
            if matched:
                return matched[:top_k], ".".join(segs)
        return [], None

    # 2. إنذارات الأعطال المحددة (E002, E004...)
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

    # 3. توجيه المنظومات بالاسم العربي الصريح
    keywords_map = {
        "مايسترو": (["maestro", "eviscerat"], ["infeed", "entry", "positioning", "shackle", "drawing", "guide"]),
        "تغليف": (["automac", "wrapping", "297", "298"], ["tray", "film", "alarm", "infeed", "stop"]),
        "تبريد": (["compressor", "chiller", "refrigeration"], ["temperature", "pressure", "oil", "cooling"]),
        "كمبرسور": (["compressor", "airpol", "atlas"], ["pressure", "filter", "separator", "alarm"]),
        "رياشة": (["plucker", "picking"], ["finger", "belt", "motor"]),
        "سمط": (["scalder", "scalding"], ["temperature", "water", "circulation"]),
        "قوانص": (["gizzard", "peeler"], ["roller", "peeling", "infeed"])
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

    return [], None

# ==========================================
# 5. المساعد الهندسي الذكي المتكامل
# ==========================================
def maintenance_copilot(query, input_image=None):
    clean_q = query.strip() if query else ""
    matched_image_path = None
    catalog_page_path = None
    response = []

    # 1. معالجة الصورة المرفوعة
    if input_image is not None:
        matched_part_no, matched_img = match_uploaded_image(input_image)
        if matched_part_no:
            response.append(f"📸 **تم التعرف بصرياً على صورة القطعة:** `{matched_part_no}`")
            matched_image_path = matched_img
            if not clean_q:
                clean_q = matched_part_no
        else:
            if not clean_q:
                tz = pytz.timezone('Asia/Hebron')
                timestamp = datetime.now(tz).strftime('%Y-%m-%d %I:%M %p')
                fail_msg = f"⚠️ *تنبيه فحص ميداني - مسلخ عزيزا*\n⏰ الوقت: {timestamp}\n📸 تم رفع صورة قطعة لم يتم التعرف عليها تلقائياً، يرجى التدقيق اليدوي."
                send_whatsapp_alert(fail_msg)
                return "❌ لم يتم العثور على صورة متطابقة بصرياً مع قطع المستودع المفهرسة. يرجى إدخال رقم القطعة كتابةً.\n---\n📲 تم إرسال إشعار لطاقم الصيانة بالمتابعة.", None, None

    if not clean_q:
        return "⚠️ يرجى استخدام الميكروفون بالصوت، أو كتابة رقم القطعة / كود الإنذار، أو رفع صورة القطعة.", None, None

    # 2. فحص قاعدة استكشاف الأعطال والإنذارات (Troubleshooting Tables)
    kb_hit = None
    alarm_match = re.search(r'\b(E0*\d+|Alarm\s*\d+)\b', clean_q, re.IGNORECASE)
    if alarm_match:
        digit_m = re.search(r'\d+', alarm_match.group(1))
        if digit_m:
            formatted_e = f"E{int(digit_m.group()):03d}"
            if formatted_e in TROUBLESHOOTING_KB:
                kb_hit = TROUBLESHOOTING_KB[formatted_e]

    if not kb_hit:
        for kw, data in TROUBLESHOOTING_KB.items():
            if kw in clean_q:
                kb_hit = data
                break

    if kb_hit:
        response.append(f"## 🚨 {kb_hit['title']}")
        response.append(f"📍 **المنظومة / الماكينة:** {kb_hit['machine']}\n")
        response.append("### 🔍 الأسباب المحتملة (Possible Causes):")
        for c in kb_hit['causes']:
            response.append(f"- {c}")
        response.append("\n### 🛠️ خطوات الضبط والمعالجة الفورية (Remedy):")
        for idx, r in enumerate(kb_hit['remedy'], 1):
            response.append(f"{idx}. {r}")
        response.append("\n---\n")

    # 3. فحص صفحات الكتالوجات واستخراج صورة الصفحة
    hits, matched_term = search_engine(clean_q, top_k=4)
    if not matched_image_path:
        matched_image_path = find_image_for_part(matched_term if matched_term else clean_q)

    if hits:
        response.append("### ✅ تم العثور على مراجع مطابقة في الكتالوجات:")
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
            
        # تحويل أول صفحة مطابقة لصورة ملونة عالية الوضوح
        catalog_page_path = render_pdf_page_as_image(hits[0]['filepath'], hits[0]['page'])
    else:
        if not kb_hit:
            response.append(f"❌ لم يتم العثور على أي تطابق لطلبك `{clean_q}` داخل صفحات الكتالوجات.")

    if matched_image_path:
        response.append("\n🖼️ **تم إرفاق صورة القطعة الحقيقية من أرشيف المستودع الميداني أدناه.**")

    # 4. إشعار الواتساب الفوري المباشر
    tz = pytz.timezone('Asia/Hebron')
    timestamp = datetime.now(tz).strftime('%Y-%m-%d %I:%M %p')

    alert_msg = f"🔔 *إشعار صيانة ومطابقة - مسلخ عزيزا*\n"
    alert_msg += f"⏰ الوقت: {timestamp}\n"
    alert_msg += f"🔍 الاستعلام / رقم القطعة: `{clean_q}`\n"
    if kb_hit:
        alert_msg += f"⚠️ التشخيص: {kb_hit['title']}\n"
    if hits:
        alert_msg += f"📖 المرجع الفني: {hits[0]['filename']} (صفحة {hits[0]['page']})\n"
    if matched_image_path:
        alert_msg += f"🖼️ الحالة: تم استخراج صورة مطابقة من أرشيف المستودع."

    send_whatsapp_alert(alert_msg)
    response.append("\n---\n📲 تم إرسال إشعار فوري لطاقم الصيانة عبر الواتساب.")

    return "\n".join(response), matched_image_path, catalog_page_path

VOICE_HTML = """
<div style="text-align: center; margin-bottom: 12px;">
    <button id="aziza_mic_btn" type="button" style="background-color: #2e7d32; color: #ffffff; border: none; padding: 12px 28px; font-size: 15px; font-weight: bold; border-radius: 30px; cursor: pointer; box-shadow: 0 4px 10px rgba(0,0,0,0.25);">
        🎤 اضغط هنا للتحدث بالصوت (للأيدي المشغولة)
    </button>
</div>

<script>
function attachMicHandler() {
    var btn = document.getElementById('aziza_mic_btn');
    if (!btn) return;

    btn.onclick = function() {
        var SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!SpeechRecognition) {
            alert("يرجى فتح الرابط من متصفح Google Chrome على الهاتف أو الكمبيوتر لتفعيل ميزة الصوت.");
            return;
        }

        var recognition = new SpeechRecognition();
        recognition.lang = 'ar-SA';
        recognition.interimResults = false;
        recognition.maxAlternatives = 1;

        btn.innerText = "🔴 جاري الاستماع... تحدث الآن";
        btn.style.backgroundColor = "#c62828";

        recognition.onresult = function(event) {
            var text = event.results[0][0].transcript;
            var inputArea = document.querySelector('textarea');
            if (inputArea) {
                inputArea.value = text;
                inputArea.dispatchEvent(new Event('input', { bubbles: true }));
            }
            btn.innerText = "🎤 اضغط هنا للتحدث بالصوت (للأيدي المشغولة)";
            btn.style.backgroundColor = "#2e7d32";
        };

        recognition.onerror = function(e) {
            console.error("Speech recognition error:", e.error);
            btn.innerText = "🎤 اضغط هنا للتحدث بالصوت (للأيدي المشغولة)";
            btn.style.backgroundColor = "#2e7d32";
            if (e.error === 'not-allowed') {
                alert("يرجى إعطاء الإذن للمتصفح بالوصول إلى الميكروفون.");
            }
        };

        recognition.onend = function() {
            btn.innerText = "🎤 اضغط هنا للتحدث بالصوت (للأيدي المشغولة)";
            btn.style.backgroundColor = "#2e7d32";
        };

        recognition.start();
    };
}

// تشغيل المستمع بمجرد اكتمال تحميل الصفحة
setTimeout(attachMicHandler, 1500);
</script>
"""
with gr.Blocks(title="منصة الصيانة الهندسية - مسلخ عزيزا") as demo:
    gr.HTML(HEADER_HTML)
    gr.HTML(VOICE_HTML)
    
    with gr.Row():
        status_box = gr.Markdown(f"📊 **حالة النظام:** تم تجهيز وفهرسة `{total_manuals}` كتالوج فني ومطابقة صور قطع المستودع الميداني.")
        
    with gr.Row():
        with gr.Column(scale=1):
            query_input = gr.Textbox(
                label="أدخل كود الإنذار / رقم القطعة (4 مقاطع) / وصف العطل (كتابة أو عبر زر الصوت بالأعلى)",
                placeholder="أمثلة: انذار E002 | مشكله ماكينه المايسترو | عطل رياشة | 0000.D409.003.01",
                lines=2
            )
            image_input = gr.Image(type="pil", label="أو ارفع صورة القطعة للتعرف البصري عليها ومطابقتها")
            
            with gr.Row():
                submit_btn = gr.Button("فحص وتشخيص العطل / مطابقة القطعة 🔍", variant="primary", scale=2)
                clear_btn = gr.Button("مسح الحقول 🔄", scale=1)
            
        with gr.Column(scale=1):
            output_box = gr.Markdown(label="تقرير الفحص الفني والحلول")
            with gr.Row():
                matched_img_output = gr.Image(type="filepath", label="صورة القطعة المطابقة من أرشيف المستودع")
                catalog_page_output = gr.Image(type="filepath", label="📄 صفحة الكتالوج الأصلية (Troubleshooting / Drawing)")
            
    submit_btn.click(
        fn=maintenance_copilot,
        inputs=[query_input, image_input],
        outputs=[output_box, matched_img_output, catalog_page_output]
    )
    clear_btn.click(
        lambda: ("", None, "", None, None),
        outputs=[query_input, image_input, output_box, matched_img_output, catalog_page_output]
    )

if __name__ == "__main__":
    demo.launch(
        server_name="0.0.0.0",
        server_port=PORT
    )
