import telebot
from PIL import Image, ImageDraw, ImageChops, ImageOps
from PIL.ExifTags import TAGS, GPSTAGS
import io
import logging
import os
import requests
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
import html
import folium
import tempfile
from datetime import datetime
import threading
import time
import exifread
import numpy as np
from cachetools import TTLCache
import re
import piexif
from hachoir.parser import createParser
from hachoir.metadata import extractMetadata
import warnings

# Игнорируем предупреждения hachoir
warnings.filterwarnings("ignore", category=UserWarning)

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация бота
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7858198753:AAFKpGKhF8ouWLpK6mGN7sFDYLZWm972zo4")
bot = telebot.TeleBot(TOKEN)
geolocator = Nominatim(user_agent="geoapiExercises")
geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1.5)
SUPPORTED_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.heic', '.tiff', '.webp']
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB

# Глобальные переменные
user_data = {}
geo_cache = TTLCache(maxsize=1000, ttl=3600)  # Кэш на 1 час
cache_lock = threading.Lock()

# Функции для конвертации координат и геолокации
def convert_to_degrees(value):
    """Конвертирует координаты в градусы"""
    try:
        if isinstance(value, tuple) and len(value) == 3:
            d, m, s = value
            return d + (m / 60.0) + (s / 3600.0)
        elif isinstance(value, exifread.classes.IfdTag):
            d = value.values[0].decimal()
            m = value.values[1].decimal()
            s = value.values[2].decimal()
            return d + (m / 60.0) + (s / 3600.0)
        else:
            return float(value)
    except Exception as e:
        logger.error(f"Coordinate conversion error: {e}")
        return None

def get_location_info(lat, lon):
    """Получает информацию о местоположении"""
    cache_key = f"{lat:.6f},{lon:.6f}"
    
    with cache_lock:
        if cache_key in geo_cache:
            return geo_cache[cache_key]
    
    try:
        location = geolocator.reverse(f"{lat}, {lon}", language='ru', timeout=15)
        if location:
            result = {
                'address': location.address,
                'details': location.raw.get('display_name', '')
            }
            with cache_lock:
                geo_cache[cache_key] = result
            return result
    except Exception as e:
        logger.error(f"Geocoding error: {e}")
        try:
            # Попробуем резервный геокодер
            from geopy.geocoders import Photon
            backup_geolocator = Photon(user_agent="geo_backup")
            location = backup_geolocator.reverse(f"{lat}, {lon}", language='ru', timeout=10)
            if location:
                result = {
                    'address': location.address,
                    'details': location.raw.get('display_name', '')
                }
                with cache_lock:
                    geo_cache[cache_key] = result
                return result
        except Exception as backup_e:
            logger.error(f"Backup geocoding error: {backup_e}")
    
    return {'address': "Местоположение не определено", 'details': ""}

def get_landmark(lat, lon):
    """Находит ближайшую достопримечательность"""
    cache_key = f"landmark_{lat:.6f},{lon:.6f}"
    
    with cache_lock:
        if cache_key in geo_cache:
            return geo_cache[cache_key]
    
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=18&addressdetails=1"
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        if 'address' in data:
            address = data['address']
            landmark = address.get('tourism') or address.get('historic') or address.get('amenity')
            result = landmark if landmark else address.get('road', '') + ', ' + address.get('city', address.get('town', ''))
            
            with cache_lock:
                geo_cache[cache_key] = result
            return result
    except Exception as e:
        logger.error(f"Landmark search error: {e}")
    
    return "Достопримечательность не найдена"

# Функции для работы со статусными сообщениями
def update_status_message(user_id, new_status):
    """Обновляет статусное сообщение"""
    try:
        if user_id not in user_data or 'status_message' not in user_data[user_id]:
            return

        msg_data = user_data[user_id]['status_message']
        
        # Проверяем, изменился ли текст
        if msg_data.get('last_edited_text') == new_status:
            return
            
        msg_data['status'] = new_status
        
        try:
            bot.edit_message_text(
                chat_id=msg_data['chat_id'],
                message_id=msg_data['message_id'],
                text=new_status,
                parse_mode='Markdown'
            )
            # Сохраняем последний отправленный текст
            msg_data['last_edited_text'] = new_status
        except Exception as e:
            logger.error(f"Error updating status: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in update_status_message: {e}")

def create_status_message(user_id, chat_id):
    """Создает статусное сообщение"""
    text = "🔍 *Анализ изображения начат...*\n\n"
    text += "• `[ ]` Извлечение метаданных\n"
    text += "• `[ ]` Поиск геолокации\n"
    text += "• `[ ]` Анализ местоположения\n"
    text += "• `[ ]` Проверка на редактирование\n\n"
    text += "_Подождите, это может занять некоторое время..._"
    
    try:
        msg = bot.send_message(chat_id, text, parse_mode='Markdown')
        user_data[user_id]['status_message'] = {
            'chat_id': chat_id,
            'message_id': msg.message_id,
            'status': text,
            'last_edited_text': text,
            'last_update': time.time()
        }
        return msg.message_id
    except Exception as e:
        logger.error(f"Error creating status message: {e}")
        return None

