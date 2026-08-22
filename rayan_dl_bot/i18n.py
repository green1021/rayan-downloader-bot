#!/usr/bin/env python3
"""
i18n — Multi-language support for Rayan Downloader Bot
Languages: fa (default), en, ar, tr, ru, de, es, fr, hi, zh, pt
"""
import json
import logging
import urllib.parse
import urllib.request

log = logging.getLogger("rayan-dl")

# Merge extra languages (tr/ru/de/es/...) into LANGUAGES
try:
    from .i18n_extra import EXTRA_LANGUAGES
except ImportError:
    from i18n_extra import EXTRA_LANGUAGES

LANGUAGES = {
    "fa": {
        "code": "fa",
        "name": "فارسی",
        "start": "🎬 **رایان دانلودر** — دانلود هر چیزی که دوست داری!\n\nفقط کافیه **یه لینک** بفرستی:\n🔹 یوتیوب 🎥 | اینستاگرام 📸 | توییتر 🐦 | تیک‌تاک ⏱\n🔹 و ده‌ها سایت دیگه!\n\n✨ **چی می‌تونی بکنی؟**\n⚡ دانلود سریع و همزمان (تا ۸ برابر سریع‌تر)\n🎵 تبدیل ویدیو به MP3\n📸 دانلود عکس‌های توییتر و اینستاگرام\n🧵 دریافت کامل رشته‌های توییتر\n📝 دیدن متن توییت‌ها\n🌍 پشتیبانی از ۱۰+ زبان\n\n🚀 **همین حالا یه لینک برام بفرست تا شروع کنیم!**\n\n💡 راهنمایی: /help — تغییر زبان: /lang",
        "help": "📖 **راهنمای ربات**\n\n▫️ **یه لینک بفرست** تا دانلود شروع بشه\n▫️ از **دکمه‌ها** برای انتخاب کیفیت استفاده کن\n▫️ 🎵 = فقط صدا (MP3)\n▫️ 🎤 **فایل صوتی/ویدیویی بفرست** تا آهنگش رو تشخیص بدم (Shazam)\n\n🛠 **دستورات:**\n/start — شروع\n/help — راهنما\n/lang — تغییر زبان\n/settings — تنظیم کیفیت پیش‌فرض\n/history — تاریخچه دانلودها\n/shazam — شناسایی آهنگ\n/about — درباره ربات\n\n👥 **در گروه‌ها:**\n/grouphelp — راهنمای حالت گروهی\n\n🌍 برای تغییر زبان: /lang",
        "about": "🤖 **رایان دانلودر**\n\n⚙️ **موتور قدرتمند:** yt-dlp + دانلودر اختصاصی\n🚀 **سرعت بالا:** دانلود همزمان ۸ تکه\n🎵 **شناسایی آهنگ:** Shazam+song detection\n🌍 **زبان:** پشتیبانی از ۱۰+ زبان\n🛡 **امکانات:** پنل ادمین، حالت گروهی، تنظیمات شخصی\n\n🔒 **حریم خصوصی:** فایل‌ها بلافاصله بعد از ارسال پاک می‌شن.\n\nساخته‌شده با ❤️ برای دانلود سریع و راحت",
        "lang_menu": "🌍 **انتخاب زبان / Choose Language:**",
        "lang_prompt": "🌍 **به رایان دانلودر خوش اومدی!**\n\nلطفاً زبانت رو انتخاب کن:\n*(اگه انتخاب نکنی، انگلیسی پیش‌فرضه)*\n\n🌍 **Welcome! Please choose your language:**\n*(English is default if you don't choose)*",
        "lang_set": "✅ زبان به **{lang}** تغییر کرد.",
        "lang_default": "فارسی (پیش‌فرض)",
        "invalid_url": "❌ یه لینک معتبر بفرست!\nمثلا: `https://youtube.com/watch?v=...`",
        "checking": "🔍 در حال بررسی لینک...",
        "processing_error": "❌ نتونستم این لینک رو پردازش کنم.\n\n`{err}`\n\n💡 اگه اینستاگرامه، ممکنه ویدیو خصوصی باشه یا نیاز به لاگین داشته باشه.",
        "no_quality": "❌ کیفیتی برای دانلود پیدا نکردم.",
        "downloading": "📥 **{title}**\n⏳ در حال دانلود {quality}...\nاین چند دقیقه طول می‌کشه.",
        "download_failed": "❌ دانلود با خطا مواجه شد. لطفاً دوباره تلاش کن.",
        "too_big": "❌ فایل {size}MB شد — خیلی بزرگه.\nکیفیت پایین‌تر رو انتخاب کن 👇",
        "sending": "📤 در حال ارسال...",
        "choose_quality": "🎯 کیفیت مورد نظرت رو انتخاب کن:",
        "more_qualities": "🎬 کیفیت‌های دیگر",
        "mp3_only": "🎵 فقط صدا (MP3)",
        "subs_available": "💬 زیرنویس موجود است",
        "subs_translating": "🌍 در حال ترجمه زیرنویس...",
        "more_options": "⚙️ گزینه‌های بیشتر:",
        "subs_sending": "💬 در حال ارسال زیرنویس ({lang})...",
        "cancel": "❌ انصراف",
        "cancelled": "❌ لغو شد.",
        "link_expired": "❌ لینک منقضی شده، دوباره بفرست.",
        "history_title": "📜 **تاریخچه دانلودهای شما** (آخرین ۱۰ مورد):",
        "history_empty": "📭 هنوز چیزی دانلود نکردی!\nیه لینک بفرست تا شروع کنیم.",
        "history_item": "▫️ {site} — {url}\n   🕒 {time}",
        "not_admin": "⛔️ دسترسی غیرمجاز! این دستور فقط برای ادمین‌هاست.",
        "banned": "🚫 شما از استفاده ربات مسدود شده‌اید.",
        "admin_menu": "🛡 **پنل مدیریت**\n\n📊 وضعیت کلی ربات:",
        "admin_stats": "📊 **آمار ربات**\n\n👥 کاربران: {users}\n🔗 لینک‌های دریافتی: {links}\n🎬 دانلودهای موفق: {success}\n❌ دانلودهای ناموفق: {failed}\n\n**سایت‌ها:**\n{by_site}\n\n💾 دیسک: {disk}",
        "admin_users": "👥 **کاربران** (۱۰ نفر برتر):\n\n{users}",
        "admin_user_line": "▫️ [{id}](tg://user?id={id}) {name} — {links} لینک",
        "no_users": "📭 هنوز کاربری ثبت نشده.",
        "broadcast_prompt": "📣 پیام ارسال گروهی:\nپیامت رو بفرست (یا /cancel برای لغو).",
        "broadcast_sent": "✅ پیام برای {sent} نفر ارسال شد.",
        "broadcast_failed": "⚠️ ارسال به {failed} نفر ناموفق بود.",
        "broadcast_cancelled": "❌ ارسال گروهی لغو شد.",
        "ban_prompt": "🚫 آیدی عددی کاربری که می‌خوای مسدود کنی رو بفرست:",
        "unban_prompt": "✅ آیدی عددی کاربری که می‌خوای از مسدودی خارج کنی رو بفرست:",
        "ban_done": "🚫 کاربر {id} مسدود شد.",
        "unban_done": "✅ کاربر {id} از مسدودی خارج شد.",
        "invalid_id": "❌ آیدی نامعتبر. یه عدد بفرست.",
        "not_found": "❌ کاربر پیدا نشد.",
        "download_started": "📥 دانلود شروع شد. لطفاً صبر کن...",
        "rate_limit": "🐢 آروم باش! یه لحظه صبر کن بعد دوباره تلاش کن.",
        "playlist_detected": "📃 پلی‌لیست! {count} ویدیو پیدا شد.\n⏳ در حال دانلود...",
        "admin_users_prompt": "👥 همه کاربران:\n{users}",
        "site_youtube": "یوتیوب",
        "site_instagram": "اینستاگرام",
        "site_twitter": "توییتر",
        "site_tiktok": "تیک‌تاک",
        "site_pornhub": "پورن‌هاب",
        "site_other": "سایر",
        "choose_site": "🌍 انتخاب زبان",
        "stats": "📊",
        "users_btn": "👥",
        "broadcast_btn": "📣",
        "ban_btn": "🚫",
        "unban_btn": "✅",
        "history_btn": "📜 تاریخچه",
        "help_btn": "📖 راهنما",
        "close": "❌ بستن",
        "back": "🔙 بازگشت",
        "links_btn": "🔗 لینک‌ها",
        "admin_links": "🔗 **آخرین لینک‌های ارسال‌شده:**\n\n{links}",
        "admin_link_line": "▫️ [{user}](tg://user?id={uid}) — {site}\n   📝 {title}\n   🔗 {url}\n   🕒 {time}",
        "no_links": "📭 هنوز لینکی ارسال نشده.",
        "group_help": "👥 **راهنمای گروه**\n\nاین ربات توی گروه هم کار می‌کنه! تنظیمات:\n\n/setquality <کیفیت> — حداکثر کیفیت (مثلا 720 یا 1080)\n/autodl on|off — دانلود خودکار بدون سوال\n/setmedia — چه رسانه‌هایی گرفته بشن (لینک/فایل/هیچ)\n/setfilter — فیلتر لینک‌ها (همه/فقط سایت‌های کامل/سفارشی)\n/silent on|off — حالت بی‌صدا (بدون پیام اضافه)\n/commandonly on|off — فقط با /ytdl دانلود کن\n/ytdl <لینک> — دانلود صریح با کامند\n/groupsettings — وضعیت فعلی\n\nفقط ادمین‌های گروه می‌تونن تنظیم کنن.",
        "group_privacy_note": "\n\n⚠️ **نکته مهم:** اگه کامندها جواب نمی‌دن، ربات رو **ادمین گروه** کن (تنظیمات گروه → ادمین‌ها → add bot) یا حالت privacy رو خاموش کن — تلگرام اگه ربات ادمین نباشه، پیام‌های گروه رو بهش نمی‌ده.",
        "group_settings": "⚙️ **تنظیمات گروه:**\n\n🎯 حداکثر کیفیت: {quality}p\n⚡ دانلود خودکار: {auto}\n🔇 حالت بی‌صدا: {silent}\n🎛 رسانه‌ها: {media}\n🔍 فیلتر لینک: {filter_}",
        "group_on": "✅ روشن",
        "group_off": "❌ خاموش",
        "setquality_usage": "🎯 استفاده: /setquality 720 (یا 480/1080/0=بالاترین)\n\nمثال: /setquality 1080",
        "setquality_done": "✅ حداکثر کیفیت گروه به **{q}p** تغییر کرد.",
        "autodl_done": "✅ دانلود خودکار: {state}",
        "silent_done": "✅ حالت بی‌صدا: {state}",
        "cmdonly_done": "✅ فقط با کامند: {state}",
        "group_admin_only": "⛔️ فقط ادمین‌های گروه می‌تونن این کار رو بکنن.",
        "only_groups": "❌ این دستور فقط توی گروه کار می‌کنه.",
        "dl_in_group": "📥 دانلود: **{title}**",
        "lang_not_supported": "❌ این زبان هنوز پشتیبانی نمی‌شه. /lang بزن.",
        "settings_title": "⚙️ **تنظیمات شما**\n\n🎯 کیفیت پیش‌فرض: {quality}p\n🎵 فرمت: {format}\n\nکیفیت پیش‌فرض = اولین چیزی که موقع ارسال لینک دانلود می‌شه.",
        "settings_quality_prompt": "🎯 کیفیت پیش‌فرض رو انتخاب کن:\n(اگه کیفیت انتخابی موجود نباشه، نزدیک‌ترین کیفیت پایین‌تر دانلود می‌شه)",
        "settings_done": "✅ کیفیت پیش‌فرض به **{q}p** تغییر کرد.",
        "ytdl_usage": "🎯 استفاده: /ytdl <لینک>\n\nمثال: /ytdl https://youtube.com/watch?v=...",
        "cmdonly_usage": "🔒 استفاده: /commandonly on|off",
        "shazam_cmd": "🎵 **شناسایی آهنگ (Shazam)**\n\nفقط کافیه **یه فایل صوتی یا ویدیویی** برام بفرستی تا آهنگش رو تشخیص بدم!\n\n🎬 پشتیبانی از: MP3, WAV, M4A, OGG, MP4, MKV, MOV و...\n⏱ بهتره حداقل ۱۰ ثانیه باشه\n\nیا یه **لینک یوتیوب** بفرست — هم ویدیو رو می‌گیرم هم آهنگش رو می‌گم!",
        "shazam_searching": "🔎 در حال جستجوی آهنگ...",
        "shazam_match": "🎵 **{title}** — {artist}\n\n💿 آلبوم: {album}\n📅 سال: {year}\n🎼 ژانر: {genre}\n\n🎧 پخش در:\n▶️ [YouTube Music]({ytm_url})\n🟢 [Spotify]({spotify_url})\n🔵 [Shazam]({shazam_url})\n\n📥 در حال ارسال فایل موسیقی...",
        "shazam_no_match": "😕 نتونستم این آهنگ رو تشخیص بدم. مطمئن شو فایل صوتی واضحی داره (حداقل ۱۰ ثانیه).",
        "shazam_error": "❌ خطا در شناسایی: {err}",
        "shazam_not_found": "📭 هنوز چیزی پیدا نشده. یه فایل صوتی/ویدیویی بفرست!",
        "search_title": "🔎 نتایج جستجو برای «{query}»\n\n👇 یکی از آهنگ‌ها رو انتخاب کن:",
        "search_no_results": "😕 چیزی پیدا نشد. اسم آهنگ رو دقیق‌تر بنویس.",
        "search_next": "▶️ بعدی",
        "search_prev": "◀️ قبلی",
        "search_page": "📄 صفحه {page}",
        "search_downloading": "📥 در حال دانلود...",
        "search_cancel": "❌ بستن جستجو",
        "force_join_required": "🔒 **برای استفاده از ربات، اول باید عضو این کانال‌ها بشی:**\n\n{channels}\n\nبعد از عضویت، دکمه «✅ عضو شدم» رو بزن.",
        "force_join_uncheckable_note": "\n\nℹ️ ربات نمی‌تونه عضویت رو خودکار چک کنه (ادمین کانال نیست). بعد از عضویت، «✅ عضو شدم» رو بزن تا تایید بشی.",
        "force_join_channels": "🔗 {title} — [عضویت](https://t.me/{username})",
        "force_join_check": "✅ عضو شدم — بررسی کن",
        "force_join_ok": "✅ ممنون! حالا می‌تونی استفاده کنی.\n\n🎬 یه لینک بفرست تا شروع کنی!",
        "force_join_still_missing": "❌ هنوز عضو این کانال‌ها نشدی:\n\n{missing}\n\nاول عضو شو، بعد دوباره امتحان کن.",
        "force_join_admin_help": "🛡 **مدیریت کانال‌های اجباری**\n\n/addchannel <یوزرنیم> — اضافه کانال\n/removechannel <یوزرنیم> — حذف کانال\n/channels — لیست کانال‌های فعلی",
        "force_join_admin_usage": "🎯 استفاده: /addchannel @mychannel",
        "force_join_added": "✅ کانال @{ch} اضافه شد.",
        "force_join_exists": "⚠️ کانال @{ch} از قبل وجود داره.",
        "force_join_removed": "✅ کانال @{ch} حذف شد.",
        "force_join_not_found": "❌ کانال @{ch} پیدا نشد.",
        "force_join_no_channels": "📭 هیچ کانال اجباری‌ای تعریف نشده.",
        "force_join_list": "🔗 **کانال‌های اجباری:**\n\n{channels}",
        "force_join_list_item": "▫️ @{username} {title}",
        "force_join_check_failed": "❌ نتونستم عضویتت رو بررسی کنم. دوباره تلاش کن.",
        "force_join_ignore": "⏭️ ربات نیاز به عضویت کانال داره. /start بزن و عضو شو.",
        "tw_tweet_header": "🐦 **{name}** (@{screen})\n\n{text}\n\n💬 {likes} لایک • 🔁 {rts} بازتوییت",
        "tw_quote_header": "💬 **{name}** (@{screen}) این رو نقل کرده:\n{text}",
        "tw_photos_sending": "📸 دارم {count} عکس می‌فرستم...",
        "tw_no_media": "🐦 این توییت فقط متن داره (عکس/ویدیو نداره):\n\n{text}",
        "tw_thread_header": "🧵 **رشته‌توییت** ({count} تا):",
        "tw_thread_item": "{n}. **{name}**: {text}",
        "tw_thread_error": "❌ نتونستم همه رشته‌توییت رو بگیرم. آخرین توییت رو نشون می‌دم.",
        "tw_quote_only": "🐦 **{name}** (@{screen}):\n\n{text}\n\n┌────────────────\n│ 💬 **{qname}** (@{qscreen}) نقل کرده:\n│ {qtext}\n└────────────────",
        "auto_checking": "🔎 در حال بررسی خودکار سایت...",
        "auto_support": "🧠 **پشتیبانی خودکار** — این سایت به صورت خودکار پردازش می‌شه.\n⚠️ این نوع پشتیبانی آزمایشیه و ممکنه باگ داشته باشه.\n\n📊 تشخیص: **{detail}**",
        "auto_failed": "⚠️ پشتیبانی خودکار ناموفق بود:\n`{err}`\n\n💡 این سایت رو می‌تونی با **پلاگین‌ساز** (از پنل ادمین) دقیق پشتیبانی کنی!",
        "auto_cant_detect": "⚠️ نتونستم این سایت رو خودکار تشخیص بدم: {err}\n\n💡 از پنل ادمین → پلاگین‌ساز می‌تونی پشتیبانی دقیق بسازی.",
        "ch_downloading": "⬇️ در حال دانلود **قسمت {num}**...\nمنو باز می‌مونه، بعدش می‌تونی قسمت بعدی رو هم بزنی.",
        "ch_error": "❌ قسمت {num} ناموفق: {err}\n\nیک قسمت دیگه انتخاب کن 👇",
        "ch_expired": "❌ لیست قسمت‌ها منقضی شد. لینک کامیک رو دوباره بفرست.",
        "ch_list": "🔰 **{name}** — {total} قسمت\n📄 صفحه {page} از {total_pages}\n",
        "ch_close": "❌ بستن",
        "ch_next": "بعدی ▶️",
        "ch_prev": "◀️ قبلی",
        "ch_pick": "📥 قسمت {num}",
        "adm_stats": "📊 آمار",
        "adm_users": "👥 کاربران",
        "adm_links": "🔗 لینک‌ها",
        "adm_channels": "🔗 کانال‌ها",
        "adm_userlinks": "📤 خروجی کاربر",
        "adm_db": "💾 دیتابیس",
        "adm_admins": "👥 ادمین‌ها",
        "adm_addadmin": "➕ افزودن ادمین",
        "adm_startmsg": "📝 متن شروع",
        "adm_resetmsg": "🔄 بازنشانی متن",
        "adm_techstats": "📊 آمار فنی",
        "adm_logs": "🪵 لاگ خطاها",
        "adm_plugins": "🔌 پلاگین‌ها",
        "adm_botsettings": "⚙️ تنظیمات ربات",
        "adm_broadcast": "📢 پیام گروهی",
        "adm_ban": "🚫 مسدود",
        "adm_unban": "✅ رفع مسدودی",
        "adm_addchan": "➕ کانال",
        "adm_install_plugin": "➕ نصب پلاگین",
        "adm_plugin_builder": "🛠 پلاگین‌ساز",
    },
    "en": {
        "code": "en",
        "name": "English",
        "start": "🎬 **Rayan Downloader** — Download anything you love!\n\nJust send **a link**:\n🔹 YouTube 🎥 | Instagram 📸 | Twitter 🐦 | TikTok ⏱\n🔹 And dozens more!\n\n✨ **What you can do:**\n⚡ Fast concurrent downloads (up to 8× faster)\n🎵 Convert videos to MP3\n📸 Download Twitter & Instagram photos\n🧵 Full Twitter threads\n📝 Read tweet text\n🌍 10+ languages\n\n🚀 **Send me a link right now and let's start!**\n\n💡 Help: /help — Change language: /lang",
        "help": "📖 **Bot Help**\n\n▫️ **Send a link** to start downloading\n▫️ Use **buttons** to pick quality\n▫️ 🎵 = audio only (MP3)\n\n🛠 **Commands:**\n/start — Start\n/help — Help\n/lang — Change language\n/settings — Default quality\n/history — Download history\n/about — About\n\n👥 **In groups:**\n/grouphelp — Group mode guide\n\n🌍 To change language: /lang",
        "about": "🤖 **Rayan Downloader**\n\n⚙️ **Powerful engine:** yt-dlp + custom downloader\n🚀 **High speed:** 8 concurrent downloads\n🌍 **Languages:** 10+ supported\n🛡 **Features:** admin panel, group mode, personal settings\n\n🔒 **Privacy:** files are deleted right after sending.\n\nMade with ❤️ for fast & easy downloads",
        "lang_menu": "🌍 **Choose Language:**",
        "lang_prompt": "🌍 **Welcome to Rayan Downloader!**\n\nPlease choose your language:\n*(English is default if you don't choose)*",
        "lang_set": "✅ Language set to **{lang}**.",
        "lang_default": "English",
        "invalid_url": "❌ Send a valid link!\nE.g. `https://youtube.com/watch?v=...`",
        "checking": "🔍 Checking link...",
        "processing_error": "❌ Could not process this link.\n\n`{err}`\n\n💡 If it's Instagram, the video may be private or need login.",
        "no_quality": "❌ No downloadable quality found.",
        "downloading": "📥 **{title}**\n⏳ Downloading {quality}...\nThis may take a few minutes.",
        "download_failed": "❌ Download failed. Please try again.",
        "too_big": "❌ File is {size}MB — too large.\nChoose a lower quality 👇",
        "sending": "📤 Sending...",
        "choose_quality": "🎯 Choose your quality:",
        "more_qualities": "🎬 More qualities",
        "mp3_only": "🎵 Audio only (MP3)",
        "subs_available": "💬 Subtitles available",
        "subs_translating": "🌍 Translating subtitles...",
        "more_options": "⚙️ More options:",
        "subs_sending": "💬 Sending subtitles ({lang})...",
        "cancel": "❌ Cancel",
        "cancelled": "❌ Cancelled.",
        "link_expired": "❌ Link expired, send it again.",
        "history_title": "📜 **Your download history** (last 10):",
        "history_empty": "📭 You haven't downloaded anything yet!\nSend a link to start.",
        "history_item": "▫️ {site} — {url}\n   🕒 {time}",
        "not_admin": "⛔️ Unauthorized! This command is admin-only.",
        "banned": "🚫 You are banned from using this bot.",
        "admin_menu": "🛡 **Admin Panel**\n\n📊 Bot status:",
        "admin_stats": "📊 **Bot Stats**\n\n👥 Users: {users}\n🔗 Links received: {links}\n🎬 Successful downloads: {success}\n❌ Failed downloads: {failed}\n\n**By site:**\n{by_site}\n\n💾 Disk: {disk}",
        "admin_users": "👥 **Users** (top 10):\n\n{users}",
        "admin_user_line": "▫️ [{id}](tg://user?id={id}) {name} — {links} links",
        "no_users": "📭 No users yet.",
        "broadcast_prompt": "📣 Broadcast message:\nSend your message (or /cancel to abort).",
        "broadcast_sent": "✅ Message sent to {sent} users.",
        "broadcast_failed": "⚠️ Failed for {failed} users.",
        "broadcast_cancelled": "❌ Broadcast cancelled.",
        "ban_prompt": "🚫 Send the numeric ID of the user to ban:",
        "unban_prompt": "✅ Send the numeric ID of the user to unban:",
        "ban_done": "🚫 User {id} banned.",
        "unban_done": "✅ User {id} unbanned.",
        "invalid_id": "❌ Invalid ID. Send a number.",
        "not_found": "❌ User not found.",
        "download_started": "📥 Download started. Please wait...",
        "rate_limit": "🐢 Slow down! Wait a moment and try again.",
        "playlist_detected": "📃 Playlist! {count} videos found.\n⏳ Downloading...",
        "admin_users_prompt": "👥 All users:\n{users}",
        "site_youtube": "YouTube",
        "site_instagram": "Instagram",
        "site_twitter": "Twitter",
        "site_tiktok": "TikTok",
        "site_pornhub": "Pornhub",
        "site_other": "Other",
        "choose_site": "🌍 Choose language",
        "stats": "📊",
        "users_btn": "👥",
        "broadcast_btn": "📣",
        "ban_btn": "🚫",
        "unban_btn": "✅",
        "history_btn": "📜 History",
        "help_btn": "📖 Help",
        "close": "❌ Close",
        "back": "🔙 Back",
        "links_btn": "🔗 Links",
        "admin_links": "🔗 **Latest links sent:**\n\n{links}",
        "admin_link_line": "▫️ [{user}](tg://user?id={uid}) — {site}\n   📝 {title}\n   🔗 {url}\n   🕒 {time}",
        "no_links": "📭 No links sent yet.",
        "group_help": "👥 **Group Help**\n\nThis bot works in groups too! Settings:\n\n/setquality <quality> — max quality (e.g. 720 or 1080)\n/autodl on|off — auto-download without asking\n/setmedia — which media to handle (links/files/none)\n/setfilter — link filter (all/core-sites/custom)\n/silent on|off — silent mode (no extra messages)\n/commandonly on|off — only download via /ytdl\n/ytdl <link> — explicit download\n/groupsettings — current settings\n\nOnly group admins can configure.",
        "group_privacy_note": "\n\n⚠️ **Important:** If commands don't respond, add the bot as a **group admin** (Group Settings → Administrators → Add Bot) or disable privacy mode — Telegram won't forward group messages to the bot unless it's an admin.",
        "group_settings": "⚙️ **Group Settings:**\n\n🎯 Max quality: {quality}p\n⚡ Auto download: {auto}\n🔇 Silent mode: {silent}\n🎛 Media: {media}\n🔍 Link filter: {filter_}",
        "group_on": "✅ On",
        "group_off": "❌ Off",
        "setquality_usage": "🎯 Usage: /setquality 720 (or 480/1080/0=highest)\n\nExample: /setquality 1080",
        "setquality_done": "✅ Group max quality set to **{q}p**.",
        "autodl_done": "✅ Auto download: {state}",
        "silent_done": "✅ Silent mode: {state}",
        "cmdonly_done": "✅ Command only: {state}",
        "group_admin_only": "⛔️ Only group admins can do this.",
        "only_groups": "❌ This command only works in groups.",
        "dl_in_group": "📥 Downloading: **{title}**",
        "lang_not_supported": "❌ This language is not supported yet. /lang",
        "settings_title": "⚙️ **Your Settings**\n\n🎯 Default quality: {quality}p\n🎵 Format: {format}\n\nDefault quality = what downloads first when you send a link.",
        "settings_quality_prompt": "🎯 Choose your default quality:\n(if not available, closest lower quality is used)",
        "settings_done": "✅ Default quality set to **{q}p**.",
        "ytdl_usage": "🎯 Usage: /ytdl <link>\n\nExample: /ytdl https://youtube.com/watch?v=...",
        "cmdonly_usage": "🔒 Usage: /commandonly on|off",
        "shazam_cmd": "🎵 **Song Recognition (Shazam)**\n\nJust send me an **audio or video file** and I'll identify the song!\n\n🎬 Supported: MP3, WAV, M4A, OGG, MP4, MKV, MOV and more\n⏱ At least ~10 seconds is best\n\nOr send a **YouTube link** — I'll grab the video AND tell you the song!",
        "shazam_searching": "🔎 Searching for the song...",
        "shazam_match": "🎵 **{title}** — {artist}\n\n💿 Album: {album}\n📅 Year: {year}\n🎼 Genre: {genre}\n\n🎧 Listen on:\n▶️ [YouTube Music]({ytm_url})\n🟢 [Spotify]({spotify_url})\n🔵 [Shazam]({shazam_url})\n\n📥 Sending the music file...",
        "shazam_no_match": "😕 Couldn't identify this song. Make sure it's clear audio (at least 10 seconds).",
        "shazam_error": "❌ Recognition error: {err}",
        "shazam_not_found": "📭 Nothing found yet. Send an audio/video file!",
        "search_title": "🔎 Results for \"{query}\"\n\n👇 Pick a song:",
        "search_no_results": "😕 Nothing found. Try a more specific song name.",
        "search_next": "▶️ Next",
        "search_prev": "◀️ Prev",
        "search_page": "📄 Page {page}",
        "search_downloading": "📥 Downloading...",
        "search_cancel": "❌ Close search",
        "force_join_required": "🔒 **To use this bot you must join these channels first:**\n\n{channels}\n\nAfter joining, press the button below.",
        "force_join_uncheckable_note": "\n\nℹ️ The bot can't auto-check membership (it's not an admin of the channel). After joining, press \"✅ I joined\" to confirm.",
        "force_join_channels": "🔗 {title} — [Join](https://t.me/{username})",
        "force_join_check": "✅ I joined — Check",
        "force_join_ok": "✅ Thanks! You can now use the bot.\n\n🎬 Send a link to start!",
        "force_join_still_missing": "❌ You haven't joined these channels yet:\n\n{missing}\n\nJoin first, then try again.",
        "force_join_admin_help": "🛡 **Force-join channel management**\n\n/addchannel <username> — add channel\n/removechannel <username> — remove channel\n/channels — list current channels",
        "force_join_admin_usage": "🎯 Usage: /addchannel @mychannel",
        "force_join_added": "✅ Channel @{ch} added.",
        "force_join_exists": "⚠️ Channel @{ch} already exists.",
        "force_join_removed": "✅ Channel @{ch} removed.",
        "force_join_not_found": "❌ Channel @{ch} not found.",
        "force_join_no_channels": "📭 No force-join channels configured.",
        "force_join_list": "🔗 **Force-join channels:**\n\n{channels}",
        "force_join_list_item": "▫️ @{username} {title}",
        "force_join_check_failed": "❌ Could not check your membership. Try again.",
        "force_join_ignore": "⏭️ Bot requires channel membership. Press /start and join.",
        "tw_tweet_header": "🐦 **{name}** (@{screen})\n\n{text}\n\n💬 {likes} likes • 🔁 {rts} retweets",
        "tw_quote_header": "💬 **Quote from {name}** (@{screen}):\n{text}",
        "tw_photos_sending": "📸 Sending {count} photos...",
        "tw_no_media": "🐦 This tweet has no media, just text:\n\n{text}",
        "tw_thread_header": "🧵 **Thread** ({count} tweets):",
        "tw_thread_item": "{n}. **{name}**: {text}",
        "tw_thread_error": "❌ Could not fetch the full thread. Showing latest tweet.",
        "tw_quote_only": "🐦 **{name}** (@{screen}):\n\n{text}\n\n┌────────────────\n│ 💬 **{qname}** (@{qscreen}):\n│ {qtext}\n└────────────────",
        "auto_checking": "🔎 Auto-analyzing this site...",
        "auto_support": "🧠 **Auto support** — this site is processed automatically.\n⚠️ This is experimental support and may have bugs.\n\n📊 Detected: **{detail}**",
        "auto_failed": "⚠️ Auto support failed:\n`{err}`\n\n💡 You can build precise support with the **Plugin Builder** (admin panel)!",
        "auto_cant_detect": "⚠️ Couldn't auto-detect this site: {err}\n\n💡 Use the Plugin Builder (admin panel) to build precise support.",
        "ch_downloading": "⬇️ Downloading **Chapter {num}**...\nThe menu stays open — you can pick the next chapter after.",
        "ch_error": "❌ Chapter {num} failed: {err}\n\nPick another one 👇",
        "ch_expired": "❌ Chapter list expired. Send the comic link again.",
        "ch_list": "🔰 **{name}** — {total} chapters\n📄 Page {page} of {total_pages}\n",
        "ch_close": "❌ Close",
        "ch_next": "Next ▶️",
        "ch_prev": "◀️ Prev",
        "ch_pick": "📥 Chapter {num}",
        "adm_stats": "📊 Stats",
        "adm_users": "👥 Users",
        "adm_links": "🔗 Links",
        "adm_channels": "🔗 Channels",
        "adm_userlinks": "📤 User exports",
        "adm_db": "💾 Database",
        "adm_admins": "👥 Admins",
        "adm_addadmin": "➕ Add admin",
        "adm_startmsg": "📝 Start text",
        "adm_resetmsg": "🔄 Reset text",
        "adm_techstats": "📊 Tech stats",
        "adm_logs": "🪵 Error logs",
        "adm_plugins": "🔌 Plugins",
        "adm_botsettings": "⚙️ Bot settings",
        "adm_broadcast": "📢 Broadcast",
        "adm_ban": "🚫 Ban",
        "adm_unban": "✅ Unban",
        "adm_addchan": "➕ Add channel",
        "adm_install_plugin": "➕ Install plugin",
        "adm_plugin_builder": "🛠 Plugin builder",
    },
    **EXTRA_LANGUAGES,
}


