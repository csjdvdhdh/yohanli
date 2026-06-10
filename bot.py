import os
import logging
import random
import json
import base64
import subprocess
import sys
import asyncio
import psutil
from io import BytesIO
from datetime import datetime

import google.auth
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from google.cloud import run_v2

# ─────────────────────────────────────────
#  إعدادات البوت
# ─────────────────────────────────────────
TELEGRAM_BOT_TOKEN = '8037680515:AAFwYaQ_b0DjxCiWNtnljLfFpeLlDQcDfbg'
IMAGE_URI           = 'docker.io/seifszx/seifszx'
ADMIN_ID            = 8650707600        # ← ضع ID التيليغرام الخاص بك
TOTAL_RAM_GB        = 2.0              # إجمالي الرام المخصص (GB)
RAM_PER_USER_GB     = 0.2             # حصة كل مستخدم (GB) — عدّلها كما تريد

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ─────────────────────────────────────────
#  قاعدة بيانات المستخدمين (في الذاكرة)
#  { user_id: { "config": "vless://...", "created_at": "..." } }
# ─────────────────────────────────────────
users_db: dict = {}

# ─────────────────────────────────────────
#  GCP Project
# ─────────────────────────────────────────
try:
    _, GCP_PROJECT_ID = google.auth.default()
    logging.info(f"تم اكتشاف معرف المشروع: {GCP_PROJECT_ID}")
except Exception:
    GCP_PROJECT_ID = None
    logging.error("لم يتم العثور على مشروع نشط.")


# ─────────────────────────────────────────
#  إعداد البيئة تلقائياً
# ─────────────────────────────────────────
def setup_environment():
    print("🔧 جاري تثبيت المكتبات...")
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install",
             "python-telegram-bot", "google-cloud-run", "psutil", "--user", "-q"],
            check=True
        )
        print("✅ تم تثبيت المكتبات.")
    except subprocess.CalledProcessError as e:
        print(f"❌ فشل التثبيت: {e}")

    print("🔧 جاري تفعيل Cloud Run API...")
    try:
        subprocess.run(
            ["gcloud", "services", "enable", "run.googleapis.com"],
            check=True
        )
        print("✅ تم تفعيل API.")
    except subprocess.CalledProcessError as e:
        print(f"❌ فشل تفعيل API: {e}")
    except FileNotFoundError:
        print("❌ gcloud غير موجود.")


# ─────────────────────────────────────────
#  مهمة إرسال إحصائيات RAM كل 15 دقيقة
# ─────────────────────────────────────────
async def ram_stats_job(app: Application):
    while True:
        await asyncio.sleep(15 * 60)  # 15 دقيقة

        mem        = psutil.virtual_memory()
        used_gb    = mem.used  / (1024 ** 3)
        total_gb   = mem.total / (1024 ** 3)
        percent    = mem.percent
        user_count = len(users_db)

        # ── رسالة للمسؤول ──
        admin_msg = (
            f"📊 *إحصائيات RAM — {datetime.now().strftime('%H:%M')}*\n\n"
            f"🖥 الإجمالي : `{total_gb:.2f} GB`\n"
            f"🔴 المستخدم : `{used_gb:.2f} GB` ({percent}%)\n"
            f"🟢 المتاح   : `{total_gb - used_gb:.2f} GB`\n"
            f"👤 عدد المستخدمين النشطين: `{user_count}`"
        )
        try:
            await app.bot.send_message(ADMIN_ID, admin_msg, parse_mode="Markdown")
        except Exception as e:
            logging.warning(f"فشل إرسال إحصائيات للمسؤول: {e}")

        # ── رسالة لكل مستخدم بحصته المتبقية ──
        for uid in list(users_db.keys()):
            remaining = max(0.0, RAM_PER_USER_GB - (used_gb / max(user_count, 1)))
            user_msg = (
                f"📡 *تحديث حصتك من الرام*\n\n"
                f"💾 حصتك الكلية : `{RAM_PER_USER_GB:.2f} GB`\n"
                f"📉 المتبقي تقريباً: `{remaining:.2f} GB`\n"
                f"⏱ التحديث كل 15 دقيقة"
            )
            try:
                await app.bot.send_message(uid, user_msg, parse_mode="Markdown")
            except Exception as e:
                logging.warning(f"فشل إرسال إحصائيات للمستخدم {uid}: {e}")


