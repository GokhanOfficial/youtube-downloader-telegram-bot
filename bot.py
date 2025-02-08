import os
import sys
import re
import time
import logging
import tempfile
import yt_dlp
import ffmpeg
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
    OWNER_ID,
    ALLOWED_USERS,
    COOKIES_URL,
    PROGRESS_UPDATE_INTERVAL,
    LOG_CHANNEL_ID,
    EQUAL_SPLIT,
    YOUTUBE_API_KEY,
    AV1_FOR_LOWRES,
    AV1_FOR_HIGHRES    # Yeni: Youtube Data API anahtarı
)
import json

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
            logger.info("Kalite seçilmedi, işlem iptal edildi")
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

# Inline modda arama sorgularını işleyen örnek handler:
@app.on_inline_query()
def inline_query_handler(client, query: types.InlineQuery):
    search_text = query.query.strip()
    if not search_text:
        # Eğer sorgu boşsa boş sonuç döndürüyoruz.
        query.answer([], cache_time=0)
        return

    try:
        # search_youtube fonksiyonu kullanılarak arama yapılıyor.
        data = search_youtube(search_text, max_results=10)
        items = data.get("items", [])
    except Exception as e:
        logger.error("Inline arama sırasında hata: %s", e)
        query.answer([], switch_pm_text="Arama sırasında hata oluştu.", switch_pm_parameter="start")
        return

    results = []
    for item in items:
        video_id = item.get("id", {}).get("videoId")
        snippet = item.get("snippet", {})
        title = snippet.get("title")
        description = snippet.get("description", "")
        if not video_id or not title:
            continue

        # Eğer başlık çok uzun ise kısaltıyoruz.
        if len(title) > 40:
            title = title[:40] + "..."

        # InlineQueryResultArticle kullanarak bir sonuç oluşturuyoruz.
        result = types.InlineQueryResultArticle(
            id=video_id,
            title=title,
            description=description,
            input_message_content=types.InputTextMessageContent(
                message_text=f"https://www.youtube.com/watch?v={video_id}"
            )
        )
        results.append(result)

    query.answer(results, cache_time=0)

def check_disk_space(required_space: int) -> bool:
    """Check if there is at least twice the required space available on the disk."""
    statvfs = os.statvfs('/')
    free_space = statvfs.f_frsize * statvfs.f_bavail
    return free_space >= required_space * 2

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
                "desc": desc,
                "filesize": filesize
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
            "ext": bestaudio.get("ext"),
            "filesize": size
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

@app.on_message(filters.command("restart") & filters.private)
def restart_bot(client, message):
    # Yetkili kullanıcı kontrolü
    if message.from_user.id != OWNER_ID:
        message.reply_text("Bu komutu kullanmaya yetkiniz yok.")
        return

    message.reply_text("Bot yeniden başlatılıyor...")
    # Kısa bir süre uyutup, ardından botu yeniden başlatıyoruz.
    # os.execv() mevcut process'i tamamen yeni process ile değiştirir.
    try:
        # Öncelikle mesajın gönderilmesi için kısa bir gecikme ekleyelim.
        time.sleep(2)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        logger.error("Bot yeniden başlatılırken hata oluştu: %s", e)
        message.reply_text(f"Bot yeniden başlatılırken hata oluştu: {e}")

def get_free_space_gb() -> float:
    """Get the available disk space in GB."""
    statvfs = os.statvfs('/')
    free_space = statvfs.f_frsize * statvfs.f_bavail
    return free_space / (1024 ** 3)

@app.on_message(filters.command("free") & filters.private)
def free_space(client, message):
    if message.from_user.id not in ALLOWED_USERS:
        message.reply_text("Üzgünüm, bu botu kullanmaya yetkiniz yok.")
        return

    free_space_gb = get_free_space_gb()
    message.reply_text(f"Diskte {free_space_gb:.2f} GB boş alan var.")

def save_allowed_users():
    with open("config.py", "r") as f:
        lines = f.readlines()
    with open("config.py", "w") as f:
        for line in lines:
            if line.startswith("ALLOWED_USERS"):
                f.write(f"ALLOWED_USERS = {json.dumps(list(ALLOWED_USERS))}\n")
            else:
                f.write(line)