def get_lang(user_lang: str = "en") -> str:
    """Map a user's language code to a supported language."""
    if not user_lang:
        return "en"
    code = user_lang.lower().split("-")[0]
    if code in LANGUAGES:
        return code
    # Map common codes
    mapping = {
        "prs": "fa", "pes": "fa", "ar": "ar", "tr": "tr", "ru": "ru",
        "de": "de", "es": "es", "fr": "fr", "hi": "hi", "zh": "zh",
        "pt": "pt", "en": "en", "az": "tr", "uz": "tr", "uk": "ru",
        "fa": "fa",
    }
    return mapping.get(code, "en")


# In-memory cache of admin-customized messages. Loaded once at first t() call and
# invalidated via set_custom_msg/remove_custom_msg (bot.py calls db.refresh_msg_cache()).
# This avoids a SQLite connection per t() call (t() runs hundreds of times per day).
_MSG_CACHE = None


def _load_msg_cache():
    global _MSG_CACHE
    if _MSG_CACHE is None:
        try:
            # Import the SAME db module used by the bot package
            try:
                from . import db as _db
            except ImportError:
                import db as _db
            _MSG_CACHE = _db.all_custom_msgs()
        except Exception:
            _MSG_CACHE = {}
    return _MSG_CACHE


def invalidate_msg_cache():
    global _MSG_CACHE
    _MSG_CACHE = None