def update_status_step(user_id, step_name, status="progress", message=""):
    """Обновляет конкретный шаг в статусном сообщении"""
    if user_id not in user_data or 'status_message' not in user_data[user_id]:
        return
    
    status_data = user_data[user_id]['status_message']
    
    # Добавляем задержку между обновлениями
    current_time = time.time()
    if current_time - status_data['last_update'] < 0.5:
        return
    
    status_symbols = {
        "completed": "✅",
        "failed": "❌",
        "progress": "🔄",
        "waiting": "⏳"
    }
    symbol = status_symbols.get(status, " ")
    
    steps = {
        "metadata": "Извлечение метаданных",
        "geolocation": "Поиск геолокации",
        "location_analysis": "Анализ местоположения",
        "manipulation_check": "Проверка на редактирование"
    }
    
    text = status_data['status']
    for key, name in steps.items():
        if key == step_name:
            # Поиск существующей строки для обновления
            pattern = re.compile(rf"•\s`\[.\]`\s{name}.*")
            new_line = f"• `{symbol}` {name}"
            if message:
                new_line += f" - {message}"
                
            if pattern.search(text):
                text = pattern.sub(new_line, text)
            else:
                text = text.replace(f"• `[ ]` {name}", new_line)
    
    user_data[user_id]['status_message']['status'] = text
    user_data[user_id]['status_message']['last_update'] = current_time
    update_status_message(user_id, text)

# Функции для анализа изображения
def check_image_manipulation(image_bytes):
    """Проверка признаков редактирования фото"""
    try:
        original = Image.open(io.BytesIO(image_bytes))
        
        # Уменьшаем большие изображения для оптимизации
        if max(original.size) > 2048:
            original.thumbnail((1024, 1024), Image.LANCZOS)
        
        compressed_buffer = io.BytesIO()
        original.save(compressed_buffer, "JPEG", quality=90)
        compressed_buffer.seek(0)
        compressed = Image.open(compressed_buffer)
        
        ela_image = ImageChops.difference(original, compressed)
        ela_image = ImageOps.autocontrast(ela_image)
        
        grayscale = ela_image.convert("L")
        pixels = np.array(grayscale)
        mean_intensity = np.mean(pixels)
        
        ela_buffer = io.BytesIO()
        ela_image.save(ela_buffer, format='JPEG')
        
        return {
            'ela_score': mean_intensity,
            'is_edited': mean_intensity > 25,
            'ela_image': ela_buffer.getvalue()
        }
    except Exception as e:
        logger.error(f"ELA analysis failed: {e}")
        return None

def extract_gps_from_exif(exif_data):
    """Извлекает GPS координаты из EXIF данных Pillow"""
    try:
        if 34853 in exif_data:  # GPSInfo tag
            gps_info = exif_data[34853]
            lat = convert_to_degrees(gps_info[2])
            lon = convert_to_degrees(gps_info[4])
            
            if lat is None or lon is None:
                return None, None
                
            if gps_info[1] == 'S': lat = -lat
            if gps_info[3] == 'W': lon = -lon
            
            return lat, lon
    except Exception as e:
        logger.error(f"GPS extraction error: {e}")
    
    return None, None

def extract_gps_from_exifread(tags):
    """Извлекает GPS координаты с помощью exifread"""
    try:
        if 'GPS GPSLatitude' in tags and 'GPS GPSLongitude' in tags:
            lat = convert_to_degrees(tags['GPS GPSLatitude'])
            lon = convert_to_degrees(tags['GPS GPSLongitude'])
            
            if lat is None or lon is None:
                return None, None
                
            lat_ref = str(tags.get('GPS GPSLatitudeRef', 'N')).strip()
            lon_ref = str(tags.get('GPS GPSLongitudeRef', 'E')).strip()
            
            if lat_ref == 'S': lat = -lat
            if lon_ref == 'W': lon = -lon
            
            return lat, lon
    except Exception as e:
        logger.error(f"Exifread GPS extraction error: {e}")
    
    return None, None