# ─────────────────────────────────────────
#  /start
# ─────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not GCP_PROJECT_ID:
        await update.message.reply_text("❌ الكنسل غير مرتبط بمشروع GCP نشط.")
        return

    user_id = update.effective_user.id

    # إذا لديه تكوين سابق أرسله مباشرة
    if user_id in users_db:
        saved = users_db[user_id]
        await update.message.reply_text(
            f"👋 لديك تكوين سابق بالفعل!\n\n"
            f"📋 *تكوين VLESS:*\n`{saved['config']}`\n\n"
            f"📅 أُنشئ في: `{saved['created_at']}`",
            parse_mode="Markdown"
        )
        return

    keyboard = [[InlineKeyboardButton("اضغط هنا لإنشاء تكوين", callback_data="create_service")]]
    await update.message.reply_text(
        "مرحباً بك في بوت تكوينات GCP 🚀\n\nاضغط على الزر للبدء في إنشاء التكوين الخاص بك.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ─────────────────────────────────────────
#  /stats — للمسؤول فقط
# ─────────────────────────────────────────
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 هذا الأمر للمسؤول فقط.")
        return

    mem        = psutil.virtual_memory()
    used_gb    = mem.used  / (1024 ** 3)
    total_gb   = mem.total / (1024 ** 3)
    cpu        = psutil.cpu_percent(interval=1)
    user_count = len(users_db)

    msg = (
        f"📊 *إحصائيات النظام الآن*\n\n"
        f"🖥 RAM الإجمالي : `{total_gb:.2f} GB`\n"
        f"🔴 RAM المستخدم : `{used_gb:.2f} GB` ({mem.percent}%)\n"
        f"🟢 RAM المتاح   : `{total_gb - used_gb:.2f} GB`\n"
        f"⚙️ CPU           : `{cpu}%`\n\n"
        f"👥 إجمالي المستخدمين: `{user_count}`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ─────────────────────────────────────────
#  /users — للمسؤول فقط
# ─────────────────────────────────────────
async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 هذا الأمر للمسؤول فقط.")
        return

    if not users_db:
        await update.message.reply_text("📭 لا يوجد مستخدمون حتى الآن.")
        return

    lines = ["👥 *قائمة المستخدمين:*\n"]
    for uid, data in users_db.items():
        lines.append(f"• `{uid}` — أُنشئ في `{data['created_at']}`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─────────────────────────────────────────
#  /reset_user <user_id> — للمسؤول فقط
# ─────────────────────────────────────────
async def reset_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 هذا الأمر للمسؤول فقط.")
        return

    if not context.args:
        await update.message.reply_text("⚠️ الاستخدام: `/reset_user <user_id>`", parse_mode="Markdown")
        return

    try:
        uid = int(context.args[0])
        if uid in users_db:
            del users_db[uid]
            await update.message.reply_text(f"✅ تم حذف تكوين المستخدم `{uid}`.", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"❌ المستخدم `{uid}` غير موجود.", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ ID غير صالح.")


# ─────────────────────────────────────────
#  زر إنشاء التكوين
# ─────────────────────────────────────────
async def handle_create_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user_id = update.effective_user.id
    await query.answer()

    # كل مستخدم له تكوين واحد فقط
    if user_id in users_db:
        saved = users_db[user_id]
        await query.edit_message_text(
            f"⚠️ لديك تكوين سابق بالفعل!\n\n"
            f"📋 *تكوين VLESS:*\n`{saved['config']}`\n\n"
            f"📅 أُنشئ في: `{saved['created_at']}`",
            parse_mode="Markdown"
        )
        return

    allowed_regions = ['us-central1', 'us-east1', 'us-east4', 'us-west1']
    await query.edit_message_text("⏳ جاري فحص السيرفر المتاح وإنشاء التكوين... انتظر لحظة.")

    client        = run_v2.ServicesClient()
    success       = False
    error_message = ""

    for selected_region in allowed_regions:
        service_id = f"service-{random.randint(100000, 999999)}"
        logging.info(f"تجربة المنطقة: {selected_region}")

        try:
            service = run_v2.Service()

            container = run_v2.Container()
            container.image = IMAGE_URI
            container.resources.limits = {"memory": "2Gi", "cpu": "1"}
            service.template.containers = [container]

            service.template.max_instance_request_concurrency = 1000
            service.template.timeout                          = "3600s"
            service.template.execution_environment            = (
                run_v2.ExecutionEnvironment.EXECUTION_ENVIRONMENT_GEN2
            )
            service.template.scaling.min_instance_count = 0
            service.template.scaling.max_instance_count = 10

            parent  = f"projects/{GCP_PROJECT_ID}/locations/{selected_region}"
            request = run_v2.CreateServiceRequest(
                parent=parent, service=service, service_id=service_id
            )

            operation = client.create_service(request=request)
            response  = operation.result()

            client.set_iam_policy(request={
                "resource": response.name,
                "policy": {
                    "bindings": [{"role": "roles/run.invoker", "members": ["allUsers"]}]
                }
            })

            extracted_host = response.uri.replace("https://", "").strip()

            # ── تكوين VLESS ──
            vless_config = (
                f"vless://ba0e3984-ccc9-48a3-8074-b2f507f41ce8@youtube.com:443"
                f"?path=%2F@Lw_dz&security=tls&encryption=none"
                f"&host={extracted_host}&type=ws&sni=youtube.com#Lw_dz-VLESS-WS"
            )

            # حفظ في قاعدة البيانات
            users_db[user_id] = {
                "config"    : vless_config,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M")
            }

            # ── إرسال VLESS فقط ──
            await query.message.reply_text(
                f"✅ *تم إنشاء التكوين بنجاح!*\n\n"
                f"🌍 *السيرفر:* `{selected_region}`\n\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📋 *تكوين VLESS:*\n`{vless_config}`",
                parse_mode="Markdown"
            )

            # ── إشعار للمسؤول ──
            try:
                await context.bot.send_message(
                    ADMIN_ID,
                    f"🔔 مستخدم جديد أنشأ تكويناً\n"
                    f"👤 ID: `{user_id}`\n"
                    f"🌍 المنطقة: `{selected_region}`\n"
                    f"📅 الوقت: `{users_db[user_id]['created_at']}`\n"
                    f"👥 إجمالي المستخدمين: `{len(users_db)}`",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

            success = True
            break

        except Exception as e:
            error_message = str(e)
            logging.warning(f"فشل في {selected_region}: {error_message}")
            continue

    if not success:
        await query.message.reply_text(
            f"❌ فشلت العملية في جميع المناطق:\n`{error_message}`"
        )


# ─────────────────────────────────────────
#  main
# ─────────────────────────────────────────
def main():
    setup_environment()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start",       start))
    application.add_handler(CommandHandler("stats",       stats))
    application.add_handler(CommandHandler("users",       list_users))
    application.add_handler(CommandHandler("reset_user",  reset_user))
    application.add_handler(CallbackQueryHandler(handle_create_service, pattern="create_service"))

    # تشغيل مهمة RAM كل 15 دقيقة
    loop = asyncio.get_event_loop()
    loop.create_task(ram_stats_job(application))

    print("🚀 البوت يعمل...")
    application.run_polling()


if __name__ == '__main__':
    main()
