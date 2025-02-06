import os
import re
import time
import logging
import tempfile
import yt_dlp
import requests
import subprocess
import glob
import threading
import copy
import math
from pyrogram import Client, filters, types
from PIL import Image
from config import (
    API_ID,
    API_HASH,
    BOT_TOKEN,
    ALLOWED_USERS,
    COOKIES_URL,
    PROGRESS_UPDATE_INTERVAL,
    LOG_CHANNEL_ID,
    EQUAL_SPLIT,
    YOUTUBE_API_KEY,
    AV1_FOR_LOWRES,
    AV1_FOR_HIGHRES    # Yeni: Youtube Data API anahtarı
)

# Eğer PROGRESS_UPDATE_INTERVAL tanımlı değilse varsayılan 7 saniye.
if not PROGRESS_UPDATE_INTERVAL:
    PROGRESS_UPDATE_INTERVAL = 7

# Loglama ayarları
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# COOKIES_URL varsa cookies.txt indiriliyor.
if COOKIES_URL:
    try:
        response = requests.get(COOKIES_URL)
        with open("cookies.txt", "wb") as f:
            f.write(response.content)
        logger.info("cookies.txt başarıyla indirildi.")
    except Exception as e:
        logger.error("Cookies dosyası indirilemedi: %s", e)

# Bot istemcisi
app = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Her kullanıcının video/ses bilgileri burada tutuluyor.
user_video_info = {}  # user_id -> {url, title, duration, formats, thumbnail, ...}
# Aynı anda sadece 1 işlem yapılsın:
user_busy = {}       # user_id -> bool
user_queue = {}      # user_id -> list of task dict'leri

def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name)

def format_duration(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

def start_quality_timeout(user_id: int, quality_msg: types.Message):
    """60 saniye içinde kalite seçilmezse uyarı verip işlemi iptal eder."""
    time.sleep(60)
    user_data = user_video_info.get(user_id)
    if user_data and not user_data.get("selection_made", False):
        try:
            quality_msg.edit_text("Herhangi bir kalite seçmedin, işlem iptal edildi.", reply_markup=None)
        except Exception as e:
            logger.error("Kalite timeout mesajı güncellenirken hata: %s", e)
        user_video_info.pop(user_id, None)

def search_youtube(query: str, max_results: int = 20):
    """
    Youtube Data API v3 kullanarak arama yapar.
    API anahtarınızın doğru olduğundan ve quota limitlerinizi kontrol ettiğinizden emin olun.
    """
    if not YOUTUBE_API_KEY:
        raise Exception("YOUTUBE_API_KEY ayarlanmamış!")
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "key": YOUTUBE_API_KEY,
        "q": query,
        "part": "snippet",
        "maxResults": max_results,
        "type": "video"
    }
    response = requests.get(url, params=params)
    if response.status_code != 200:
        raise Exception(f"Youtube API hatası: {response.status_code} - {response.text}")
    return response.json()

