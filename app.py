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

# ضبط إعدادات واجهة الصفحة
st.set_page_config(page_title="منصة الدعم الهندسي - مسلخ عزيزا", layout="wide")

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
    """إرسال تنبيه صامت ومباشر إلى حسابك على الواتساب عند كل فحص"""
    try:
        url = f"https://7107.api.greenapi.com/waInstance{ID_INSTANCE}/sendMessage/{API_TOKEN}"
        local_tz = pytz.timezone("Asia/Gaza")
        now_str = datetime.now(local_tz).strftime("%I:%M %p")
        payload = {
            "chatId": f"{MY_PHONE}@c.us",
            "message": (
                f"🏭 *مسلخ شركة دواجن فلسطين - فحص فني*\n"
                f"⏰ الوقت: {now_str}\n"
                f"🔍 المدخل / الاستفسار: {query_text}\n"
                f"📋 النتيجة: {info_summary}"
            )
        }
        requests.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=4)
    except Exception as e:
        print(f"⚠️ تعذر إرسال الواتساب: {e}")

# ==========================================
# 3. محرك الرؤية البصرية السريع (CLIP)
# ==========================================
@st.cache_resource(show_spinner="جاري تحميل محرك الرؤية البصرية...")
def load_visual_engine():
    torch.set_num_threads(1)
    model = SentenceTransformer('clip-ViT-B-32', device='cpu')
    return model

visual_model = load_visual_engine()

@st.cache_resource(show_spinner="جاري تجهيز فهرس الصور السريع...")
def build_visual_database():
    image_paths = []
    valid_exts = ("*.png", "*.jpg", "*.jpeg", "*.webp")
    all_imgs = []
    
    # حصر البحث في مجلد الصور لتجنب إجهاد السيرفر
    for ext in valid_exts:
        all_imgs.extend(glob.glob(f"**/Real_Parts_Images/**/{ext}", recursive=True))
        all_imgs.extend(glob.glob(f"**/Images/**/{ext}", recursive=True))
    
    if not all_imgs:
        for ext in valid_exts:
            all_imgs.extend(glob.glob(f"*{ext}"))
            all_imgs.extend(glob.glob(f"*/*{ext}"))

    # استبعاد الشعار
    all_imgs = [p for p in all_imgs if "logo" not in os.path.basename(p).lower()]

    if not all_imgs:
        return None, []

    valid_pil = []
    valid_paths = []
    for path in all_imgs[:150]:
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
    """مقارنة صورة الفني بصور المستودع المخزنة"""
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
# 4. محرك البحث الهجين (قطع غيار + أعطال وإنذارات وتغليف)
# ==========================================
def search_part_in_manuals(raw_input, target_category="الكل"):
    query = raw_input.strip()
    if not query:
        return []

    # التمييز بين البحث عن رقم قطعة أو استكشاف عطل / إنذار
    is_part_number = bool(re.search(r'\d{3,}', query))
    keywords = [k.lower() for k in re.split(r'\s+', query) if len(k) >= 2]
    
    all_pdfs = glob.glob("**/*.pdf", recursive=True)
    matches = []

    for pdf_path in all_pdfs:
        doc_name = os.path.basename(pdf_path)
        path_lower = pdf_path.lower()
        
        # تصفية ماكينات التغليف والأقسام
        is_packaging_doc = any(k in path_lower for k in ["automac", "pack", "wrap", "tray", "fabbri"])
        if target_category == "ماكينات التغليف (Automac / Packaging)" and not is_packaging_doc:
            continue
        elif target_category == "ماكينات الذبح والتجهيز (Meyn)" and is_packaging_doc:
            continue

        try:
            doc = fitz.open(pdf_path)
            for page_num in range(len(doc)):
                text = doc[page_num].get_text()
                text_lower = text.lower()
                
                matched = False
                snippet = []

                if is_part_number:
                    clean_q = re.sub(r'[^a-zA-Z0-9]', '', query).lower()
                    clean_page = re.sub(r'[^a-zA-Z0-9]', '', text).lower()
                    if clean_q in clean_page or query.lower() in text_lower:
                        matched = True
                else:
                    if any(kw in text_lower for kw in keywords):
                        matched = True

                if matched:
                    lines = [l.strip() for l in text.split("\n") if l.strip()]
                    for i, line in enumerate(lines):
                        if any(kw in line.lower() for kw in keywords) or query.lower() in line.lower():
                            snippet = lines[max(0, i - 1):min(len(lines), i + 5)]
                            break
                    
                    matches.append({
                        "file_path": pdf_path,
                        "doc_name": doc_name,
                        "machine_name": doc_name.replace(".pdf", ""),
                        "page": page_num + 1,
                        "details": " | ".join(snippet) if snippet else "مطابقة مسجلة في الدليل الفني."
                    })
                    if len(matches) >= 5:
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
# 5. واجهة التطبيق الرسمية
# ==========================================
st.markdown("""
<div style="background-color: #1a365d; padding: 15px; border-radius: 10px; margin-bottom: 20px; color: white; text-align: center;">
    <h2 style="margin:0;">منصة الدعم الهندسي - مسلخ شركة دواجن فلسطين</h2>
    <p style="margin:5px 0 0 0; font-size: 14px; opacity: 0.9;">نظام الرؤية البصرية، تشخيص الأعطال، ومطابقة الكتالوجات (Meyn & Automac Packaging)</p>
</div>
""", unsafe_allow_html=True)

