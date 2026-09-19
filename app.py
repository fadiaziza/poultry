import os
import re
import io
import json
import base64
import difflib
from pathlib import Path
from datetime import datetime

import fitz  # PyMuPDF
import pytz
import requests
from PIL import Image
import gradio as gr
from google.cloud import storage

# Gemini / Vertex AI (Google Gen AI SDK)
try:
    from google import genai
    from google.genai.types import HttpOptions, Part
    GENAI_AVAILABLE = True
except Exception:
    GENAI_AVAILABLE = False


# ============================================================
# 0) الإعدادات العامة والبيئة السحابية
# ============================================================
PORT = int(os.environ.get("PORT", "8080"))

BUCKET_NAME = os.environ.get("BUCKET_NAME", "aziza-manuals-storage")
GCS_PREFIX = os.environ.get("GCS_PREFIX", "Maintenance_Manuals/")
BASE_DIR = Path(os.environ.get("BASE_DIR", "/tmp/Maintenance_Manuals"))
IMAGE_DIR = BASE_DIR / "Real_Parts_Images"
MANIFEST_FILE = BASE_DIR / ".gcs_manifest.json"

GOOGLE_CLOUD_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "gen-lang-client-0093400131")
GOOGLE_CLOUD_LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# إعدادات Green-API للواتساب
ID_INSTANCE = os.environ.get("GREEN_API_INSTANCE", "710722737613")
API_TOKEN_INSTANCE = os.environ.get("GREEN_API_TOKEN", "")
ALERT_GROUP_ID = os.environ.get("ALERT_GROUP_ID", "970599431267@c.us")

BASE_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# 1) معالجة وتطبيع النصوص واستخراج الأكواد الرباعية والإنذارات
# ============================================================
ARABIC_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")

ARABIC_ALIASES = {
    "ماكينة": "machine",
    "ماكينه": "machine",
    "مكينة": "machine",
    "مكينه": "machine",
    "اعطال": "fault",
    "عطل": "fault",
    "انذار": "alarm",
    "إنذار": "alarm",
    "كمبروسر": "compressor",
    "كمبرسور": "compressor",
    "ضاغط": "compressor",
    "تغليف": "automac wrapping packaging",
    "تعبئة": "packaging",
    "مايسترو": "maestro eviscerator",
    "رياشة": "plucker picking",
    "رياشه": "plucker picking",
    "سمط": "scalder scalding",
    "نزع": "evisceration",
    "احشاء": "viscera eviscerator",
    "أحشاء": "viscera eviscerator",
    "سير": "conveyor belt",
    "موتور": "motor",
    "محرك": "motor",
    "حساس": "sensor",
    "ضغط": "pressure",
    "حرارة": "temperature",
    "زيت": "oil",
    "فيلم": "film"
}

MACHINE_FILTERS = {
    "مايسترو": ["maestro", "eviscerat"],
    "تغليف": ["automac", "wrapping", "fabbri", "29703", "29800"],
    "تبريد": ["compressor", "chiller", "refrigeration"],
    "كمبرسور": ["compressor", "airpol", "atlas"],
    "رياشة": ["plucker", "picking"],
    "سمط": ["scalder", "scalding"]
}

def normalize_text(value: str) -> str:
    if not value:
        return ""
    s = str(value).strip().lower()
    s = ARABIC_DIACRITICS.sub("", s)
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ى", "ي").replace("ة", "ه")
    s = re.sub(r"[_/\\|]+", " ", s)
    return re.sub(r"\s+", " ", s)

def clean_code(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "", normalize_text(value)).lower()

def extract_identifiers(text: str):
    """استخراج كود القطعة المكون من 4 مقاطع بنقاط أو مسافات، أو أكواد الإنذارات."""
    if not text:
        return []
    
    found = []
    
    # 1. كود القطعة الرباعي: 0990.AD05.007.00 أو 89 3608 904 0096 أو 89.3844.900.0034
    pattern_4_seg = r"\b[A-Za-z0-9]{2,8}[\.\s\-_]+[A-Za-z0-9]{2,8}[\.\s\-_]+[A-Za-z0-9]{2,8}[\.\s\-_]+[A-Za-z0-9]{2,8}\b"
    found.extend(re.findall(pattern_4_seg, text))
    
    # 2. أكواد الإنذارات: E002, E02, E 02, Alarm 02, Error 1
    pattern_alarm = r"\b(?:E|F|ALARM|ERROR)\s*0*\d{1,4}\b"
    found.extend(re.findall(pattern_alarm, text, re.IGNORECASE))
    
    # 3. معرفات إضافية
    pattern_std = r"\b[A-Za-z]{1,4}\d{2,8}\b|\b\d{6,14}\b"
    found.extend(re.findall(pattern_std, text))

    out = []
    seen = set()
    for x in found:
        k = clean_code(x)
        if len(k) >= 3 and k not in seen:
            seen.add(k)
            out.append(x.strip())
    return out


