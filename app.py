import os
import re
import io
import json
import glob
import logging
from typing import Dict, List, Optional, Tuple

import fitz
import gradio as gr
import requests
from PIL import Image
from google.cloud import storage

# Optional semantic search. The app still works if the model cannot be loaded.
try:
    from sentence_transformers import SentenceTransformer, util
except Exception:
    SentenceTransformer = None
    util = None

# ============================================================
# AZIZA AI MAINTENANCE COPILOT
# GitHub = code | Google Cloud Storage = company data
# ============================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aziza-maintenance")

PORT = int(os.getenv("PORT", "7860"))
BUCKET_NAME = os.getenv("GCS_BUCKET_NAME", "aziza-manuals-storage")
GCS_PREFIX = os.getenv("GCS_PREFIX", "Maintenance_Manuals").strip("/")
BASE_DIR = os.getenv("LOCAL_DATA_DIR", "/tmp/Maintenance_Manuals")
IMAGE_DIR = os.path.join(BASE_DIR, "Real_Parts_Images")
ALARM_DIR = os.path.join(BASE_DIR, "alarms")
CACHE_DIR = os.path.join(BASE_DIR, ".cache")
MANIFEST_FILE = os.path.join(CACHE_DIR, "gcs_manifest.json")

# Multilingual model: Arabic + English maintenance descriptions.
EMBEDDING_MODEL_NAME = os.getenv(
    "EMBEDDING_MODEL_NAME", "paraphrase-multilingual-MiniLM-L12-v2"
)
ENABLE_SEMANTIC_SEARCH = os.getenv("ENABLE_SEMANTIC_SEARCH", "true").lower() == "true"
SEMANTIC_TOP_K = int(os.getenv("SEMANTIC_TOP_K", "5"))

# Optional Green-API WhatsApp notification. NEVER put these values in GitHub.
GREEN_API_ID = os.getenv("GREEN_API_ID")
GREEN_API_TOKEN = os.getenv("GREEN_API_TOKEN")
ALERT_GROUP_ID = os.getenv("ALERT_GROUP_ID")

for directory in (BASE_DIR, IMAGE_DIR, ALARM_DIR, CACHE_DIR):
    os.makedirs(directory, exist_ok=True)

manual_pages: List[dict] = []
part_images_map: Dict[str, str] = {}
troubleshooting_kb: Dict[str, dict] = {}
embedding_model = None
manual_embeddings = None

DEFAULT_TROUBLESHOOTING_KB = {
    "E002": {
        "title": "Tray Infeed Jam",
        "machine": "Automac 75/297",
        "causes": [
            "وجود انحشار في الصينية أو المنتج عند منطقة الإدخال.",
            "عدم محاذاة الصينية أو الأدلة الجانبية.",
            "وجود عائق ميكانيكي.",
            "عدم عمل حساس الإدخال أو اتساخه أو عدم محاذاته.",
        ],
        "remedies": [
            "إيقاف الماكينة حسب إجراء السلامة المعتمد.",
            "إزالة سبب الانحشار.",
            "فحص محاذاة الصينية والأدلة.",
            "تنظيف وفحص الحساسات.",
            "تشغيل الماكينة بسرعة منخفضة والتأكد من التغذية الطبيعية.",
        ],
    },
    "E004": {
        "title": "Film Reel Empty / Broken",
        "machine": "Automac",
        "causes": [
            "بكرة الفيلم فارغة.",
            "انقطاع الفيلم.",
            "خطأ في مسار الفيلم أو الشد.",
            "اتساخ أو سوء محاذاة حساس الفيلم.",
        ],
        "remedies": [
            "فحص بكرة الفيلم واستبدالها عند الحاجة.",
            "إعادة تركيب أو توصيل الفيلم.",
            "فحص شد ومسار الفيلم.",
            "تنظيف وفحص حساس الفيلم.",
        ],
    },
    "E014": {
        "title": "Sealing Belt Temperature Fault",
        "machine": "Automac",
        "causes": [
            "درجة حرارة سير اللحام خارج القيمة المضبوطة.",
            "مشكلة في عنصر التسخين.",
            "مشكلة في حساس الحرارة.",
            "خلل في الكنترول أو التوصيلات.",
        ],
        "remedies": [
            "فحص Setpoint درجة الحرارة.",
            "فحص عنصر التسخين.",
            "فحص حساس الحرارة والأسلاك.",
            "فحص خرج وحدة التحكم.",
        ],
    },
    "MAESTRO": {
        "title": "Meyn Maestro Eviscerator",
        "machine": "Meyn Maestro",
        "causes": [
            "عدم ضبط ماكينة نزع الأحشاء.",
            "تآكل أو عدم محاذاة أجزاء ميكانيكية.",
            "مشكلة في وضع المنتج.",
            "وجود اتساخ أو جسم غريب.",
        ],
        "remedies": [
            "إيقاف الماكينة حسب إجراءات السلامة.",
            "فحص الأدلة والأجزاء العاملة.",
            "فحص المحاذاة والضبط.",
            "تنظيف المنطقة وفحص التآكل.",
        ],
    },
    "رياشة": {
        "title": "Plucker",
        "machine": "Plucker",
        "causes": [
            "تآكل أو تلف أصابع الرياشة.",
            "ضبط غير صحيح.",
            "مشكلة في المياه أو التدفق.",
            "عائق ميكانيكي.",
        ],
        "remedies": [
            "فحص أصابع الرياشة.",
            "فحص الضبط ودخول المنتج.",
            "فحص مصدر وتدفق المياه.",
            "فحص الحركة والأجزاء الميكانيكية.",
        ],
    },
    "قوانص": {
        "title": "Gizzard Peeler",
        "machine": "Meyn CD-6000",
        "causes": [
            "ضبط غير صحيح.",
            "تآكل السكين أو الرولات.",
            "مشكلة في وضع المنتج.",
            "اتساخ أو جسم غريب.",
        ],
        "remedies": [
            "فحص مجموعة التقشير.",
            "فحص حالة السكين والرولات.",
            "فحص الضبط.",
            "تنظيف الماكينة وفحصها قبل التشغيل.",
        ],
    },
}

