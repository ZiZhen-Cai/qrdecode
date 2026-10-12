# -*- coding: utf-8 -*-
"""
安卓二维码文件解析器（Kivy）

从相册/文件选 GIF 或视频，逐帧解码二维码，按帧协议重组还原文件。

依赖：kivy / pillow / zbarlight / plyer
帧协议（与 PC 端 encode.py 一致）：
  头帧   QRH|<总字节数>|<总帧数>|<crc32>|<md5>|<原始文件名>
  数据帧 QRD|<序号>|<总帧数>|<base64>

v1.1 提速与设置：
  - 识别前降采样（长边 >_MAX_SIDE 的帧先缩小，像素量减 40~60%）
  - 视频帧改内存 JPEG（pyjnius byte[] → Python bytes），去掉临时文件落盘
  - fork 多进程并行识别（_MAX_WORKERS 个 worker 真正吃满多核；
    创建失败/超时/进程退出均自动回退单线程全量重解）
  - 设置卡片：解码时屏幕常亮 / 锁屏后继续解码(PARTIAL_WAKE_LOCK) /
    多核并行开关，本地 settings.json 持久化
"""
import base64
import io
import json
import os
import sys
import threading
import time
import types
import zlib

# zbarlight 内部 `from pkg_resources import get_distribution` 取版本号，
# 但安卓（Python 3.14 + setuptools 新版）没有 pkg_resources，会导致 import 失败并闪退。
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
from kivy.uix.switch import Switch
from kivy.utils import get_color_from_hex

_HERE = os.path.dirname(os.path.abspath(__file__))

_META_DURATION = 9
_META_FRAME_COUNT = 32

# 解码提速：长边超过该值的帧先降采样再识别（二维码每格仍有约 5px，
# 识别率基本无损；拍摄时让二维码占画面大一些更稳）
_MAX_SIDE = 1280
# 并行解码 worker 数上限（fork 子进程，真正吃满多核；失败自动回退单线程）
_MAX_WORKERS = 4


# ==================== 字体 ====================

def _register_fonts():
    """优先用打包进 APK 的中文字体，其次找系统 CJK 字体。"""
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


# ==================== 配色 ====================

CLR_BG = get_color_from_hex('#12141C')
CLR_CARD = get_color_from_hex('#1E2230')
CLR_PRIMARY = get_color_from_hex('#4A7BF7')
CLR_PRIMARY_D = get_color_from_hex('#3A63D0')
CLR_TEXT = get_color_from_hex('#EEF1F7')
CLR_SUB = get_color_from_hex('#8A93A8')
CLR_OK = get_color_from_hex('#37C08A')
CLR_ERR = get_color_from_hex('#F2555A')

_PICK_REQ = 0x5A5C

# 还原文件的保存位置：公共「下载」目录下的 App 专属子文件夹
_RELATIVE_DIR = 'Download/QRDecode'                    # MediaStore 用
_DISPLAY_DIR = '下载/QRDecode'                          # 界面显示
_DOC_URI = ('content://com.android.externalstorage.documents/document/'
            'primary%3ADownload%2FQRDecode')           # 浏览该目录用


# ==================== 设置持久化 / 电源管理 ====================

_SETTINGS_FILE = 'settings.json'
_DEFAULT_SETTINGS = {'keep_on': True, 'wakelock': False, 'parallel': True}


def _load_settings(app):
    s = dict(_DEFAULT_SETTINGS)
    try:
        with open(os.path.join(app.user_data_dir, _SETTINGS_FILE),
                  'r', encoding='utf-8') as f:
            d = json.load(f)
        if isinstance(d, dict):
            for k in s:
                if k in d:
                    s[k] = bool(d[k])
    except Exception:
        pass
    return s


def _save_settings(app, d):
    try:
        with open(os.path.join(app.user_data_dir, _SETTINGS_FILE),
                  'w', encoding='utf-8') as f:
            json.dump(d, f)
    except Exception:
        pass


_UI_RUNNABLES = []   # PythonJavaClass 引用保活（JNI 对象被 GC 会崩）


def _short(s, n=40):
    """异常文本截断（完整异常可达数百字符，会撑爆界面）。"""
    s = str(s or '')
    return s if len(s) <= n else s[:n] + '…'


def _post_to_ui_thread(fn):
    """把可调用对象投递到 Android UI 线程执行。

    关键：Kivy 的主循环跑在 p4a 的 SDL 线程上，**不是** Android UI 线程；
    Window 属性变更（addFlags/setKeepScreenOn）必须在 UI 线程执行，
    否则抛 ViewRootImpl$CalledFromWrongThreadException（v1.2/v1.3 真机实测）。
    """
    try:
        from jnius import autoclass, PythonJavaClass, java_method

        class _UiRunnable(PythonJavaClass):
            __javainterfaces__ = ['java/lang/Runnable']

            @java_method('()V')
            def run(self):
                try:
                    fn()
                except Exception:
                    pass

        activity = autoclass('org.kivy.android.PythonActivity').mActivity
        r = _UiRunnable()
        _UI_RUNNABLES.append(r)
        if len(_UI_RUNNABLES) > 32:
            del _UI_RUNNABLES[:16]
        activity.runOnUiThread(r)
        return True
    except Exception:
        return False


