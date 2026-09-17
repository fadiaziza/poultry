import os
import glob
import re
import urllib.parse
import zipfile
import gdown
import fitz  # PyMuPDF
from PIL import Image
import streamlit as st

# ضبط إعدادات الصفحة
st.set_page_config(page_title="منصة الدعم الفني الهندسي ومطابقة الكتالوجات", layout="wide")

# -------------------------------------------------------------
# 1. تنزيل وفك ضغط الكتالوجات تلقائياً من Google Drive
# -------------------------------------------------------------
FILE_ID = "1jDTo_gaulygHfewtm49SFUy0cg3X-BNS"
ZIP_NAME = "manuals.zip"
FLAG_FILE = ".manuals_downloaded_v3"

if not os.path.exists(FLAG_FILE):
    try:
        url = f"https://drive.google.com/uc?id={FILE_ID}"
        gdown.download(url, ZIP_NAME, quiet=False)
        if os.path.exists(ZIP_NAME):
            with zipfile.ZipFile(ZIP_NAME, 'r') as zip_ref:
                zip_ref.extractall(".")
            os.remove(ZIP_NAME)
            with open(FLAG_FILE, "w") as f:
                f.write("done")
    except Exception as e:
        print(f"خطأ أثناء تنزيل الملفات: {e}")

# عرض الشعار إن وُجد
if os.path.exists("logo.png"):
    st.image("logo.png", width=140)

st.title("🛠️ منصة الدعم الفني الهندسي ومطابقة الكتالوجات")
st.markdown("فحص القطع ومطابقتها الآلية مع كتالوجات ومخططات الصيانة المعتمدة لخطوط المسلخ.")

# -------------------------------------------------------------
# 2. دوال المعالجة والبحث الخفيفة
# -------------------------------------------------------------
def clean_code(text):
    return re.sub(r'[^a-zA-Z0-9]', '', str(text)).lower()

@st.cache_resource(show_spinner="جاري قراءة وفهرسة كافة الكتالوجات الهندسية...")
def build_lightweight_index():
    pdf_files = glob.glob("*.pdf") + glob.glob("**/*.pdf", recursive=True)
    catalog_data = []

    for pdf_path in pdf_files:
        machine_name = os.path.basename(pdf_path).replace(".pdf", "")
        try:
            doc = fitz.open(pdf_path)
            for page_num in range(len(doc)):
                page = doc[page_num]
                raw_text = page.get_text("text")
                if raw_text.strip():
                    catalog_data.append({
                        "file_path": pdf_path,
                        "machine": machine_name,
                        "page_num": page_num,
                        "raw_text": raw_text,
                        "clean_text": clean_code(raw_text)
                    })
            doc.close()
        except Exception:
            continue
    return catalog_data

catalog_index = build_lightweight_index()

def get_page_snapshot(pdf_path, page_num):
    try:
        doc = fitz.open(pdf_path)
        if 0 <= page_num < len(doc):
            page = doc[page_num]
            pix = page.get_pixmap(dpi=150)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            doc.close()
            return img
        doc.close()
        return None
    except Exception as e:
        st.error(f"خطأ أثناء استخراج الرسم: {e}")
        return None

def find_real_part_image(target_clean):
    image_extensions = ('*.jpg', '*.jpeg', '*.png', '*.webp')
    for ext in image_extensions:
        for img_path in glob.glob(f"**/{ext}", recursive=True):
            clean_name = clean_code(os.path.splitext(os.path.basename(img_path))[0])
            if target_clean and (target_clean in clean_name or clean_name in target_clean):
                return img_path
    return None

# -------------------------------------------------------------
# 3. واجهة المستخدم والتفاعل
# -------------------------------------------------------------
col1, col2 = st.columns([1, 1])

with col1:
    st.subheader("🔍 إدخال بيانات القطعة")
    uploaded_file = st.file_uploader("📷 ارفع صورة القطعة أو لوحة البيانات (Nameplate):", type=["jpg", "jpeg", "png"])
    
    inferred_code = ""
    if uploaded_file:
        st.image(uploaded_file, caption="صورة القطعة المرفوعة", width=250)
        base_name = os.path.splitext(uploaded_file.name)[0]
        match = re.search(r'[0-9a-zA-Z]+[0-9a-zA-Z\.\-_]*', base_name)
        if match:
            inferred_code = match.group(0)

    part_query = st.text_input("📝 رقم القطعة (يُستخرج تلقائياً من الصورة أو اكتبه هنا):", value=inferred_code)
    
    # حقل اختياري لتحديد رقم واتساب المستلم (المهندس أو مسؤول المشتريات)
    whatsapp_target = st.text_input("📱 رقم هاتف الواتساب للمستلم (مع المقدمة الدولية، مثال: 970599431267):", value="")
    
    search_button = st.button("🚀 فحص ومطابقة بالكتالوجات", type="primary", use_container_width=True)