@app.on_message(filters.command("sudo") & filters.private)
def sudo_user(client, message):
    if message.from_user.id != OWNER_ID:
        message.reply_text("Bu komutu kullanmaya yetkiniz yok.")
        return

    try:
        user_id = int(message.command[1])
        if user_id in ALLOWED_USERS:
            message.reply_text("Bu kullanıcı zaten yetkili.")
        else:
            ALLOWED_USERS.add(user_id)
            save_allowed_users()
            message.reply_text(f"Kullanıcı {user_id} yetkilendirildi.")
            logger.info(f"Kullanıcı {user_id} yetkilendirildi.")
    except (IndexError, ValueError):
        message.reply_text("Geçerli bir kullanıcı ID'si girin.")

@app.on_message(filters.command("unsudo") & filters.private)
def unsudo_user(client, message):
    if message.from_user.id != OWNER_ID:
        message.reply_text("Bu komutu kullanmaya yetkiniz yok.")
        return

    try:
        user_id = int(message.command[1])
        if user_id not in ALLOWED_USERS:
            message.reply_text("Bu kullanıcı zaten yetkili değil.")
        else:
            ALLOWED_USERS.remove(user_id)
            save_allowed_users()
            message.reply_text(f"Kullanıcı {user_id} yetkisi kaldırıldı.")
            logger.info(f"Kullanıcı {user_id} yetkisi kaldırıldı.")
    except (IndexError, ValueError):
        message.reply_text("Geçerli bir kullanıcı ID'si girin.")

def download_direct_link(url: str, output_path: str, status_msg: types.Message):
    try:
        cmd = ["curl", "--progress-bar", "--no-buffer", "-L", "-o", output_path, url]
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        start_time = time.time()
        last_progress_update = time.time()
        pattern = re.compile(r'(\d+(?:\.\d+)?)%')
        eta_pattern = re.compile(r'eta\s+(\d+)\s*s', re.IGNORECASE)

        while process.poll() is None:
            line = process.stderr.readline().strip()
            if not line:
                continue
            m = pattern.search(line)
            if m:
                try:
                    percent = float(m.group(1))
                    elapsed = time.time() - start_time
                    if percent > 0:
                        eta = elapsed * (100 - percent) / percent
                    else:
                        eta = 0
                except Exception as e:
                    logger.error("Yüzde ve ETA hesaplama hatası: %s", e)
                    percent = 0
                if time.time() - last_progress_update >= PROGRESS_UPDATE_INTERVAL:
                    last_progress_update = time.time()
                    try:
                        logger.info(f"İndiriliyor: {percent:.2f}% - Kalan süre: {int(eta)} sn")
                        status_msg.edit_text(f"İndiriliyor: {percent:.2f}% - Kalan süre: {int(eta)} sn")
                    except Exception as e:
                        logger.error("İndirme güncelleme hatası: %s", e)
        return process.returncode == 0
    except Exception as e:
        logger.error("Direct download failed: %s", e)
        return False

def is_thumb_avaible(thumb_path):
    try:
        if not os.path.exists(thumb_path):
            logger.info("Thumbnail mevcut değil.")
            return False

        with Image.open(thumb_path) as img:
            img.verify()
            if img.format.upper() == "JPEG":
                logger.info("Thumbnail dosyası geçerli.")
                return True
            else: return False
    except Exception as e:
        logger.info("Thumbnail dosyası geçerli JPEG değil, işlem devam ediyor: %s", e)
        return False
        



def extract_thumbnail(video_path, thumb_path, timestamp="00:00:10"):
    try:
        # FFmpeg ile thumbnail oluştur
        (
            ffmpeg
            .input(video_path, ss=timestamp)  # 10. saniyeden itibaren başla
            .output(thumb_path, vframes=1)  # Tek bir kare al
            .run(overwrite_output=True, capture_stdout=True, capture_stderr=True)  # Sessiz çalıştır
        )

        # Oluşan dosyanın var olup olmadığını kontrol et
        if not os.path.exists(thumb_path):
            logger.error("Thumbnail oluşturulamadı: %s", e)
            return False
        
        # PIL ile görüntüyü açıp RGB olarak tekrar kaydet
        with Image.open(thumb_path) as img:
            rgb_im = img.convert("RGB")
            rgb_im.save(thumb_path, format="JPEG")
        
        logger.info("Thumbnail ffmpeg ile üretildi")
        return True  # Başarıyla tamamlandı

    except Exception as e:
        logger.error("Thumbnail oluşturulurken hata: %s", e)
        return False  # Hata oluştuysa başarısız olduğunu döndür