def extract_metadata_advanced(image_bytes):
    """Извлекает метаданные всеми доступными способами"""
    metadata = {}
    lat, lon = None, None
    extracted_count = 0
    
    try:
        # 1. Метод 1: Используем Pillow (EXIF)
        image_stream = io.BytesIO(image_bytes)
        image = Image.open(image_stream)
        
        # EXIF через Pillow
        exif_data = image._getexif() or {}
        for tag_id, value in exif_data.items():
            tag = TAGS.get(tag_id, tag_id)
            metadata[f"Pillow_{tag}"] = str(value)
            extracted_count += 1
        
        # GPS через Pillow
        lat, lon = extract_gps_from_exif(exif_data)
        
        # 2. Метод 2: Используем exifread
        image_stream.seek(0)
        tags = exifread.process_file(image_stream, details=False)
        for tag, value in tags.items():
            if tag not in ('JPEGThumbnail', 'TIFFThumbnail', 'Filename', 'EXIF MakerNote'):
                metadata[f"ExifRead_{tag}"] = str(value)
                extracted_count += 1
        
        # GPS через exifread (если не нашли через Pillow)
        if lat is None or lon is None:
            lat, lon = extract_gps_from_exifread(tags)
        
        # 3. Метод 3: Используем piexif
        try:
            image_stream.seek(0)
            exif_dict = piexif.load(image_stream.getvalue())
            for ifd in exif_dict:
                if ifd != "thumbnail":
                    for tag, value in exif_dict[ifd].items():
                        tag_name = piexif.TAGS[ifd][tag]["name"]
                        metadata[f"Piexif_{ifd}_{tag_name}"] = str(value)
                        extracted_count += 1
        except Exception as piexif_e:
            logger.warning(f"Piexif extraction warning: {piexif_e}")
        
        # 4. Метод 4: Используем hachoir (для не-EXIF метаданных)
        try:
            with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
                tmp_file.write(image_bytes)
                tmp_file.flush()
                parser = createParser(tmp_file.name)
                if parser:
                    with parser:
                        hachoir_metadata = extractMetadata(parser)
                        if hachoir_metadata:
                            for line in hachoir_metadata.exportPlaintext():
                                key_val = line.split(":", 1)
                                if len(key_val) == 2:
                                    key = key_val[0].strip()
                                    val = key_val[1].strip()
                                    metadata[f"Hachoir_{key}"] = val
                                    extracted_count += 1
            os.unlink(tmp_file.name)
        except Exception as hachoir_e:
            logger.warning(f"Hachoir extraction warning: {hachoir_e}")
        
        # 5. Метод 5: Анализ самого изображения
        metadata["Image_Width"] = str(image.width)
        metadata["Image_Height"] = str(image.height)
        metadata["Image_Mode"] = str(image.mode)
        metadata["Image_Format"] = str(image.format)
        extracted_count += 4
        
    except Exception as e:
        logger.error(f"Advanced metadata extraction error: {e}")
    
    return metadata, lat, lon, extracted_count