ARABIC_MACHINE_TERMS = {
    "مايسترو": ["MAESTRO", "MEYN"],
    "رياشة": ["PLUCKER"],
    "سمط": ["SCALDER", "SCALDING"],
    "قوانص": ["GIZZARD", "CD-6000"],
    "تغليف": ["AUTOMAC", "PACKAGING"],
    "تبريد": ["REFRIGERATION", "CHILLER", "COOLING"],
    "كمبرسور": ["COMPRESSOR"],
}


def normalize_text(value: str) -> str:
    if value is None:
        return ""
    value = str(value).upper().strip()
    value = value.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    value = value.replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"[^A-Z0-9\u0600-\u06FF]+", "", value)


def normalize_part(value: str) -> str:
    if value is None:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value)


def sync_data_from_gcs() -> bool:
    """Download GCS data into /tmp without putting company files in GitHub."""
    try:
        client = storage.Client()
        bucket = client.bucket(BUCKET_NAME)
        blobs = list(bucket.list_blobs(prefix=f"{GCS_PREFIX}/"))
        manifest = {}
        try:
            with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:
            manifest = {}

        downloaded = 0
        for blob in blobs:
            relative = blob.name[len(GCS_PREFIX):].lstrip("/")
            if not relative or blob.name.endswith("/"):
                continue

            local_path = os.path.join(BASE_DIR, relative)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            remote_signature = f"{blob.generation}:{blob.size}:{blob.updated}"
            previous = manifest.get(blob.name)

            if previous != remote_signature or not os.path.exists(local_path):
                blob.download_to_filename(local_path)
                downloaded += 1

            manifest[blob.name] = remote_signature

        with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        logger.info("GCS sync: %d objects, %d downloaded", len(blobs), downloaded)
        return True
    except Exception as exc:
        logger.exception("GCS sync failed: %s", exc)
        return False


