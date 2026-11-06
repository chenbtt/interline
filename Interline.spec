# -*- mode: python ; coding: utf-8 -*-
"""奶蛙阅读器 Interline 打包配置。

单文件 onefile，不启用 UPX：onefile 本身已 deflate，UPX 净收益约 10%，
但会让每次启动多一层解压、易被误报、Qt 的大 DLL 被压后偶发加载失败。
"""
import re

_src = open("main.py", encoding="utf-8").read()
_bv = re.search(r"^BUILD_VERSION\s*=\s*(\d+)", _src, re.M).group(1)

# 奶蛙素材。逐条列，不用通配：PyInstaller 的 datas 不展开 glob。
# 运行时路径由 assets.resource_path() 按 sys._MEIPASS 解析。
# 贴边那 24 张例外——spec 本身就是 Python，在这儿展开 glob 比手抄 24 行
# 可靠得多：加相位不用改这里，数量对不上也直接断言失败，而不是只在
# 打包版里表现为「图加载不出来」（源码跑一切正常，这种漏法最难发现）。
import glob as _glob

_NAIWA = "assets/naiwa"
_EDGE_DIRS = ("n", "ne", "e", "se", "s", "sw", "w", "nw")
_EDGE_PHASES = ("base", "blink", "hi")
_EDGES = sorted(_glob.glob(f"{_NAIWA}/edges/*.png"))
assert len(_EDGES) == len(_EDGE_DIRS) * len(_EDGE_PHASES), (
    f"贴边素材应为 {len(_EDGE_DIRS) * len(_EDGE_PHASES)} 张，"
    f"实际 {len(_EDGES)} 张——先跑 make_pet_edges.py --build")

_PET_DATAS = [
    (f"{_NAIWA}/sheet.png", _NAIWA),
    (f"{_NAIWA}/manifest.json", _NAIWA),
    (f"{_NAIWA}/sfx/laugh.mp3", f"{_NAIWA}/sfx"),
    (f"{_NAIWA}/sfx/voice_01.mp3", f"{_NAIWA}/sfx"),
    (f"{_NAIWA}/sfx/voice_02.mp3", f"{_NAIWA}/sfx"),
    (f"{_NAIWA}/sfx/voice_03.mp3", f"{_NAIWA}/sfx"),
    (f"{_NAIWA}/sfx/voice_04.mp3", f"{_NAIWA}/sfx"),
    (f"{_NAIWA}/edges/meta.json", f"{_NAIWA}/edges"),
]
_PET_DATAS += [(_f, f"{_NAIWA}/edges") for _f in _EDGES]

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
    datas=_PET_DATAS,
    # QtTextToSpeech 是延迟导入的，静态分析抓不到；
    # QtMultimedia 是 texttospeech 插件的隐藏依赖（音频输出链路）
    hiddenimports=[
        "PyQt6.QtTextToSpeech",
        "PyQt6.QtMultimedia",
        # 在线音色：edge_tts 及其 aiohttp/certifi 依赖链由 PyInstaller hook 收集，
        # 这里显式列入口模块，免得静态分析漏掉（实测最小探针 exe 可联网合成）
        "edge_tts",
        "edge_speech",
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
