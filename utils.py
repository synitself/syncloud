# utils.py
import unicodedata
import re
import logging

logger = logging.getLogger(__name__)

def sanitize_filename(filename: str) -> str:
    filename = str(filename)
    filename = filename.replace("/", "-").replace("\\", "-")
    try:
        filename_ascii = unicodedata.normalize('NFKD', filename).encode('ascii', 'ignore').decode('ascii')
        if filename_ascii.strip(): filename = filename_ascii
    except Exception: pass
    filename = re.sub(r'[^\w\s\.\-_()]', '', filename).strip()
    filename = re.sub(r'\s+', ' ', filename)
    if not filename: filename = "downloaded_track"
    return filename

def create_progress_bar(percentage: int, length: int = 10, stage_message: str = "") -> str:
    percentage = max(0, min(100, percentage))
    filled_length = int(length * percentage // 100)
    bar = '█' * filled_length + '░' * (length - filled_length)
    return f"[{bar}] {int(percentage)}% {stage_message}"