import os
import glob
import re
import zipfile
from datetime import datetime
import pytz
import requests
import fitz  # PyMuPDF
from PIL import Image
import torch
from sentence_transformers import SentenceTransformer, util
import gdown
import streamlit as st

# ضبط واجهة الصفحة
st.set_page_config(page_title="منصة الدعم الهندسي - مسلخ عزيزا", layout="wide")

# ==========================================
# ==========================================
# 1. تنزيل وفك ضغط الكتالوجات تلقائياً
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FILE_ID = "1jDTo_gaulygHfewtm49SFUy0cg3X-BNS"
ZIP_PATH = os.path.join(BASE_DIR, "manuals.zip")
FLAG_FILE = os.path.join(BASE_DIR, ".manuals_downloaded_v3")

if not os.path.exists(FLAG_FILE):
    try:
        url = f"https://drive.google.com/uc?id={FILE_ID}"
        gdown.download(url, ZIP_PATH, quiet=False)
        if os.path.exists(ZIP_PATH):
            with zipfile.ZipFile(ZIP_PATH, 'r') as zip_ref:
                zip_ref.extractall(BASE_DIR)
            if os.path.exists(ZIP_PATH):
                os.remove(ZIP_PATH)
            with open(FLAG_FILE, "w") as f:
                f.write("done")
    except Exception as e:
        print(f"Download Error: {e}")

# ==========================================
# 2. إعدادات Green-API لتنبيهات الواتساب التلقائية
# ==========================================
ID_INSTANCE = "710722737613"
API_TOKEN = "8902219901b2411cb1ebfa944bbfc3d7d499d671111c4fe18e"
MY_PHONE = "970599431267"

def send_whatsapp_alert(query_text, info_summary):
    """إرسال تنبيه صامت ومباشر إلى هاتفك عند كل فحص"""
    try:
        url = f"https://7107.api.greenapi.com/waInstance{ID_INSTANCE}/sendMessage/{API_TOKEN}"
        local_tz = pytz.timezone("Asia/Gaza")
        now_str = datetime.now(local_tz).strftime("%I:%M %p")
        payload = {
            "chatId": f"{MY_PHONE}@c.us",
            "message": (
                f"🏭 *مسلخ شركة دواجن فلسطين - فحص قطعة*\n"
                f"⏰ الوقت: {now_str}\n"
                f"🔍 الاستفسار: {query_text}\n"
                f"📋 النتيجة: {info_summary}"
            )
        }
        requests.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=4)
    except Exception as e:
        print(f"⚠️ تعذر إرسال الواتساب: {e}")

# ==========================================
# 3. نموذج الذكاء الاصطناعي وقاعدة البصمات البصرية
# ==========================================
@st.cache_resource(show_spinner="جاري تحميل نموذج الرؤية البصرية...")
def load_visual_engine():
    # ضبط خيوط المعالجة لمنع تجاوز استهلاك السيرفر
    torch.set_num_threads(1)
    model = SentenceTransformer('clip-ViT-B-32', device='cpu')
    return model

visual_model = load_visual_engine()

@st.cache_resource(show_spinner="جاري تجهيز فهرس الصور السريع...")
def build_visual_database():
    image_paths = []
    
    # تحديد امتدادات الصور
    valid_exts = ("*.png", "*.jpg", "*.jpeg", "*.webp")
    all_imgs = []
    
    # البحث المباشر في مجلد الصور الحقيقية لتجنب فحص السيرفر بالكامل
    for ext in valid_exts:
        all_imgs.extend(glob.glob(f"**/Real_Parts_Images/**/{ext}", recursive=True))
        all_imgs.extend(glob.glob(f"**/Images/**/{ext}", recursive=True))
    
    # إذا لم يجد داخل المجلد المحدد، يبحث في المجلدات القريبة فقط
    if not all_imgs:
        for ext in valid_exts:
            all_imgs.extend(glob.glob(f"*{ext}"))
            all_imgs.extend(glob.glob(f"*/*{ext}"))

    # استبعاد الشعار
    all_imgs = [p for p in all_imgs if "logo" not in os.path.basename(p).lower()]

    if not all_imgs:
        return None, []

    # معالجة الصور دفعة واحدة سريعة (Batching) بدلاً من صورة صورة
    valid_pil = []
    valid_paths = []
    for path in all_imgs[:150]:  # فحص أول 150 صورة كحد أقصى لمنع تعليق السيرفر
        try:
            img = Image.open(path).convert('RGB')
            valid_pil.append(img)
            valid_paths.append(path)
        except Exception:
            continue

    if valid_pil:
        with torch.no_grad():
            embeddings = visual_model.encode(valid_pil, convert_to_tensor=True, batch_size=32, show_progress_bar=False)
        return embeddings, valid_paths
    
    return None, []