def upload_file(
    file_path,
    status_msg,
    download_type,
    chat_id,
    caption,
    duration,
    caption_file_name=None,
    tmpdirname=None,
    thumb_file_path=None,
    max_file_size=2097152000,
):
    # Geçici dizin ve dosya adını ayarla
    if tmpdirname is None:
        tmpdirname = os.path.dirname(file_path)
    if caption_file_name is None:
        caption_file_name = os.path.basename(file_path)
    if thumb_file_path is None:
        thumb_file_path = os.path.join(tmpdirname, os.path.splitext(caption_file_name)[0]+".jpg")

    try:
        file_size = os.path.getsize(file_path)
    except Exception as e:
        logger.error("Dosya boyutu alınamadı: %s", e)
        try:
            status_msg.edit_text("Dosya boyutu alınamadı.")
        except Exception as ex:
            logger.error("Status mesajı güncelleme hatası: %s", ex)
        return False

    if download_type == "video" and not is_thumb_avaible(thumb_file_path):
        if(extract_thumbnail(file_path,thumb_file_path)):
            logger.info("Thumbnail oluşturuldu")

    # Dosya parçalara ayrılacak mı kontrolü
    if file_size > max_file_size:
        try:
            logger.info("Dosya 2GB'dan büyük, parçalara ayrılıyor...")
            status_msg.edit_text("Dosya 2GB'dan büyük, parçalara ayrılıyor...")
        except Exception as e:
            logger.error("Parçalama mesajı güncelleme hatası: %s", e)

        if EQUAL_SPLIT:
            num_parts = math.ceil(file_size / max_file_size)
            part_size = math.ceil(file_size / num_parts)  # Her parçanın eşit büyüklüğü
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
                logger.error("Parçalanmış dosyalar bulunamadı.\n" + file_path)
                status_msg.edit_text("Parçalanmış dosyalar bulunamadı.")
            except Exception as e:
                logger.error("Parçalanmış dosya bulunamadı mesajı güncelleme hatası: %s", e)
            return False

        total_parts = len(part_files)
        try:
            logger.info("Yükleme başlatılıyor (parçalı)...")
            status_msg.edit_text("Yükleme başlatılıyor (parçalı)...")
        except Exception as e:
            logger.error("Yükleme başlatma mesajı güncelleme hatası: %s", e)

        start_time = time.time()
        last_progress_update = time.time()

        def upload_progress(current, total):
            nonlocal last_progress_update
            percent = (current / total * 100) if total else 0
            elapsed = time.time() - start_time
            eta = (elapsed / current * (total - current)) if current else 0
            if time.time() - last_progress_update >= PROGRESS_UPDATE_INTERVAL:
                last_progress_update = time.time()
                try:
                    logger.info(f"Yükleniyor: {percent:.2f}% - Kalan süre: {int(eta)} sn")
                    status_msg.edit_text(f"Yükleniyor: {percent:.2f}% - Kalan süre: {int(eta)} sn")
                except Exception as e:
                    logger.error("Yükleme güncelleme hatası: %s", e)

        # Parçaları teker teker yükle
        for i, part in enumerate(part_files, start=1):
            overall_progress = (i / total_parts) * 100
            try:
                logger.info(f"Parçaların {overall_progress:.2f}%'si hazırlandı ve yükleniyor...")
                status_msg.edit_text(f"Parçaların {overall_progress:.2f}%'si hazırlandı ve yükleniyor...")
            except Exception as e:
                logger.error("Genel ilerleme güncelleme hatası: %s", e)
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
                    logger.info("İndirilen dosya kanala iletildi")
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
        # Dosya 2GB'dan küçükse doğrudan yükleme
        try:
            logger.info("Yükleme başlatılıyor...")
            status_msg.edit_text("Yükleme başlatılıyor...")
        except Exception as e:
            logger.error("Yükleme başlatma mesajı güncelleme hatası: %s", e)
        start_time = time.time()
        last_progress_update = time.time()

        def upload_progress(current, total):
            nonlocal last_progress_update
            percent = (current / total * 100) if total else 0
            elapsed = time.time() - start_time
            eta = (elapsed / current * (total - current)) if current else 0
            if time.time() - last_progress_update >= PROGRESS_UPDATE_INTERVAL:
                last_progress_update = time.time()
                try:
                    logger.info(f"Yükleniyor: {percent:.2f}% - Kalan süre: {int(eta)} sn")
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
                logger.info("İndirilen dosya kanala iletildi")
            except Exception as e:
                logger.error("Yükleme log mesajı gönderilemedi: %s", e)
        except Exception as e:
            logger.error("Gönderim sırasında hata: %s", e)
            try:
                status_msg.edit_text("Dosya gönderilirken hata oluştu.")
            except Exception as ex:
                logger.error("Hata mesajı güncelleme hatası: %s", ex)
            return False
    return True

