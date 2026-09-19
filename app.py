import os
import re
import io
import json
import base64
import difflib
import hashlib
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
# 0) Configuration
# ============================================================
PORT = int(os.environ.get("PORT", "8080"))

BUCKET_NAME = os.environ.get("BUCKET_NAME", "aziza-manuals-storage")
GCS_PREFIX = os.environ.get("GCS_PREFIX", "Maintenance_Manuals/")
BASE_DIR = Path(os.environ.get("BASE_DIR", "/tmp/Maintenance_Manuals"))
IMAGE_DIR = BASE_DIR / "Real_Parts_Images"
MANIFEST_FILE = BASE_DIR / ".gcs_manifest.json"

GOOGLE_CLOUD_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
GOOGLE_CLOUD_LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")

# WhatsApp is optional. Never put secrets directly in GitHub.
ID_INSTANCE = os.environ.get("GREEN_API_INSTANCE", "")
API_TOKEN_INSTANCE = os.environ.get("GREEN_API_TOKEN", "")
ALERT_GROUP_ID = os.environ.get("ALERT_GROUP_ID", "")

BASE_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# 1) Text normalization / tokenization
# ============================================================
ARABIC_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
NON_WORD = re.compile(r"[^a-zA-Z0-9\u0600-\u06FF]+")

ARABIC_ALIASES = {
    "ماكينة": "machine",
    "ماكينه": "machine",
    "مكينة": "machine",
    "مكينه": "machine",
    "اعطال": "عطل",
    "اعطال": "عطل",
    "انذار": "alarm",
    "إنذار": "alarm",
    "تبريد": "refrigeration chiller compressor",
    "كمبروسر": "compressor",
    "كمبرسور": "compressor",
    "ضاغط": "compressor",
    "تغليف": "packaging wrapping",
    "تعبئة": "packaging filling",
    "مايسترو": "maestro",
    "رياشة": "plucker picking",
    "رياشه": "plucker picking",
    "سمط": "scalder scalding",
    "سَمْط": "scalder scalding",
    "نزع": "evisceration",
    "احشاء": "evisceration viscera",
    "أحشاء": "evisceration viscera",
    "سيور": "conveyor belt",
    "سير": "conveyor belt",
    "موتور": "motor",
    "محرك": "motor",
    "حساس": "sensor",
    "مستشعر": "sensor",
    "ضغط": "pressure",
    "حرارة": "temperature",
    "درجة": "temperature",
    "زيت": "oil",
    "فيلم": "film",
}

MACHINE_ALIASES = {
    "meyn": ["meyn", "maestro", "evisceration", "eviscerator"],
    "maestro": ["meyn", "maestro", "evisceration", "eviscerator"],
    "automac": ["automac", "packaging", "wrapping", "tray", "film"],
    "fabbri": ["fabbri", "packaging", "wrapping", "tray", "film"],
    "packaging": ["automac", "fabbri", "packaging", "wrapping", "film"],
    "تغليف": ["automac", "fabbri", "packaging", "wrapping", "film"],
    "refrigeration": ["refrigeration", "chiller", "compressor", "evaporator", "condenser"],
    "تبريد": ["refrigeration", "chiller", "compressor", "evaporator", "condenser"],
    "compressor": ["compressor", "airpol", "atlas", "pressure", "oil", "separator"],
    "كمبرسور": ["compressor", "airpol", "atlas", "pressure", "oil", "separator"],
    "plucker": ["plucker", "picking", "finger", "belt", "motor"],
    "رياشة": ["plucker", "picking", "finger", "belt", "motor"],
    "scalder": ["scalder", "scalding", "temperature", "water", "circulation"],
    "سمط": ["scalder", "scalding", "temperature", "water", "circulation"],
}