# ============================================================
# 2) مزامنة الملفات من Cloud Storage
# ============================================================
def load_manifest():
    try:
        return json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_manifest(manifest):
    try:
        MANIFEST_FILE.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[GCS] manifest save error: {e}")

def sync_data_from_gcs():
    manifest = load_manifest()
    new_manifest = {}
    downloaded = 0
    removed = 0

    try:
        client = storage.Client()
        bucket = client.bucket(BUCKET_NAME)

        for blob in bucket.list_blobs(prefix=GCS_PREFIX):
            if blob.name.endswith("/"):
                continue

            relative = blob.name[len(GCS_PREFIX):]
            if not relative or any(part == ".." for part in Path(relative).parts):
                continue

            local_path = BASE_DIR / relative
            local_path.parent.mkdir(parents=True, exist_ok=True)

            fingerprint = f"{blob.generation}:{blob.crc32c}:{blob.size}"
            new_manifest[relative] = {"fingerprint": fingerprint}

            old = manifest.get(relative, {})
            if not local_path.exists() or old.get("fingerprint") != fingerprint:
                blob.download_to_filename(str(local_path))
                downloaded += 1

        for relative in manifest:
            if relative not in new_manifest:
                stale = BASE_DIR / relative
                if stale.exists() and stale.is_file():
                    stale.unlink()
                    removed += 1

        save_manifest(new_manifest)
        print(f"[GCS] sync complete: downloaded={downloaded}, removed={removed}")
        return True, f"تم التحديث بنجاح: {downloaded} ملف جديد."
    except Exception as exc:
        print(f"[GCS] sync error: {exc}")
        return False, f"استخدام النسخة المحلية المؤقتة (خطأ: {exc})"


# ============================================================
# 3) فهرسة الكتالوجات وصور المستودع بدون تسريب ذاكرة
# ============================================================
manual_pages = []
part_images = []

def build_manual_index():
    global manual_pages
    manual_pages = []

    pdf_files = sorted(BASE_DIR.rglob("*.pdf"))
    for pdf_path in pdf_files:
        try:
            with fitz.open(pdf_path) as doc:
                for page_num in range(len(doc)):
                    text = doc[page_num].get_text("text") or ""
                    text = text.strip()
                    if len(text) < 15:
                        continue

                    manual_pages.append({
                        "filename": pdf_path.name,
                        "relative_path": str(pdf_path.relative_to(BASE_DIR)),
                        "page": page_num + 1,
                        "text": text,
                        "norm": normalize_text(text),
                    })
        except Exception as exc:
            print(f"[PDF] failed {pdf_path.name}: {exc}")

    print(f"[INDEX] PDF pages indexed: {len(manual_pages)}")

def build_image_index():
    global part_images
    part_images = []

    extensions = {".jpg", ".jpeg", ".png", ".webp"}
    if IMAGE_DIR.exists():
        for p in IMAGE_DIR.rglob("*"):
            if p.is_file() and p.suffix.lower() in extensions:
                part_images.append({
                    "filename": p.name,
                    "stem": p.stem,
                    "clean": clean_code(p.stem),
                    "path": str(p),
                })

    print(f"[INDEX] Part images indexed: {len(part_images)}")


# ============================================================
# 4) محرك البحث الصارم والدقيق لمنع التخبط
# ============================================================
def score_page(page, query, active_filters=None):
    text = page["norm"]
    filename = normalize_text(page["filename"])
    score = 0.0

    # استبعاد الكتالوجات غير المعنية إذا تم تحديد ماكينة معينة
    if active_filters:
        if not any(flt in filename for flt in active_filters):
            return 0.0

    # 1. مطابقة الأكواد الصريحة والإنذارات
    identifiers = extract_identifiers(query)
    for ident in identifiers:
        c = clean_code(ident)
        match_alarm = re.search(r"([a-z]+)0*(\d+)", c)
        if match_alarm:
            pref, num = match_alarm.groups()
            pat = rf"\b{pref}\s*0*{num}\b"
            if re.search(pat, text, re.I):
                score += 50.0
        elif c in clean_code(text):
            score += 45.0

    # 2. مطابقة الكلمات المفتاحية بالترجمة الفنية المعتمدة
    q_norm = normalize_text(query)
    for ar_term, en_trans in ARABIC_ALIASES.items():
        if ar_term in q_norm:
            for w in en_trans.split():
                if w in text:
                    score += 4.0
                if w in filename:
                    score += 8.0

    return score