# Функции генерации отчета
def generate_html_report(metadata, lat=None, lon=None, address=None, 
                        landmark=None, manipulation_check=None):
    """Генерирует интерактивный HTML отчет"""
    # Генерация карты
    map_html = ""
    if lat and lon:
        m = folium.Map(location=[lat, lon], zoom_start=15, tiles='cartodbpositron')
        folium.Marker(
            [lat, lon],
            popup="Место съемки",
            icon=folium.Icon(color='red', icon='camera', prefix='fa')
        ).add_to(m)
        
        # Добавляем слой спутниковых снимков
        folium.TileLayer(
            tiles='https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
            attr='Esri',
            name='Спутниковый снимок',
            overlay=False,
            control=True
        ).add_to(m)
        
        # Добавляем контроль слоев
        folium.LayerControl().add_to(m)
        
        with tempfile.NamedTemporaryFile(suffix='.html', delete=False) as temp_file:
            map_path = temp_file.name
            m.save(map_path)
        
        with open(map_path, 'r', encoding='utf-8') as f:
            map_html = f.read()
        os.unlink(map_path)
    
    # Генерация HTML
    html_content = f"""
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Детальный анализ изображения</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;700&family=Raleway:wght@300;400;600&display=swap" rel="stylesheet">
    <style>
        :root {{
            --primary: #4361ee;
            --primary-light: #4cc9f0;
            --secondary: #3f37c9;
            --success: #2ecc71;
            --danger: #e74c3c;
            --warning: #f39c12;
            --light: #f8f9fa;
            --dark: #2c3e50;
            --gray: #95a5a6;
        }}
        
        body {{
            font-family: 'Raleway', sans-serif;
            background: linear-gradient(135deg, #f5f7fa 0%, #e4edf5 100%);
            color: var(--dark);
            min-height: 100vh;
            padding: 20px;
            line-height: 1.6;
        }}
        
        .report-container {{
            background: white;
            border-radius: 20px;
            box-shadow: 0 15px 50px rgba(0,0,0,0.1);
            overflow: hidden;
            max-width: 1200px;
            margin: 30px auto;
            transition: transform 0.3s ease;
        }}
        
        .report-container:hover {{
            transform: translateY(-5px);
            box-shadow: 0 20px 60px rgba(0,0,0,0.15);
        }}
        
        .header-section {{
            background: linear-gradient(135deg, var(--primary) 0%, var(--secondary) 100%);
            color: white;
            padding: 40px 30px;
            text-align: center;
            position: relative;
            overflow: hidden;
        }}
        
        .header-section::before {{
            content: "";
            position: absolute;
            top: -50%;
            left: -50%;
            width: 200%;
            height: 200%;
            background: radial-gradient(circle, rgba(255,255,255,0.15) 0%, transparent 70%);
            transform: rotate(30deg);
        }}
        
        .report-title {{
            font-family: 'Montserrat', sans-serif;
            font-weight: 800;
            font-size: 2.8rem;
            margin-bottom: 15px;
            position: relative;
            text-shadow: 0 3px 6px rgba(0,0,0,0.2);
            letter-spacing: -0.5px;
        }}
        
        .report-subtitle {{
            font-weight: 500;
            font-size: 1.3rem;
            opacity: 0.92;
            position: relative;
            max-width: 700px;
            margin: 0 auto;
        }}
        
        .section-title {{
            color: var(--primary);
            font-family: 'Montserrat', sans-serif;
            font-weight: 700;
            border-bottom: 3px solid var(--primary);
            padding-bottom: 12px;
            margin-top: 40px;
            margin-bottom: 25px;
            display: flex;
            align-items: center;
            font-size: 1.8rem;
        }}
        
        .section-title i {{
            margin-right: 15px;
            font-size: 1.8rem;
            background: linear-gradient(135deg, var(--primary-light), var(--primary));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }}
        
        .metadata-card {{
            background: white;
            border-radius: 15px;
            box-shadow: 0 8px 25px rgba(0,0,0,0.06);
            margin-bottom: 30px;
            overflow: hidden;
            transition: all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.1);
            border: 1px solid #edf2f7;
        }}
        
        .metadata-card:hover {{
            transform: translateY(-8px);
            box-shadow: 0 15px 35px rgba(0,0,0,0.1);
        }}
        
        .metadata-card-header {{
            background: linear-gradient(to right, var(--primary), var(--secondary));
            color: white;
            padding: 18px 25px;
            font-weight: 700;
            font-size: 1.3rem;
            display: flex;
            align-items: center;
            font-family: 'Montserrat', sans-serif;
        }}
        
        .metadata-card-header i {{
            margin-right: 12px;
            font-size: 1.4rem;
        }}
        
        .metadata-card-body {{
            padding: 25px;
        }}
        
        .metadata-table {{
            width: 100%;
            border-collapse: separate;
            border-spacing: 0;
        }}
        
        .metadata-table th, .metadata-table td {{
            padding: 15px 20px;
            text-align: left;
            border-bottom: 1px solid #f1f5f9;
        }}
        
        .metadata-table th {{
            background-color: #f8fafc;
            font-weight: 700;
            color: #4a5568;
            font-family: 'Montserrat', sans-serif;
        }}
        
        .metadata-table tr:last-child td {{
            border-bottom: none;
        }}
        
        .metadata-table tr:hover td {{
            background-color: #f7f9fc;
            transition: background 0.3s ease;
        }}
        
        .metadata-table tr:nth-child(even) {{
            background-color: #fafcff;
        }}
        
        .map-container {{
            height: 450px;
            margin: 25px 0;
            border-radius: 15px;
            overflow: hidden;
            box-shadow: 0 8px 25px rgba(0,0,0,0.1);
            border: 1px solid #e2e8f0;
            transition: all 0.3s ease;
        }}
        
        .map-container:hover {{
            box-shadow: 0 12px 30px rgba(0,0,0,0.15);
        }}
        
        .analysis-result {{
            display: flex;
            align-items: center;
            padding: 30px;
            background: white;
            border-radius: 15px;
            box-shadow: 0 8px 25px rgba(0,0,0,0.06);
            margin-top: 25px;
            border: 1px solid #edf2f7;
            transition: all 0.3s ease;
        }}
        
        .analysis-result:hover {{
            transform: translateY(-5px);
            box-shadow: 0 12px 30px rgba(0,0,0,0.1);
        }}
        
        .result-icon {{
            width: 100px;
            height: 100px;
            border-radius: 20px;
            display: flex;
            align-items: center;
            justify-content: center;
            margin-right: 30px;
            flex-shrink: 0;
            font-size: 2.5rem;
            box-shadow: 0 5px 15px rgba(0,0,0,0.1);
        }}
        
        .original-icon {{
            background: linear-gradient(135deg, #2ecc71, #1abc9c);
            color: white;
        }}
        
        .edited-icon {{
            background: linear-gradient(135deg, #e74c3c, #c0392b);
            color: white;
        }}
        
        .result-content h3 {{
            font-weight: 800;
            margin-bottom: 10px;
            font-family: 'Montserrat', sans-serif;
            font-size: 1.8rem;
        }}
        
        .progress-container {{
            background: #e9ecef;
            border-radius: 50px;
            height: 14px;
            margin: 20px 0;
            overflow: hidden;
            box-shadow: inset 0 2px 5px rgba(0,0,0,0.1);
        }}
        
        .progress-bar {{
            height: 100%;
            border-radius: 50px;
            background: linear-gradient(90deg, var(--primary-light), var(--primary));
            transition: width 0.8s cubic-bezier(0.22, 0.61, 0.36, 1);
            box-shadow: 0 3px 10px rgba(67, 97, 238, 0.3);
        }}
        
        .location-info {{
            background: white;
            border-radius: 15px;
            box-shadow: 0 8px 25px rgba(0,0,0,0.06);
            padding: 30px;
            margin-bottom: 30px;
            border: 1px solid #edf2f7;
            transition: all 0.3s ease;
        }}
        
        .location-info:hover {{
            transform: translateY(-5px);
            box-shadow: 0 12px 30px rgba(0,0,0,0.1);
        }}
        
        .info-item {{
            display: flex;
            margin-bottom: 20px;
            padding-bottom: 20px;
            border-bottom: 1px dashed #e2e8f0;
        }}
        
        .info-item:last-child {{
            margin-bottom: 0;
            padding-bottom: 0;
            border-bottom: none;
        }}
        
        .info-icon {{
            width: 50px;
            height: 50px;
            border-radius: 12px;
            background: linear-gradient(135deg, #edf2ff, #dbe4ff);
            display: flex;
            align-items: center;
            justify-content: center;
            margin-right: 20px;
            flex-shrink: 0;
            color: var(--primary);
            font-size: 1.4rem;
            box-shadow: 0 4px 10px rgba(67, 97, 238, 0.15);
        }}
        
        .info-content {{
            flex-grow: 1;
        }}
        
        .info-title {{
            font-weight: 700;
            margin-bottom: 5px;
            color: #4a5568;
            font-family: 'Montserrat', sans-serif;
            font-size: 1.1rem;
        }}
        
        .info-value {{
            font-size: 1.3rem;
            font-weight: 600;
            color: var(--dark);
        }}
        
        .map-buttons {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin-top: 30px;
        }}
        
        .map-btn {{
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 16px;
            border-radius: 12px;
            font-weight: 700;
            text-align: center;
            transition: all 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275);
            border: none;
            color: white;
            text-decoration: none;
            font-family: 'Montserrat', sans-serif;
            font-size: 1.1rem;
            box-shadow: 0 6px 15px rgba(0,0,0,0.15);
        }}
        
        .map-btn:hover {{
            transform: translateY(-5px);
            box-shadow: 0 12px 25px rgba(0,0,0,0.2);
            text-decoration: none;
            color: white;
        }}
        
        .map-btn i {{
            margin-right: 10px;
            font-size: 1.4rem;
        }}
        
        .google-btn {{
            background: linear-gradient(135deg, #4285F4, #34A853);
        }}
        
        .google-btn:hover {{
            background: linear-gradient(135deg, #3a76d9, #2d9648);
        }}
        
        .yandex-btn {{
            background: linear-gradient(135deg, #FF0000, #FFCC00);
        }}
        
        .yandex-btn:hover {{
            background: linear-gradient(135deg, #e60000, #e6b800);
        }}
        
        .timestamp {{
            text-align: center;
            padding: 25px;
            color: var(--gray);
            font-size: 0.95rem;
            border-top: 1px solid #edf2f7;
            background: #f9fbfd;
        }}
        
        .tag {{
            display: inline-block;
            padding: 8px 18px;
            border-radius: 50px;
            font-size: 0.95rem;
            font-weight: 700;
            margin-right: 10px;
            margin-bottom: 10px;
            font-family: 'Montserrat', sans-serif;
            box-shadow: 0 4px 10px rgba(0,0,0,0.08);
        }}
        
        .tag-success {{
            background: linear-gradient(135deg, #e6f7ee, #d1f2e5);
            color: #27ae60;
            border: 1px solid #2ecc71;
        }}
        
        .tag-warning {{
            background: linear-gradient(135deg, #fff8e6, #fff2cc);
            color: #e67e22;
            border: 1px solid #f39c12;
        }}
        
        .tag-danger {{
            background: linear-gradient(135deg, #fdecea, #fadbd8);
            color: #c0392b;
            border: 1px solid #e74c3c;
        }}
        
        .metadata-count {{
            background: linear-gradient(135deg, #e6f0ff, #d4e3ff);
            color: var(--primary);
            padding: 5px 15px;
            border-radius: 50px;
            font-weight: 700;
            margin-left: 10px;
            font-size: 0.9rem;
        }}
        
        .report-summary {{
            display: flex;
            justify-content: space-between;
            margin-top: 30px;
            flex-wrap: wrap;
            gap: 15px;
        }}
        
        .summary-card {{
            background: white;
            border-radius: 15px;
            padding: 25px;
            flex: 1;
            min-width: 200px;
            box-shadow: 0 8px 25px rgba(0,0,0,0.06);
            border: 1px solid #edf2f7;
            text-align: center;
            transition: all 0.3s ease;
        }}
        
        .summary-card:hover {{
            transform: translateY(-8px);
            box-shadow: 0 12px 30px rgba(0,0,0,0.1);
        }}
        
        .summary-value {{
            font-size: 2.5rem;
            font-weight: 800;
            margin: 15px 0;
            color: var(--primary);
            font-family: 'Montserrat', sans-serif;
        }}
        
        .summary-label {{
            color: var(--gray);
            font-weight: 600;
        }}
        
        .summary-icon {{
            font-size: 2.5rem;
            color: var(--primary-light);
            margin-bottom: 15px;
        }}
        
        .fade-in {{
            animation: fadeIn 0.8s ease forwards;
        }}
        
        @keyframes fadeIn {{
            from {{ opacity: 0; transform: translateY(20px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}
        
        .delay-1 {{ animation-delay: 0.1s; }}
        .delay-2 {{ animation-delay: 0.2s; }}
        .delay-3 {{ animation-delay: 0.3s; }}
        .delay-4 {{ animation-delay: 0.4s; }}
    </style>
</head>
<body>
    <div class="report-container">
        <div class="header-section">
            <h1 class="report-title">
                <i class="fas fa-camera-retro"></i> Детальный анализ изображения
            </h1>
            <p class="report-subtitle">Полный отчет о метаданных, геолокации и признаках редактирования</p>
        </div>
        
        <div class="container py-4">
            <div class="report-summary">
                <div class="summary-card fade-in">
                    <div class="summary-icon">
                        <i class="fas fa-database"></i>
                    </div>
                    <div class="summary-value">{len(metadata)}</div>
                    <div class="summary-label">Метаданных извлечено</div>
                </div>
                
                <div class="summary-card fade-in delay-1">
                    <div class="summary-icon">
                        <i class="fas fa-map-marked-alt"></i>
                    </div>
                    <div class="summary-value">{"Да" if lat and lon else "Нет"}</div>
                    <div class="summary-label">Геоданные найдены</div>
                </div>
                
                <div class="summary-card fade-in delay-2">
                    <div class="summary-icon">
                        <i class="fas fa-edit"></i>
                    </div>
                    <div class="summary-value">{"Да" if manipulation_check and manipulation_check['is_edited'] else "Нет"}</div>
                    <div class="summary-label">Признаки редактирования</div>
                </div>
            </div>
            
            <h2 class="section-title fade-in">
                <i class="fas fa-database"></i> Метаданные изображения
            </h2>
            
            <div class="metadata-card fade-in">
                <div class="metadata-card-header">
                    <i class="fas fa-info-circle"></i> Технические параметры изображения
                </div>
                <div class="metadata-card-body">
                    <div class="table-responsive">
                        <table class="metadata-table">
                            <thead>
                                <tr>
                                    <th>Параметр</th>
                                    <th>Значение</th>
                                </tr>
                            </thead>
                            <tbody>
                                {"".join(f'<tr><td>{html.escape(k)}</td><td>{html.escape(str(v))}</td></tr>' for k, v in list(metadata.items())[:50])}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
            
            <h2 class="section-title fade-in delay-1">
                <i class="fas fa-search"></i> Анализ на редактирование
            </h2>
            
            {generate_manipulation_section(manipulation_check)}
            
            <h2 class="section-title fade-in delay-2">
                <i class="fas fa-map-marker-alt"></i> Геолокация
            </h2>
            
            {generate_location_section(lat, lon, address, landmark, map_html)}
        </div>
        
        <div class="timestamp fade-in delay-3">
            Отчет сгенерирован: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')} | Image Analyzer Bot v3.0
        </div>
    </div>
    
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        // Анимация прогресс-бара
        document.addEventListener('DOMContentLoaded', function() {{
            const progressBar = document.getElementById('ela-progress');
            if (progressBar) {{
                const width = progressBar.getAttribute('data-progress');
                let currentWidth = 0;
                
                const animation = setInterval(() => {{
                    if (currentWidth >= width) {{
                        clearInterval(animation);
                    }} else {{
                        currentWidth += 1;
                        progressBar.style.width = currentWidth + '%';
                    }}
                }}, 20);
            }}
            
            // Анимация появления элементов
            const fadeElements = document.querySelectorAll('.fade-in');
            fadeElements.forEach(el => {{
                el.style.opacity = 0;
            }});
            
            setTimeout(() => {{
                fadeElements.forEach((el, index) => {{
                    setTimeout(() => {{
                        el.style.opacity = 1;
                        el.style.transform = 'translateY(0)';
                    }}, index * 200);
                }});
            }}, 300);
        }});
    </script>
</body>
</html>
"""
    return html_content

