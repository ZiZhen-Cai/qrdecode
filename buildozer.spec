[app]
title = 二维码文件解析器
package.name = qrdecode
package.domain = org.getfile

source.dir = .
source.include_exts = py,png,jpg,jpeg,kv,atlas,gif
source.include_patterns = assets/*,images/*.png

version = 1.0

requirements = python3,kivy,pillow,zbarlight,plyer

orientation = portrait
fullscreen = 0

android.permissions = INTERNET,READ_EXTERNAL_STORAGE,READ_MEDIA_VIDEO,READ_MEDIA_IMAGES
android.api = 33
android.minapi = 21
android.archs = arm64-v8a

android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 0