def _keep_screen_on(enable, on_result=None):
    """设置屏幕常亮（自动投递到 Android UI 线程执行）。

    on_result(ok, err) 在 UI 线程回调（可选）。
    优先 Window flags，失败退 decorView.setKeepScreenOn 双保险。
    """

    def _apply():
        ok, err = False, ''
        try:
            from jnius import autoclass
            activity = autoclass('org.kivy.android.PythonActivity').mActivity
            window = activity.getWindow()
            if enable:
                window.addFlags(0x00000080)   # FLAG_KEEP_SCREEN_ON
            else:
                window.clearFlags(0x00000080)
            ok = True
        except Exception as e:
            err = str(e)
        if not ok:
            try:
                from jnius import autoclass
                activity = autoclass('org.kivy.android.PythonActivity').mActivity
                activity.getWindow().getDecorView().setKeepScreenOn(bool(enable))
                ok, err = True, ''
            except Exception as e2:
                err = '%s / %s' % (err or '?', str(e2))
        if on_result:
            try:
                on_result(ok, err)
            except Exception:
                pass

    if not _post_to_ui_thread(_apply):
        if on_result:
            try:
                on_result(False, 'UI 线程投递失败')
            except Exception:
                pass
        return False, 'UI 线程投递失败'
    return True, ''


def _has_wake_lock_permission():
    """检查 APK 是否带 WAKE_LOCK 权限（非 Android 环境视为通过）。"""
    try:
        from jnius import autoclass
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        return PythonActivity.mActivity.checkSelfPermission(
            'android.permission.WAKE_LOCK') == 0
    except Exception:
        return True


class _WakeLock(object):
    """PARTIAL_WAKE_LOCK：锁屏后保持 CPU 运行，解码线程继续跑。

    说明：这是「够用级」的后台继续方案；彻底后台保活需要前台 Service。
    acquire() 失败不再静默——返回 (ok, err)，由界面显示原因。
    """

    def __init__(self):
        self._wl = None
        self.last_error = ''

    def acquire(self):
        if not _has_wake_lock_permission():
            self.last_error = 'APK 缺少 WAKE_LOCK 权限'
            return False, self.last_error
        try:
            if self._wl is None:
                from jnius import autoclass
                PythonActivity = autoclass('org.kivy.android.PythonActivity')
                Context = autoclass('android.content.Context')
                PowerManager = autoclass('android.os.PowerManager')
                pm = PythonActivity.mActivity.getSystemService(Context.POWER_SERVICE)
                wl = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, 'qrdecode:decode')
                wl.setReferenceCounted(False)
                self._wl = wl
            self._wl.acquire()
            self.last_error = ''
            return True, ''
        except Exception as e:
            self._wl = None
            self.last_error = str(e)
            return False, self.last_error

    def held(self):
        try:
            return self._wl is not None and self._wl.isHeld()
        except Exception:
            return False

    def release(self):
        try:
            if self._wl is not None and self._wl.isHeld():
                self._wl.release()
        except Exception:
            pass


# ==================== 进程资源采样（CPU 使用率展示） ====================
#
# 口径：本进程（含全部线程）+ 并行 fork 子进程的 CPU 时间增量，
# 除以墙钟增量 = 等效占用核数（1.0 表示吃满 1 个核，>1 表示用了多核）。
# - 本进程：Android 官方 Process.getElapsedCpuTime()（无需权限）；
#   桌面回退 os.times()。
# - fork 子进程：尝试读 /proc/<pid>/stat（同 uid 可能被 SELinux 拒绝，
#   失败静默跳过——此时只统计主进程）。

_CLK_TCK = 100
try:
    _CLK_TCK = os.sysconf('SC_CLK_TCK')
except Exception:
    pass

_ACTIVE_POOL_PIDS = []   # 并行解码 fork 子进程 pid 列表（供 CPU 采样读取）


def _cpu_cores():
    try:
        from jnius import autoclass
        return int(autoclass('java.lang.Runtime').getRuntime().availableProcessors())
    except Exception:
        return os.cpu_count() or 1


def _proc_cpu_seconds():
    """本进程累计 CPU 秒数（含全部线程）。"""
    try:
        from jnius import autoclass
        return autoclass('android.os.Process').getElapsedCpuTime() / 1000.0
    except Exception:
        pass
    try:
        t = os.times()
        return float(t[0]) + float(t[1])
    except Exception:
        return 0.0


def _pid_cpu_seconds(pid):
    """读 /proc/<pid>/stat 的 utime+stime（秒）。失败返回 None。"""
    try:
        with open('/proc/%d/stat' % pid, 'rb') as f:
            raw = f.read().decode('ascii', 'ignore')
        after = raw.rsplit(')', 1)[1].split()
        return (int(after[11]) + int(after[12])) / float(_CLK_TCK)
    except Exception:
        return None


def _total_cpu_seconds():
    total = _proc_cpu_seconds()
    for pid in list(_ACTIVE_POOL_PIDS):
        v = _pid_cpu_seconds(pid)
        if v:
            total += v
    return total