def load_alarm_database() -> Dict[str, dict]:
    kb = {k.upper(): v for k, v in DEFAULT_TROUBLESHOOTING_KB.items()}
    for path in glob.glob(os.path.join(ALARM_DIR, "*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for key, value in data.items():
                    if isinstance(value, dict):
                        kb[str(key).upper()] = value
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        code = item.get("code") or item.get("alarm") or item.get("id")
                        if code:
                            kb[str(code).upper()] = item
        except Exception as exc:
            logger.warning("Alarm JSON failed: %s", exc)
    return kb


def build_manual_index() -> None:
    global manual_pages
    manual_pages = []
    for filepath in glob.glob(os.path.join(BASE_DIR, "**", "*.pdf"), recursive=True):
        try:
            doc = fitz.open(filepath)
            for page_no, page in enumerate(doc, start=1):
                text = re.sub(r"\s+", " ", page.get_text("text") or "").strip()
                if len(text) >= 15:
                    manual_pages.append({
                        "filename": os.path.basename(filepath),
                        "filepath": filepath,
                        "page": page_no,
                        "text": text,
                        "norm": normalize_text(text),
                    })
            doc.close()
        except Exception as exc:
            logger.warning("PDF failed %s: %s", filepath, exc)
    logger.info("Indexed %d PDF pages", len(manual_pages))


def build_semantic_index() -> None:
    global embedding_model, manual_embeddings
    if not ENABLE_SEMANTIC_SEARCH or SentenceTransformer is None or not manual_pages:
        return
    try:
        logger.info("Loading embedding model: %s", EMBEDDING_MODEL_NAME)
        embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME)
        texts = [p["text"][:5000] for p in manual_pages]
        manual_embeddings = embedding_model.encode(
            texts, convert_to_tensor=True, normalize_embeddings=True,
            show_progress_bar=False,
        )
        logger.info("Semantic index ready: %d pages", len(texts))
    except Exception as exc:
        embedding_model = None
        manual_embeddings = None
        logger.warning("Semantic model unavailable; lexical search will be used: %s", exc)


def build_image_index() -> None:
    global part_images_map
    part_images_map = {}
    for filepath in glob.glob(os.path.join(IMAGE_DIR, "**", "*"), recursive=True):
        if not os.path.isfile(filepath):
            continue
        if os.path.splitext(filepath)[1].lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            continue
        stem = os.path.splitext(os.path.basename(filepath))[0]
        key = normalize_part(stem)
        if key:
            part_images_map[key] = filepath
    logger.info("Indexed %d part images", len(part_images_map))


def render_pdf_page(filepath: str, page_number: int) -> Optional[str]:
    try:
        doc = fitz.open(filepath)
        index = page_number - 1
        if index < 0 or index >= len(doc):
            doc.close()
            return None
        pix = doc[index].get_pixmap(matrix=fitz.Matrix(1.7, 1.7), alpha=False)
        image = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        out = os.path.join("/tmp", f"catalog_{safe_filename(os.path.basename(filepath))}_{page_number}.png")
        image.save(out)
        doc.close()
        return out
    except Exception as exc:
        logger.warning("Render page failed: %s", exc)
        return None


def find_image_for_part(query: str) -> Optional[str]:
    q = normalize_part(query)
    if not q:
        return None
    if q in part_images_map:
        return part_images_map[q]
    for key, path in part_images_map.items():
        if q in key or key in q:
            return path
    return None


def simple_image_match(uploaded: Image.Image) -> Tuple[Optional[str], Optional[float]]:
    """Fallback visual matcher; replaceable later by a dedicated vision model."""
    if uploaded is None or not part_images_map:
        return None, None
    try:
        query = uploaded.convert("RGB").resize((32, 32))
        q_pixels = list(query.getdata())
        best_key, best_score = None, float("inf")
        for key, path in part_images_map.items():
            try:
                ref = Image.open(path).convert("RGB").resize((32, 32))
                r_pixels = list(ref.getdata())
                score = sum(
                    abs(a[0]-b[0]) + abs(a[1]-b[1]) + abs(a[2]-b[2])
                    for a, b in zip(q_pixels, r_pixels)
                ) / (32 * 32 * 3)
                if score < best_score:
                    best_key, best_score = key, score
            except Exception:
                continue
        return (best_key, best_score) if best_key and best_score <= 65 else (None, best_score)
    except Exception:
        return None, None


def extract_alarm_codes(query: str) -> List[str]:
    return [re.sub(r"[-_ ]", "", x) for x in re.findall(r"\bE[-_ ]?\d{2,5}\b", str(query).upper())]


def lexical_search(query: str, top_k: int = 5) -> List[Tuple[float, dict]]:
    qnorm = normalize_text(query)
    tokens = [normalize_text(x) for x in re.findall(r"[\w\u0600-\u06FF]+", str(query).upper()) if len(x) >= 2]
    results = []

    for page in manual_pages:
        score = 0.0
        if qnorm and qnorm in page["norm"]:
            score += 10
        for token in tokens:
            if token and token in page["norm"]:
                score += 1
        for code in extract_alarm_codes(query):
            if normalize_text(code) in page["norm"]:
                score += 15
        for keyword, aliases in ARABIC_MACHINE_TERMS.items():
            if keyword in str(query):
                for alias in aliases:
                    if alias.upper() in page["text"].upper():
                        score += 3
        if score > 0:
            results.append((score, page))

    results.sort(key=lambda x: x[0], reverse=True)
    return results[:top_k]


def semantic_search(query: str, top_k: int = 5) -> List[Tuple[float, dict]]:
    if embedding_model is None or manual_embeddings is None or util is None:
        return []
    try:
        q = embedding_model.encode(query, convert_to_tensor=True, normalize_embeddings=True)
        scores = util.cos_sim(q, manual_embeddings)[0]
        values, indices = scores.topk(min(top_k, len(manual_pages)))
        return [(float(v), manual_pages[int(i)]) for v, i in zip(values, indices)]
    except Exception as exc:
        logger.warning("Semantic search failed: %s", exc)
        return []


def search_engine(query: str, top_k: int = 5) -> List[dict]:
    lexical = lexical_search(query, top_k=max(top_k, 8))
    semantic = semantic_search(query, top_k=max(top_k, 8))
    combined: Dict[Tuple[str, int], Tuple[float, dict]] = {}

    for score, page in lexical:
        key = (page["filepath"], page["page"])
        combined[key] = (score, page)

    # Semantic score is 0..1. Give it enough weight to surface meaning-based matches,
    # while lexical exact matches remain dominant for part numbers and alarm codes.
    for score, page in semantic:
        key = (page["filepath"], page["page"])
        semantic_score = score * 8
        if key in combined:
            combined[key] = (combined[key][0] + semantic_score, page)
        else:
            combined[key] = (semantic_score, page)

    ranked = sorted(combined.values(), key=lambda x: x[0], reverse=True)
    return [page for _, page in ranked[:top_k]]


def find_troubleshooting(query: str) -> Tuple[Optional[str], Optional[dict]]:
    for code in extract_alarm_codes(query):
        if code in troubleshooting_kb:
            return code, troubleshooting_kb[code]
    upper = str(query).upper()
    for key, item in troubleshooting_kb.items():
        if str(key).upper() in upper:
            return key, item
    for keyword in ARABIC_MACHINE_TERMS:
        if keyword in str(query):
            for key, item in troubleshooting_kb.items():
                blob = json.dumps(item, ensure_ascii=False).upper()
                if any(alias.upper() in blob for alias in ARABIC_MACHINE_TERMS[keyword]):
                    return key, item
    return None, None


def send_whatsapp_alert(message: str) -> bool:
    if not all((GREEN_API_ID, GREEN_API_TOKEN, ALERT_GROUP_ID)):
        return False
    try:
        url = f"https://api.green-api.com/waInstance{GREEN_API_ID}/sendMessage/{GREEN_API_TOKEN}"
        response = requests.post(
            url,
            json={"chatId": ALERT_GROUP_ID, "message": message},
            timeout=10,
        )
        return response.ok
    except Exception as exc:
        logger.warning("WhatsApp failed: %s", exc)
        return False


def maintenance_copilot(query: str, input_image=None):
    query = (query or "").strip()
    recognized_part = None
    image_score = None
    messages = []

    if input_image is not None:
        recognized_part, image_score = simple_image_match(input_image)
        if recognized_part:
            messages.append(f"📷 **التعرف المبدئي من الصورة:** `{recognized_part}`")
            if not query:
                query = recognized_part
        elif not query:
            return (
                "⚠️ لم أتمكن من مطابقة الصورة بثقة كافية. أدخل رقم القطعة أو وصف العطل.",
                None,
                None,
            )

    if not query:
        return "اكتب رقم القطعة أو Alarm أو وصف المشكلة.", None, None

    alarm_code, troubleshooting = find_troubleshooting(query)
    results = search_engine(query, top_k=5)
    part_image = find_image_for_part(recognized_part or query)

    response = [
        "## 🔧 المساعد الذكي للصيانة – مسلخ شركة دواجن فلسطين / عزيزا",
        f"**البحث:** {query}",
    ]
    if messages:
        response.extend(messages)

    if alarm_code and troubleshooting:
        response += [
            "\n### 🚨 تشخيص العطل",
            f"**Alarm:** `{alarm_code}`",
            f"**المعدة:** {troubleshooting.get('machine', 'غير محددة')}",
            f"**العنوان:** {troubleshooting.get('title', '')}",
        ]
        causes = troubleshooting.get("causes", [])
        remedies = troubleshooting.get("remedies", [])
        if causes:
            response.append("\n**الأسباب المحتملة:**")
            response.extend(f"- {x}" for x in causes)
        if remedies:
            response.append("\n**إجراءات الفحص والمعالجة:**")
            response.extend(f"- {x}" for x in remedies)

    if results:
        response.append("\n### 📚 النتائج من الكتالوجات")
        for i, page in enumerate(results, 1):
            snippet = page["text"][:500].strip()
            response.append(
                f"\n**{i}. {page['filename']} — صفحة {page['page']}**\n> {snippet}"
            )
    else:
        response.append("\n### 📚 الكتالوجات\nلم يتم العثور على نتيجة مطابقة كافية.")

    page_image = None
    if results:
        page_image = render_pdf_page(results[0]["filepath"], results[0]["page"])

    if part_image:
        response.append(
            f"\n### 🧩 قطعة الغيار\nتم العثور على صورة: `{os.path.basename(part_image)}`"
        )
    elif recognized_part:
        response.append("\n⚠️ تم التعرف على كود مبدئيًا ولكن لم توجد صورة محفوظة له.")

    # Notify only for unresolved cases, not every normal search.
    if (not results and not troubleshooting) or (input_image is not None and not recognized_part):
        send_whatsapp_alert(
            "طلب مساعدة من مساعد الصيانة الذكي\n"
            f"الاستعلام: {query}\n"
            f"Alarm: {alarm_code or 'غير معروف'}"
        )

    return "\n".join(response), part_image, page_image


def initialize() -> None:
    global troubleshooting_kb
    logger.info("Initializing Aziza AI Maintenance Copilot")
    sync_data_from_gcs()
    troubleshooting_kb = load_alarm_database()
    build_manual_index()
    build_image_index()
    build_semantic_index()
    logger.info(
        "Ready: pages=%d, parts=%d, alarms=%d, semantic=%s",
        len(manual_pages), len(part_images_map), len(troubleshooting_kb),
        embedding_model is not None,
    )


initialize()

# ============================================================
# Gradio interface
# ============================================================

with gr.Blocks(title="Aziza AI Maintenance Copilot", theme=gr.themes.Soft()) as demo:
    gr.Markdown(
        """
# 🔧 المساعد الذكي للصيانة
### شركة دواجن فلسطين – مسلخ عزيزا

ابحث عن **رقم قطعة، Part Number، Alarm، أو وصف مشكلة**، أو ارفع صورة لقطعة الغيار.
"""
    )

    with gr.Row():
        with gr.Column(scale=2):
            query_box = gr.Textbox(
                label="رقم القطعة / Alarm / وصف العطل",
                placeholder="مثال: E002 أو D409-003-01 أو ماكينة الرياشة لا تعمل",
                lines=4,
            )
            image_input = gr.Image(label="صورة قطعة الغيار", type="pil")
            with gr.Row():
                search_btn = gr.Button("🔍 بحث", variant="primary")
                clear_btn = gr.Button("🧹 مسح")

        with gr.Column(scale=3):
            result_box = gr.Markdown(label="نتيجة البحث")

    with gr.Row():
        part_output = gr.Image(label="🧩 صورة قطعة الغيار", type="filepath")
        catalog_output = gr.Image(label="📖 صفحة الكتالوج", type="filepath")

    search_btn.click(
        maintenance_copilot,
        inputs=[query_box, image_input],
        outputs=[result_box, part_output, catalog_output],
    )
    query_box.submit(
        maintenance_copilot,
        inputs=[query_box, image_input],
        outputs=[result_box, part_output, catalog_output],
    )
    clear_btn.click(
        lambda: ("", None, "", None, None),
        outputs=[query_box, image_input, result_box, part_output, catalog_output],
    )

    gr.Markdown(
        """
---
**GitHub:** الكود فقط | **Google Cloud Storage:** الكتالوجات والصور وبيانات الأعطال

> البحث النصي والبحث الدلالي يعملان معًا عند توفر نموذج `sentence-transformers`.
> مطابقة الصور الحالية هي مطابقة بصرية خفيفة؛ يمكن تطويرها لاحقًا إلى نموذج رؤية متخصص.
"""
    )


if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=PORT, show_error=True)