def generate_manipulation_section(manipulation_check):
    """Генерирует секцию анализа редактирования"""
    if not manipulation_check:
        return """
        <div class="analysis-result fade-in delay-1">
            <div class="result-icon" style="background: linear-gradient(135deg, #f39c12, #e67e22);">
                <i class="fas fa-exclamation-triangle"></i>
            </div>
            <div class="result-content">
                <h3>Анализ не выполнен</h3>
                <p>Не удалось провести проверку изображения на признаки редактирования</p>
            </div>
        </div>
        """
    
    if manipulation_check['is_edited']:
        status = "Вероятно редактировано"
        icon_class = "edited-icon"
        icon = "fas fa-exclamation-circle"
        tag_class = "tag-danger"
        description = "Обнаружены признаки возможного редактирования изображения"
    else:
        status = "Оригинальное изображение"
        icon_class = "original-icon"
        icon = "fas fa-check-circle"
        tag_class = "tag-success"
        description = "Признаков редактирования изображения не обнаружено"
    
    # Рассчитываем прогресс (максимальное значение 50 для визуализации)
    progress_width = min(manipulation_check['ela_score'] * 2, 100)
    score = manipulation_check['ela_score']
    
    # Определяем уровень риска
    if score < 10:
        risk_level = "Низкий риск"
        risk_icon = "fa-smile"
        risk_color = "tag-success"
    elif score < 25:
        risk_level = "Средний риск"
        risk_icon = "fa-meh"
        risk_color = "tag-warning"
    else:
        risk_level = "Высокий риск"
        risk_icon = "fa-frown"
        risk_color = "tag-danger"
    
    return f"""
    <div class="analysis-result fade-in delay-1">
        <div class="result-icon {icon_class}">
            <i class="{icon}"></i>
        </div>
        <div class="result-content">
            <h3>{status}</h3>
            <p>{description}</p>
            
            <div class="mt-4">
                <div class="info-title">Интенсивность изменений</div>
                <div class="d-flex align-items-center">
                    <div class="info-value">{score:.2f}</div>
                    <span class="tag {risk_color}"><i class="fas {risk_icon} me-2"></i>{risk_level}</span>
                </div>
                <div class="progress-container mt-3">
                    <div id="ela-progress" class="progress-bar" data-progress="{progress_width}" style="width: 0%"></div>
                </div>
                <div class="d-flex justify-content-between mt-2">
                    <small>0 (Минимальная)</small>
                    <small>12.5</small>
                    <small>25</small>
                    <small>50 (Максимальная)</small>
                </div>
            </div>
            
            <div class="mt-4">
                <h4>Интерпретация результатов:</h4>
                <div class="d-flex flex-wrap">
                    <div class="tag tag-success"><i class="fas fa-check-circle me-2"></i>0-10: Маловероятно редактирование</div>
                    <div class="tag tag-warning"><i class="fas fa-exclamation-triangle me-2"></i>10-25: Возможно редактирование</div>
                    <div class="tag tag-danger"><i class="fas fa-exclamation-circle me-2"></i>25+: Вероятно редактирование</div>
                </div>
            </div>
        </div>
    </div>
    """