def _sys_cpu_snapshot():
    """读 /proc/stat 第一行 cpu 汇总 → (总 jiffies, 空闲 jiffies)。

    注：Android 10+ 起 /proc/stat 被 SELinux 对第三方 App 屏蔽，
    非 root 通常读不到，返回 None（由界面降级展示）。
    """
    try:
        with open('/proc/stat', 'rb') as f:
            parts = f.readline().decode('ascii', 'ignore').split()
        if not parts or parts[0] != 'cpu' or len(parts) < 5:
            return None
        vals = [int(x) for x in parts[1:9]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)   # idle + iowait
        return sum(vals), idle
    except Exception:
        return None


def _sys_loadavg():
    """读 /proc/loadavg 的 1 分钟负载（部分设备可读）。不可读返回 None。"""
    try:
        with open('/proc/loadavg', 'rb') as f:
            return float(f.read().decode('ascii', 'ignore').split()[0])
    except Exception:
        return None


class CpuMeter(object):
    """CPU 占用采样器：cur/peak 单位是「等效核数」。

    同时尽力采集系统级数据（Android 权限受限时自动为 None）：
    sys_pct 系统整体 CPU%（/proc/stat 差分）；sys_load 系统 1 分钟负载。
    """

    def __init__(self):
        self.cores = _cpu_cores()
        self.cur = 0.0
        self.peak = 0.0
        self.sys_pct = None     # 系统整体 CPU %（读得到 /proc/stat 时）
        self.sys_load = None    # 系统 1 分钟负载（读得到 /proc/loadavg 时）
        self.sys_ok = False     # /proc/stat 是否可读（决定界面展示口径）
        self._t = None
        self._cpu = None
        self._sum_cpu = 0.0
        self._sum_t = 0.0
        self._st = None         # /proc/stat 上次快照
        self._si = None

    def sample(self):
        t = time.monotonic()
        cpu = _total_cpu_seconds()
        if self._t is not None:
            dt = t - self._t
            dc = cpu - self._cpu
            if dt > 0.05 and dc >= 0:
                self.cur = dc / dt
                if self.cur > self.peak:
                    self.peak = self.cur
                self._sum_cpu += dc
                self._sum_t += dt
        self._t = t
        self._cpu = cpu
        # 系统级（尽力而为）
        snap = _sys_cpu_snapshot()
        if snap is not None:
            self.sys_ok = True
            if self._st is not None:
                dt2 = snap[0] - self._st
                di = snap[1] - self._si
                if dt2 > 0:
                    self.sys_pct = max(0.0, min(100.0,
                                                (1.0 - di / float(dt2)) * 100.0))
            self._st, self._si = snap
        self.sys_load = _sys_loadavg()

    def avg(self):
        return (self._sum_cpu / self._sum_t) if self._sum_t > 0 else 0.0


# ==================== 帧协议 ====================

def crc32(data):
    return zlib.crc32(data) & 0xFFFFFFFF


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
                try:
                    self.chunks[seq] = base64.b64decode(q[3])
                    self.seen.add(seq)
                except Exception:
                    pass  # 坏帧丢弃，GIF 循环 / 重扫会补上

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
        data = b''.join(self.chunks[i]
                        for i in range(1, self.header['total'] + 1))
        c = format(crc32(data), '08x')
        if c != self.header['crc']:
            raise RuntimeError('CRC32 校验失败')
        return data, self.header.get('name', 'restored.bin')


# ==================== 二维码识别 ====================

def decode_qr(pil_img):
    """对 PIL Image 做二维码识别，返回文本或 None。

    长边超过 _MAX_SIDE 的帧先降采样再识别：像素量减 40~60%，
    二维码每格仍有约 5px，识别率基本无损。
    """
    try:
        img = pil_img if pil_img.mode in ('L', 'RGB') else pil_img.convert('RGB')
        m = max(img.size)
        if m > _MAX_SIDE:
            r = _MAX_SIDE / float(m)
            img = img.resize((max(1, int(img.width * r)),
                              max(1, int(img.height * r))), Image.BILINEAR)
        if img.mode != 'L':
            img = img.convert('L')   # draft 已输出 L 时跳过转换
        img.load()
        codes = zbarlight.scan_codes('qrcode', img)
    except Exception:
        return None
    if codes:
        return codes[0].decode('utf-8', errors='ignore')
    return None


# ==================== 帧源（迭代器） ====================
#
# 帧负载统一为两种：
#   ('rgb', (w, h, rgb_bytes))  —— GIF 逐帧
#   ('jpg', jpeg_bytes)         —— 视频帧（Java 侧已降采样 + 内存压缩）
# 识别在主进程（单线程模式）或 fork 子进程（并行模式）中执行。

def _payload_to_image(payload):
    kind, obj = payload
    if kind == 'rgb':
        w, h, data = obj
        return Image.frombytes('RGB', (w, h), data)
    img = Image.open(io.BytesIO(obj))
    # JPEG 走 draft 降采样解码：解码器直接输出灰度（L），并按 1/2^k 档把长边
    # 缩到 <= _MAX_SIDE（PIL 的 DCT 缩放档为 1/1、1/2、1/4、1/8，请求尺寸
    # 必须 <= 源尺寸/2^k 才命中对应档），比「全解码 → resize → convert(L)」
    # 快 2~4 倍；其它格式会忽略 draft。
    try:
        w, h = img.size
        k = 0
        while k < 3 and (max(w, h) >> k) > _MAX_SIDE:
            k += 1
        img.draft('L', (max(1, w >> k), max(1, h >> k)))
    except Exception:
        pass
    return img


