Enter# ─────────────────────────────────────────
# Dockerfile
# ─────────────────────────────────────────
FROM python:3.11-slim

# منع Python من كتابة .pyc وتفعيل الـ output الفوري
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# مجلد العمل
WORKDIR /app

# نسخ الملفات
COPY requirements.txt .
COPY bot.py .

# تثبيت المكتبات
RUN pip install --no-cache-dir -r requirements.txt

# تشغيل البوت
CMD ["python", "bot.py"]
