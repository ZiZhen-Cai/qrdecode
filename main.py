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

# zbarlight 内部 `from pkg_resources import get_distribution` 取版本号，
# 但安卓（Python 3.14 + setuptools 新版）没有 pkg_resources，会导致 import 失败并闪退。
# 这里在导入 zbarlight 前 mock 一个最小实现（仅需 get_distribution().version）。
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
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.progressbar import ProgressBar
from kivy.clock import Clock


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
    """对 PIL Image 做二维码识别，返回文本或 None。"""
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
    try:
        from jnius import autoclass
        MediaMetadataRetriever = autoclass('android.media.MediaMetadataRetriever')
        mmr = MediaMetadataRetriever()
        mmr.setDataSource(path)
        duration_ms = int(mmr.extractMetadata(9))
        step_us = 200000
        i = 0
        while i * 1000 < duration_ms:
            bmp = mmr.getFrameAtTime(i * 1000, 2)
            if bmp is not None:
                w = bmp.getWidth()
                h = bmp.getHeight()
                buf = bmp.getPixels([0] * (w * h), 0, w, 0, 0, w, h)
                from PIL import Image as _Image
                px = bytearray()
                for c in buf:
                    px.append((c >> 16) & 0xFF)
                    px.append((c >> 8) & 0xFF)
                    px.append(c & 0xFF)
                img = _Image.frombytes('RGB', (w, h), bytes(px))
                t = decode_qr(img)
                if t:
                    rb.feed(t)
            i += step_us // 1000
            if progress_cb:
                progress_cb(i, duration_ms // 1000)
            if rb.is_done():
                break
        mmr.release()
    except Exception as e:
        raise RuntimeError('视频解码失败: %s' % e)


class RootWidget(BoxLayout):
    def __init__(self, **kw):
        super().__init__(orientation='vertical', padding=24, spacing=14, **kw)
        self.app = App.get_running_app()
        self.title = Label(text='二维码文件解析器', size_hint_y=None, height=48,
                           font_size=22, bold=True)
        self.add_widget(self.title)
        self.btn_pick = Button(text='选择 GIF / 视频文件', size_hint_y=None,
                               height=56, font_size=18)
        self.btn_pick.bind(on_press=self.pick_file)
        self.add_widget(self.btn_pick)
        self.status = Label(text='等待选择文件…', size_hint_y=None, height=40,
                            font_size=15)
        self.add_widget(self.status)
        self.progress = ProgressBar(max=100, value=0)
        self.add_widget(self.progress)
        self._thread = None

    def pick_file(self, instance):
        from plyer import filechooser
        filechooser.open_file(on_selection=self.on_selected)

    def on_selected(self, selection):
        if not selection:
            return
        path = selection[0]
        self.status.text = '解码中: %s' % os.path.basename(path)
        self.btn_pick.disabled = True
        self.progress.value = 0
        self._thread = threading.Thread(target=self._decode_worker, args=(path,))
        self._thread.daemon = True
        self._thread.start()

    def _decode_worker(self, path):
        rb = Rebuilder()
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext == '.gif':
                decode_gif(path, rb, lambda i, n: self._update(i, n))
            else:
                decode_video(path, rb, lambda i, n: self._update(i, n))
            if not rb.is_done():
                Clock.schedule_once(lambda dt: self._fail('帧未收齐: %s' % rb.progress()))
                return
            data, name = rb.rebuild()
            out = self._save(data, name)
            Clock.schedule_once(lambda dt: self._done(out, name))
        except Exception as e:
            Clock.schedule_once(lambda dt: self._fail(str(e)))

    def _update(self, i, n):
        def _do(dt):
            if isinstance(n, int) and n > 0:
                self.progress.value = min(100, i * 100 // n)
            self.status.text = '解码中… 帧 %s / %s' % (i, n)
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
        self.status.text = '还原成功: %s' % name
        self._share(path, name)

    def _fail(self, msg):
        self.btn_pick.disabled = False
        self.progress.value = 0
        self.status.text = '失败: %s' % msg

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