def iter_gif_frames(path):
    """逐帧产出 GIF 帧；提前停止时由调用方 close() 生成器以关闭文件。"""
    im = Image.open(path)
    try:
        n = getattr(im, 'n_frames', 1)
        for i in range(n):
            im.seek(i)
            rgb = im.convert('RGB')
            yield i + 1, n, ('rgb', (rgb.width, rgb.height, rgb.tobytes()))
    finally:
        try:
            im.close()
        except Exception:
            pass


def _scale_bitmap(Bitmap, bmp):
    """Java 侧降采样（在 JPEG 压缩之前做，省一半以上的编码/解码开销）。"""
    try:
        w = bmp.getWidth()
        h = bmp.getHeight()
        m = max(w, h)
        if m > _MAX_SIDE:
            r = _MAX_SIDE / float(m)
            small = Bitmap.createScaledBitmap(
                bmp, max(1, int(w * r)), max(1, int(h * r)), True)
            if small is not None:
                return small
    except Exception:
        pass
    return bmp


_JPEG_CLS = {}


def _jpeg_cls():
    """懒加载并缓存 Java 压缩相关类（fork 子进程不会调用到这里）。"""
    if not _JPEG_CLS:
        from jnius import autoclass
        _JPEG_CLS['Bitmap'] = autoclass('android.graphics.Bitmap')
        _JPEG_CLS['CompressFormat'] = autoclass('android.graphics.Bitmap$CompressFormat')
        _JPEG_CLS['BS'] = autoclass('java.io.ByteArrayOutputStream')
        _JPEG_CLS['FOS'] = autoclass('java.io.FileOutputStream')
    return _JPEG_CLS


def _bitmap_to_payload(bmp, path, state):
    """Bitmap → ('jpg', bytes)（Java 侧降采样 + 内存 JPEG）。

    state: {'mem_ok': bool, 'tmp': str|None}，跨帧复用。
    byte[] 转换不可用的机型自动退回临时文件路径。
    """
    if bmp is None:
        return None
    cls = _jpeg_cls()
    try:
        bmp = _scale_bitmap(cls['Bitmap'], bmp)
    except Exception:
        pass
    payload = None
    if state.get('mem_ok', True):
        try:
            baos = cls['BS']()
            bmp.compress(cls['CompressFormat'].JPEG, 85, baos)
            data = baos.toByteArray()
            if not isinstance(data, (bytes, bytearray)) or not data:
                raise TypeError('byte[] 无法转为 Python bytes')
            payload = ('jpg', bytes(data))
            try:
                baos.close()
            except Exception:
                pass
        except Exception:
            state['mem_ok'] = False   # 该机型数组转换受限，整段退回临时文件
    if payload is None:
        if not state.get('tmp'):
            state['tmp'] = '%s_frame_%d.jpg' % (path, threading.get_ident())
        fos = cls['FOS'](state['tmp'])
        try:
            bmp.compress(cls['CompressFormat'].JPEG, 85, fos)
        finally:
            try:
                fos.close()
            except Exception:
                pass
        with open(state['tmp'], 'rb') as f:
            payload = ('jpg', f.read())
    try:
        bmp.recycle()
    except Exception:
        pass
    return payload