col_left, col_right = st.columns([1, 1])

with col_left:
    st.subheader("⚙️ إدخال الاستفسار الفني أو الصورة")
    category_option = st.selectbox(
        "📂 تحديد الخط أو الماكينة المستهدفة:",
        ["الكل", "ماكينات التغليف (Automac / Packaging)", "ماكينات الذبح والتجهيز (Meyn)"]
    )
    
    uploaded_file = st.file_uploader("📷 ارفع صورة القطعة / لوحة البيانات (Nameplate):", type=["jpg", "jpeg", "png", "webp"])
    text_input = st.text_input("📝 أدخل رقم القطعة أو وصف العطل / الإنذار:", placeholder="مثال: D409.003 أو عطل سحب الفيلم أو E04")
    search_btn = st.button("🚀 بدء الفحص والتشخيص الهندسي", type="primary", use_container_width=True)

with col_right:
    st.subheader("📋 نتيجة التشخيص والكتالوج المعتمد")
    
    if search_btn:
        detected_code = ""
        matched_image_path = None
        sim_score = 0.0

        if uploaded_file is not None:
            pil_img = Image.open(uploaded_file)
            st.image(pil_img, caption="الصورة المرفوعة للفحص", width=220)
            
            with st.spinner("🧠 جاري فحص البصمة البصرية ومطابقة شكل القطعة..."):
                matched_image_path, sim_score = find_part_by_image(pil_img)
                
            if matched_image_path:
                detected_code = os.path.splitext(os.path.basename(matched_image_path))[0]
                st.success(f"🎯 تم التعرف على شكل القطعة بنسبة تطابق: `{int(sim_score * 100)}%`")
                st.info(f"🏷️ رقم القطعة المكتشف: `{detected_code}`")
            else:
                st.warning(f"⚠️ لم يتم العثور على تطابق شكلي قوي في المستودع ({int(sim_score * 100)}%). سيتم الاعتماد على النص.")

        final_query = text_input.strip() if text_input.strip() else detected_code

        if not final_query:
            st.error("الرجاء رفع صورة واضحة للقطعة أو كتابة رقمها/وصف العطل في مربع البحث.")
        else:
            with st.spinner("📖 جاري فحص الكتالوجات الفنية وأدلة الأعطال..."):
                records = search_part_in_manuals(final_query, category_option)
                
                # إرسال التنبيه التلقائي المباشر للواتساب
                res_summary = f"ماكينة: {records[0]['machine_name']} - ص {records[0]['page']}" if records else "لم يُعثر على تطابق في الكتالوج"
                send_whatsapp_alert(final_query, res_summary)

                if records:
                    top = records[0]
                    st.success(f"✅ تم العثور على تطابق في سجلات الصيانة!")
                    st.write(f"🏭 **الماكينة:** `{top['machine_name']}`")
                    st.write(f"📄 **الصفحة داخل الدليل:** صفحة **{top['page']}**")
                    st.write(f"⚙️ **التفاصيل الفنية المكتشفة:** `{top['details']}`")

                    # عرض صورة القطعة وصورة صفحة الكتالوج
                    col_img1, col_img2 = st.columns(2)
                    with col_img1:
                        if matched_image_path:
                            st.image(Image.open(matched_image_path), caption="صورة القطعة في المستودع", use_container_width=True)
                    with col_img2:
                        page_snapshot = get_page_snapshot(top["file_path"], top["page"] - 1)
                        if page_snapshot:
                            st.image(page_snapshot, caption=f"المخطط الهندسي / صفحة الدليل ({top['page']})", use_container_width=True)
                    
                    if len(records) > 1:
                        st.info("ℹ️ صفحات أو ماكينات أخرى ورد فيها نفس البند:")
                        for other in records[1:4]:
                            st.write(f"- `{other['machine_name']}` (صفحة {other['page']})")
                else:
                    st.error(f"❌ لم يتم العثور على نتائج للبحث `{final_query}` داخل الكتالوجات المحددة.")

st.markdown("""
<hr style="margin-top:40px;">
<p style="text-align:center; color:#718096; font-size:13px;">
⚙️ تم تطوير المنصة بواسطة <strong>م. فادي محمود</strong> | قسم الصيانة والدعم الهندسي — مسلخ شركة دواجن فلسطين
</p>
""", unsafe_allow_html=True)