def search_engine(query, top_k=6):
    if not query or not manual_pages:
        return []

    q_norm = normalize_text(query)
    active_filters = None
    for m_key, filters in MACHINE_FILTERS.items():
        if m_key in q_norm:
            active_filters = filters
            break

    scored = []
    for page in manual_pages:
        s = score_page(page, query, active_filters)
        if s > 0:
            scored.append((s, page))

    if not scored and active_filters:
        for page in manual_pages:
            s = score_page(page, query, None)
            if s > 0:
                scored.append((s, page))

    scored.sort(key=lambda x: (-x[0], x[1]["filename"], x[1]["page"]))
    return [{"score": round(s, 2), **p} for s, p in scored[:top_k]]


# ============================================================
# 5) مطابقة صورة القطعة مع أرشيف المستودع
# ============================================================
def find_image_for_part(query_text):
    if not query_text or not part_images:
        return None

    candidates = extract_identifiers(query_text)
    
    # مطابقة دقيقة للكود المنظف
    for candidate in candidates:
        c = clean_code(candidate)
        if len(c) < 3:
            continue
        for item in part_images:
            if item["clean"] == c:
                return item["path"]

    # مطابقة الاحتواء للأرقام الرباعية
    for candidate in candidates:
        c = clean_code(candidate)
        if len(c) >= 6:
            for item in part_images:
                if c in item["clean"] or item["clean"] in c:
                    return item["path"]

    # مطابقة التشابه
    for candidate in candidates:
        c = clean_code(candidate)
        if len(c) >= 5:
            best_match = None
            highest_ratio = 0.0
            for item in part_images:
                ratio = difflib.SequenceMatcher(None, c, item["clean"]).ratio()
                if ratio > highest_ratio:
                    highest_ratio = ratio
                    best_match = item["path"]
            if highest_ratio >= 0.82:
                return best_match

    return None


# ============================================================
# 6) تكامل الذكاء الاصطناعي مع Gemini
# ============================================================
gemini_client = None

def init_gemini():
    global gemini_client
    if not GENAI_AVAILABLE or not GOOGLE_CLOUD_PROJECT:
        return False

    try:
        os.environ.setdefault("GOOGLE_GENAI_USE_ENTERPRISE", "True")
        gemini_client = genai.Client(
            vertexai=True,
            project=GOOGLE_CLOUD_PROJECT,
            location=GOOGLE_CLOUD_LOCATION,
            http_options=HttpOptions(api_version="v1"),
        )
        print(f"[GEMINI] Connected successfully: {GEMINI_MODEL}")
        return True
    except Exception as exc:
        print(f"[GEMINI] Init error: {exc}")
        gemini_client = None
        return False

def image_to_identification(pil_image, user_text=""):
    if pil_image is None or gemini_client is None:
        return ""

    try:
        img = pil_image.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        image_part = Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg")

        prompt = """
أنت مهندس صيانة صناعية بمصنع دواجن عزيزا. استخرج بدقة تامة ما تراه في الصورة:
1) رقم القطعة (Part Number / Article Code) بدقة تامة.
2) كود الإنذار (Alarm / Error Code) إن ظهر على شاشة.
3) الموديل أو الصانع (Meyn, Automac, Bitzer, Airpol, etc.).
4) مصطلحات تقنية بالإنجليزية تفيد في البحث داخل الكتالوجات.
هام: لا تؤلف أي رقم غير مقروء.
"""
        if user_text:
            prompt += f"\nملاحظة الفني: {user_text}"

        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[prompt, image_part],
        )
        return (response.text or "").strip()
    except Exception as exc:
        print(f"[GEMINI VISION] {exc}")
        return ""

