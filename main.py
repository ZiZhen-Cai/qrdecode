# -*- coding: utf-8 -*-
"""
安卓二维码文件解析器（Kivy）

从相册/文件选 GIF 或视频，逐帧解码二维码，按帧协议重组还原文件。

依赖：kivy / pillow / zbarlight / plyer
帧协议（与 PC 端 encode.py 一致）：
  头帧   QRH|<总字节数>|<总帧数>|<crc32>|<md5>|<原始文件名>
  数据帧 QRD|<序号>|<总帧数>|<base64>
"""
import base64
import os
import sys
import threading
import types
import zlib

try:
    import pkg_resources  # noqa: F401
except ImportError:
    _m = types.ModuleType('pkg_resources')

    class _Dist(object):
        version = '2.1'

    _m.get_distribution = lambda *a, **k: _Dist()
    sys.modules['pkg_resources'] = _m

from PIL import Image
import zbarlight

from kivy.app import App
from kivy.clock import Clock
from kivy.core.text import LabelBase
from kivy.graphics import Color, RoundedRectangle
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.progressbar import ProgressBar
from kivy.utils import get_color_from_hex

_HERE = os.path.dirname(os.path.abspath(__file__))


def _register_fonts():
    bundled = os.path.join(_HERE, 'NotoSansSC-Regular.otf')
    if os.path.exists(bundled):
        LabelBase.register(name='Roboto', fn_regular=bundled)
        return
    d = '/system/fonts'
    try:
        names = os.listdir(d)
    except Exception:
        return
    keys = ('cjk', 'misans', 'droidsansfallback', 'notosanssc', 'sourcehansans')
    cands = [os.path.join(d, fn) for fn in names
             if fn.lower().endswith(('.ttf', '.otf', '.ttc'))
             and any(k in fn.lower() for k in keys)]
    cands.sort(key=lambda p: (p.lower().endswith('.ttc'), p))
    for p in cands:
        try:
            LabelBase.register(name='Roboto', fn_regular=p)
            return
        except Exception:
            continue


_register_fonts()


CLR_BG = get_color_from_hex('#12141C')
CLR_CARD = get_color_from_hex('#1E2230')
CLR_PRIMARY = get_color_from_hex('#4A7BF7')
CLR_PRIMARY_D = get_color_from_hex('#3A63D0')
CLR_TEXT = get_color_from_hex('#EEF1F7')
CLR_SUB = get_color_from_hex('#8A93A8')
CLR_OK = get_color_from_hex('#37C08A')
CLR_ERR = get_color_from_hex('#F2555A')

_PICK_REQ = 0x5A5C


def crc32(data):
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ (0xEDB88320 if crc & 1 else 0)
    return crc ^ 0xFFFFFFFF


class Rebuilder(object):
    def __init__(self):
        self.header = None
        self.chunks = {}
        self.seen = set()

    def feed(self, text):
        text = (text or '').strip()
        if text.startswith('QRH|'):
            p = text.split('|')
            if len(p) >= 6:
                self.header = {'size': int(p[1]), 'total': int(p[2]),
                               'crc': p[3], 'name': '|'.join(p[5:])}
            else:
                self.header = {'size': int(p[1]), 'total': int(p[2]),
                               'crc': p[3], 'name': '|'.join(p[4:])}
        elif text.startswith('QRD|'):
            q = text.split('|')
            seq = int(q[1])
            if seq not in self.seen:
                self.seen.add(seq)
                self.chunks[seq] = q[3]

    def progress(self):
        if not self.header:
            return 0, '等待头帧…'
        return len(self.seen), self.header['total']

    def is_done(self):
        return self.header is not None and len(self.seen) >= self.header['total']

    def rebuild(self):
        if not self.header:
            raise RuntimeError('未收到头帧(QRH)')
        miss = [i for i in range(1, self.header['total'] + 1) if i not in self.seen]
        if miss:
            raise RuntimeError('缺失帧: %s' % miss[:20])
        data = b''.join(base64.b64decode(self.chunks[i])
                        for i in range(1, self.header['total'] + 1))
        c = format(crc32(data), '08x')
        if c != self.header['crc']:
            raise RuntimeError('CRC32 校验失败')
        return data, self.header.get('name', 'restored.bin')