with col2:
    st.subheader("📋 نتيجة المطابقة والرسم الهندسي")
    
    if search_button:
        target_raw = part_query.strip()
        target_clean = clean_code(target_raw)

        if not target_clean:
            st.warning("⚠️ الرجاء رفع صورة واضحة أو إدخال رقم القطعة للبدء بالفحص.")
        else:
            with st.spinner("جاري فحص الكتالوجات والمخططات الهندسية..."):
                matches = [
                    item for item in catalog_index 
                    if (target_clean in item["clean_text"]) or (target_raw.lower() in item["raw_text"].lower())
                ]

                if matches:
                    st.success(f"✅ تم العثور على {len(matches)} مطابقة للقطعة: `{target_raw}`")
                    top_match = matches[0]

                    st.markdown(f"**🏭 اسم الماكينة:** `{top_match['machine']}`")
                    st.markdown(f"**📄 صفحة الجدول في الكتالوج:** `الصفحة {top_match['page_num'] + 1}`")
                    st.markdown(f"**📁 اسم ملف الكتالوج:** `{os.path.basename(top_match['file_path'])}`")

                    # زر إرسال تفاصيل المطابقة إلى الواتساب
                    msg_text = (
                        f"طلب قطعة غيار / مطابقة فنية:\n"
                        f"الماكينة: {top_match['machine']}\n"
                        f"رقم القطعة: {target_raw}\n"
                        f"الكتالوج: {os.path.basename(top_match['file_path'])}\n"
                        f"رقم الصفحة: {top_match['page_num'] + 1}"
                    )
                    encoded_msg = urllib.parse.quote(msg_text)
                    phone_clean = re.sub(r'[^0-9]', '', whatsapp_target)
                    wa_url = f"https://wa.me/{phone_clean}?text={encoded_msg}" if phone_clean else f"https://wa.me/?text={encoded_msg}"
                    
                    st.markdown(
                        f'''<a href="{wa_url}" target="_blank" style="text-decoration:none;">
                            <button style="background-color:#25D366; color:white; border:none; padding:10px 20px; font-size:16px; border-radius:8px; cursor:pointer; width:100%; margin-top:10px; font-weight:bold;">
                                📲 إرسال بيانات القطعة عبر WhatsApp
                            </button>
                        </a>''',
                        unsafe_allow_html=True
                    )

                    # 1. عرض الصورة الحقيقية إن وُجدت
                    real_img_path = find_real_part_image(target_clean)
                    if real_img_path:
                        st.markdown("#### 📸 الصورة الحقيقية المعتمدة للقطعة:")
                        st.image(real_img_path, caption=f"القطعة: {os.path.basename(real_img_path)}", width=300)

                    # 2. عرض المخطط التفكيكي الهندسي
                    st.markdown("#### 📐 المخطط التفكيكي وجدول أرقام الأجزاء:")
                    prev_page_num = top_match['page_num'] - 1 if top_match['page_num'] > 0 else 0
                    
                    col_draw, col_tbl = st.columns(2)
                    with col_draw:
                        draw_img = get_page_snapshot(top_match["file_path"], prev_page_num)
                        if draw_img:
                            st.image(draw_img, caption=f"رسم المخطط التفكيكي (صفحة {prev_page_num + 1})", use_container_width=True)
                    
                    with col_tbl:
                        table_img = get_page_snapshot(top_match["file_path"], top_match["page_num"])
                        if table_img:
                            st.image(table_img, caption=f"جدول القطع والأرقام (صفحة {top_match['page_num'] + 1})", use_container_width=True)

                    if len(matches) > 1:
                        st.info("ℹ️ صفحات أو ماكينات أخرى ذات صلة:")
                        for other in matches[1:5]:
                            st.write(f"- ماكينة: **{other['machine']}** (صفحة {other['page_num'] + 1})")
                else:
                    st.error(f"❌ لم يتم العثور على تطابق للرمز: `{target_raw}` داخل الكتالوجات المتوفرة.")