def normalize_text(value: str) -> str:
    if not value:
        return ""
    s = str(value).strip().lower()
    s = ARABIC_DIACRITICS.sub("", s)
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ى", "ي").replace("ة", "ه")
    s = re.sub(r"[_/\\|]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s

def expand_query(query: str) -> str:
    q = normalize_text(query)
    words = re.findall(r"[a-zA-Z0-9\u0600-\u06FF]+", q)
    expanded = [q]
    for w in words:
        if w in ARABIC_ALIASES:
            expanded.append(ARABIC_ALIASES[w])
        if w in MACHINE_ALIASES:
            expanded.extend(MACHINE_ALIASES[w])
    return " ".join(expanded)

def clean_code(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "", normalize_text(value)).lower()

def extract_identifiers(text: str):
    """Extract useful part numbers, alarms, model numbers and numeric identifiers."""
    if not text:
        return []
    patterns = [
        r"\b[A-Za-z]{1,6}\d{1,8}\b",                 # E002, M123
        r"\b\d{2,8}(?:[.\-_]\d{1,8}){1,6}\b",       # 0990.AD05.007.00
        r"\b[A-Za-z0-9]+(?:[.\-_][A-Za-z0-9]+){2,6}\b",
        r"\b\d{6,18}\b",
    ]
    found = []
    for pat in patterns:
        found.extend(re.findall(pat, text))
    out = []
    seen = set()
    for x in found:
        k = clean_code(x)
        if len(k) >= 3 and k not in seen:
            seen.add(k)
            out.append(x)
    return out


# ============================================================
# 2) Google Cloud Storage synchronization
# ============================================================
def load_manifest():
    try:
        return json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_manifest(manifest):
    MANIFEST_FILE.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def sync_data_from_gcs():
    """Sync manuals/images from GCS and remove stale local files."""
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
            if not relative:
                continue

            # Prevent path traversal.
            rel_path = Path(relative)
            if any(part == ".." for part in rel_path.parts):
                continue

            local_path = BASE_DIR / rel_path
            local_path.parent.mkdir(parents=True, exist_ok=True)

            generation = str(blob.generation or "")
            crc32c = str(blob.crc32c or "")
            fingerprint = f"{generation}:{crc32c}:{blob.size}"

            new_manifest[relative] = {
                "fingerprint": fingerprint,
                "generation": generation,
                "size": blob.size,
            }

            old = manifest.get(relative, {})
            if not local_path.exists() or old.get("fingerprint") != fingerprint:
                blob.download_to_filename(str(local_path))
                downloaded += 1

        # Remove local files that disappeared from GCS.
        for relative in manifest:
            if relative not in new_manifest:
                stale = BASE_DIR / relative
                if stale.exists() and stale.is_file():
                    stale.unlink()
                    removed += 1

        save_manifest(new_manifest)
        print(f"[GCS] sync complete: downloaded={downloaded}, removed={removed}")
        return True, f"GCS sync OK: {downloaded} downloaded, {removed} removed."

    except Exception as exc:
        print(f"[GCS] sync warning: {exc}")
        # The application can still operate on already cached files.
        return False, f"GCS sync failed; using local cache. Error: {exc}"


# ============================================================
# 3) Local indexes
# ============================================================
manual_pages = []
part_images = []

def build_manual_index():
    global manual_pages
    manual_pages = []

    pdf_files = sorted(BASE_DIR.rglob("*.pdf"))
    for pdf_path in pdf_files:
        try:
            doc = fitz.open(pdf_path)
            for page_num in range(len(doc)):
                text = doc[page_num].get_text("text") or ""
                text = text.strip()
                if len(text) < 10:
                    continue

                filename = pdf_path.name
                rel = str(pdf_path.relative_to(BASE_DIR))

                manual_pages.append({
                    "filename": filename,
                    "relative_path": rel,
                    "page": page_num + 1,
                    "text": text,
                    "norm": normalize_text(text),
                })
            doc.close()
        except Exception as exc:
            print(f"[PDF] failed: {pdf_path}: {exc}")

    print(f"[INDEX] PDF pages indexed: {len(manual_pages)}")

def build_image_index():
    global part_images
    part_images = []

    extensions = {".jpg", ".jpeg", ".png", ".webp"}
    for p in IMAGE_DIR.rglob("*"):
        if p.is_file() and p.suffix.lower() in extensions:
            stem = p.stem
            part_images.append({
                "filename": p.name,
                "relative_path": str(p.relative_to(IMAGE_DIR)),
                "stem": stem,
                "clean": clean_code(stem),
                "path": str(p),
            })

    print(f"[INDEX] part images indexed: {len(part_images)}")


# ============================================================
# 4) Deterministic local search
# ============================================================
def score_page(page, query):
    q_norm = normalize_text(query)
    expanded = expand_query(query)
    text = page["norm"]
    filename = normalize_text(page["filename"])

    score = 0.0

    # Exact phrase is highly valuable.
    if q_norm and q_norm in text:
        score += 20

    # Identifiers / part numbers.
    for ident in extract_identifiers(query):
        c = clean_code(ident)
        if c and c in clean_code(text):
            score += 30

    # Query tokens.
    tokens = [t for t in re.findall(r"[a-zA-Z0-9\u0600-\u06FF]+", expanded)
              if len(t) >= 3]
    for token in tokens:
        if token in text:
            score += 2.5
        if token in filename:
            score += 5

    # Machine-specific vocabulary.
    for key, aliases in MACHINE_ALIASES.items():
        if normalize_text(key) in q_norm:
            for alias in aliases:
                if normalize_text(alias) in text or normalize_text(alias) in filename:
                    score += 4

    # Error/alarm exactness.
    for alarm in re.findall(r"\b(?:E|F|ALARM|ERROR)\s*0*\d+\b", query, re.I):
        digits = re.search(r"\d+", alarm)
        if digits:
            n = int(digits.group())
            patterns = [
                rf"\bE0*{n}\b",
                rf"\bF0*{n}\b",
                rf"\balarm\s*0*{n}\b",
                rf"\berror\s*0*{n}\b",
            ]
            if any(re.search(p, text, re.I) for p in patterns):
                score += 35

    return score

def search_engine(query, top_k=8):
    if not query or not manual_pages:
        return []

    scored = []
    for page in manual_pages:
        s = score_page(page, query)
        if s > 0:
            scored.append((s, page))

    scored.sort(key=lambda x: (-x[0], x[1]["filename"], x[1]["page"]))
    return [{"score": round(s, 2), **p} for s, p in scored[:top_k]]


# ============================================================
# 5) Part image matching
# ============================================================
def find_image_for_part(query_text):
    if not query_text or not part_images:
        return None

    identifiers = extract_identifiers(query_text)
    candidates = identifiers + re.findall(
        r"[A-Za-z0-9][A-Za-z0-9._-]{3,}", query_text
    )

    # Exact normalized code.
    for candidate in candidates:
        c = clean_code(candidate)
        if len(c) < 3:
            continue
        for item in part_images:
            if item["clean"] == c:
                return item["path"]

    # Containment for long part numbers.
    for candidate in candidates:
        c = clean_code(candidate)
        if len(c) < 6:
            continue
        for item in part_images:
            if c in item["clean"] or item["clean"] in c:
                return item["path"]

    # Fuzzy filename match.
    query_clean = clean_code(" ".join(candidates))
    if len(query_clean) >= 6:
        best = None
        for item in part_images:
            ratio = difflib.SequenceMatcher(None, query_clean, item["clean"]).ratio()
            if best is None or ratio > best[0]:
                best = (ratio, item["path"])
        if best and best[0] >= 0.78:
            return best[1]

    return None


# ============================================================
# 6) Gemini / Vertex AI multimodal analysis
# ============================================================
gemini_client = None

def init_gemini():
    global gemini_client
    if not GENAI_AVAILABLE:
        print("[GEMINI] google-genai is not installed.")
        return False

    if not GOOGLE_CLOUD_PROJECT:
        print("[GEMINI] GOOGLE_CLOUD_PROJECT is not configured.")
        return False

    try:
        os.environ.setdefault("GOOGLE_GENAI_USE_ENTERPRISE", "True")
        gemini_client = genai.Client(
            vertexai=True,
            project=GOOGLE_CLOUD_PROJECT,
            location=GOOGLE_CLOUD_LOCATION,
            http_options=HttpOptions(api_version="v1"),
        )
        print(f"[GEMINI] ready: {GEMINI_MODEL}")
        return True
    except Exception as exc:
        print(f"[GEMINI] initialization failed: {exc}")
        gemini_client = None
        return False

def image_to_identification(pil_image, user_text=""):
    """Use Gemini vision to read labels/part numbers and identify machine context."""
    if pil_image is None:
        return ""

    if gemini_client is None:
        return ""

    try:
        img = pil_image.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92)
        image_part = Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg")

        prompt = """
أنت مهندس صيانة صناعية متخصص في معدات مسالخ الدواجن.
حلّل الصورة بهدف IDENTIFICATION وليس التخمين.

أعد نصاً قصيراً يحتوي على:
1) اسم/نوع القطعة الظاهر إن أمكن.
2) الشركة المصنعة إن ظهرت.
3) رقم القطعة Part Number / Item Number إن ظهر.
4) Model / Type / Serial إن ظهر.
5) رقم Alarm أو Error إن ظهر على الشاشة.
6) اسم الماكينة المحتمل فقط إذا كان هناك دليل بصري واضح.
7) كلمات إنجليزية فنية يمكن البحث بها داخل كتالوجات الصيانة.

مهم جداً:
- لا تخترع أي رقم غير ظاهر.
- إذا كان النص غير مقروء اكتب "غير مقروء".
- احتفظ بالأرقام والحروف كما تظهر في الصورة.
"""
        if user_text:
            prompt += f"\nوصف الفني المرافق للصورة:\n{user_text}"

        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[prompt, image_part],
        )
        return (response.text or "").strip()

    except Exception as exc:
        print(f"[GEMINI IMAGE] {exc}")
        return ""