image_index, image_paths = build_visual_database()

def find_part_by_image(uploaded_pil):
    """مقارنة صورة الفني مع صور المستودع"""
    if image_index is None or len(image_paths) == 0:
        return None, 0.0

    uploaded_rgb = uploaded_pil.convert('RGB')
    query_emb = visual_model.encode(uploaded_rgb, convert_to_tensor=True)
    cos_scores = util.cos_sim(query_emb, image_index)[0]
    best_idx = torch.argmax(cos_scores).item()
    best_score = float(cos_scores[best_idx])

    if best_score > 0.65:
        return image_paths[best_idx], best_score
    return None, best_score

# ==========================================
# 4. محرك البحث الهجين داخل الكتالوجات (Meyn / Automac)
# ==========================================
def search_part_in_manuals(raw_input, target_category="الكل"):
    clean_input = raw_input.strip()
    core_match = re.search(r'(\d{3,4}[\.\-_]\d{3,4}[\.\-_]\d{2,4})', clean_input)
    core_number = core_match.group(1) if core_match else clean_input

    search_targets = {clean_input, core_number, f"D{core_number}", f"C{core_number}", core_number.replace(".", "")}
    valid_targets = [re.escape(t) for t in search_targets if len(t) >= 4]
    if not valid_targets:
        valid_targets = [re.escape(clean_input)]
    search_regex = re.compile("|".join(valid_targets), re.IGNORECASE)

    all_pdfs = glob.glob("**/*.pdf", recursive=True)
    matches = []

    for pdf_path in all_pdfs:
        doc_name = os.path.basename(pdf_path)
        
        # تصنيف ماكينات التغليف والأقسام
        if target_category == "ماكينات التغليف (Automac / Packaging)":
            if not any(k in pdf_path.lower() for k in ["automac", "pack", "wrap", "tray"]):
                continue
        elif target_category == "ماكينات الذبح والتجهيز (Meyn)":
            if any(k in pdf_path.lower() for k in ["automac", "wrap"]):
                continue

        try:
            doc = fitz.open(pdf_path)
            for page_num in range(len(doc)):
                text = doc[page_num].get_text()
                if search_regex.search(text):
                    lines = [l.strip() for l in text.split("\n") if l.strip()]
                    snippet = []
                    for i, line in enumerate(lines):
                        if search_regex.search(line):
                            snippet = lines[max(0, i - 1):min(len(lines), i + 4)]
                            break
                    matches.append({
                        "file_path": pdf_path,
                        "doc_name": doc_name,
                        "machine_name": doc_name.replace(".pdf", ""),
                        "page": page_num + 1,
                        "details": " | ".join(snippet) if snippet else "مطابقة مسجلة في جدول الكتالوج."
                    })
                    break
            doc.close()
        except Exception:
            continue
    return matches

def get_page_snapshot(pdf_path, page_num):
    try:
        doc = fitz.open(pdf_path)
        if 0 <= page_num < len(doc):
            pix = doc[page_num].get_pixmap(dpi=150)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            doc.close()
            return img
        doc.close()
        return None
    except Exception:
        return None

# ==========================================
# 5. واجهة المستخدم الرسمية
# ==========================================
st.markdown("""
<div style="background-color: #1a365d; padding: 15px; border-radius: 10px; margin-bottom: 20px; color: white; text-align: center;">
    <h2 style="margin:0;">منصة الدعم الهندسي - مسلخ شركة دواجن فلسطين</h2>
    <p style="margin:5px 0 0 0; font-size: 14px; opacity: 0.9;">نظام الرؤية البصرية ومطابقة الكتالوجات (Meyn & Automac Packaging)</p>
</div>
""", unsafe_allow_html=True)

