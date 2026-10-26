# -*- mode: python ; coding: utf-8 -*-
"""行间 Interline 打包配置。

单文件 onefile，不启用 UPX：onefile 本身已 deflate，UPX 净收益约 10%，
但会让每次启动多一层解压、易被误报、Qt 的大 DLL 被压后偶发加载失败。
"""
import re

_src = open("main.py", encoding="utf-8").read()
_bv = re.search(r"^BUILD_VERSION\s*=\s*(\d+)", _src, re.M).group(1)

# 版本资源：右键属性、任务管理器里显示的就是这里的英文名
_VERSION_INFO = f'''# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({_bv}, 0, 0, 0),
    prodvers=({_bv}, 0, 0, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
    ),
  kids=[
    StringFileInfo(
      [
      StringTable(
        '040904b0',
        [StringStruct('FileDescription', 'Interline Text Layout'),
         StringStruct('FileVersion', '{_bv}.0.0.0'),
         StringStruct('ProductName', 'Interline'),
         StringStruct('ProductVersion', '{_bv}.0.0.0'),
         StringStruct('OriginalFilename', 'Interline.exe')])
      ]),
    VarFileInfo([VarStruct('Translation', [0x0409, 0x04b0])])
  ]
)
'''
with open("version_info.txt", "w", encoding="utf-8") as _f:
    _f.write(_VERSION_INFO)

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[],
    # QtTextToSpeech 是延迟导入的，静态分析抓不到；
    # QtMultimedia 是 texttospeech 插件的隐藏依赖（音频输出链路）
    hiddenimports=[
        "PyQt6.QtTextToSpeech",
        "PyQt6.QtMultimedia",
        # PDF 解析在函数里延迟导入，显式列出来免得漏收
        "pdfminer.high_level",
        "pdfminer.layout",
        "pdfminer.pdfdocument",
        "pdfminer.pdfpage",
        "pdfminer.pdfparser",
        "pdfminer.pdftypes",
        "pdfminer.converter",
        "pdfminer.cmapdb",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets", "PyQt6.QtWebEngineQuick",
        "PyQt6.QtPdf", "PyQt6.QtPdfWidgets",
        "PyQt6.Qt3DCore", "PyQt6.Qt3DRender", "PyQt6.Qt3DAnimation", "PyQt6.Qt3DInput",
        "PyQt6.QtQuick", "PyQt6.QtQml", "PyQt6.QtQuick3D", "PyQt6.QtQuickWidgets",
        "PyQt6.QtBluetooth", "PyQt6.QtNfc", "PyQt6.QtPositioning", "PyQt6.QtSerialPort",
        "PyQt6.QtSql", "PyQt6.QtTest", "PyQt6.QtDesigner", "PyQt6.QtHelp",
        "tkinter", "unittest", "pydoc", "doctest",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# 剔除用不上的重货：ffmpeg 解码后端（~15MB）、软件 OpenGL 回退（~20MB）
_DROP = ("ffmpeg", "avcodec", "avformat", "avutil", "swresample", "swscale",
         "opengl32sw")
a.binaries = [b for b in a.binaries if not any(k in b[0].lower() for k in _DROP)]
a.datas = [d for d in a.datas if not any(k in d[0].lower() for k in _DROP)]

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Interline",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["icon.ico"],
    version="version_info.txt",
)
