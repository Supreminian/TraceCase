"""Private, session-scoped profiles and normalized raster avatars."""
from io import BytesIO
from pathlib import Path
import time
import warnings

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from sdlc.accounts import AccessError

MAX_AVATAR_BYTES = 2 * 1024 * 1024


class ProfileConflict(AccessError):
    pass


def normalize_avatar(data, filename):
    if not isinstance(data, bytes) or not data or len(data) > MAX_AVATAR_BYTES:
        raise AccessError('头像文件不能为空，且不能超过 2 MiB。')
    if Path(filename).suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp'}:
        raise AccessError('头像仅支持 PNG、JPEG 或 WebP 图片。')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as source:
                if source.format not in {'PNG', 'JPEG', 'WEBP'} or getattr(source, 'n_frames', 1) != 1:
                    raise AccessError('请选择静态 PNG、JPEG 或 WebP 图片。')
                width, height = source.size
                if max(width, height) > 4096 or width * height > 16_000_000:
                    raise AccessError('头像尺寸不能超过 4096 × 4096，且总像素不能超过 1600 万。')
                source.verify()
            with Image.open(BytesIO(data)) as source:
                image = ImageOps.exif_transpose(source).convert('RGBA')
                image = ImageOps.fit(image, (256, 256), method=Image.Resampling.LANCZOS)
                canvas = Image.new('RGB', (256, 256), 'white')
                canvas.paste(image, mask=image.getchannel('A'))
                result = BytesIO()
                # Encoding a fresh image discards EXIF, comments, and trailing payloads.
                canvas.save(result, format='PNG')
                return result.getvalue()
    except AccessError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise AccessError('头像文件无法识别或已损坏，请换一张图片。') from None


def get_profile(store, token):
    with store.db() as db:
        user = store.identity(db, token)
        row = db.execute('SELECT bio,revision,updated_at,avatar IS NOT NULL AS has_avatar FROM user_profiles WHERE user_id=?', (user['id'],)).fetchone()
    return {**user, **(dict(row) if row else {'bio': '', 'revision': 0, 'updated_at': None, 'has_avatar': False})}


def save_profile(store, token, display_name, bio, revision, *, avatar=None, filename='', remove_avatar=False):
    store.me(token)
    if not isinstance(display_name, str) or not 1 <= len(display_name.strip()) <= 40:
        raise AccessError('显示名称须为 1–40 个字符。')
    if not isinstance(bio, str) or len(bio) > 500:
        raise AccessError('个人简介最多 500 个字符。')
    if type(revision) is not int or revision < 0:
        raise AccessError('资料版本无效，请刷新后重试。')
    if avatar is not None and remove_avatar:
        raise AccessError('上传头像和恢复默认头像不能同时选择。')
    normalized = normalize_avatar(avatar, filename) if avatar is not None else None
    with store.db(write=True) as db:
        user = store.identity(db, token)
        row = db.execute('SELECT revision,avatar FROM user_profiles WHERE user_id=?', (user['id'],)).fetchone()
        if revision != (row['revision'] if row else 0):
            raise ProfileConflict('资料已在其他页面更新，请核对最新内容后再保存。')
        image = normalized if avatar is not None else (None if remove_avatar or not row else row['avatar'])
        db.execute('UPDATE users SET display_name=? WHERE id=?', (display_name.strip(), user['id']))
        db.execute('''INSERT INTO user_profiles(user_id,bio,avatar,revision,updated_at) VALUES (?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET bio=excluded.bio,avatar=excluded.avatar,
            revision=excluded.revision,updated_at=excluded.updated_at''',
                   (user['id'], bio.strip(), image, revision + 1, time.time()))
        store._audit(db, {**user, 'display_name': display_name.strip()}, 'profile_updated',
                     revision=revision + 1, avatar_changed=avatar is not None or remove_avatar)


def avatar_png(store, token):
    with store.db() as db:
        user = store.identity(db, token)
        row = db.execute('SELECT avatar FROM user_profiles WHERE user_id=?', (user['id'],)).fetchone()
    if row and row['avatar'] is not None:
        return bytes(row['avatar'])
    image = Image.new('RGB', (256, 256), '#1e3455')
    ImageDraw.Draw(image).text((128, 125), user['username'][0].upper(),
                              font=ImageFont.load_default(size=100), anchor='mm', fill='white')
    result = BytesIO()
    image.save(result, format='PNG')
    return result.getvalue()