def prepare_video_info_and_show_quality(chat_id: int, user_id: int, video_url: str, status_msg: types.Message = None):
    """
    Verilen video_url için yt-dlp ile video bilgilerini alır, 
    user_video_info'yu günceller ve kalite seçeneklerini inline butonlarla kullanıcıya sunar.
    Eğer status_msg parametresi verilmişse, o mesaj üzerinden düzenleme yapılır.
    """
    
    user_video_info[user_id] = {
        "url": video_url,
        "formats": {},
        "title": "",
        "duration": 0,
        "thumbnail": None,
        "selection_made": False,
        "bestaudio_info": None
    }

    ydl_opts = {
        'skip_download': True,
        'quiet': True,
        'no_warnings': True,
        'logger': logger,
        'cookiefile': 'cookies.txt' if os.path.exists("cookies.txt") else None
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            user_video_info[user_id]["title"] = info.get("title", "Video")
            user_video_info[user_id]["duration"] = info.get("duration", 0)
            user_video_info[user_id]["thumbnail"] = info.get("thumbnail")
    except Exception as e:
        logger.error("Video/Ses bilgileri alınırken hata: %s", e)
        if status_msg:
            try:
                status_msg.edit_text("Bilgiler alınırken hata oluştu.")
            except Exception:
                pass
        else:
            app.send_message(chat_id, "Bilgiler alınırken hata oluştu.")
        return

    formats = info.get('formats', [])
    video_options = []
    for f in formats:
        if f.get('vcodec') != 'none':
            fmt_id = f.get('format_id')
            # AV1 için düşük çözünürlüklü formatlar: 394,395,396,397
            if fmt_id in ["394", "395", "396", "397"]:
                if not AV1_FOR_LOWRES:
                    continue  # Bu seçenekleri listeye ekleme
            # AV1 için yüksek çözünürlüklü formatlar: 398,399,400,401,402
            if fmt_id in ["398", "399", "400", "401", "402"]:
                if not AV1_FOR_HIGHRES:
                    continue  # Bu seçenekleri listeye ekleme

            height = f.get('height')
            fps = f.get('fps')
            if height:
                quality_label = f"{height}p"
                if fps and int(fps) != 30:
                    quality_label += str(int(fps))
            else:
                quality_label = "Bilinmiyor"
            filesize = f.get('filesize') or f.get('filesize_approx')
            size_str = f"{filesize/1024/1024:.2f} MB" if filesize else "Bilinmiyor"
            ext = f.get('ext', "bilinmiyor")
            # Eğer vcodec içinde "av01" varsa ve AV1 seçenekleri açık ise, formatın sonuna "-av1" ekle
            if f.get('vcodec') and "av01" in f.get('vcodec').lower():
                ext = "mp4-av1"
            desc = f"{quality_label} - {size_str} (ext: {ext})"
            video_options.append((fmt_id, desc))
            user_video_info[user_id]["formats"][fmt_id] = {
                "has_audio": f.get('acodec') != 'none',
                "desc": desc
            }

    # Ses için bestaudio:
    audio_candidates = [f for f in formats if f.get('vcodec') == 'none' and f.get('acodec') != 'none']
    if audio_candidates:
        bestaudio = max(audio_candidates, key=lambda f: f.get('abr', 0))
        size = bestaudio.get('filesize') or bestaudio.get('filesize_approx')
        size_str = f"{size/1024/1024:.2f} MB" if size else "Bilinmiyor"
        audio_button_text = f"Müzik: en iyi - {size_str} (id: {bestaudio.get('format_id')}, ext: mp3)"
        user_video_info[user_id]["bestaudio_info"] = {
            "format_id": bestaudio.get("format_id"),
            "ext": bestaudio.get("ext")
        }
    else:
        audio_button_text = "Müzik: en iyi (bilgi yok)"

    buttons = []
    if video_options:
        for fmt, desc in video_options:
            buttons.append([types.InlineKeyboardButton(text=f"Video: {desc}", callback_data=f"video|{fmt}")])
    else:
        if status_msg:
            try:
                status_msg.edit_text("Uygun video formatı bulunamadı.")
            except Exception as e:
                logger.error("Mesaj güncelleme hatası: %s", e)
        else:
            app.send_message(chat_id, "Uygun video formatı bulunamadı.")
        return

    buttons.append([types.InlineKeyboardButton(text=audio_button_text, callback_data="audio|bestaudio")])
    keyboard = types.InlineKeyboardMarkup(buttons)

    if status_msg:
        try:
            status_msg.edit_text("Lütfen indirmek istediğiniz kaliteyi seçin:", reply_markup=keyboard)
        except Exception as e:
            logger.error("Kalite seçim mesajı güncellenirken hata: %s", e)
    else:
        app.send_message(chat_id, "Lütfen indirmek istediğiniz kaliteyi seçin:", reply_markup=keyboard)
    if status_msg:
        threading.Thread(target=start_quality_timeout, args=(user_id, status_msg), daemon=True).start()

@app.on_message(filters.command("start") & filters.private)
def start(client, message):
    if message.from_user.id not in ALLOWED_USERS:
        message.reply_text("Üzgünüm, bu botu kullanmaya yetkiniz yok.")
        return
    message.reply_text("Merhaba! Lütfen indirmek istediğiniz video/ses linkini veya arama sorgusunu gönderiniz.")

@app.on_message(filters.text & filters.private)
def handle_link(client, message):
    user_id = message.from_user.id
    if user_id not in ALLOWED_USERS:
        message.reply_text("Üzgünüm, bu botu kullanmaya yetkiniz yok.")
        return

    text = message.text.strip()

    # Eğer gönderilen metin bir URL içermiyorsa Youtube Data API V3 ile arama yap.
    if not re.search(r'https?://', text):
        try:
            data = search_youtube(text, max_results=20)
            items = data.get("items", [])
        except Exception as e:
            logger.error("Arama sırasında hata: %s", e)
            message.reply_text("Arama sırasında hata oluştu.")
            return

        if not items:
            message.reply_text("Arama sonucu bulunamadı.")
            return

        buttons = []
        for item in items:
            video_id = item.get("id", {}).get("videoId")
            title = item.get("snippet", {}).get("title")
            if not video_id or not title:
                continue
            if len(title) > 40:
                title = title[:40] + "..."
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            buttons.append([types.InlineKeyboardButton(text=title, callback_data="search|" + video_url)])

        keyboard = types.InlineKeyboardMarkup(buttons)
        message.reply_text("Arama sonuçları:", reply_markup=keyboard)
        return
    else:
        # Link gönderilmişse, LOG_CHANNEL'a kullanıcının adı ve id bilgileriyle birlikte log gönderiliyor.
        user = message.from_user
        username = f"@{user.username}" if user.username else user.first_name
        log_text = f"{text}\n{username} (ID: {user.id})"
        try:
            app.send_message(LOG_CHANNEL_ID, log_text)
        except Exception as e:
            logger.error("LOG_CHANNEL'a mesaj gönderilirken hata: %s", e)
            
        # Metin bir link içeriyorsa, linki kullan.
        status_msg = message.reply_text("Lütfen indirmek istediğiniz kaliteyi seçin:")
        prepare_video_info_and_show_quality(message.chat.id, user_id, text, status_msg=status_msg)

@app.on_callback_query(filters.regex(r"^search\|"))
def search_result_callback(client, callback_query):
    try:
        _, video_url = callback_query.data.split("|", 1)
    except Exception:
        callback_query.answer("Hatalı seçim!")
        return
    user_id = callback_query.from_user.id
    chat_id = callback_query.message.chat.id
    
    # Link tıklanmışsa, LOG_CHANNEL'a kullanıcının adı ve id bilgileriyle birlikte log gönderiliyor.
    username = f"@{callback_query.from_user.username}" if callback_query.from_user.username else callback_query.from_user.first_name
    log_text = f"{video_url}\n{username} (ID: {user_id})"
    try:
        app.send_message(LOG_CHANNEL_ID, log_text)
    except Exception as e:
        logger.error("LOG_CHANNEL'a mesaj gönderilirken hata: %s", e)
    
    try:
        client.edit_message_reply_markup(chat_id, callback_query.message.message_id, reply_markup=None)
    except Exception as e:
        logger.error("Inline keyboard temizlenirken hata: %s", e)
    prepare_video_info_and_show_quality(chat_id, user_id, video_url, status_msg=callback_query.message)

def process_task(user_id: int, download_type: str, selection: str, chat_id: int, status_msg: types.Message):
    """
    İşlem tamamlanınca kuyruğu kontrol edip sıradakini başlatır.
    İşlemin tüm aşamalarında (indirme, işleme, yükleme) tek bir mesaj (status_msg) güncellenecektir.
    """
    try:
        success = _process_task(user_id, download_type, selection, chat_id, status_msg)
    finally:
        check_next(user_id)
    if success:
        try:
            status_msg.delete()
        except Exception as e:
            logger.error("Mesaj silinirken hata: %s", e)

def _process_task(user_id: int, download_type: str, selection: str, chat_id: int, status_msg: types.Message) -> bool:
    user_data = user_video_info.get(user_id)
    if not user_data:
        app.send_message(chat_id, "İşlem bilgileri bulunamadı.")
        return False

    user_data["selection_made"] = True
    title = sanitize_filename(user_data.get("title"))
    duration = user_data.get("duration", 0)
    duration_str = format_duration(duration)

    if download_type == "video":
        file_ext = "mp4"
        download_file_name = f"{title}.{file_ext}"
        caption_file_name = f"{title}.{file_ext}"
        fmt_id = selection
        fmt_info = user_data["formats"].get(fmt_id)
        if not fmt_info:
            app.send_message(chat_id, "Seçilen format bilgileri bulunamadı.")
            return False
        quality_desc = fmt_info.get("desc")
        fmt_spec = f"{fmt_id}+bestaudio" if not fmt_info.get("has_audio") else fmt_id
        quality_line = f"Kalite: {quality_desc}, Format: mp4, Süre: {duration_str}"
        postprocessors = []
    elif download_type == "audio":
        bestaudio_info = user_data.get("bestaudio_info")
        if bestaudio_info is None:
            app.send_message(chat_id, "Ses format bilgisi bulunamadı.")
            return False
        file_ext = bestaudio_info.get("ext", "m4a")
        download_file_name = f"{title}"
        fmt_spec = "bestaudio"
        postprocessors = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "0"  # Orijinal kaliteyi korur
        }]
        quality_line = f"Kalite: en iyi, Format: mp3, Süre: {duration_str}"
        caption_file_name = f"{title}.mp3"
    else:
        app.send_message(chat_id, "Bilinmeyen tür.")
        return False

    caption = f"{caption_file_name}\n{quality_line}\n{user_data.get('url')}"

    try:
        status_msg.edit_text("İndirme başladı...")
    except Exception as e:
        logger.error("İndirme başlangıç mesajı güncellenemedi: %s", e)

    last_download_update = time.time()

    def progress_hook(d):
        nonlocal last_download_update
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate')
            downloaded = d.get('downloaded_bytes', 0)
            percent = (downloaded / total * 100) if total else 0
            eta = d.get('eta', 0)
            if time.time() - last_download_update >= PROGRESS_UPDATE_INTERVAL:
                last_download_update = time.time()
                try:
                    status_msg.edit_text(f"İndiriliyor: {percent:.2f}% - Kalan süre: {eta} sn")
                except Exception as e:
                    logger.error("İndirme güncelleme hatası: %s", e)
        elif d['status'] == 'finished':
            try:
                status_msg.edit_text("İndirme tamamlandı, dosya işleniyor...")
            except Exception as e:
                logger.error("İndirme bitiş mesajı güncelleme hatası: %s", e)

    ydl_opts = {
        'format': fmt_spec,
        'outtmpl': None,  # Daha sonra ayarlanacak
        'quiet': True,
        'no_warnings': True,
        'postprocessors': postprocessors,
        'cookiefile': 'cookies.txt' if os.path.exists("cookies.txt") else None,
        'logger': logger,
        'progress_hooks': [progress_hook]
    }
    if download_type == "video":
        ydl_opts["merge_output_format"] = "mp4"

    download_success = False
    try:
        with tempfile.TemporaryDirectory() as tmpdirname:
            file_path = os.path.join(tmpdirname, download_file_name)
            ydl_opts['outtmpl'] = file_path
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.extract_info(user_data.get("url"), download=True)
                download_success = True
                file_path = os.path.join(tmpdirname, caption_file_name)
            except Exception as e:
                logger.error("İndirme sırasında hata: %s", e)
                try:
                    status_msg.edit_text("İndirme sırasında hata oluştu.")
                except Exception as ex:
                    logger.error("Hata mesajı güncelleme hatası: %s", ex)
                return False

            if not download_success or not os.path.exists(file_path):
                try:
                    status_msg.edit_text("İndirilen dosya bulunamadı.")
                except Exception as e:
                    logger.error("Dosya bulunamadı mesajı güncelleme hatası: %s", e)
                return False

            # Thumbnail indirimi
            thumb_file_path = None
            thumb_url = user_data.get("thumbnail")
            if thumb_url:
                try:
                    # Eğer maxresdefault görünmüyorsa hqdefault deneyin
                    if "maxresdefault" in thumb_url:
                        test_resp = requests.get(thumb_url, timeout=10)
                        if test_resp.status_code != 200:
                            thumb_url = thumb_url.replace("maxresdefault", "hqdefault")

                    headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.93 Safari/537.36"
                    }
                    resp = requests.get(thumb_url, headers=headers, timeout=10)
                    if resp.status_code == 200 and len(resp.content) > 0:
                        thumb_file_path = os.path.join(tmpdirname, "thumb.jpg")
                        with open(thumb_file_path, "wb") as f:
                            f.write(resp.content)
                        
                        # Açıp, RGB formatına çevirip yeniden kaydediyoruz
                        try:
                            with Image.open(thumb_file_path) as img:
                                rgb_im = img.convert("RGB")
                                rgb_im.save(thumb_file_path, format="JPEG")
                        except Exception as e:
                            logger.error("Thumbnail dönüştürme hatası: %s", e)
                    else:
                        logger.warning("Thumbnail indirilemedi veya içerik boş. Status code: %s", resp.status_code)
                except Exception as e:
                    logger.error("Thumbnail indirilirken hata: %s", e)


            file_size = os.path.getsize(file_path)
            max_file_size = 2097152000
            if file_size > max_file_size:
                try:
                    status_msg.edit_text("Dosya 2GB'dan büyük, parçalara ayrılıyor...")
                except Exception as e:
                    logger.error("Parçalama mesajı güncelleme hatası: %s", e)
                if EQUAL_SPLIT:
                    num_parts = math.ceil(file_size / max_file_size)
                    part_size = math.ceil(file_size / num_parts)
                else:
                    part_size = max_file_size
                part_prefix = os.path.splitext(caption_file_name)[0]
                part_ext = os.path.splitext(caption_file_name)[1]
                output_prefix = os.path.join(tmpdirname, part_prefix + ".part")
                cmd = [
                    "split",
                    "-b", str(part_size),
                    "--numeric-suffixes=1",
                    "--additional-suffix=" + part_ext,
                    file_path,
                    output_prefix
                ]
                try:
                    subprocess.run(cmd, check=True)
                except Exception as e:
                    logger.error("Dosya parçalara ayrılırken hata: %s", e)
                    try:
                        status_msg.edit_text("Dosya parçalara ayrılırken hata oluştu.")
                    except Exception as ex:
                        logger.error("Hata mesajı güncelleme hatası: %s", ex)
                    return False
                part_files = sorted(glob.glob(os.path.join(tmpdirname, f"{part_prefix}.part*{part_ext}")))
                if not part_files:
                    try:
                        status_msg.edit_text("Parçalanmış dosyalar bulunamadı.")
                    except Exception as e:
                        logger.error("Parçalanmış dosya bulunamadı mesajı güncelleme hatası: %s", e)
                    return False

                try:
                    status_msg.edit_text("Yükleme başlatılıyor (parçalı)...")
                except Exception as e:
                    logger.error("Yükleme başlatma mesajı güncelleme hatası: %s", e)
                start_time = time.time()
                last_upload_update = time.time()

                def upload_progress(current, total):
                    nonlocal last_upload_update
                    percent = (current / total * 100) if total else 0
                    elapsed = time.time() - start_time
                    eta = (elapsed / current * (total - current)) if current else 0
                    if time.time() - last_upload_update >= PROGRESS_UPDATE_INTERVAL:
                        last_upload_update = time.time()
                        try:
                            status_msg.edit_text(f"Yükleniyor: {percent:.2f}% - Kalan süre: {int(eta)} sn")
                        except Exception as e:
                            logger.error("Yükleme güncelleme hatası: %s", e)

                for part in part_files:
                    try:
                        if download_type == "video":
                            sent = app.send_video(
                                chat_id=chat_id,
                                video=part,
                                caption=caption,
                                duration=duration,
                                progress=upload_progress,
                                thumb=thumb_file_path
                            )
                        else:
                            sent = app.send_audio(
                                chat_id=chat_id,
                                audio=part,
                                caption=caption,
                                duration=duration,
                                progress=upload_progress,
                                thumb=thumb_file_path
                            )
                        try:
                            app.forward_messages(LOG_CHANNEL_ID, chat_id, sent.id)
                        except Exception as e:
                            logger.error("Parça log mesajı gönderilemedi: %s", e)
                    except Exception as e:
                        logger.error("Parça gönderimi sırasında hata: %s", e)
                        try:
                            status_msg.edit_text("Dosya parça gönderilirken hata oluştu.")
                        except Exception as ex:
                            logger.error("Hata mesajı güncelleme hatası: %s", ex)
                        return False
            else:
                try:
                    status_msg.edit_text("Yükleme başlatılıyor...")
                except Exception as e:
                    logger.error("Yükleme başlatma mesajı güncelleme hatası: %s", e)
                start_time = time.time()
                last_upload_update = time.time()

                def upload_progress(current, total):
                    nonlocal last_upload_update
                    percent = (current / total * 100) if total else 0
                    elapsed = time.time() - start_time
                    eta = (elapsed / current * (total - current)) if current else 0
                    if time.time() - last_upload_update >= PROGRESS_UPDATE_INTERVAL:
                        last_upload_update = time.time()
                        try:
                            status_msg.edit_text(f"Yükleniyor: {percent:.2f}% - Kalan süre: {int(eta)} sn")
                        except Exception as e:
                            logger.error("Yükleme güncelleme hatası: %s", e)

                try:
                    if download_type == "video":
                        sent = app.send_video(
                            chat_id=chat_id,
                            video=file_path,
                            caption=caption,
                            duration=duration,
                            progress=upload_progress,
                            thumb=thumb_file_path
                        )
                    else:
                        sent = app.send_audio(
                            chat_id=chat_id,
                            audio=file_path,
                            caption=caption,
                            duration=duration,
                            progress=upload_progress,
                            thumb=thumb_file_path
                        )
                    try:
                        app.forward_messages(LOG_CHANNEL_ID, chat_id, sent.id)
                    except Exception as e:
                        logger.error("Yükleme log mesajı gönderilemedi: %s", e)
                except Exception as e:
                    logger.error("Gönderim sırasında hata: %s", e)
                    try:
                        status_msg.edit_text("Dosya gönderilirken hata oluştu.")
                    except Exception as ex:
                        logger.error("Hata mesajı güncelleme hatası: %s", ex)
                    return False
    except Exception as e:
        logger.error("İşlem sırasında beklenmeyen hata: %s", e)
        try:
            status_msg.edit_text("İşlem sırasında beklenmeyen hata oluştu.")
        except Exception as ex:
            logger.error("Hata mesajı güncelleme hatası: %s", ex)
        return False

    return True

