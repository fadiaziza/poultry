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
# 0. إعدادات السحابة والمنفذ والمجلدات
# ==========================================
PORT = int(os.environ.get("PORT", 8080))
BUCKET_NAME = "aziza-manuals-storage"
BASE_DIR = "/tmp/Maintenance_Manuals"
IMAGE_DIR = os.path.join(BASE_DIR, "Real_Parts_Images")

def sync_data_from_gcs():
    os.makedirs(BASE_DIR, exist_ok=True)
    os.makedirs(IMAGE_DIR, exist_ok=True)
    print(f"[*] جاري مزامنة الملفات والكتالوجات من Google Cloud Storage: {BUCKET_NAME}...")
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
        print(f"[✓] تمت المزامنة بنجاح. تم تحميل {count} ملفاً.")
    except Exception as e:
        print(f"[!] تحذير أثناء المزامنة: {e}")

sync_data_from_gcs()

# ==========================================
# 1. إعدادات تنبيهات الواتساب المباشرة (Green-API)
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
        print(f"[!] خطأ في إرسال الواتساب: {err}")

# ==========================================
# 2. جداول كشف الأعطال والإنذارات الفنية بالعربية
# ==========================================
TROUBLESHOOTING_KB = {
    "E002": {
        "title": "إنذار E002 - انحشار / عدم تغذية صواني التغليف (Tray Infeed Jam)",
        "machine": "ماكينة التغليف Automac 75 / 297",
        "causes": [
            "اتساخ أو انحراف محاذاة حساس دخول الصواني الفوتوسيل (Photocell).",
            "انحشار صينية عند بوابة السحب أو تباعد غير منتظم للصواني القادمة من خط التعبئة.",
            "خلل في شوط أو تزامن دافع الصواني الميكانيكي (Pusher Arm)."
        ],
        "remedy": [
            "تنظيف عدسة حساس الدخول بقطعة قماش ناعمة وجافة والتأكد من محاذاة العاكس.",
            "إزالة أي صينية منحشرة بالمسار والتأكد من حركة الناقل بسلاسة.",
            "إعادة ضبط الحساس ومراقبة إشارة الاستشعار، ثم تصفير الإنذار من شاشة التحكم."
        ]
    },
    "E004": {
        "title": "إنذار E004 - انتهاء أو انقطاع فيلم التغليف (Film Reel Empty / Broken)",
        "machine": "ماكينة التغليف Automac",
        "causes": [
            "نفاد رول فيلم التغليف في الحامل السفلي بالكامل.",
            "تمزق الفيلم نتيجة شد مفرط أو عائق على بكرات التوجيه الجانبية.",
            "عدم قفل ذراع تثبيت الرول بإحكام."
        ],
        "remedy": [
            "تركيب رول فيلم جديد وتمريره وفق المسار الهندسي المحدد بالملصق التوضيحي.",
            "فحص مرونة دوران بكرات الشد وضبط عيار ضغط الشداد لتفادي القطع المفاجئ."
        ]
    },
    "E014": {
        "title": "إنذار E014 - حرارة حزام اللحام السفلي خارج النطاق (Sealing Belt Temp Fault)",
        "machine": "ماكينة التغليف Automac",
        "causes": [
            "تلف مقاومة التسخين السفلية أو قراءة غير دقيقة للثرموكابل (Thermocouple).",
            "فصل القاطع الحراري أو فيوز قدرة وحدة التسخين داخل لوحة التحكم الكهربائية."
        ],
        "remedy": [
            "قياس حرارة سطح اللحام بجهاز خارجي ومقارنتها بقراءة شاشة التشغيل.",
            "فحص التوصيلات الكهربائية لفيوزات وحدة التسخين وإعادة تصفير الإنذار."
        ]
    },
    "مايسترو": {
        "title": "استكشاف أعطال جهاز تفريغ الأحشاء وفتح البطن (Meyn Maestro Eviscerator)",
        "machine": "خط التجهيز Meyn Maestro",
        "causes": [
            "تمزق الكبد أو المرارة: عدم تناسب ارتفاع شوكة الاستخراج (Drawing Spoon) مع متوسط أوزان القطيع.",
            "عدم ثبات الطيور: تآكل أو اتساخ مرابط التعليق (Shackles) أو انحراف سكة التوجيه المركزية.",
            "خلل في زمن الفتح: ضعف نوابض الترجيع (Springs) أو تآكل عجلات الكامة (Cam Followers)."
        ],
        "remedy": [
            "إعادة معايرة الارتفاع المركزي لوحدات Maestro وفق جدول متوسط أوزان القطيع اليومي.",
            "فحص نوابض الترجيع وعجلات الكامات واستبدال الأجزاء المستلكة لضمان الحركة المتزنة.",
            "التأكد من انتظام ضغط خط غسيل وتزييت الشوكات أثناء الدوران المستمر."
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
            "استبدال الأصابع المطاطية المكسورة والمتآكلة في جميع الديسكات فوراً.",
            "معايرة حرارة مياه السمط وثبات دورة الماء.",
            "ضبط مسافة بنوك الترييش لتتلامس أطراف الأصابع فقط مع ريش الطير دون صدمه."
        ]
    },
    "قوانص": {
        "title": "استكشاف أعطال ماكينة نزع دهون وقشور القوانص (Gizzard Peeler)",
        "machine": "ماكينة نزع قشور القوانص Meyn CD-6000",
        "causes": [
            "عدم تقشير القوانص بالكامل: تآكل أسنان درافيل التقشير (Peeling Rollers) أو ضعف تدفق مياه الغسيل.",
            "انحشار القوانص عند المدخل: عدم ضبط المسافة البينية بين الدرافيل بدقة."
        ],
        "remedy": [
            "فحص درافيل التقشير وتنظيف مجاري الأسنان من أي مخلفات متراكمة.",
            "التحقق من ضغط رشاشات المياه الموجهة على منطقة التقشير.",
            "ضبط خلوص درافيل السحب وفق قياسات كتالوج التشغيل المعتمد."
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
    print(f"[*] جاري فهرسة {len(pdf_files)} كتالوج فني...")
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
    print(f"[✓] تمت فهرسة {len(manual_pages)} صفحة كتالوج بنجاح.")

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
        print(f"[!] خطأ في استخراج صورة صفحة الكتالوج: {e}")
        return None

# ==========================================
# 4. محرك البصمة البصرية الدقيق للمستودع
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
    print(f"[✓] تمت فهرسة {len(image_signatures)} صورة لقطع المستودع الميداني.")

build_image_index()

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
        print(f"[!] خطأ في المطابقة البصرية: {e}")
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

# ==========================================
# 5. محرك البحث الذكي والمتوازن (Strict & Resilient)
# ==========================================
def search_engine(query, top_k=5):
    if not manual_pages:
        return [], None
    clean_q = query.strip()
    
    # 1. مطابقة كود القطعة الرباعي بمرونة الفواصل
    codes_4 = re.findall(r'([A-Za-z0-9]+)[\.\s\-_/]+([A-Za-z0-9]+)[\.\s\-_/]+([A-Za-z0-9]+)[\.\s\-_/]+([A-Za-z0-9]+)', clean_q)
    if codes_4:
        for segs in codes_4:
            s0, s1, s2, s3 = segs[0], segs[1], segs[2], segs[3]
            # مطابقة المقاطع الأربعة معاً بأي فاصل أو متصلة
            pat_full = rf'{re.escape(s0)}[\.\s\-_/]*{re.escape(s1)}[\.\s\-_/]*{re.escape(s2)}[\.\s\-_/]*{re.escape(s3)}'
            matched = [p for p in manual_pages if re.search(pat_full, p["text"], re.IGNORECASE)]
            if matched:
                return matched[:top_k], ".".join(segs)

            # إذا لم يطابق الأربعة معاً، طابق أول مقطعين معاً (الرقم الأساسي للقطعة)
            pat_base = rf'{re.escape(s0)}[\.\s\-_/]+{re.escape(s1)}'
            matched_base = [p for p in manual_pages if re.search(pat_base, p["text"], re.IGNORECASE)]
            if matched_base:
                return matched_base[:top_k], f"{s0}.{s1}"
        return [], None

    # 2. إنذارات الأعطال (E002, E004, Alarm...)
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
        "قوانص": (["gizzard", "peeler", "cd-6000"], ["roller", "peeling", "infeed", "shaft"])
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
# 6. المساعد الفني الميداني (Maintenance Copilot)
# ==========================================
def maintenance_copilot(query, input_image=None):
    clean_q = query.strip() if query else ""
    matched_image_path = None
    catalog_page_path = None
    response = []

    # 1. فحص الصورة المرفوعة
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
        return "⚠️ يرجى استخدام زر التحدث الصوتي، أو كتابة رقم القطعة / كود الإنذار، أو رفع صورة القطعة.", None, None

    # 2. فحص قاعدة الأعطال والإنذارات
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
        response.append("\n### 🛠️ خطوات الضبط والمعالجة الهندسية (Remedy):")
        for idx, r in enumerate(kb_hit['remedy'], 1):
            response.append(f"{idx}. {r}")
        response.append("\n---\n")

    # 3. فحص الكتالوجات واستخراج صورة الصفحة
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
            
        catalog_page_path = render_pdf_page_as_image(hits[0]['filepath'], hits[0]['page'])
    else:
        if not kb_hit:
            response.append(f"❌ لم يتم العثور على أي تطابق لطلبك `{clean_q}` داخل صفحات الكتالوجات.")

    if matched_image_path:
        response.append("\n🖼️ **تم إرفاق صورة القطعة الحقيقية من أرشيف المستودع الميداني أدناه.**")

    # 4. إرسال تنبيه الواتساب المباشر
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

# ==========================================
# 7. واجهة المستخدم الرسومية (Gradio Interface)
# ==========================================
total_manuals = len(glob.glob(os.path.join(BASE_DIR, "**/*.pdf"), recursive=True))

logo_base64 = ""
for p in ["logo.png", "/app/logo.png"]:
    if os.path.exists(p):
        try:
            with open(p, "rb") as f:
                logo_base64 = base64.b64encode(f.read()).decode("utf-8")
            break
        except Exception:
            pass

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
            alert("يرجى فتح الرابط من متصفح Google Chrome لتفعيل ميزة التحدث بالصوت.");
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
        server_port=PORT,
        allowed_paths=["/tmp"]
    )