@app.on_message(filters.text & filters.private)
def handle_link(client, message):
    user_id = message.from_user.id
    if user_id not in ALLOWED_USERS:
        message.reply_text("Üzgünüm, bu botu kullanmaya yetkiniz yok.")
        return

    text = message.text.strip()
    direct_download_extensions = (".mkv", ".mp4", ".mp3", ".m4a", ".avi", ".flv")

    if any(text.lower().endswith(ext) for ext in direct_download_extensions):
        # Handle direct download links
        status_msg = message.reply_text("Dosya indiriliyor...")
        with tempfile.TemporaryDirectory() as tmpdirname:
            file_name = sanitize_filename(os.path.basename(text))
            file_path = os.path.join(tmpdirname, file_name)
            # Check if there is enough disk space for the file
            file_size = int(requests.head(text).headers.get('content-length', 0))
            if not check_disk_space(file_size):
                status_msg.edit_text("Sistem hatası, yeterli disk alanı mevcut değil.")
                return
            if download_direct_link(text, file_path, status_msg):
                try:
                    try:
                        probe = ffmpeg.probe(file_path)
                        duration = int(float(probe['format']['duration']))
                        logger.info ("Yüklenecek dosya %s saniye", duration)
                    except Exception as e:
                        logger.error("Dosya süresi ffmpeg ile hesaplanamadı: %s", e)
                        duration = 0
                    
                    duration_str = format_duration(duration)
                    # Dosya boyutunu MB cinsine çevir
                    try:
                        real_file_size = os.path.getsize(file_path) / (1024 * 1024)  # MB cinsine çevrildi
                        real_file_size_str = f"{real_file_size:,.2f} MB"  # Nokta yerine virgül ile formatlama
                        logger.info("Yüklenecek dosya %s", real_file_size_str)
                    except Exception as e:
                        logger.error("Dosya boyutu hesaplanamadı: %e", e)
                        real_file_size_str = 0

                    quality_line = f"Boyut: {real_file_size_str}, Format: {os.path.splitext(file_path)[1][1:]}, Süre: {duration_str}"
                    caption = f"{file_name}\n{quality_line}\n{text}"

                    if text.lower().endswith((".mkv", ".mp4", ".avi", ".flv")):
                        download_type = "video"
                    else:
                        download_type = "audio"

                    logger.info(f"{file_path} yüklenmeye başlıyor.")
                    if(upload_file(file_path, status_msg, download_type, message.chat.id, caption, duration, file_name, tmpdirname)):
                        logger.info("Dosya yüklendi")
                except Exception as e:
                    logger.error("Dosya yüklenirken hata: %s", e)
                    status_msg.edit_text("Dosya yüklenirken hata oluştu.")
            else:
                status_msg.edit_text("Dosya indirilemedi.")
        return

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
        resolution = quality_desc.split(" - ")[0]
        fmt_spec = f"{fmt_id}+bestaudio" if not fmt_info.get("has_audio") else fmt_id
        postprocessors = []
        required_space = fmt_info.get("filesize", 0)
    elif download_type == "audio":
        bestaudio_info = user_data.get("bestaudio_info")
        if bestaudio_info is None:
            app.send_message(chat_id, "Ses format bilgisi bulunamadı.")
            return False
        file_ext = bestaudio_info.get("ext", "m4a")
        download_file_name = f"{title}"
        resolution = "en iyi"
        fmt_spec = "bestaudio"
        postprocessors = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "0"  # Orijinal kaliteyi korur
        }]
        caption_file_name = f"{title}.mp3"
        required_space = bestaudio_info.get("filesize", 0)
    else:
        app.send_message(chat_id, "Bilinmeyen tür.")
        return False

    if not check_disk_space(required_space):
        logger.error("Sistem hatası, yeterli disk alanı mevcut değil.")
        app.send_message(chat_id, "Sistem hatası, yeterli disk alanı mevcut değil.")
        return False

    try:
        logger.info("İndirme başladı...")
        status_msg.edit_text("İndirme başladı...")
    except Exception as e:
        logger.error("İndirme başlangıç mesajı güncellenemedi: %s", e)

    last_progress_update = time.time()

    def progress_hook(d):
        nonlocal last_progress_update
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate')
            downloaded = d.get('downloaded_bytes', 0)
            percent = (downloaded / total * 100) if total else 0
            eta = d.get('eta', 0)
            if time.time() - last_progress_update >= PROGRESS_UPDATE_INTERVAL:
                last_progress_update = time.time()
                try:
                    logger.info(f"İndiriliyor: {percent:.2f}% - Kalan süre: {eta} sn")
                    status_msg.edit_text(f"İndiriliyor: {percent:.2f}% - Kalan süre: {eta} sn")
                except Exception as e:
                    logger.error("İndirme güncelleme hatası: %s", e)
        elif d['status'] == 'finished':
            try:
                logger.info("İndirme tamamlandı, dosya işleniyor...")
                status_msg.edit_text(f"İndirme tamamlandı, dosya işleniyor...")
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
    with tempfile.TemporaryDirectory() as tmpdirname:
        try:
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
                    logger.info ("İndirilen dosya bulunamadı.\n"+file_path)
                    status_msg.edit_text("İndirilen dosya bulunamadı.")
                except Exception as e:
                    logger.error("Dosya bulunamadı mesajı güncelleme hatası: %s", e)
                return False

            # Thumbnail indirimi
            thumb_file_path = None
            thumb_url = user_data.get("thumbnail")
            if thumb_url:
                try:
                    thumb_file_path = os.path.join(tmpdirname, os.path.splitext(caption_file_name)[0]+".jpg")

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
                        with open(thumb_file_path, "wb") as f:
                            f.write(resp.content)
                        logger.info("Thumbnail yt-dlp ile indirildi. %s", thumb_file_path)
                    else:
                        logger.warning("Thumbnail indirilemedi veya içerik boş. Status code: %s", resp.status_code)
                        thumb_file_path = None

                    # Açıp, RGB formatına çevirip yeniden kaydediyoruz
                    try:
                        with Image.open(thumb_file_path) as img:
                            rgb_im = img.convert("RGB")
                            rgb_im.save(thumb_file_path, format="JPEG")
                    except Exception as e:
                        logger.error("Thumbnail dönüştürme hatası: %s", e)
                except Exception as e:
                    logger.error("Thumbnail indirilirken hata: %s", e)

            try:
                real_file_size = os.path.getsize(file_path) / (1024 * 1024)  # MB cinsine çevrildi
                real_file_size_str = f"{real_file_size:,.2f} MB"  # Nokta yerine virgül ile formatlama
                logger.info("Yüklenecek dosya %s", real_file_size_str)
            except Exception as e:
                logger.error("Dosya boyutu hesaplanamadı: %e", e)
                real_file_size_str = "0 MB"
            quality_line = f"Kalite: {resolution}, Boyut: {real_file_size_str} Format: {os.path.splitext(caption_file_name)[1][1:]}, Süre: {duration_str}"
            caption = f"{caption_file_name}\n{quality_line}\n{user_data.get('url')}"

            if(upload_file(file_path, status_msg, download_type, chat_id, caption, duration, caption_file_name, tmpdirname, thumb_file_path)):
                        logger.info("Dosya yüklendi")
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
        logger.info("Geçersiz seçim.")
        callback_query.answer("Geçersiz seçim.")
        return
    user_id = callback_query.from_user.id
    chat_id = callback_query.message.chat.id
    if user_busy.get(user_id, False):
        queue = user_queue.setdefault(user_id, [])
        logger.info(f"Devam eden işlemin tamamlanması bekleniyor, sıranız: {len(queue)+1}")
        status_msg = app.send_message(chat_id, f"Devam eden işlemin tamamlanması bekleniyor, sıranız: {len(queue)+1}")
        task = {
            "download_type": download_type,
            "selection": selection,
            "chat_id": chat_id,
            "data": copy.deepcopy(user_video_info[user_id]),
            "status_msg": status_msg
        }
        queue.append(task)
    else:
        user_busy[user_id] = True
        logger.info("İşleminiz başlatıldı...")
        callback_query.answer("İşleminiz başlatıldı...")
        threading.Thread(target=process_task, args=(user_id, download_type, selection, chat_id, callback_query.message), daemon=True).start()

if __name__ == "__main__":
    logger.info("Bot çalışmaya başladı...")
    app.run()