def generate_location_section(lat, lon, address, landmark, map_html):
    """Генерирует секцию геолокации"""
    if not lat or not lon:
        return """
        <div class="analysis-result fade-in delay-2">
            <div class="result-icon" style="background: linear-gradient(135deg, #95a5a6, #7f8c8d);">
                <i class="fas fa-map-marker-slash"></i>
            </div>
            <div class="result-content">
                <h3>Геоданные не обнаружены</h3>
                <p>В метаданных изображения отсутствует информация о местоположении</p>
            </div>
        </div>
        """
    
    # Форматируем координаты
    lat_str = f"{abs(lat):.6f}° {'N' if lat >= 0 else 'S'}"
    lon_str = f"{abs(lon):.6f}° {'E' if lon >= 0 else 'W'}"
    
    return f"""
    <div class="row fade-in delay-2">
        <div class="col-lg-5">
            <div class="location-info">
                <div class="info-item">
                    <div class="info-icon">
                        <i class="fas fa-map-pin"></i>
                    </div>
                    <div class="info-content">
                        <div class="info-title">Координаты</div>
                        <div class="info-value">{lat_str}, {lon_str}</div>
                    </div>
                </div>
                
                <div class="info-item">
                    <div class="info-icon">
                        <i class="fas fa-road"></i>
                    </div>
                    <div class="info-content">
                        <div class="info-title">Приблизительный адрес</div>
                        <div class="info-value">{html.escape(address) if address else 'Не определен'}</div>
                    </div>
                </div>
                
                <div class="info-item">
                    <div class="info-icon">
                        <i class="fas fa-landmark"></i>
                    </div>
                    <div class="info-content">
                        <div class="info-title">Ближайшая достопримечательность</div>
                        <div class="info-value">{html.escape(landmark) if landmark else 'Не определена'}</div>
                    </div>
                </div>
                
                <div class="map-buttons">
                    <a href="https://www.google.com/maps/search/?api=1&query={lat},{lon}" 
                       target="_blank" class="map-btn google-btn">
                        <i class="fab fa-google"></i> Google Maps
                    </a>
                    <a href="https://yandex.ru/maps/?pt={lon},{lat}&z=15" 
                       target="_blank" class="map-btn yandex-btn">
                        <i class="fab fa-yandex"></i> Яндекс Карты
                    </a>
                </div>
            </div>
        </div>
        
        <div class="col-lg-7">
            <div class="map-container">
                {map_html}
            </div>
        </div>
    </div>
    """