def iter_video_frames(path):
    """逐帧产出视频帧（'jpg' 负载）。

    - 帧数可用时逐帧 getFrameAtIndex（API 28+）；退化用
      getFrameAtTime(t, 3=OPTION_CLOSEST)（注意 option=2 是 CLOSEST_SYNC，
      只返回关键帧，会大量漏帧）。
    - Bitmap → Java 侧降采样 → 内存 JPEG（pyjnius 把 byte[] 转 Python bytes），
      不落临时文件；转换不可用的机型自动退回临时文件。
    - 说明：v1.3 曾改成「多线程 + getFramesAtIndex 批量取帧」，真机实测反而
      更慢（多个 MMR 解码会话争抢解码器资源），v1.4 回滚为单线程逐帧。
    """
    from jnius import autoclass
    MMR = autoclass('android.media.MediaMetadataRetriever')
    mmr = MMR()
    mmr.setDataSource(path)
    state = {'mem_ok': True, 'tmp': None}
    try:
        frame_count = 0
        try:
            v = mmr.extractMetadata(_META_FRAME_COUNT)
            if v:
                frame_count = int(v)
        except Exception:
            frame_count = 0
        if frame_count > 0:
            for i in range(frame_count):
                try:
                    bmp = mmr.getFrameAtIndex(i)
                except Exception:
                    bmp = None
                yield i + 1, frame_count, _bitmap_to_payload(bmp, path, state)
        else:
            duration_ms = 0
            try:
                duration_ms = int(mmr.extractMetadata(_META_DURATION) or 0)
            except Exception:
                pass
            step_ms = 100
            total = max(1, duration_ms // step_ms)
            for i in range(total):
                try:
                    bmp = mmr.getFrameAtTime(i * step_ms * 1000, 3)
                except Exception:
                    bmp = None
                yield i + 1, total, _bitmap_to_payload(bmp, path, state)
    finally:
        try:
            mmr.release()
        except Exception:
            pass
        if state.get('tmp'):
            try:
                if os.path.exists(state['tmp']):
                    os.remove(state['tmp'])
            except Exception:
                pass



# ==================== 解码驱动（单线程 / 并行） ====================

def decode_frames_sequential(make_iter, rb, progress_cb):
    """单线程逐帧解码。make_iter 为帧源工厂（调用时才创建帧源）。"""
    frame_iter = make_iter()
    try:
        for idx, total, payload in frame_iter:
            if rb.is_done():
                break
            if payload is not None:
                try:
                    img = _payload_to_image(payload)
                    try:
                        text = decode_qr(img)
                    finally:
                        try:
                            img.close()
                        except Exception:
                            pass
                except Exception:
                    text = None
                if text:
                    rb.feed(text)
            if progress_cb:
                progress_cb(idx, total)
    finally:
        try:
            frame_iter.close()
        except Exception:
            pass


def _parallel_worker(recv_conn, send_conn):
    """并行解码子进程主体（fork 启动；只碰 PIL/zbar，绝不碰 JNI）。"""
    try:
        while True:
            payload = recv_conn.recv()
            if payload is None:
                break
            text = None
            try:
                img = _payload_to_image(payload)
                try:
                    text = decode_qr(img)
                finally:
                    try:
                        img.close()
                    except Exception:
                        pass
            except Exception:
                text = None
            send_conn.send(text)
    except Exception:
        pass   # 管道关闭 / 进程被终止：安静退出


class _ParallelDecoder(object):
    """fork 多进程并行解码器（仅 Android/POSIX 可用）。

    超时 / 进程死亡都以异常暴露，由调用方回退单线程全量重解。
    """

    FIRST_TIMEOUT = 30.0    # 首帧结果超时（秒）
    RESULT_TIMEOUT = 60.0   # 后续每帧结果超时（秒）

    def __init__(self, workers=_MAX_WORKERS):
        import multiprocessing
        self._ctx = multiprocessing.get_context('fork')
        self._n = max(2, min(workers, self._ctx.cpu_count() or 2))
        self._procs = []
        self._sends = []   # 父进程发任务
        self._recvs = []   # 父进程收结果
        self._rr = 0
        self._send_lock = threading.Lock()   # 多取帧线程并发 send 保护

    @property
    def n(self):
        return self._n

    def start(self):
        try:
            for _ in range(self._n):
                p2c_r, p2c_s = self._ctx.Pipe(duplex=False)
                c2p_r, c2p_s = self._ctx.Pipe(duplex=False)
                p = self._ctx.Process(target=_parallel_worker,
                                      args=(p2c_r, c2p_s), daemon=True)
                p.start()
                try:
                    p2c_r.close()   # 关掉父进程不用的端，保证 EOF 能被感知
                    c2p_s.close()
                except Exception:
                    pass
                self._procs.append(p)
                self._sends.append(p2c_s)
                self._recvs.append(c2p_r)
        except Exception:
            self.shutdown()
            raise

    def send(self, payload):
        # Connection 不支持并发写：多取帧线程下必须串行化
        with self._send_lock:
            self._sends[self._rr % self._n].send(payload)
            self._rr += 1

    def recv_any(self, first):
        from multiprocessing.connection import wait
        timeout = self.FIRST_TIMEOUT if first else self.RESULT_TIMEOUT
        ready = wait(self._recvs, timeout=timeout)
        if not ready:
            raise RuntimeError('并行解码等待结果超时')
        out = []
        for c in ready:
            try:
                out.append(c.recv())
            except (EOFError, OSError) as e:
                raise RuntimeError('并行解码 worker 异常退出: %s' % e)
        return out

    def shutdown(self):
        for s in self._sends:
            try:
                s.send(None)
            except Exception:
                pass
        for p in self._procs:
            try:
                p.join(timeout=0.5)
            except Exception:
                pass
        for p in self._procs:
            try:
                if p.is_alive():
                    p.terminate()
            except Exception:
                pass
        for c in self._recvs + self._sends:
            try:
                c.close()
            except Exception:
                pass


def decode_frames_parallel(make_iter, rb, progress_cb):
    """并行解码：主进程取帧，多个 fork 子进程并行识别。

    帧之间互相独立（协议按序号重组），乱序返回不影响正确性。
    任一异常向上抛出，调用方回退单线程全量重解。
    make_iter 为帧源工厂——fork worker 须先创建、再创建帧源
    （视频取帧会启动线程，fork 要先于线程创建）。
    """
    global _ACTIVE_POOL_PIDS
    pool = _ParallelDecoder()
    pool.start()
    _ACTIVE_POOL_PIDS = [p.pid for p in pool._procs]   # 供 CPU 采样统计子进程
    frame_iter = None
    in_flight = 0   # 已发未收的帧数
    results = 0
    total = 0
    first = True
    done = False
    try:
        frame_iter = make_iter()
        it = frame_iter
        while not done:
            # 填流水线：在途帧数保持在 worker 数的 4 倍，吸收取帧/解码波动
            while in_flight < 4 * pool.n:
                try:
                    idx, total, payload = next(it)
                except StopIteration:
                    break
                if payload is not None:
                    pool.send(payload)
                    in_flight += 1
            if in_flight == 0:
                break
            texts = pool.recv_any(first)
            first = False
            for text in texts:
                in_flight -= 1
                results += 1
                if text:
                    rb.feed(text)
                if progress_cb and total:
                    progress_cb(results, total)
                if rb.is_done():
                    done = True
                    break
    finally:
        if frame_iter is not None:
            try:
                frame_iter.close()
            except Exception:
                pass
        pool.shutdown()
        _ACTIVE_POOL_PIDS = []


# ==================== 自定义控件 ====================

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


# ==================== 主界面 ====================

class RootWidget(BoxLayout):
    def __init__(self, **kw):
        super().__init__(orientation='vertical', padding=dp(20), spacing=dp(12), **kw)
        self.app = App.get_running_app()
        self._settings = _load_settings(self.app)
        with self.canvas.before:
            Color(*CLR_BG)
            self._bgrect = RoundedRectangle(radius=[0])
        self.bind(pos=self._sync_bg, size=self._sync_bg)

        title = Label(text='二维码文件解析器', size_hint_y=None, height=dp(42),
                      font_size=dp(25), bold=True, color=CLR_TEXT)
        self.add_widget(title)
        subtitle = Label(text='从 GIF / 视频中解码二维码，还原文件',
                         size_hint_y=None, height=dp(26),
                         font_size=dp(13.5), color=CLR_SUB)
        self.add_widget(subtitle)
        self.add_widget(BoxLayout(size_hint_y=None, height=dp(6)))

        self.btn_pick = PrimaryButton(text='选择 GIF / 视频文件', size_hint_y=None,
                                      height=dp(62))
        self.btn_pick.bind(on_press=self.pick_file)
        self.add_widget(self.btn_pick)

        status_card = Card(orientation='vertical', size_hint_y=None, height=dp(160),
                           padding=dp(14), spacing=dp(10))
        self.status = Label(text='等待选择文件…', color=CLR_TEXT,
                            font_size=dp(14), halign='left', valign='top')
        self.status.bind(size=lambda s, *a: setattr(s, 'text_size', (s.width, None)))
        status_card.add_widget(self.status)
        self.progress = RoundedProgress(max=100, value=0, size_hint_y=None,
                                        height=dp(12))
        status_card.add_widget(self.progress)
        self.btn_open = Button(text='在文件管理器中查看', size_hint_y=None,
                               height=dp(40), background_normal='',
                               background_color=(0, 0, 0, 0), color=CLR_PRIMARY,
                               font_size=dp(14), bold=True, disabled=True)
        self.btn_open.bind(on_press=self._on_open_folder)
        status_card.add_widget(self.btn_open)
        self.add_widget(status_card)

        tips_card = Card(orientation='vertical', size_hint_y=None, height=dp(104),
                         padding=dp(16), spacing=dp(6))
        tips_card.add_widget(Label(text='使用说明', color=CLR_TEXT, bold=True,
                                   font_size=dp(15), size_hint_y=None, height=dp(24),
                                   halign='left', valign='middle'))
        for line in ('1. 选择含二维码的 GIF 或视频文件',
                     '2. 自动逐帧解码并按协议重组',
                     '3. 还原后文件存到「下载」目录，可点上方查看'):
            lb = Label(text=line, color=CLR_SUB, font_size=dp(13),
                       size_hint_y=None, height=dp(22), halign='left', valign='middle')
            lb.bind(size=lambda s, *a: setattr(s, 'text_size', (s.width, None)))
            tips_card.add_widget(lb)
        self.add_widget(tips_card)

        settings_card = Card(orientation='vertical', size_hint_y=None,
                             height=dp(150), padding=dp(14), spacing=dp(2))
        settings_card.add_widget(Label(
            text='设置', color=CLR_TEXT, bold=True, font_size=dp(15),
            size_hint_y=None, height=dp(24), halign='left', valign='middle'))
        for caption, key in (('解码时保持屏幕常亮', 'keep_on'),
                             ('锁屏后继续解码（唤醒锁）', 'wakelock'),
                             ('多核并行解码（失败自动回退）', 'parallel')):
            row = BoxLayout(size_hint_y=None, height=dp(30), spacing=dp(8))
            lb = Label(text=caption, color=CLR_SUB, font_size=dp(13.5),
                       halign='left', valign='middle')
            lb.bind(size=lambda s, *a: setattr(s, 'text_size', (s.width, None)))
            row.add_widget(lb)
            sw = Switch(active=bool(self._settings.get(key)))
            sw.size_hint_x = None
            sw.width = dp(64)
            sw.bind(active=self._on_setting(key))
            row.add_widget(sw)
            settings_card.add_widget(row)
        self.add_widget(settings_card)

        self.add_widget(BoxLayout())
        self._thread = None
        self._last_path = None
        self._wakelock = _WakeLock()
        # 解码运行统计（_update 在 worker 线程写，_refresh_stats 在主线程读）
        self._busy = False
        self._prog = (0, 0)          # (已处理帧, 总帧)
        self._t_start = 0.0
        self._last_beat = 0.0        # 最近一次帧进展时间
        self._max_gap = 0.0          # 相邻帧最大间隔（锁屏被冻结的证据）
        self._wl_check_t = 0.0       # 唤醒锁自检节流
        self._keep_note = ''         # 屏幕常亮状态（主线程更新）
        self._wl_note = ''           # 唤醒锁状态（worker 线程更新）
        self._cpu = CpuMeter()
        Clock.schedule_interval(self._refresh_stats, 0.5)

    def _sync_bg(self, *a):
        self._bgrect.pos = self.pos
        self._bgrect.size = self.size

    def _tmp_dir(self):
        """临时目录（放读取到的源文件与中间帧），用 App 私有目录即可。"""
        d = self.app.user_data_dir
        os.makedirs(d, exist_ok=True)
        return d

    # ---------- 文件选择 ----------

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
        self.btn_open.disabled = True
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
        d = self._tmp_dir()
        out = os.path.join(d, '_src_' + name)

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

    # ---------- 解码 ----------

    def _on_setting(self, key):
        def _bind(sw, value):
            self._settings[key] = bool(value)
            _save_settings(self.app, self._settings)
        return _bind

    def _apply_power(self, decoding):
        """解码期间按设置加/释放：屏幕常亮（投递 UI 线程）+ 唤醒锁。"""
        s = self._settings
        if s.get('keep_on', True):
            self._do_keep_screen(decoding)
        elif not decoding:
            self._keep_note = ''
        if s.get('wakelock', False):
            if decoding:
                ok, err = self._wakelock.acquire()
                self._wl_note = ('唤醒锁已持有' if ok
                                 else '唤醒锁失败(%s)' % _short(err or '未知'))
            else:
                self._wakelock.release()
                self._wl_note = ''

    def _do_keep_screen(self, enable):
        """常亮设置：自动投递到 Android UI 线程，执行结果回调更新状态。

        注意：Kivy 主线程 ≠ Android UI 线程（p4a 的 Python 跑在 SDL 线程），
        必须经 runOnUiThread 投递，否则抛 CalledFromWrongThreadException
        （v1.2/v1.3 真机实测）。
        """
        def _cb(ok, err):
            if not enable:
                self._keep_note = ''
            elif ok:
                self._keep_note = '屏幕常亮已开启'
            else:
                self._keep_note = '常亮失败(%s)' % _short(err)
        _keep_screen_on(enable, _cb)

    def _decode_worker(self, path):
        Clock.schedule_once(lambda dt: self._set_status(
            '已读取，开始解码: %s' % os.path.basename(path), CLR_TEXT))
        rb = Rebuilder()
        ext = os.path.splitext(str(path))[1].lower()
        if ext == '.gif':
            make_iter = lambda: iter_gif_frames(path)
        else:
            make_iter = lambda: iter_video_frames(path)
        progress = lambda i, n: self._update(i, n)
        # 运行统计初始化（供 _refresh_stats 展示）
        now = time.monotonic()
        self._busy = True
        self._prog = (0, 0)
        self._t_start = now
        self._last_beat = now
        self._max_gap = 0.0
        self._wl_check_t = 0.0
        self._cpu = CpuMeter()
        self._cpu.sample()
        self._apply_power(True)
        try:
            used_parallel = False
            if self._settings.get('parallel', True):
                try:
                    decode_frames_parallel(make_iter, rb, progress)
                    used_parallel = True
                except Exception:
                    import traceback
                    traceback.print_exc()
                    Clock.schedule_once(lambda dt: self._set_status(
                        '并行解码不可用，回退单线程…', CLR_SUB))
            if not used_parallel:
                decode_frames_sequential(make_iter, rb, progress)

            if not rb.is_done():
                got, total = rb.progress()
                Clock.schedule_once(
                    lambda dt: self._fail('帧未收齐（%s / %s）' % (got, total)))
                return
            data, name = rb.rebuild()
            where = self._save(data, name)
            Clock.schedule_once(lambda dt: self._done(where, name))
        except Exception as e:
            import traceback
            traceback.print_exc()
            msg = str(e)
            Clock.schedule_once(lambda dt: self._fail(msg))
        finally:
            self._busy = False
            self._apply_power(False)

    def _update(self, i, n):
        """worker 线程每条帧进展回调：只写共享数据，界面由 _refresh_stats 定时刷新。"""
        oi, on = self._prog
        if (isinstance(i, int) and isinstance(oi, int)
                and i < oi and n == on):
            i = oi   # 进度只增不减（并行回退等场景也不会让进度条倒退）
        self._prog = (i, n)
        now = time.monotonic()
        if self._last_beat > 0:
            gap = now - self._last_beat
            if gap > self._max_gap:
                self._max_gap = gap
        self._last_beat = now
        # 唤醒锁自愈：开关开着却没持有（首次获取失败 / 被系统回收）就重试
        if self._settings.get('wakelock', False) and now - self._wl_check_t > 5:
            self._wl_check_t = now
            if not self._wakelock.held():
                ok, err = self._wakelock.acquire()
                self._wl_note = ('唤醒锁已持有' if ok
                                 else '唤醒锁失败(%s)' % (err or '未知'))

    def _refresh_stats(self, dt):
        """主线程定时器（0.5s）：解码中刷新进度条与 CPU / 速率 / 电源状态。"""
        if not self._busy:
            return
        self._cpu.sample()
        i, n = self._prog
        if isinstance(n, int) and n > 0 and isinstance(i, int):
            self.progress.value = min(100, i * 100 // n)
        cores = self._cpu.cores or 1
        rate = ''
        if self._t_start and isinstance(i, int) and i:
            el = time.monotonic() - self._t_start
            if el > 0.5:
                rate = '（%.1f 帧/秒）' % (i / el)
        line2 = '应用CPU %.0f%%（%.1f/%d核）' % (
            self._cpu.cur * 100.0 / cores, self._cpu.cur, cores)
        if self._cpu.sys_pct is not None:
            line2 += ' ｜ 系统CPU %.0f%%' % self._cpu.sys_pct
        elif self._cpu.sys_ok:
            line2 += ' ｜ 系统CPU …'      # /proc/stat 可读，首次差分未就绪
        elif self._cpu.sys_load is not None:
            line2 += ' ｜ 系统负载 %.1f' % self._cpu.sys_load
        else:
            line2 += ' ｜ 系统CPU受限'   # Android 10+ SELinux 屏蔽 /proc/stat
        line2 += ' ｜ 停顿%.1fs' % self._max_gap
        text = '解码中… 帧 %s / %s%s\n%s' % (i, n, rate, line2)
        extra = [x for x in (self._keep_note, self._wl_note) if x]
        if extra:
            text += '\n' + ' ｜ '.join(extra)
        self._set_status(text, CLR_TEXT)

    def _save(self, data, name):
        """保存到公共「下载/QRDecode」目录（MediaStore，Android 10+）。

        失败退回 App 私有目录。返回给界面显示的保存位置描述。
        """
        try:
            from jnius import autoclass
            MediaStoreDownloads = autoclass('android.provider.MediaStore$Downloads')
            ContentValues = autoclass('android.content.ContentValues')
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            resolver = PythonActivity.mActivity.getContentResolver()

            values = ContentValues()
            values.put('_display_name', name)
            # App 专属子目录：/storage/emulated/0/Download/QRDecode/
            values.put('relative_path', _RELATIVE_DIR)
            uri = resolver.insert(MediaStoreDownloads.EXTERNAL_CONTENT_URI, values)
            if uri is None:
                raise RuntimeError('MediaStore.insert 返回空')
            # 用 fd + Python os.write 写入（避开 pyjnius 的 Java 流/数组问题）
            pfd = resolver.openFileDescriptor(uri, 'w')
            try:
                fd = pfd.getFd()
                mv = memoryview(data)
                off = 0
                while off < len(mv):
                    off += os.write(fd, mv[off:])
            finally:
                try:
                    pfd.close()
                except Exception:
                    pass
            return _DISPLAY_DIR + '/' + name
        except Exception:
            import traceback
            traceback.print_exc()
            d = self._tmp_dir()
            out = os.path.join(d, name)
            with open(out, 'wb') as f:
                f.write(data)
            return out

    def _done(self, where, name):
        self.btn_pick.disabled = False
        self.progress.value = 100
        self._last_path = where
        self.btn_open.disabled = False
        dur = (time.monotonic() - self._t_start) if self._t_start else 0.0
        cores = self._cpu.cores or 1
        avg = self._cpu.avg()
        extra = '用时 %.1fs ｜ 平均应用CPU %.0f%%（%.1f/%d核）' % (
            dur, avg * 100.0 / cores, avg, cores)
        if self._max_gap > 1.0:
            extra += ' ｜ 最长停顿 %.1fs' % self._max_gap
        self._set_status('还原成功：%s\n保存位置：%s\n%s' % (name, where, extra),
                         CLR_OK)

    def _fail(self, msg):
        self.btn_pick.disabled = False
        self.btn_open.disabled = True
        self.progress.value = 0
        self._set_status('失败: %s' % msg, CLR_ERR)

    def _set_status(self, text, color):
        self.status.text = text
        self.status.color = color

    # ---------- 打开文件管理器 ----------

    def _on_open_folder(self, instance):
        self._open_folder()

    def _open_folder(self):
        """打开系统文件管理器并直接进入 App 的「下载/QRDecode」目录。

        用 ACTION_VIEW + SAF 的 document URI（MIME = vnd.android.document/directory）
        直接浏览该目录。注意：
        - `resource/folder` 是第三方约定，系统 DocumentsUI 不认（会弹「打开方式」）；
        - `ACTION_OPEN_DOCUMENT_TREE` 是「选择目录」语义，且 Android 11+ 禁止选中
          Download / Android/data（会提示「无法使用此文件夹」）。
        """
        from jnius import autoclass
        Intent = autoclass('android.content.Intent')
        Uri = autoclass('android.net.Uri')
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        activity = PythonActivity.mActivity

        # 1) 直接浏览「下载/QRDecode」目录
        try:
            uri = Uri.parse(_DOC_URI)
            intent = Intent(Intent.ACTION_VIEW)
            intent.setDataAndType(uri, 'vnd.android.document/directory')
            activity.startActivity(intent)
            return
        except Exception:
            pass

        # 2) 退化为浏览「下载」目录
        try:
            uri = Uri.parse(
                'content://com.android.externalstorage.documents/document/primary%3ADownload')
            intent = Intent(Intent.ACTION_VIEW)
            intent.setDataAndType(uri, 'vnd.android.document/directory')
            activity.startActivity(intent)
            return
        except Exception:
            pass

        # 3) 兜底：文件管理器的内容选择界面
        try:
            intent = Intent(Intent.ACTION_GET_CONTENT)
            intent.setType('*/*')
            activity.startActivity(intent)
            return
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._set_status('无法打开文件管理器: %s' % e, CLR_ERR)


class QrDecodeApp(App):
    def build(self):
        return RootWidget()


if __name__ == '__main__':
    QrDecodeApp().run()
