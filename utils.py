import unicodedata
import re
import logging
import os

logger = logging.getLogger(__name__)


def sanitize_filename(filename: str) -> str:
    filename = str(filename)
    filename = filename.replace("/", "-").replace("\\", "-")
    try:
        filename_ascii = unicodedata.normalize('NFKD', filename).encode('ascii', 'ignore').decode('ascii')
        if filename_ascii.strip():
            filename = filename_ascii
    except Exception:
        pass  # Keep original if NFKD fails

    filename = re.sub(r'[^\w\s\.\-_()\']', '', filename).strip()  # Allow single quote
    filename = re.sub(r'\s+', ' ', filename)
    if not filename:
        filename = "downloaded_track"

    max_len = 200
    if len(filename) > max_len:
        name_part, ext_part = os.path.splitext(filename)
        # Ensure ext_part is not overly long itself, though unlikely for common extensions
        if len(ext_part) > 10: ext_part = ext_part[:10]

        name_part_max_len = max_len - len(ext_part) - (1 if ext_part else 0)
        if name_part_max_len < 1 and ext_part:  # If extension itself is too long
            filename = ext_part[:max_len] if ext_part else "track"[:max_len]
        elif name_part_max_len < 1 and not ext_part:
            filename = "track"[:max_len]
        else:
            name_part = name_part[:name_part_max_len]
            filename = name_part + ext_part
    return filename


def create_progress_bar(percentage: int, length: int = 10) -> str:
    percentage = max(0, min(100, percentage))
    filled_length = int(length * percentage // 100)
    filled_char = '█'
    empty_char = '░'
    bar = filled_char * filled_length + empty_char * (length - filled_length)
    return f"⏳ [{bar}] {percentage:3d}%"


def escape_markdown_v2(text: str) -> str:
    if not isinstance(text, str): text = str(text)
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    # Python's re.escape escapes more than just these, but these are the specific V2 ones.
    # A direct replacement is safer and more targeted for TG MarkdownV2.
    # Must escape backslash first
    text = text.replace('\\', '\\\\')
    for char_to_escape in escape_chars:
        text = text.replace(char_to_escape, f'\\{char_to_escape}')
    return text


def escape_markdown_legacy(text: str) -> str:
    if not isinstance(text, str): text = str(text)
    escape_chars = r'_*`[]()'  # For legacy Markdown
    text = text.replace('\\', '\\\\')
    for char_to_escape in escape_chars:
        text = text.replace(char_to_escape, f'\\{char_to_escape}')
    return text