# Обработчики бота
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    """Обработчик команд /start и /help"""
    welcome_text = """
🖼️ *Бот для анализа метаданных фотографий*
Создатель: @coaox
Отправьте мне фотографию (как изображение или файл), и я:
1. Извлеку все EXIF и другие метаданные
2. Определю координаты съемки (если есть)
3. Найду примерное местоположение
4. Проверю признаки редактирования фото
5. Создам подробный HTML отчет

Поддерживаемые форматы: JPG, JPEG, PNG, HEIC, TIFF, WEBP (максимум 20МБ)

*Команды:*
/start - показать это сообщение
/help - помощь по использованию бота
"""
    bot.reply_to(message, welcome_text, parse_mode='Markdown')

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    """Обработчик фотографий"""
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        process_image(message, downloaded_file)
    except Exception as e:
        logger.error(f"Photo error: {e}")
        bot.reply_to(message, "❌ Ошибка обработки фото. Попробуйте отправить как файл.")

@bot.message_handler(content_types=['document'])
def handle_document(message):
    """Обработчик документов"""
    try:
        if not message.document.file_name:
            bot.reply_to(message, "❌ Файл без имени не поддерживается.")
            return

        ext = os.path.splitext(message.document.file_name.lower())[1]
        if ext not in SUPPORTED_EXTENSIONS:
            bot.reply_to(message, f"❌ Формат {ext} не поддерживается.")
            return

        if message.document.file_size > MAX_FILE_SIZE:
            bot.reply_to(message, "❌ Файл слишком большой (максимум 20МБ)")
            return

        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        process_image(message, downloaded_file)
    except Exception as e:
        logger.error(f"Document error: {e}")
        bot.reply_to(message, "❌ Ошибка обработки файла. Убедитесь, что это изображение.")

