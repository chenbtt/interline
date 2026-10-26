"""书本解析：TXT（自动识别 utf-8 / gb18030）与 EPUB（zipfile + ElementTree + HTMLParser）。

对外只暴露 Book / Chapter 和一个 load_book()，上层不关心格式差异。
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import urllib.parse
import zipfile
from html.parser import HTMLParser
from xml.etree import ElementTree as ET

# 单章上限：超过就拆开，避免一个 QTextDocument 太大导致滚动发涩
MAX_CHAPTER_CHARS = 60000
# 无章节标记的纯文本，按这个大小切块
BLOCK_CHARS = 20000

CHAPTER_RE = re.compile(
    r"^\s*(?:"
    r"第\s*[0-9０-９零〇一二三四五六七八九十百千万两]+\s*[章节回卷集部篇话]"
    r"|[Cc]hapter\s+\d+"
    r"|序章|序言|楔子|引子|尾声|后记|番外|终章"
    r")"
)

# 标题不该有的字符。正文句子几乎必带句末标点，靠这个把
# 「第二章的内容在这里。」这类以「第X章」开头的正文挡掉。
_TITLE_STOP = "。！？；!?;"
_TITLE_MAX_LEN = 28


def looks_like_title(s: str) -> bool:
    if not s or len(s) > _TITLE_MAX_LEN:
        return False
    if any(c in s for c in _TITLE_STOP):
        return False
    return bool(CHAPTER_RE.match(s))


class Chapter:
    __slots__ = ("title", "text")

    def __init__(self, title: str, text: str):
        self.title = title
        self.text = text

    def __repr__(self):
        return f"<Chapter {self.title!r} {len(self.text)}字>"


class Book:
    def __init__(self, path: str, title: str, fmt: str, chapters: list[Chapter]):
        self.path = path
        self.title = title
        self.format = fmt
        self.chapters = chapters
        self.encoding = ""

    def __len__(self):
        return len(self.chapters)


# ---------------------------------------------------------------- 解码

def decode_bytes(raw: bytes) -> tuple[str, str]:
    """返回 (文本, 编码名)。先严格 utf-8，失败再退 gb18030（GBK 超集）。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace"), "utf-8-sig"
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    for enc in ("gb18030", "big5"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace"), "utf-8(替换)"


# ---------------------------------------------------------------- TXT

def _split_txt_chapters(text: str) -> list[Chapter]:
    lines = text.splitlines()
    chapters: list[Chapter] = []
    cur_title = ""
    buf: list[str] = []

    for line in lines:
        s = line.strip()
        if s and looks_like_title(s):
            if buf:
                chapters.append(Chapter(cur_title or "开篇", "\n".join(buf)))
            cur_title = s
            buf = []
        elif s:
            buf.append(s)
    if buf:
        chapters.append(Chapter(cur_title or "开篇", "\n".join(buf)))

    if not chapters:
        chapters = [Chapter(cur_title or "正文", text.strip())]
    return chapters


def _split_oversize(chapters: list[Chapter]) -> list[Chapter]:
    """超长章节按段落拆开；整本没有章节标记时按字数切块。"""
    out: list[Chapter] = []
    for ch in chapters:
        if len(ch.text) <= MAX_CHAPTER_CHARS:
            out.append(ch)
            continue
        paras = ch.text.split("\n")
        buf: list[str] = []
        size = 0
        part = 1
        for p in paras:
            # 单段就超限（极少见）时硬切
            if len(p) > MAX_CHAPTER_CHARS:
                for i in range(0, len(p), MAX_CHAPTER_CHARS):
                    out.append(Chapter(f"{ch.title} · {part}" if part > 1 else ch.title,
                                       p[i:i + MAX_CHAPTER_CHARS]))
                    part += 1
                continue
            buf.append(p)
            size += len(p) + 1
            if size >= MAX_CHAPTER_CHARS:
                out.append(Chapter(f"{ch.title} · {part}" if part > 1 else ch.title,
                                   "\n".join(buf)))
                buf = []
                size = 0
                part += 1
        if buf:
            out.append(Chapter(f"{ch.title} · {part}" if part > 1 else ch.title,
                               "\n".join(buf)))
    return out


def _split_plain_blocks(text: str) -> list[Chapter]:
    """没有任何章节标记的书：按字数和段落边界切。"""
    paras = [p for p in text.split("\n") if p.strip()]
    out: list[Chapter] = []
    buf: list[str] = []
    size = 0
    idx = 1
    for p in paras:
        buf.append(p)
        size += len(p) + 1
        if size >= BLOCK_CHARS:
            out.append(Chapter(f"第 {idx} 部分", "\n".join(buf)))
            buf = []
            size = 0
            idx += 1
    if buf:
        out.append(Chapter(f"第 {idx} 部分", "\n".join(buf)))
    return out or [Chapter("正文", text)]


def parse_txt(path: str) -> Book:
    with open(path, "rb") as f:
        raw = f.read()
    text, enc = decode_bytes(raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u3000", " ")

    chapters = _split_txt_chapters(text)
    # 只解析出「开篇」一章且体积不小 → 这本书没有章节标记，按字数切块便于导航
    if len(chapters) == 1 and len(chapters[0].text) > BLOCK_CHARS:
        chapters = _split_plain_blocks(chapters[0].text)
    chapters = _split_oversize(chapters)

    title = _guess_title(path, chapters)
    book = Book(path, title, "txt", chapters)
    book.encoding = enc
    return book


def _guess_title(path: str, chapters: list[Chapter]) -> str:
    name = os.path.splitext(os.path.basename(path))[0]
    name = re.sub(r"[（(\[].*?[)）\]]", "", name).strip()
    return name or "未命名"


# ---------------------------------------------------------------- EPUB

_SKIP_TAGS = {"script", "style", "head", "svg", "noscript", "rt", "rp", "template"}
_VOID_TAGS = {"br", "img", "hr", "meta", "link", "input", "area", "base", "col",
              "embed", "source", "track", "wbr", "image"}
_BLOCK_TAGS = {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li",
               "blockquote", "section", "article", "tr", "pre", "figure", "figcaption"}


class HtmlToText(HTMLParser):
    """把 XHTML 抓成纯文本。容错优先——真实电子书里非良构文档非常多。"""

    def __init__(self, hidden_classes=None):
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip_stack: list[str] = []
        self._hidden_classes = {c.lower() for c in (hidden_classes or ())}

    # -- 内部 --
    def _hidden(self, attrs) -> bool:
        d = dict(attrs)
        style = (d.get("style") or "").replace(" ", "").lower()
        if "display:none" in style or "visibility:hidden" in style:
            return True
        cls = (d.get("class") or "").lower()
        if cls and self._hidden_classes:
            if any(c in self._hidden_classes for c in cls.split()):
                return True
        return False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _VOID_TAGS:
            if tag in ("br", "hr"):
                self._out.append("\n")
            return
        if self._skip_stack or tag in _SKIP_TAGS or self._hidden(attrs):
            self._skip_stack.append(tag)
            return
        if tag in _BLOCK_TAGS:
            self._out.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag.lower() in ("br", "hr"):
            self._out.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._skip_stack:
            # 容错：从栈里最近一个同名标签开始出栈
            if tag in self._skip_stack:
                while self._skip_stack:
                    if self._skip_stack.pop() == tag:
                        break
            return
        if tag in _BLOCK_TAGS:
            self._out.append("\n")

    def handle_data(self, data):
        if self._skip_stack:
            return
        if data:
            self._out.append(data)

    def text(self) -> str:
        raw = "".join(self._out)
        lines = []
        for line in raw.split("\n"):
            s = re.sub(r"[ \t\u00a0]+", " ", line).strip()
            if s:
                lines.append(s)
        return "\n".join(lines)


_STYLE_BLOCK_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.S | re.I)
_CSS_RULE_RE = re.compile(r"([^{}]+)\{([^}]*)\}")
_CSS_CLASS_RE = re.compile(r"\.([A-Za-z_][\w-]*)")


def hidden_classes_from_css(html: str) -> set[str]:
    """找出被 CSS 藏起来的类名。

    不少电子书用 `.dup{display:none}` 放重复正文、或用它藏注释，
    不排除的话正文里会混进不该出现的内容。
    """
    found: set[str] = set()
    for block in _STYLE_BLOCK_RE.findall(html):
        for selector, body in _CSS_RULE_RE.findall(block):
            flat = body.replace(" ", "").lower()
            if "display:none" in flat or "visibility:hidden" in flat:
                for cls in _CSS_CLASS_RE.findall(selector):
                    found.add(cls.lower())
    return found


def html_to_text(html: str, hidden_classes=None) -> str:
    if hidden_classes is None:
        hidden_classes = hidden_classes_from_css(html)
    p = HtmlToText(hidden_classes)
    try:
        p.feed(html)
        p.close()
    except Exception:
        # 解析器崩了也要拿到已经解析出来的部分
        pass
    return p.text()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _join(base: str, href: str) -> str:
    href = urllib.parse.unquote(href.split("#")[0])
    if href.startswith("/"):
        href = href.lstrip("/")
    return posixpath.normpath(posixpath.join(base, href))


class _Zip:
    """zip 里的大小写不一定规范，统一走大小写不敏感查找。"""

    def __init__(self, zf: zipfile.ZipFile):
        self.zf = zf
        self._map = {n.lower(): n for n in zf.namelist()}

    def names(self):
        return list(self._map.values())

    def read(self, name: str):
        real = self._map.get(name.lower())
        if not real:
            return None
        try:
            info = self.zf.getinfo(real)
            if info.file_size > 32 * 1024 * 1024:  # 防 zip 炸弹
                return None
            return self.zf.read(real)
        except Exception:
            return None

    def text(self, name: str) -> str:
        raw = self.read(name)
        if raw is None:
            return ""
        for enc in ("utf-8", "gb18030"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "replace")


def _parse_nav_titles(z: _Zip, manifest: dict, base: str) -> dict:
    """从 EPUB3 nav 或 EPUB2 ncx 里取 href -> 标题，取不到就返回空表。

    manifest 由 parse_epub 建好传进来，避免同一份 OPF 解析两遍。
    """
    titles: dict[str, str] = {}

    nav_href = ""
    ncx_href = ""
    for item in manifest.values():
        href = item.get("href") or ""
        mtype = item.get("type") or ""
        props = item.get("props") or ""
        if "nav" in props.split() and mtype == "application/xhtml+xml":
            nav_href = href
        if mtype == "application/x-dtbncx+xml":
            ncx_href = href

    if nav_href:
        html = z.text(_join(base, nav_href))
        if html:
            class _Nav(HTMLParser):
                def __init__(self):
                    super().__init__(convert_charrefs=True)
                    self.items = []
                    self._href = None
                    self._buf = []

                def handle_starttag(self, tag, attrs):
                    if tag.lower() == "a":
                        self._href = dict(attrs).get("href")
                        self._buf = []

                def handle_data(self, data):
                    if self._href is not None:
                        self._buf.append(data)

                def handle_endtag(self, tag):
                    if tag.lower() == "a" and self._href:
                        label = "".join(self._buf).strip()
                        if label:
                            self.items.append((self._href, label))
                        self._href = None

            nav = _Nav()
            try:
                nav.feed(html)
                nav.close()
            except Exception:
                pass
            nav_base = dirname_of(_join(base, nav_href))
            for href, label in nav.items:
                titles[_join(nav_base, href)] = label

    if not titles and ncx_href:
        ncx = z.text(_join(base, ncx_href))
        if ncx:
            try:
                nroot = ET.fromstring(ncx.encode("utf-8"))
                for el in nroot.iter():
                    if _local(el.tag) != "navPoint":
                        continue
                    src = label = ""
                    for child in el.iter():
                        ln = _local(child.tag)
                        if ln == "content" and not src:
                            src = child.get("src") or ""
                        elif ln == "text" and not label:
                            label = (child.text or "").strip()
                    if src and label:
                        titles[_join(base, src)] = label
            except ET.ParseError:
                pass
    return titles


def dirname_of(path: str) -> str:
    d = posixpath.dirname(path)
    return d + "/" if d else ""


def parse_epub(path: str) -> Book:
    with zipfile.ZipFile(path) as zf:
        z = _Zip(zf)

        # 1) container.xml -> opf 路径
        opf_path = ""
        container = z.text("META-INF/container.xml")
        if container:
            try:
                croot = ET.fromstring(container.encode("utf-8"))
                for el in croot.iter():
                    if _local(el.tag) == "rootfile":
                        opf_path = el.get("full-path") or ""
                        break
            except ET.ParseError:
                pass
        if not opf_path:
            for name in z.names():
                if name.lower().endswith(".opf"):
                    opf_path = name
                    break
        if not opf_path:
            raise ValueError("epub 里找不到 opf 文件")

        opf_text = z.text(opf_path)
        if not opf_text:
            raise ValueError("opf 读取失败")
        try:
            opf_root = ET.fromstring(opf_text.encode("utf-8"))
        except ET.ParseError as e:
            raise ValueError(f"opf 不是合法 XML：{e}")

        base = dirname_of(opf_path)

        # 2) 书名
        title = ""
        for el in opf_root.iter():
            if _local(el.tag) == "title" and (el.text or "").strip():
                title = el.text.strip()
                break

        # 3) manifest / spine
        manifest = {}
        for el in opf_root.iter():
            if _local(el.tag) == "item":
                mid = el.get("id")
                if mid:
                    manifest[mid] = {
                        "href": el.get("href") or "",
                        "type": el.get("media-type") or "",
                        "props": el.get("properties") or "",
                    }

        order: list[tuple[str, str]] = []  # (zip 内路径, media type)
        for el in opf_root.iter():
            if _local(el.tag) != "itemref":
                continue
            if (el.get("linear") or "yes").lower() == "no":
                continue
            item = manifest.get(el.get("idref") or "")
            if not item or not item["href"]:
                continue
            mtype = item["type"].lower()
            if "html" not in mtype:
                continue
            order.append((_join(base, item["href"]), mtype))

        if not order:
            # spine 缺失时按文件名兜底
            for name in sorted(z.names()):
                if name.lower().endswith((".xhtml", ".html", ".htm")):
                    order.append((name, "application/xhtml+xml"))
        if not order:
            raise ValueError("epub 里没有可读的正文文档")

        nav_titles = _parse_nav_titles(z, manifest, base)

        # 4) 逐篇抽正文
        chapters: list[Chapter] = []
        for doc_path, _mtype in order:
            html = z.text(doc_path)
            if not html:
                continue
            body = html_to_text(html)
            if not body.strip():
                continue
            label = nav_titles.get(doc_path, "")
            if not label:
                first = body.split("\n", 1)[0].strip()
                label = first[:30] if len(first) <= 30 else f"第 {len(chapters) + 1} 节"
            chapters.append(Chapter(label, body))

        if not chapters:
            raise ValueError("epub 正文为空，可能是加密或固定版式书")

    title = title or _guess_title(path, chapters)
    book = Book(path, title, "epub", _split_oversize(chapters))
    book.encoding = "utf-8"
    return book


# ---------------------------------------------------------------- 统一入口

def load_book(path: str) -> Book:
    suffix = os.path.splitext(path)[1].lower()
    if suffix == ".pdf":
        return load_pdf(path)
    if suffix == ".epub":
        return parse_epub(path)
    if suffix in (".html", ".htm", ".xhtml"):
        with open(path, "rb") as f:
            text, enc = decode_bytes(f.read())
        book = Book(path, _guess_title(path, []), "html",
                    _split_oversize(_split_txt_chapters(html_to_text(text))))
        book.encoding = enc
        return book
    # 无扩展名的文件当纯文本读；其余一律拒绝，
    # 免得把 pdf/mobi 当文本解出一屏乱码
    if suffix in (".txt", ".md", ".text", ".log", ""):
        return parse_txt(path)
    raise ValueError(
        f"不支持的格式：{suffix or '无扩展名'}（支持 txt / md / epub / html / pdf）")


# ---------------------------------------------------------------- PDF
#
# PDF 是版面格式：文字是散落在页面坐标上的文本块，没有段落概念。
# 这里靠「首行缩进」断段——正文左边距固定，段首行会比它多出两个字宽。
# 不能只看行尾标点：段落中间的句子也常以句号结尾，会切出半句话。

_PDF_LAPARAMS = None

# 段末常见的右对齐日期，如「（2015-05-25）」
_DATE_LINE = re.compile(r"^[（(]\s*\d{4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2}\s*[）)]$")


def _pdf_libs():
    """延迟导入：没装 pdfminer 时，TXT/EPUB 照样能用。"""
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LAParams, LTTextContainer, LTTextLine
    from pdfminer.pdfdocument import PDFDocument
    from pdfminer.pdfpage import PDFPage
    from pdfminer.pdfparser import PDFParser
    from pdfminer.pdftypes import resolve1
    return (extract_pages, LAParams, LTTextContainer, LTTextLine,
            PDFDocument, PDFPage, PDFParser, resolve1)


def _dest_page(dest, page_index, resolve1):
    if dest is None:
        return None
    try:
        d = resolve1(dest)
    except Exception:
        return None
    if not isinstance(d, list) or not d:
        return None
    ref = d[0]
    if ref in page_index:
        return page_index[ref]
    obj = getattr(ref, "objid", None)
    if obj is not None:
        for key, val in page_index.items():
            key_id = key[0] if isinstance(key, tuple) else key
            if key_id == obj:
                return val
    return None


def _pdf_outline(path: str):
    """返回 ([(标题, 起始页)], 总页数)。没有大纲时标题表为空。"""
    (_, _, _, _, PDFDocument, PDFPage, PDFParser, resolve1) = _pdf_libs()
    try:
        with open(path, "rb") as f:
            doc = PDFDocument(PDFParser(f))
            pages = list(PDFPage.create_pages(doc))
            index = {p.pageid: i for i, p in enumerate(pages)}
            rows = []
            for _level, title, dest, _a, _se in doc.get_outlines():
                page = _dest_page(dest, index, resolve1)
                if page is not None:
                    rows.append((str(title).strip(), page))
            return rows, len(pages)
    except Exception:
        return [], 0


def _pdf_page_text(path: str, pages) -> str:
    """把若干页的文字按首行缩进合并成段落。"""
    import logging
    import statistics
    from collections import Counter

    (extract_pages, LAParams, LTTextContainer, LTTextLine,
     *_rest) = _pdf_libs()
    logging.getLogger("pdfminer").setLevel(logging.ERROR)

    global _PDF_LAPARAMS
    if _PDF_LAPARAMS is None:
        _PDF_LAPARAMS = LAParams(line_margin=0.35, char_margin=1.5,
                                 word_margin=0.15)
    rows = []
    try:
        for page in extract_pages(path, page_numbers=list(pages),
                                  laparams=_PDF_LAPARAMS):
            for el in page:
                if not isinstance(el, LTTextContainer):
                    continue
                for line in el:
                    if not isinstance(line, LTTextLine):
                        continue
                    t = line.get_text().strip()
                    if not t:
                        continue
                    # 页脚页码：短且纯数字
                    if len(t) <= 4 and t.isdigit():
                        continue
                    rows.append((line.x0, line.x1, t))
    except Exception:
        pass
    if not rows:
        return ""

    base = Counter(round(x0) for x0, _, _ in rows).most_common(1)[0][0]
    widths = [(x1 - x0) / max(1, len(t)) for x0, x1, t in rows]
    cw = statistics.median(widths) if widths else 15.0

    paras, cur = [], []
    for x0, _x1, t in rows:
        # 段末的右对齐日期（如「（2015-05-25）」）会被 x0 当成新段首行，
        # 单拎出来会多出一个空段落，这里并回上一段
        if cur and not _DATE_LINE.match(t) and x0 > base + cw * 1.2:
            paras.append("".join(cur))
            cur = [t]
        else:
            cur.append(t)
    if cur:
        paras.append("".join(cur))
    return "\n\n".join(p.strip() for p in paras if p.strip())


def _cut_at_title(text: str, title: str) -> str:
    """从标题处截断，并把标题那段本身去掉（外壳会再渲染一次标题）。

    PDF 大纲的页码只精确到「页」，而标题常在某页中间，于是本章开头会带出
    上一章的尾巴（半句话）。正文里既然有这个标题文字，就从它那里切。
    找不到就原样返回——宁可贵几行，也不要把正文裁掉。
    """
    if not text or not title:
        return text
    t = title.strip()
    i = text[:1200].find(t)
    if i < 0:
        return text
    rest = text[i:]
    head, sep, tail = rest.partition("\n\n")
    if sep and head.strip() == t:
        return tail        # 标题自成一段，连它一起去掉
    if rest.strip() == t:
        return ""
    return rest


def _pdf_cache_dir() -> str:
    base = os.environ.get("INTERLINE_DATA_DIR")
    if not base:
        base = os.path.join(
            os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Interline")
    d = os.path.join(base, "pdfcache")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


class _PdfCache:
    """按「文件 + 修改时间」缓存已解析的章节，第二次打开就不用再等。"""

    def __init__(self, path: str):
        import hashlib
        try:
            mtime = int(os.path.getmtime(path))
        except OSError:
            mtime = 0
        h = hashlib.md5(os.path.abspath(path).encode("utf-8")).hexdigest()[:12]
        self.file = os.path.join(_pdf_cache_dir(), f"{h}_{mtime}.json")
        try:
            with open(self.file, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            self.data = {}

    def get(self, idx: int):
        v = self.data.get(str(idx))
        return v if isinstance(v, str) else None

    def put(self, idx: int, text: str) -> None:
        self.data[str(idx)] = text
        try:
            tmp = self.file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.file)
        except Exception:
            pass


class PdfChapter:
    """PDF 章节：正文第一次被访问时才去解析页，避免打开书就卡一分钟。"""

    __slots__ = ("title", "_text", "_load", "_pages")

    def __init__(self, title: str, load, pages):
        self.title = title
        self._text = None
        self._load = load
        self._pages = pages

    @property
    def text(self) -> str:
        if self._text is None:
            self._text = _cut_at_title(self._load(self._pages), self.title)
        return self._text

    @property
    def loaded(self) -> bool:
        return self._text is not None

    def __repr__(self):
        n = "未解析" if self._text is None else f"{len(self._text)}字"
        return f"<PdfChapter {self.title!r} {n}>"


def load_pdf(path: str) -> Book:
    outline, total = _pdf_outline(path)
    if not total:
        raise ValueError("这个 PDF 打不开，可能已损坏")

    cache = _PdfCache(path)

    def make_loader(idx: int):
        def load(pages):
            hit = cache.get(idx)
            if hit is not None:
                return hit
            text = _pdf_page_text(path, pages)
            cache.put(idx, text)
            return text
        return load

    marks = []
    seen = set()
    for title, start in outline:
        if start in seen or start >= total:
            continue
        seen.add(start)
        marks.append((title, start))

    chapters = []
    if marks:
        for i, (title, start) in enumerate(marks):
            end = total
            for _t, nxt in marks[i + 1:]:
                if nxt > start:
                    end = nxt
                    break
            chapters.append(PdfChapter(title, make_loader(i),
                                       range(start, min(end, total))))
    else:
        # 没有大纲就按每 10 页一块切，至少能读
        step = 10
        for i in range(0, total, step):
            chapters.append(PdfChapter(f"第 {i // step + 1} 部分", make_loader(i),
                                       range(i, min(i + step, total))))

    chapters = [c for c in chapters if c._pages]
    if not chapters:
        raise ValueError("这个 PDF 读不出文字，可能是扫描版（需要 OCR）")
    book = Book(path, _guess_title(path, []), "pdf", chapters)
    book.total_pages = total
    return book


# ---------------------------------------------------------------- 切句（给听书用）

_SENT_END = "。！？…!?；;"
_SOFT_BREAK = "，,、：:"


def split_sentences(text: str, lo: int = 15, hi: int = 90) -> list[str]:
    """把一段文字切成适合朗读的句子。

    单句控制在 lo~hi 字之间：太短会让朗读频繁换气，太长会出现
    「声音已经读到下一句，高亮还停在上句」的错位。
    """
    out: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in _SENT_END and len(buf) >= lo:
            s = buf.strip()
            if s:
                out.append(s)
            buf = ""
        elif len(buf) >= hi:
            cut = -1
            for mark in _SOFT_BREAK:
                cut = max(cut, buf.rfind(mark))
            if cut >= lo // 2:
                out.append(buf[:cut + 1].strip())
                buf = buf[cut + 1:]
            else:
                out.append(buf.strip())
                buf = ""
    if buf.strip():
        out.append(buf.strip())
    return [s for s in out if s]


# ---------------------------------------------------------------- 全文搜索

def search(book: Book, keyword: str, limit: int = 300) -> list[dict]:
    kw = (keyword or "").strip().lower()
    if not kw:
        return []
    hits: list[dict] = []
    for ci, ch in enumerate(book.chapters):
        text = ch.text
        low = text.lower()
        pos = 0
        while True:
            idx = low.find(kw, pos)
            if idx < 0:
                break
            head = max(0, idx - 24)
            tail = min(len(text), idx + len(kw) + 24)
            hits.append({
                "chapter": ci,
                "offset": idx,
                "length": len(kw),
                "preview": text[head:tail].replace("\n", " "),
            })
            if len(hits) >= limit:
                return hits
            pos = idx + max(1, len(kw))
    return hits