def decode_qr(pil_img):
    try:
        img = pil_img.convert('L')
        img.load()
        codes = zbarlight.scan_codes('qrcode', img)
    except Exception:
        return None
    if codes:
        return codes[0].decode('utf-8', errors='ignore')
    return None


def decode_gif(path, rb, progress_cb):
    im = Image.open(path)
    n = getattr(im, 'n_frames', 1)
    for i in range(n):
        im.seek(i)
        t = decode_qr(im.convert('RGB'))
        if t:
            rb.feed(t)
        if progress_cb:
            progress_cb(i + 1, n)
        if rb.is_done():
            break
    im.close()


def decode_video(path, rb, progress_cb):
    from jnius import autoclass
    MediaMetadataRetriever = autoclass('android.media.MediaMetadataRetriever')
    mmr = MediaMetadataRetriever()
    mmr.setDataSource(path)
    duration_ms = int(mmr.extractMetadata(9))
    step_ms = 200
    i = 0
    while i < duration_ms:
        bmp = mmr.getFrameAtTime(i * 1000, 2)
        if bmp is not None:
            w = bmp.getWidth()
            h = bmp.getHeight()
            buf = bmp.getPixels([0] * (w * h), 0, w, 0, 0, w, h)
            px = bytearray()
            for c in buf:
                px.append((c >> 16) & 0xFF)
                px.append((c >> 8) & 0xFF)
                px.append(c & 0xFF)
            img = Image.frombytes('RGB', (w, h), bytes(px))
            t = decode_qr(img)
            if t:
                rb.feed(t)
        i += step_ms
        if progress_cb:
            progress_cb(i, duration_ms)
        if rb.is_done():
            break
    mmr.release()


class Card(BoxLayout):
    def __init__(self, bg=None, radius=18, **kw):
        super().__init__(**kw)
        self._bg = bg or CLR_CARD
        with self.canvas.before:
            self._color = Color(*self._bg)
            self._rect = RoundedRectangle(radius=[dp(radius)])
        self.bind(pos=self._sync, size=self._sync)

    def _sync(self, *a):
        self._rect.pos = self.pos
        self._rect.size = self.size


class PrimaryButton(Button):
    def __init__(self, **kw):
        kw.setdefault('background_normal', '')
        kw.setdefault('background_color', (0, 0, 0, 0))
        kw.setdefault('color', (1, 1, 1, 1))
        kw.setdefault('font_size', dp(19))
        kw.setdefault('bold', True)
        super().__init__(**kw)
        with self.canvas.before:
            self._color = Color(*CLR_PRIMARY)
            self._rect = RoundedRectangle(radius=[dp(14)])
        self.bind(pos=self._sync, size=self._sync, state=self._on_state)

    def _sync(self, *a):
        self._rect.pos = self.pos
        self._rect.size = self.size

    def _on_state(self, *a):
        self._color.rgba = CLR_PRIMARY_D if self.state == 'down' else CLR_PRIMARY


class RoundedProgress(ProgressBar):
    def __init__(self, **kw):
        super().__init__(**kw)
        with self.canvas.before:
            Color(*CLR_CARD)
            self._bg_rect = RoundedRectangle(radius=[dp(6)])
        with self.canvas.after:
            self._bar_color = Color(*CLR_PRIMARY)
            self._bar_rect = RoundedRectangle(radius=[dp(6)])
        self.bind(pos=self._sync, size=self._sync, value=self._sync_value)

    def _sync(self, *a):
        self._bg_rect.pos = self.pos
        self._bg_rect.size = self.size
        self._sync_value()

    def _sync_value(self, *a):
        frac = (self.value / self.max) if self.max else 0
        frac = max(0.0, min(1.0, frac))
        self._bar_rect.pos = self.pos
        self._bar_rect.size = (self.width * frac, self.height)