def process_image(message, image_bytes):
    """Обрабатывает изображение в отдельном потоке"""
    user_id = message.from_user.id
    chat_id = message.chat.id
    
    user_data[user_id] = {
        'image_bytes': image_bytes,
        'message': message,
        'processed': False
    }
    
    if not create_status_message(user_id, chat_id):
        bot.reply_to(message, "❌ Не удалось начать анализ изображения")
        return
    
    thread = threading.Thread(target=process_image_thread, args=(user_id,))
    thread.start()

def process_image_thread(user_id):
    """Поток обработки изображения"""
    try:
        data = user_data[user_id]
        message = data['message']
        image_bytes = data['image_bytes']
        
        # 1. Извлечение метаданных (используем улучшенную функцию)
        update_status_step(user_id, "metadata", "progress", "Извлечение данных...")
        metadata, lat, lon, extracted_count = extract_metadata_advanced(image_bytes)
        update_status_step(user_id, "metadata", "completed", f"Найдено {extracted_count} параметров")
        time.sleep(1)

        # 2. Поиск геолокации
        update_status_step(user_id, "geolocation", "progress", "Поиск координат...")
        address, landmark = None, None
        
        if lat and lon:
            try:
                update_status_step(user_id, "geolocation", "progress", "Определение местоположения...")
                location = get_location_info(lat, lon)
                address = location['address'] if location else None
                landmark = get_landmark(lat, lon)
                update_status_step(user_id, "geolocation", "completed", "Координаты найдены")
            except Exception as e:
                logger.error(f"Geocoding error: {e}")
                update_status_step(user_id, "geolocation", "completed", "Ошибка геокодирования")
        else:
            update_status_step(user_id, "geolocation", "completed", "GPS данные отсутствуют")
        time.sleep(1)

        # 3. Анализ местоположения
        update_status_step(user_id, "location_analysis", "progress", "Обработка...")
        if lat and lon:
            update_status_step(user_id, "location_analysis", "completed", "Данные получены")
        else:
            update_status_step(user_id, "location_analysis", "completed", "Требуются координаты")
        time.sleep(0.5)

        # 4. Проверка на редактирование
        update_status_step(user_id, "manipulation_check", "progress", "Анализ ELA...")
        manipulation_check = check_image_manipulation(image_bytes)
        if manipulation_check:
            status = "Возможно редактировано" if manipulation_check['is_edited'] else "Оригинальное"
            update_status_step(user_id, "manipulation_check", "completed", status)
        else:
            update_status_step(user_id, "manipulation_check", "completed", "Анализ не выполнен")
        time.sleep(0.5)

        # Сохраняем данные
        user_data[user_id].update({
            'processed': True,
            'metadata': metadata,
            'lat': lat,
            'lon': lon,
            'address': address,
            'landmark': landmark,
            'manipulation_check': manipulation_check
        })

        # Финальное сообщение
        try:
            status_data = user_data[user_id]['status_message']
            final_text = status_data['status'].replace("🔍 *Анализ изображения начат...*", "✅ *Анализ успешно завершен!*")
            update_status_message(user_id, final_text)
            
            # Генерируем HTML отчет
            html_content = generate_html_report(
                metadata=metadata,
                lat=lat,
                lon=lon,
                address=address,
                landmark=landmark,
                manipulation_check=manipulation_check
            )
            
            # Создаем файл отчета
            file_stream = io.BytesIO(html_content.encode('utf-8'))
            file_stream.name = f"image_report_{datetime.now().strftime('%d%m%Y_%H%M%S')}.html"
            
            # Отправляем ELA анализ если есть
            if manipulation_check and 'ela_image' in manipulation_check:
                bot.send_photo(
                    message.chat.id, 
                    manipulation_check['ela_image'], 
                    caption="🔍 Результат анализа на редактирование (ELA)"
                )
            
            # Отправляем HTML отчет
            bot.send_document(
                message.chat.id,
                file_stream,
                caption="📊 Вот ваш детализированный отчет об анализе изображения"
            )
            
            # Удаляем статусное сообщение
            try:
                bot.delete_message(message.chat.id, status_data['message_id'])
            except Exception as e:
                logger.error(f"Error deleting status message: {e}")

        except Exception as e:
            logger.error(f"Final processing error: {e}")
            bot.send_message(message.chat.id, "⚠️ Произошла ошибка при формировании отчета.")

    except Exception as e:
        logger.error(f"Processing thread error: {e}")
        bot.send_message(user_data[user_id]['message'].chat.id, "⚠️ Произошла критическая ошибка при анализе изображения")

if __name__ == '__main__':
    logger.info("Бот запущен и готов к работе")
    bot.infinity_polling()