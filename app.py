import os
import glob
import fitz  # PyMuPDF
from PIL import Image
import streamlit as st

# ضبط إعدادات الصفحة
st.set_page_config(page_title="منصة الدعم الفني الهندسي", layout="wide")

# عرض الشعار إن وُجد
if os.path.exists("logo.png"):
    st.image("logo.png", width=140)

st.title("🛠️ منصة الدعم الفني الهندسي ومطابقة الكتالوجات")
st.markdown("فحص القطع ومطابقتها الآلية مع كتالوجات الصيانة المعتمدة لخطوط المسلخ.")

# -------------------------------------------------------------
# 1. فهرسة سريعة وخفيفة جداً للكتالوجات (في الذاكرة المؤقتة)
# -------------------------------------------------------------
@st.cache_resource(show_spinner="جاري تجهيز فهرس الكتالوجات الهندسية...")
def build_lightweight_index():
    """فهرسة النصوص وأرقام القطع فقط بدون استهلاك الـ RAM"""
    pdf_files = glob.glob("*.pdf") + glob.glob("**/*.pdf", recursive=True)
    catalog_data = []

    for pdf_path in pdf_files:
        machine_name = os.path.basename(pdf_path).replace(".pdf", "")
        try:
            doc = fitz.open(pdf_path)
            for page_num in range(len(doc)):
                page = doc[page_num]
                text = page.get_text("text")
                if text.strip():
                    catalog_data.append({
                        "file_path": pdf_path,
                        "machine": machine_name,
                        "page_num": page_num,
                        "text": text.lower()
                    })
            doc.close()
        except Exception as e:
            print(f"تخطي الملف {pdf_path}: {e}")

    return catalog_data

catalog_index = build_lightweight_index()

# -------------------------------------------------------------
# 2. دالة استخراج صورة الصفحة عند الطلب فقط (توفير الذاكرة)
# -------------------------------------------------------------
def get_page_snapshot(pdf_path, page_num):
    """تحويل صفحة معينة فقط لصورة عند الحاجة"""
    try:
        doc = fitz.open(pdf_path)
        page = doc[page_num]
        pix = page.get_pixmap(dpi=150)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        doc.close()
        return img
    except Exception as e:
        st.error(f"خطأ أثناء استخراج الرسم الهندسي: {e}")
        return None

# -------------------------------------------------------------
# 3. واجهة الاستخدام
# -------------------------------------------------------------
col1, col2 = st.columns([1, 1])

with col1:
    st.subheader("🔍 إدخال بيانات القطعة")
    uploaded_file = st.file_uploader("📷 ارفع صورة القطعة أو لوحة البيانات (Nameplate):", type=["jpg", "jpeg", "png"])
    
    if uploaded_file:
        st.image(uploaded_file, caption="صورة القطعة المرفوعة", width=250)

    part_query = st.text_input("📝 أدخل رقم القطعة أو اسمها (مثال: 0409.003 أو Bearing):")
    search_button = st.button("🚀 فحص ومطابقة بالكتالوجات", type="primary", use_container_width=True)

with col2:
    st.subheader("📋 نتيجة المطابقة والرسم الهندسي")
    
    if search_button:
        query_cleaned = part_query.strip().lower() if part_query else ""
        
        if not query_cleaned:
            st.warning("⚠️ الرجاء كتابة رقم القطعة أو رمزها للبدء بعملية الفحص.")
        else:
            with st.spinner("جاري فحص الكتالوجات واستخراج المخطط..."):
                matches = []
                for item in catalog_index:
                    if query_cleaned in item["text"]:
                        matches.append(item)

                if matches:
                    st.success(f"✅ تم العثور على {len(matches)} مطابقة في سجلات الصيانة!")
                    
                    # عرض أول وأدق نتيجة مطابقة
                    top_match = matches[0]
                    st.markdown(f"**🏭 اسم الماكينة:** `{top_match['machine']}`")
                    st.markdown(f"**📄 رقم الصفحة في الكتالوج:** `الصفحة {top_match['page_num'] + 1}`")
                    st.markdown(f"**📁 اسم الملف:** `{os.path.basename(top_match['file_path'])}`")

                    # استخراج صورة الصفحة المحددة فوراً
                    page_img = get_page_snapshot(top_match["file_path"], top_match["page_num"])
                    if page_img:
                        st.image(page_img, caption=f"المخطط الهندسي - {top_match['machine']} (صفحة {top_match['page_num'] + 1})", use_container_width=True)
                    
                    # إذا وُجدت صفحات أخرى بها نفس الرقم
                    if len(matches) > 1:
                        st.info("ℹ️ توجد مطابقة في صفحات أو ماكينات أخرى:")
                        for other in matches[1:4]:
                            st.write(f"- ماكينة: **{other['machine']}** (صفحة {other['page_num'] + 1})")
                else:
                    st.error(f"❌ لم يتم العثور على تطابق للرمز: `{part_query}` في الكتالوجات المتوفرة.")