def t(lang: str, key: str, **kwargs) -> str:
    """Translate a key to the given language, fallback to en (then fa).
    Admin-customized messages (bot_messages table) override the default text."""
    # Admin override (custom start text, etc.) — read from in-memory cache
    cache = _load_msg_cache()
    override = (cache.get(lang, {}) or {}).get(key)
    if override is None and lang != "en":
        override = (cache.get("en", {}) or {}).get(key)
    if override:
        text = override
        if kwargs:
            try:
                return text.format(**kwargs)
            except Exception as e:
                log.warning("i18n override format failed lang=%s key=%s: %s", lang, key, e)
                return text
        return text
    text = (LANGUAGES.get(lang, {}).get(key)
            or LANGUAGES["en"].get(key)
            or LANGUAGES["fa"].get(key)
            or key)
    if kwargs:
        try:
            text = text.format(**kwargs)
        except Exception as e:
            log.warning("i18n format failed lang=%s key=%s: %s", lang, key, e)
    return text


# ── Automatic translation (admin sets a start text in one language, auto-translates) ──
# Language code -> Google Translate 'tl' target
TARGET_MAP = {
    "fa": "fa", "en": "en", "ar": "ar", "tr": "tr", "ru": "ru",
    "de": "de", "es": "es", "fr": "fr", "hi": "hi", "zh": "zh", "pt": "pt",
}


def _gt_translate(text: str, srclang: str, tgtlang: str, timeout: int = 30) -> str | None:
    """Translate via free Google translate endpoint. Returns None on failure."""
    if srclang == tgtlang:
        return text
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx"
           f"&sl={srclang}&tl={tgtlang}&dt=t&q=" + urllib.parse.quote(text))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        return "".join(part[0] or "" for part in d[0])
    except Exception as e:
        log = None
        try:
            import logging
            logging.getLogger("rayan-dl").warning("gt_translate %s→%s: %s", srclang, tgtlang, e)
        except Exception:
            pass
        return None


def translate_to_all(base_text: str, srclang: str) -> dict:
    """Translate base_text into all bot languages. Returns {code: translated}."""
    out = {}
    for code, tl in TARGET_MAP.items():
        if code == srclang:
            out[code] = base_text
            continue
        tr = _gt_translate(base_text, srclang, tl) or base_text
        out[code] = tr
    return out