class RootWidget(BoxLayout):
    def __init__(self, **kw):
        super().__init__(orientation='vertical', padding=dp(20), spacing=dp(14), **kw)
        self.app = App.get_running_app()
        with self.canvas.before:
            Color(*CLR_BG)
            self._bgrect = RoundedRectangle(radius=[0])
        self.bind(pos=self._sync_bg, size=self._sync_bg)

        title = Label(text='二维码文件解析器', size_hint_y=None, height=dp(46),
                      font_size=dp(25), bold=True, color=CLR_TEXT)
        self.add_widget(title)
        subtitle = Label(text='从 GIF / 视频中解码二维码，还原文件',
                         size_hint_y=None, height=dp(26),
                         font_size=dp(13.5), color=CLR_SUB)
        self.add_widget(subtitle)
        self.add_widget(BoxLayout(size_hint_y=None, height=dp(14)))

        self.btn_pick = PrimaryButton(text='选择 GIF / 视频文件', size_hint_y=None,
                                      height=dp(62))
        self.btn_pick.bind(on_press=self.pick_file)
        self.add_widget(self.btn_pick)

        status_card = Card(orientation='vertical', size_hint_y=None, height=dp(96),
                           padding=dp(14), spacing=dp(10))
        self.status = Label(text='等待选择文件…', color=CLR_TEXT,
                            font_size=dp(14.5), halign='left', valign='middle')
        self.status.bind(size=lambda s, *a: setattr(s, 'text_size', (s.width, None)))
        status_card.add_widget(self.status)
        self.progress = RoundedProgress(max=100, value=0, size_hint_y=None,
                                        height=dp(12))
        status_card.add_widget(self.progress)
        self.add_widget(status_card)

        tips_card = Card(orientation='vertical', size_hint_y=None, height=dp(150),
                         padding=dp(16), spacing=dp(6))
        tips_card.add_widget(Label(text='使用说明', color=CLR_TEXT, bold=True,
                                   font_size=dp(15), size_hint_y=None, height=dp(26),
                                   halign='left', valign='middle'))
        for line in ('1. 选择含二维码的 GIF 或视频文件',
                     '2. 自动逐帧解码并按协议重组',
                     '3. 还原完成后可分享 / 保存'):
            lb = Label(text=line, color=CLR_SUB, font_size=dp(13),
                       size_hint_y=None, height=dp(28), halign='left', valign='middle')
            lb.bind(size=lambda s, *a: setattr(s, 'text_size', (s.width, None)))
            tips_card.add_widget(lb)
        self.add_widget(tips_card)

        self.add_widget(BoxLayout())
        self._thread = None

    def _sync_bg(self, *a):
        self._bgrect.pos = self.pos
        self._bgrect.size = self.size

    def pick_file(self, instance):
        self._set_status('正在打开文件选择器…', CLR_SUB)
        try:
            from android import activity as android_activity
            from jnius import autoclass
            Intent = autoclass('android.content.Intent')
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            intent = Intent(Intent.ACTION_OPEN_DOCUMENT)
            intent.addCategory(Intent.CATEGORY_OPENABLE)
            intent.setType('*/*')
            android_activity.bind(on_activity_result=self._on_activity_result)
            PythonActivity.mActivity.startActivityForResult(intent, _PICK_REQ)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._set_status('选择器打开失败: %s' % e, CLR_ERR)

    def _on_activity_result(self, request_code, result_code, intent):
        try:
            from android import activity as android_activity
            android_activity.unbind(on_activity_result=self._on_activity_result)
        except Exception:
            pass
        if request_code != _PICK_REQ:
            return
        if result_code != -1:
            Clock.schedule_once(lambda dt: self._set_status('已取消选择', CLR_SUB))
            return
        uri = intent.getData() if intent is not None else None
        if uri is None:
            Clock.schedule_once(lambda dt: self._set_status('未获取到文件', CLR_ERR))
            return
        Clock.schedule_once(lambda dt: self._handle_uri(uri))

    def _handle_uri(self, uri):
        self._set_status('正在读取文件…', CLR_TEXT)
        self.btn_pick.disabled = True
        self.progress.value = 0
        t = threading.Thread(target=self._copy_and_decode, args=(uri,))
        t.daemon = True
        t.start()

    def _copy_and_decode(self, uri):
        try:
            local = self._uri_to_file(uri)
        except Exception as e:
            import traceback
            traceback.print_exc()
            msg = '读取文件失败: %s' % e
            Clock.schedule_once(lambda dt: self._fail(msg))
            return
        if not local:
            Clock.schedule_once(lambda dt: self._fail('无法读取所选文件'))
            return
        self._decode_worker(local)

    def _uri_to_file(self, uri):
        from jnius import autoclass
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        resolver = PythonActivity.mActivity.getContentResolver()

        name = 'input.dat'
        try:
            cursor = resolver.query(uri, None, None, None, None)
            if cursor is not None:
                if cursor.moveToFirst():
                    idx = cursor.getColumnIndex('_display_name')
                    if idx >= 0:
                        v = cursor.getString(idx)
                        if v:
                            name = v
                cursor.close()
        except Exception:
            pass

        name = os.path.basename(name) or 'input.dat'
        d = self.app.user_data_dir
        os.makedirs(d, exist_ok=True)
        out = os.path.join(d, name)

        # 用 ParcelFileDescriptor 拿 Linux fd，再由 Python 原生 os.read 读取。
        # （绕开 pyjnius 的 Java 数组创建与重载方法解析问题）
        pfd = resolver.openFileDescriptor(uri, 'r')
        try:
            fd = pfd.getFd()
            with open(out, 'wb') as f:
                while True:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    f.write(chunk)
        finally:
            try:
                pfd.close()
            except Exception:
                pass
        return out

    def _decode_worker(self, path):
        Clock.schedule_once(lambda dt: self._set_status(
            '已读取，开始解码: %s' % os.path.basename(path), CLR_TEXT))
        rb = Rebuilder()
        ext = os.path.splitext(str(path))[1].lower()
        try:
            if ext == '.gif':
                decode_gif(path, rb, lambda i, n: self._update(i, n))
            else:
                decode_video(path, rb, lambda i, n: self._update(i, n))

            if not rb.is_done():
                got, total = rb.progress()
                Clock.schedule_once(
                    lambda dt: self._fail('帧未收齐（%s / %s）' % (got, total)))
                return
            data, name = rb.rebuild()
            out = self._save(data, name)
            Clock.schedule_once(lambda dt: self._done(out, name))
        except Exception as e:
            import traceback
            traceback.print_exc()
            msg = str(e)
            Clock.schedule_once(lambda dt: self._fail(msg))

    def _update(self, i, n):
        def _do(dt):
            if isinstance(n, int) and n > 0:
                self.progress.value = min(100, i * 100 // n)
            self._set_status('解码中… 帧 %s / %s' % (i, n), CLR_TEXT)
        Clock.schedule_once(_do)

    def _save(self, data, name):
        d = self.app.user_data_dir
        os.makedirs(d, exist_ok=True)
        out = os.path.join(d, name)
        with open(out, 'wb') as f:
            f.write(data)
        return out

    def _done(self, path, name):
        self.btn_pick.disabled = False
        self.progress.value = 100
        self._set_status('还原成功: %s' % name, CLR_OK)
        self._share(path, name)

    def _fail(self, msg):
        self.btn_pick.disabled = False
        self.progress.value = 0
        self._set_status('失败: %s' % msg, CLR_ERR)

    def _set_status(self, text, color):
        self.status.text = text
        self.status.color = color

    def _share(self, path, name):
        try:
            from plyer import share
            share.share(title='分享文件', text='已还原: %s' % name, path=path)
        except Exception:
            pass


class QrDecodeApp(App):
    def build(self):
        return RootWidget()


if __name__ == '__main__':
    QrDecodeApp().run()