def generate_grounded_diagnosis(query, hits, image_identification=""):
    """Generate a diagnosis strictly grounded in retrieved manual pages."""
    if gemini_client is None:
        return ""

    if not hits:
        return ""

    context_blocks = []
    for i, h in enumerate(hits[:8], start=1):
        text = h["text"].replace("\x00", " ")
        # Keep context bounded.
        text = text[:3500]
        context_blocks.append(
            f"[REFERENCE {i}]\n"
            f"File: {h['filename']}\n"
            f"Page: {h['page']}\n"
            f"Content:\n{text}"
        )

    context = "\n\n".join(context_blocks)

    prompt = f"""
أنت مساعد صيانة هندسي لطاقم صيانة في مسلخ دواجن.
مهمتك تحليل بلاغ الفني اعتماداً على المراجع المسترجعة فقط.

بلاغ الفني:
{query}

تحليل الصورة إن وجد:
{image_identification or "لا توجد صورة أو لم يتم التعرف عليها."}

المراجع:
{context}

التزم بالقواعد:
- لا تدّعي أن معلومة موجودة في الكتالوج إذا لم تكن موجودة.
- لا تخترع أرقام قطع أو قيم ضبط أو ضغوط أو درجات حرارة.
- فرّق بوضوح بين "مذكور في المرجع" و"استنتاج تشخيصي".
- أعط خطوات فحص عملية وآمنة، بدءاً من الفحوصات الأقل خطورة.
- إذا كانت البيانات غير كافية، اذكر بالضبط ما يحتاجه الفني: موديل الماكينة، رقم الإنذار، صورة لوحة البيانات، قياس ضغط/حرارة، إلخ.
- اربط كل معلومة مهمة باسم الملف ورقم الصفحة.
- اكتب بالعربية المهنية الواضحة، مع إبقاء أسماء القطع والـ Part Numbers بالإنجليزية.

صيغة الرد:
### التشخيص المبدئي
### خطوات الفحص
### الإجراء المقترح
### ما يحتاج إلى تحقق
### المراجع
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
# 7) WhatsApp alert
# ============================================================
def send_whatsapp_alert(message):
    if not (ID_INSTANCE and API_TOKEN_INSTANCE and ALERT_GROUP_ID):
        return

    url = f"https://api.green-api.com/waInstance{ID_INSTANCE}/sendMessage/{API_TOKEN_INSTANCE}"
    try:
        requests.post(
            url,
            json={"chatId": ALERT_GROUP_ID, "message": message},
            timeout=8,
        )
    except Exception as exc:
        print(f"[WHATSAPP] {exc}")


# ============================================================
# 8) Main copilot
# ============================================================
def make_snippet(text, query):
    text = text.replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()

    terms = extract_identifiers(query)
    idx = -1
    for term in terms:
        idx = text.lower().find(term.lower())
        if idx >= 0:
            break

    if idx < 0:
        q_words = [w for w in normalize_text(query).split() if len(w) >= 4]
        for word in q_words:
            idx = normalize_text(text).find(word)
            if idx >= 0:
                break

    if idx < 0:
        return text[:500]

    return text[max(0, idx - 180): min(len(text), idx + 520)]

def maintenance_copilot(query, input_image):
    query = (query or "").strip()

    if not query and input_image is None:
        return (
            "⚠️ أدخل وصف العطل أو رقم القطعة/الإنذار، أو ارفع صورة واضحة للقطعة.",
            None,
        )

    image_identification = ""
    if input_image is not None:
        image_identification = image_to_identification(input_image, query)

    combined_query = "\n".join(x for x in [query, image_identification] if x)
    hits = search_engine(combined_query, top_k=8)

    matched_image = find_image_for_part(combined_query)

    # If Gemini identified a code but local search missed it, search each identifier separately.
    if not hits:
        for ident in extract_identifiers(image_identification):
            hits = search_engine(ident, top_k=8)
            if hits:
                break

    diagnosis = generate_grounded_diagnosis(
        query or "استفسار من صورة",
        hits,
        image_identification=image_identification,
    )

    response = []

    if image_identification:
        response.append("### 🔎 قراءة الصورة بالذكاء الاصطناعي")
        response.append(image_identification)

    if hits:
        response.append("\n### 📚 المراجع التي تم العثور عليها")
        for h in hits[:6]:
            response.append(
                f"- **{h['filename']} — صفحة {h['page']}** "
                f"(درجة المطابقة {h['score']})"
            )
            response.append(f"  > {make_snippet(h['text'], combined_query)}")

    else:
        response.append(
            "\n### ❌ لم يتم العثور على مرجع مطابق في الكتالوجات المحلية."
        )
        response.append(
            "جرّب إضافة **Model / Part Number / Alarm Code** أو صورة لوحة البيانات."
        )

    if diagnosis:
        response.append("\n---\n")
        response.append(diagnosis)

    if matched_image:
        response.append(
            "\n### 🖼️ صورة القطعة من أرشيف المستودع"
        )
    else:
        if input_image is not None:
            response.append(
                "\nℹ️ لم يتم العثور على صورة أرشيفية مطابقة. "
                "الصورة المرفوعة استُخدمت للتعرّف على رقم/نوع القطعة، "
                "لكن لا يوجد تطابق موثوق في أرشيف الصور."
            )

    # WhatsApp alert only for actual fault reports.
    fault_words = [
        "عطل", "مشكلة", "مشكله", "انذار", "إنذار", "تالف",
        "كسر", "توقف", "متوقف", "alarm", "error", "fault", "stop"
    ]
    if any(w in normalize_text(query) for w in fault_words):
        tz = pytz.timezone("Asia/Hebron")
        timestamp = datetime.now(tz).strftime("%Y-%m-%d %I:%M %p")
        alert = (
            f"⚠️ بلاغ صيانة ميداني\n"
            f"الوقت: {timestamp}\n"
            f"البلاغ: {query}\n"
        )
        if hits:
            alert += f"المرجع: {hits[0]['filename']} صفحة {hits[0]['page']}"
        send_whatsapp_alert(alert)

    return "\n".join(response), matched_image


# ============================================================
# 9) Startup
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
    f'<img src="data:image/png;base64,{logo_base64}" '
    'style="width:100%;height:100%;object-fit:contain;">'
    if logo_base64
    else '<span style="font-size:20px;font-weight:900;color:#1b5e20;">عزيزا</span>'
)

HEADER_HTML = f"""
<div style="background:linear-gradient(135deg,#0b3d20 0%,#1b5e20 100%);
padding:18px 25px;border-radius:14px;color:white;margin-bottom:20px;
box-shadow:0 4px 15px rgba(0,0,0,.18);direction:rtl;text-align:right;
border-bottom:4px solid #ffcc00;">
<div style="display:flex;align-items:center;justify-content:space-between;
flex-wrap:wrap;gap:15px;">
<div style="display:flex;align-items:center;gap:20px;">
<div style="background:#fff;border-radius:50%;padding:4px;
display:flex;align-items:center;justify-content:center;width:85px;height:85px;
border:3px solid #ffcc00;overflow:hidden;">{logo_html}</div>
<div>
<h1 style="margin:0;font-size:23px;font-weight:800;color:#fff;">
شركة دواجن فلسطين - مسلخ عزيزا
</h1>
<p style="margin:4px 0 0;font-size:14px;color:#e8f5e9;">
مساعد الصيانة الهندسي الذكي — Meyn • Automac • Fabbri • التبريد • الضواغط
</p>
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
    f"📊 **حالة النظام:** {TOTAL_PDFS} كتالوجات، {TOTAL_PAGES} صفحة مفهرسة، "
    f"{TOTAL_IMAGES} صورة قطعة. "
    f"Gemini: {'متصل ✅' if gemini_ok else 'غير متصل ⚠️'}"
)

with gr.Blocks(title="مساعد الصيانة الهندسي - مسلخ عزيزا") as demo:
    gr.HTML(HEADER_HTML)
    gr.Markdown(STATUS)

    with gr.Row():
        with gr.Column(scale=1):
            query_input = gr.Textbox(
                label="وصف العطل / رقم القطعة / Alarm / Model",
                placeholder=(
                    "مثال: ماكينة التغليف تتوقف ويظهر E002\n"
                    "أو: 0990.AD05.007.00\n"
                    "أو: الضاغط ضغط الزيت منخفض"
                ),
                lines=4,
            )
            image_input = gr.Image(
                type="pil",
                label="📷 ارفع صورة القطعة أو لوحة البيانات أو شاشة الإنذار",
            )

            with gr.Row():
                submit_btn = gr.Button(
                    "🔍 فحص وتشخيص ومطابقة",
                    variant="primary",
                )
                clear_btn = gr.Button("مسح")

        with gr.Column(scale=1):
            output_box = gr.Markdown()
            matched_img_output = gr.Image(
                type="filepath",
                label="🖼️ صورة القطعة المطابقة من الأرشيف",
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