def check_next(user_id: int):
    """Kuyruktaki işi başlatır; eğer kuyruk boşsa busy durumunu kapatır."""
    queue = user_queue.get(user_id, [])
    if queue:
        next_task = queue.pop(0)
        user_video_info[user_id] = next_task["data"]
        process_task(user_id, next_task["download_type"], next_task["selection"], next_task["chat_id"], next_task["status_msg"])
        if not queue:
            user_queue.pop(user_id, None)
    else:
        user_busy[user_id] = False
        user_video_info.pop(user_id, None)

@app.on_callback_query()
def quality_chosen(client, callback_query):
    if callback_query.data == "ignore":
        callback_query.answer()
        return
    try:
        download_type, selection = callback_query.data.split("|")
    except Exception:
        callback_query.answer("Geçersiz seçim.")
        return
    user_id = callback_query.from_user.id
    chat_id = callback_query.message.chat.id
    if user_busy.get(user_id, False):
        queue = user_queue.setdefault(user_id, [])
        status_msg = app.send_message(chat_id, f"Devam eden işlemin tamamlanması bekleniyor, sıranız: {len(queue)+1}")
        task = {
            "download_type": download_type,
            "selection": selection,
            "chat_id": chat_id,
            "data": copy.deepcopy(user_video_info[user_id]),
            "status_msg": status_msg
        }
        queue.append(task)
        try:
            client.edit_message_reply_markup(chat_id, callback_query.message.message_id, reply_markup=None)
        except Exception as e:
            logger.error("Kalite seçim mesajı temizlenirken hata: %s", e)
        return
    else:
        user_busy[user_id] = True
        try:
            client.edit_message_reply_markup(chat_id, callback_query.message.message_id, reply_markup=None)
        except Exception as e:
            logger.error("Kalite seçim mesajı temizlenirken hata: %s", e)
        callback_query.answer("İşleminiz başlatıldı.")
        threading.Thread(target=process_task, args=(user_id, download_type, selection, chat_id, callback_query.message), daemon=True).start()

if __name__ == "__main__":
    logger.info("Bot çalışmaya başladı...")
    app.run()