col_left, col_right = st.columns([1, 1])

with col_left:
    st.subheader("📷 فحص القطعة أو لوحة البيانات")
    category_option = st.selectbox(
        "📂 نطاق الفحص والماكينة:",
        ["الكل", "ماكينات التغليف (Automac / Packaging)", "ماكينات الذبح والتجهيز (Meyn)"]
    )
    
    uploaded_file = st.file_uploader("التقط صورة للقطعة أو ارفعها من الهاتف:", type=["jpg", "jpeg", "png", "webp"])
    text_input = st.text_input("📝 أو أدخل رقم القطعة مباشرة (اختياري):", placeholder="مثال: 0000.D475.000.94 أو اسم القطعة")
    search_btn = st.button("🔍 بدء الفحص والمطابقة الهندسية", type="primary", use_container_width=True)

with col_right:
    st.subheader("📋 التقرير الفني والمطابقة")
    
    if search_btn:
        detected_code = ""
        matched_image_path = None
        sim_score = 0.0

        # أ. التعرف البصري إذا تم رفع صورة
        if uploaded_file is not None:
            pil_img = Image.open(uploaded_file)
            st.image(pil_img, caption="الصورة المدخلة للفحص", width=220)
            
            with st.spinner("🧠 جاري مطابقة الشكل مع بصمات صور المستودع..."):
                matched_image_path, sim_score = find_part_by_image(pil_img)
                
            if matched_image_path:
                detected_code = os.path.splitext(os.path.basename(matched_image_path))[0]
                st.success(f"🎯 تم التعرف على القطعة بنسبة تطابق: `{int(sim_score * 100)}%`")
                st.info(f"🏷️ رقم القطعة المصنعي: `{detected_code}`")
            else:
                st.warning(f"⚠️ نسبة التطابق البصري منخفضة ({int(sim_score * 100)}%). سيتم استخدام النص إن وُجد.")

        # ب. تحديد الرمز النهائي المعتمد للبحث
        final_query = text_input.strip() if text_input.strip() else detected_code

        if not final_query:
            st.error("الرجاء التقاط صورة واضحة للقطعة أو كتابة رقمها للبحث.")
        else:
            with st.spinner("📖 جاري البحث في الكتالوجات والمخططات الهندسية..."):
                records = search_part_in_manuals(final_query, category_option)
                
                # إرسال تنبيه فوري لحسابك على الواتساب
                res_summary = f"ماكينة: {records[0]['machine_name']} - ص {records[0]['page']}" if records else "لم يُعثر على كتالوج مطابق"
                send_whatsapp_alert(final_query, res_summary)

                if records:
                    top = records[0]
                    st.write(f"🏭 **الماكينة:** `{top['machine_name']}`")
                    st.write(f"📄 **الصفحة داخل الدليل:** صفحة **{top['page']}**")
                    st.write(f"⚙️ **المواصفات المسجلة:** `{top['details']}`")

                    # عرض صورة المستودع وصورة صفحة الكتالوج
                    col_img1, col_img2 = st.columns(2)
                    with col_img1:
                        if matched_image_path:
                            st.image(Image.open(matched_image_path), caption="صورة القطعة في المستودع", use_container_width=True)
                    with col_img2:
                        page_snapshot = get_page_snapshot(top["file_path"], top["page"] - 1)
                        if page_snapshot:
                            st.image(page_snapshot, caption=f"المخطط الهندسي (صفحة {top['page']})", use_container_width=True)
                else:
                    st.error(f"❌ لم يتم العثور على أرقام مطابقة للرمز `{final_query}` داخل الكتالوجات المحددة.")

st.markdown("""
<hr style="margin-top:40px;">
<p style="text-align:center; color:#718096; font-size:13px;">
⚙️ تم تطوير المنصة بواسطة <strong>م. فادي محمود</strong> | قسم الصيانة والدعم الهندسي — مسلخ شركة دواجن فلسطين
</p>
""", unsafe_allow_html=True)