def generate_grounded_diagnosis(query, hits, image_identification=""):
    if gemini_client is None or not hits:
        return ""

    context_blocks = []
    for i, h in enumerate(hits[:5], start=1):
        text_snip = h["text"][:1800].replace("\x00", " ")
        context_blocks.append(
            f"[مرجع {i}]: الملف {h['filename']} - صفحة {h['page']}\n{text_snip}"
        )

    context = "\n\n".join(context_blocks)

    prompt = f"""
أنت مهندس صيانة أول لمسلخ دواجن عزيزا. مهمتك صياغة تقرير عطل مباشر وعملي اعتماداً على نصوص الكتالوجات المرفقة فقط.

بلاغ الفني:
{query}

بيانات الصورة (إن وجدت):
{image_identification or "لا توجد صورة مرفقة"}

النصوص المستخرجة من الكتالوجات:
{context}

الشروط:
1. اذكر الحل وسبب العطل بناءً على الكتالوج المرفق دون أي اختراع.
2. حدد أرقام الصفحات وأسماء الملفات بجانب كل إجراء مقترح.
3. قدم خطوات فحص مرتبة وآمنة لطاقم الصيانة.
"""
    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        return (response.text or "").strip()
    except Exception as exc:
        print(f"[GEMINI DIAGNOSIS] {exc}")
        return ""


# ============================================================
# 7) تنبيهات الواتساب عبر Green-API
# ============================================================
def send_whatsapp_alert(message):
    token = API_TOKEN_INSTANCE or os.environ.get("GREEN_API_TOKEN", "")
    if not (ID_INSTANCE and token and ALERT_GROUP_ID):
        return

    url = f"https://api.green-api.com/waInstance{ID_INSTANCE}/sendMessage/{token}"
    try:
        requests.post(
            url,
            json={"chatId": ALERT_GROUP_ID, "message": message},
            timeout=5,
        )
    except Exception as exc:
        print(f"[WHATSAPP] {exc}")


# ============================================================
# 8) تشغيل المساعد الهندسي
# ============================================================
def make_snippet(text, query):
    text = re.sub(r"\s+", " ", text.replace("\r", " ").replace("\n", " ")).strip()
    terms = extract_identifiers(query)
    
    idx = -1
    for term in terms:
        idx = text.lower().find(term.lower().split()[0])
        if idx >= 0:
            break

    if idx < 0:
        return text[:300] + "..."

    start = max(0, idx - 100)
    end = min(len(text), idx + 350)
    return f"...{text[start:end]}..."

def maintenance_copilot(query, input_image):
    query = (query or "").strip()
    if not query and input_image is None:
        return "⚠️ أدخل وصف العطل أو رقم القطعة/الإنذار، أو ارفع صورة واضحة للقطعة.", None

    image_identification = ""
    extracted_from_image = []
    
    if input_image is not None:
        image_identification = image_to_identification(input_image, query)
        extracted_from_image = extract_identifiers(image_identification)

    search_query = query
    if not search_query and extracted_from_image:
        search_query = extracted_from_image[0]

    hits = search_engine(search_query, top_k=6)
    
    matched_image = find_image_for_part(query)
    if not matched_image and extracted_from_image:
        matched_image = find_image_for_part(" ".join(extracted_from_image))

    diagnosis = generate_grounded_diagnosis(
        query or search_query,
        hits,
        image_identification=image_identification
    )

    response = []

    if image_identification:
        response.append("### 🔎 تحليل وقراءة الصورة بالذكاء الاصطناعي")
        response.append(image_identification)

    if hits:
        response.append("\n### 📚 المراجع الهندسية المطابقة من الكتالوجات")
        for h in hits[:5]:
            response.append(f"- **{h['filename']} — صفحة {h['page']}** (درجة المطابقة: {h['score']})")
            response.append(f"  > {make_snippet(h['text'], search_query)}")
    else:
        response.append("\n### ❌ لم يتم العثور على مراجع مطابقة في الكتالوجات المفهرسة.")
        response.append("تأكد من كتابة كود الإنذار (مثل `E002`) أو رقم القطعة بدقة.")

    if diagnosis:
        response.append("\n---\n### 🛠️ تقرير الصيانة والإجراء المقترح\n" + diagnosis)

    fault_words = ["عطل", "مشكلة", "مشكله", "انذار", "إنذار", "تالف", "كسر", "alarm", "error", "fault"]
    if any(w in normalize_text(query) for w in fault_words):
        tz = pytz.timezone("Asia/Hebron")
        timestamp = datetime.now(tz).strftime("%Y-%m-%d %I:%M %p")
        alert = f"⚠️ *بلاغ صيانة ميداني*\n⏰ {timestamp}\n📝 *الطلب:* {query}\n"
        if hits:
            alert += f"📖 *المرجع:* {hits[0]['filename']} (صفحة {hits[0]['page']})"
        send_whatsapp_alert(alert)

    return "\n".join(response), matched_image


# ============================================================
# 9) تشغيل الواجهة والشعار المعتمد
# ============================================================
sync_ok, sync_message = sync_data_from_gcs()
build_manual_index()
build_image_index()
gemini_ok = init_gemini()

TOTAL_PDFS = len({x["relative_path"] for x in manual_pages})
TOTAL_PAGES = len(manual_pages)
TOTAL_IMAGES = len(part_images)

logo_base64 = ""
for candidate in [Path("logo.png"), Path("/app/logo.png"), BASE_DIR / "logo.png"]:
    if candidate.exists():
        try:
            logo_base64 = base64.b64encode(candidate.read_bytes()).decode("utf-8")
            break
        except Exception:
            pass

logo_html = (
    f'<img src="data:image/png;base64,{logo_base64}" style="width:100%;height:100%;object-fit:contain;">'
    if logo_base64
    else '<span style="font-size:22px;font-weight:900;color:#1b5e20;">عزيزا</span>'
)

HEADER_HTML = f"""
<div style="background:linear-gradient(135deg,#0b3d20 0%,#1b5e20 100%);padding:18px 25px;border-radius:14px;color:white;margin-bottom:20px;box-shadow:0 4px 15px rgba(0,0,0,.18);direction:rtl;text-align:right;border-bottom:4px solid #ffcc00;">
<div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:15px;">
<div style="display:flex;align-items:center;gap:20px;">
<div style="background:#fff;border-radius:50%;padding:4px;display:flex;align-items:center;justify-content:center;width:85px;height:85px;border:3px solid #ffcc00;overflow:hidden;">{logo_html}</div>
<div>
<h1 style="margin:0;font-size:23px;font-weight:800;color:#fff;">شركة دواجن فلسطين - مسلخ عزيزا</h1>
<p style="margin:4px 0 0;font-size:14px;color:#e8f5e9;">مساعد الصيانة الهندسي الذكي — Meyn • Automac • Fabbri • التبريد والضواغط</p>
</div>
</div>
<div style="border-right:2px solid rgba(255,255,255,.25);padding-right:20px;">
<span style="font-size:12px;color:#c8e6c9;display:block;">إعداد وتطوير النظام:</span>
<span style="font-size:16px;font-weight:bold;color:#ffeb3b;">م. فادي محمود</span>
<span style="font-size:12px;color:#e8f5e9;display:block;">قسم الصيانة والأتمتة</span>
</div>
</div>
</div>
"""

STATUS = (
    f"📊 **حالة النظام:** مفهرس `{TOTAL_PDFS}` كتالوجات ({TOTAL_PAGES} صفحة)، و `{TOTAL_IMAGES}` صورة قطعة. "
    f"الذكاء الاصطناعي: {'متصل ومفعّل ✅' if gemini_ok else 'محلي فقط ⚠️'}"
)

with gr.Blocks(title="مساعد الصيانة الهندسي - مسلخ عزيزا") as demo:
    gr.HTML(HEADER_HTML)
    gr.Markdown(STATUS)

    with gr.Row():
        with gr.Column(scale=1):
            query_input = gr.Textbox(
                label="وصف العطل / رقم القطعة (4 مقاطع) / كود الإنذار",
                placeholder="أمثلة: انذار ماكينة التغليف E002 | مشكله ماكينه المايسترو | 0990.AD05.007.00 | 89 3608 904 0096",
                lines=3,
            )
            image_input = gr.Image(
                type="pil",
                label="📷 ارفع صورة القطعة أو لوحة البيانات أو شاشة الإنذار",
            )

            with gr.Row():
                submit_btn = gr.Button("🔍 فحص وتشخيص ومطابقة", variant="primary")
                clear_btn = gr.Button("مسح")

        with gr.Column(scale=1):
            output_box = gr.Markdown()
            matched_img_output = gr.Image(
                type="filepath",
                label="🖼️ صورة القطعة المطابقة من المستودع",
            )

    submit_btn.click(
        fn=maintenance_copilot,
        inputs=[query_input, image_input],
        outputs=[output_box, matched_img_output],
    )

    clear_btn.click(
        fn=lambda: ("", None, "", None),
        inputs=[],
        outputs=[query_input, image_input, output_box, matched_img_output],
    )

if __name__ == "__main__":
    demo.launch(
        server_name="0.0.0.0",
        server_port=PORT,
        allowed_paths=[str(BASE_DIR), ".", "/tmp"],
    )
