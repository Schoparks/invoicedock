"""本地发票内容提取。所有字段仅为待核对建议，不作验真或税务判断。"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from io import BytesIO
from pathlib import PurePosixPath
import re
import importlib.util
import json
import posixpath
import sys
import unicodedata
import stat
import subprocess
import tempfile
import time
import zipfile

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_TEXT_CHARS = 2_000_000
MAX_PIXELS = 25_000_000
EXTENSIONS = {".pdf", ".xml", ".ofd", ".png", ".jpg", ".jpeg", ".webp"}
REVIEW_NOTE = "自动提取仅供参考，请核对发票号码、日期、金额与购销方后确认。"


class OcrUnavailable(ValueError):
    """OCR 环境不可用，允许有效原件进入待手工填写状态。"""


def _extension(filename: str) -> str:
    return PurePosixPath(filename).suffix.lower()


def _check_file(data: bytes) -> None:
    if not data:
        raise ValueError("文件为空")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("单个文件不得超过 20 MB")


def _safe_member(info: zipfile.ZipInfo) -> None:
    name = info.filename
    parts = PurePosixPath(name).parts
    if (not name or name.startswith(("/", "\\")) or "\\" in name
            or ".." in parts or (parts and ":" in parts[0]) or "\x00" in name):
        raise ValueError("压缩包包含不安全的文件路径")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise ValueError("压缩包不能包含符号链接")
    if info.flag_bits & 1:
        raise ValueError("暂不支持加密压缩包，请先解密")


def _junk(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return "__MACOSX" in parts or any(part == ".DS_Store" or part.startswith("._") for part in parts)


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    if info.file_size > limit:
        raise ValueError("压缩包内单个文件过大")
    with archive.open(info) as source:
        value = source.read(limit + 1)
    if len(value) > limit:
        raise ValueError("压缩包内单个文件过大")
    return value


def expand_upload(data: bytes, filename: str) -> list[tuple[str, bytes]]:
    """展开 ZIP，保留安全的相对文件名；普通文件原样返回。不展开嵌套 ZIP。"""
    if _extension(filename) != ".zip":
        _check_file(data)
        if _extension(filename) not in EXTENSIONS:
            raise ValueError("支持 PDF、XML、OFD、PNG、JPG、WEBP 或 ZIP 文件")
        return [(PurePosixPath(filename).name, data)]
    if not data:
        raise ValueError("文件为空")
    if len(data) > MAX_EXPANDED_BYTES:
        raise ValueError("单个 ZIP 上传包不得超过 100 MB")
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            if len(archive.infolist()) > 1000:
                raise ValueError("ZIP 内部条目过多，最多 200 个发票文件")
            output = []
            total = 0
            for info in archive.infolist():
                _safe_member(info)
                if info.is_dir() or _junk(info.filename):
                    continue
                if len(output) >= 200:
                    raise ValueError("一个 ZIP 最多包含 200 个文件")
                if _extension(info.filename) == ".zip":
                    raise ValueError("不支持嵌套 ZIP，请先解压内层压缩包")
                if _extension(info.filename) not in EXTENSIONS:
                    raise ValueError(f"压缩包包含不支持的文件：{PurePosixPath(info.filename).name}")
                total += info.file_size
                if total > MAX_EXPANDED_BYTES:
                    raise ValueError("ZIP 解压后总大小不得超过 100 MB")
                content = _read_member(archive, info, MAX_FILE_BYTES)
                if not content:
                    raise ValueError(f"压缩包中存在空文件：{PurePosixPath(info.filename).name}")
                output.append((info.filename, content))
            if not output:
                raise ValueError("ZIP 中没有可导入的发票文件")
            return output
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError) as exc:
        raise ValueError("ZIP 文件损坏或采用了不支持的压缩方式") from exc


def _money(value: str) -> int | None:
    value = re.sub(r"[￥¥,，\s元]", "", value)
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", value):
        return None
    try:
        return int(Decimal(value) * 100)
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _date(value: str) -> str:
    match = re.search(r"(?<!\d)(20\d{2})\s*(?:[-/.年]\s*)?(\d{1,2})\s*(?:[-/.月]\s*)?(\d{1,2})\s*日?", value)
    if not match:
        return ""
    try:
        return date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        return ""


def _party(text: str, side: str) -> str:
    label = "(?:销售方|销方)" if side == "seller" else "(?:购买方|购方)"
    direct = re.search(label + r"(?:信息)?\s*名称\s*[:：]?\s*([^\n]+)", text)
    if direct:
        value = direct.group(1)
    else:
        section = re.search(label + r"(?:信息)?\s*[:：]?", text)
        if not section:
            # 数电票将“购买方信息/销售方信息”竖排在左右两栏。
            # 仅接受成对的“购 名称… 销 名称…”行，避免把项目名称当作公司。
            columns = re.search(r"(?:^|\n)\s*购\s+名称\s*[:：]\s*(.+?)\s+销\s+名称\s*[:：]\s*([^\n]+)", text)
            return columns.group(2 if side == "seller" else 1).strip()[:200] if columns else ""
        remaining = text[section.end():section.end() + 400]
        remaining = re.split(r"购买方|销售方|统一社会信用代码|纳税人识别号", remaining)[0]
        name = re.search(r"名称\s*[:：]?\s*([^\n]+)", remaining)
        if not name:
            return ""
        value = name.group(1)
    value = re.split(r"购买方|销售方|统一社会信用代码|纳税人识别号|地址[、,，]?电话|开户行", value)[0]
    return value.strip(" :：\t")[:200]


def _category(text: str) -> str:
    rules = [
        ("住宿", r"住宿|客房|酒店|宾馆|旅馆"),
        ("餐饮", r"餐饮|餐费|餐厅|饭店|食品|饮料|咖啡"),
        ("交通", r"运输服务|客运|出行|出租车|网约车|火车票|铁路|航空|机票|汽油|加油|停车|通行费"),
        ("通讯", r"通信服务|通讯|电信|移动通信|联通|话费|宽带"),
        ("办公", r"办公|文具|打印|复印|纸张|电脑|计算机|软件"),
    ]
    for category, pattern in rules:
        if re.search(pattern, text):
            return category
    return "其他"


def _text_fields(raw: str) -> dict:
    text = unicodedata.normalize("NFKC", raw).replace("\r", "\n").replace("\u3000", " ")
    text = re.sub(r"名\s+称", "名称", text)
    text = re.sub(r"发\s*票\s*号\s*码", "发票号码", text)
    result = {"number": "", "seller": _party(text, "seller"), "buyer": _party(text, "buyer"),
              "date": "", "category": _category(text), "amount_cents": 0}
    number = re.search(r"(?:发票号码|发票号|Invoice\s*(?:Number|No\.?))\s*[:：]?\s*([A-Za-z0-9-]{6,40})", text, re.I)
    if number and re.search(r"\d", number.group(1)):
        result["number"] = number.group(1)
    date_match = re.search(r"(?:开票日期|开具日期|Invoice\s*Date|Issue\s*Date)\s*[:：]?\s*([^\n]{6,30})", text, re.I)
    if date_match:
        result["date"] = _date(date_match.group(1))
    # 只读取标明含税合计/小写的金额，不猜测税额或行项目金额。
    amount_patterns = [
        r"[（(]?小写[）)]?\s*[:：]?\s*[￥¥]?\s*(\d[\d,，]*(?:\.\d{1,2})?)(?!\d|\.)",
        r"(?:价税合计|含税总额|含税金额|实付金额)\s*[:：]?\s*[￥¥]?\s*(\d[\d,，]*(?:\.\d{1,2})?)(?!\d|\.)",
        r"(?:Total\s*(?:Including\s*Tax|With\s*Tax)|Grand\s*Total)\s*[:：]?\s*[￥¥]?\s*(\d[\d,，]*(?:\.\d{1,2})?)(?!\d|\.)",
    ]
    for pattern in amount_patterns:
        values = [_money(match.group(1)) for match in re.finditer(pattern, text, re.I)]
        values = [value for value in values if value is not None]
        if values:
            # 重复且冲突的合计不自动挑选，交给用户核对。
            if len(set(values)) == 1:
                result["amount_cents"] = values[0]
            break
    return result


def _tag(tag: str) -> str:
    return re.sub(r"[\s_\-（）()]", "", tag.rsplit("}", 1)[-1]).lower()


XML_FIELDS = {
    "number": {"invoiceno", "invoicenumber", "fphm", "发票号码", "发票号"},
    "seller": {"sellername", "sellercompanyname", "salesname", "xsfmc", "xfmc", "销售方名称", "销方名称"},
    "buyer": {"buyername", "buyercompanyname", "purchasername", "gmfmc", "gfmc", "购买方名称", "购方名称"},
    "date": {"invoicedate", "issuedate", "issuetime", "kprq", "开票日期", "开具日期"},
    "amount_cents": {"totaltaxincludedamount", "taxinclusivetotalamount", "totalamountwithtax",
                     "taxinclusiveamount", "jshj", "价税合计", "价税合计小写", "含税总额"},
}


def _xml_extract(data: bytes, references: dict | None = None) -> tuple[str, dict]:
    from defusedxml import ElementTree
    try:
        root = ElementTree.fromstring(data)
    except Exception as exc:
        raise ValueError("XML 结构无效，或含有被禁止的外部实体") from exc
    lines = []
    fields: dict[str, list[str]] = {}
    for element in root.iter():
        key = _tag(element.tag)
        value = (element.text or "").strip()
        if not value and references is not None and any(key in keys for keys in XML_FIELDS.values()):
            refs = [child for child in element.iter() if _tag(child.tag) == "objectref"]
            resolved = [references.get((ref.get("PageRef", ""), (ref.text or "").strip())) for ref in refs]
            if resolved and all(part is not None for part in resolved):
                value = "".join(resolved)
        if not value:
            continue
        lines.append(value if key == "textcode" else f"{element.tag.rsplit('}', 1)[-1]}: {value}")
        for field, keys in XML_FIELDS.items():
            if key in keys:
                fields.setdefault(field, []).append(value)
    raw_text = "\n".join(lines)
    if len(raw_text) > MAX_TEXT_CHARS:
        raise ValueError("XML 文本内容过大")
    result = {}
    for field, values in fields.items():
        distinct = set(values)
        if len(distinct) != 1:
            continue
        value = values[0]
        if field == "amount_cents":
            amount = _money(value)
            if amount is not None:
                result[field] = amount
        elif field == "date":
            result[field] = _date(value)
        else:
            result[field] = value[:200]
    return raw_text, result


@lru_cache(maxsize=1)
def _ocr_config() -> bool:
    if importlib.util.find_spec("rapidocr_onnxruntime") is None:
        raise OcrUnavailable("本机未安装 RapidOCR，未进行 OCR；请对照原件手动填写发票信息。")
    return True


def _ocr_image(image, deadline: float) -> tuple[str, str]:
    _ocr_config()
    if image.width * image.height > MAX_PIXELS:
        raise ValueError("图片尺寸过大，最多支持 2,500 万像素")
    seconds = min(30, deadline - time.monotonic())
    if seconds <= 0:
        raise ValueError("OCR 处理超时，请拆分文件或降低图片分辨率后重试")
    with tempfile.TemporaryDirectory(prefix="invoice-ocr-") as directory:
        from pathlib import Path
        source = Path(directory) / "page.png"
        image.convert("RGB").save(source)
        try:
            process = subprocess.run([sys.executable, str(Path(__file__).with_name("ocr_worker.py")), str(source)],
                                     capture_output=True, text=True, timeout=seconds)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("OCR 处理超时，请拆分文件或降低图片分辨率后重试") from exc
        except OSError as exc:
            raise ValueError("无法运行本机 OCR，请检查应用依赖安装") from exc
    if process.returncode:
        raise ValueError("OCR 识别失败，请检查本机依赖和图片是否清晰且可正常打开")
    try:
        result = json.loads(process.stdout)
        return result["text"][:MAX_TEXT_CHARS], result.get("note", "")
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("本机 OCR 返回结果无效") from exc


def _pdf_extract(data: bytes) -> tuple[str, str, list[str]]:
    import pymupdf
    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ValueError("PDF 文件损坏或无法读取") from exc
    texts, notes = [], []
    used_ocr = False
    ocr_unavailable = False
    deadline = time.monotonic() + 90
    with document:
        if document.needs_pass:
            raise ValueError("PDF 已加密，请先解密后导入")
        if document.page_count > 50:
            raise ValueError("单份 PDF 最多支持 50 页，请拆分后导入")
        for page in document:
            try:
                text = page.get_text("text", sort=True)
            except Exception as exc:
                raise ValueError("PDF 页面损坏或无法提取文字") from exc
            if not text.strip():
                if page.rect.width * page.rect.height * (300 / 72) ** 2 > MAX_PIXELS:
                    raise ValueError("PDF 页面尺寸过大，请缩小页面后导入")
                if ocr_unavailable:
                    texts.append(text)
                    continue
                from PIL import Image
                try:
                    pixmap = page.get_pixmap(dpi=300, alpha=False, colorspace=pymupdf.csRGB)
                except Exception as exc:
                    raise ValueError("PDF 页面无法渲染，请重新导出后导入") from exc
                with Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples) as image:
                    try:
                        text, note = _ocr_image(image, deadline)
                        used_ocr = True
                    except OcrUnavailable as exc:
                        text, note = "", str(exc)
                        ocr_unavailable = True
                if note and note not in notes:
                    notes.append(note)
            texts.append(text)
            if sum(len(part) for part in texts) > MAX_TEXT_CHARS:
                raise ValueError("PDF 文本内容过大")
    source = "PDF（OCR 不可用）" if ocr_unavailable else "PDF 文本 + OCR" if used_ocr else "PDF 文本"
    return "\n".join(texts), source, notes


def _ofd_extract(data: bytes) -> tuple[str, dict]:
    from defusedxml import ElementTree
    texts = []
    all_fields: dict[str, list] = {}
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 2000:
                raise ValueError("OFD 内部文件过多")
            total = 0
            documents = {}
            for info in entries:
                _safe_member(info)
                total += info.file_size
                if total > MAX_EXPANDED_BYTES:
                    raise ValueError("OFD 解压后总大小不得超过 100 MB")
                if info.is_dir() or not info.filename.lower().endswith(".xml"):
                    continue
                xml = _read_member(archive, info, MAX_FILE_BYTES)
                try:
                    root = ElementTree.fromstring(xml)
                except Exception as exc:
                    raise ValueError("OFD XML 结构无效") from exc
                documents[info.filename] = (xml, root)
            # PageRef 指向 Document.xml 中的页面 ID，ObjectRef 指向该页的 TextObject。
            references = {}
            for name, (_, root) in documents.items():
                if _tag(root.tag) != "document":
                    continue
                scope = posixpath.dirname(name)
                for page in root.iter():
                    if _tag(page.tag) != "page" or not page.get("BaseLoc"):
                        continue
                    page_name = posixpath.normpath(posixpath.join(scope, page.get("BaseLoc")))
                    if page_name not in documents:
                        continue
                    for obj in documents[page_name][1].iter():
                        if _tag(obj.tag) == "textobject" and obj.get("ID"):
                            value = "".join(e.text or "" for e in obj.iter() if _tag(e.tag) == "textcode")
                            references.setdefault(scope, {})[(page.get("ID", ""), obj.get("ID"))] = value
            for name, (xml, root) in documents.items():
                # 保留票面文字，不让 Glyphs、路径、字体和 ObjectRef 元数据进入正则识别。
                texts.extend(e.text.strip() for e in root.iter() if _tag(e.tag) == "textcode" and e.text and e.text.strip())
                if sum(len(part) for part in texts) > MAX_TEXT_CHARS:
                    raise ValueError("OFD 文本内容过大")
                scope = next((scope for scope in references if name.startswith(scope + "/")), None)
                _, fields = _xml_extract(xml, references.get(scope, {}))
                for key, value in fields.items():
                    all_fields.setdefault(key, []).append(value)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError) as exc:
        raise ValueError("OFD 文件损坏或无法读取") from exc
    fields = {key: values[0] for key, values in all_fields.items() if len(set(values)) == 1}
    return "\n".join(texts), fields


def parse_invoice(data: bytes, filename: str) -> dict:
    """提取单份发票；所有结果都应保存为待核对，不能据此直接占用或报销。"""
    _check_file(data)
    extension = _extension(filename)
    notes = []
    explicit_fields = {}
    if extension == ".pdf":
        raw_text, source, notes = _pdf_extract(data)
    elif extension == ".xml":
        raw_text, explicit_fields = _xml_extract(data)
        source = "XML 结构"
    elif extension == ".ofd":
        raw_text, explicit_fields = _ofd_extract(data)
        source = "OFD 结构"
        notes.append("OFD 仅提取可读取的 XML 文本，复杂排版、图像及部分厂商字段可能无法识别，请对照原件核对。")
    elif extension in {".png", ".jpg", ".jpeg", ".webp"}:
        from PIL import Image, ImageOps, UnidentifiedImageError
        source = "本机 OCR"
        try:
            with Image.open(BytesIO(data)) as image:
                if image.width * image.height > MAX_PIXELS:
                    raise ValueError("图片尺寸过大，最多支持 2,500 万像素")
                image.load()
                oriented = ImageOps.exif_transpose(image)
                try:
                    raw_text, note = _ocr_image(oriented, time.monotonic() + 90)
                except OcrUnavailable as exc:
                    raw_text, note = "", str(exc)
                    source = "图片（OCR 不可用）"
                if note:
                    notes.append(note)
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError("图片文件损坏、尺寸过大或格式不受支持") from exc
    else:
        raise ValueError("不支持此文件类型，请导入 PDF、XML、OFD、PNG、JPG 或 WEBP")
    fields = _text_fields(raw_text)
    fields.update({key: value for key, value in explicit_fields.items() if value != ""})
    if not raw_text.strip():
        notes.append("未识别到可用文字，请对照原件手动补全信息。")
    missing = [label for key, label in (("number", "发票号码"), ("date", "日期"),
                                      ("amount_cents", "价税合计"), ("seller", "销售方"), ("buyer", "购买方")) if not fields.get(key)]
    if missing:
        notes.append("未可靠提取：" + "、".join(missing) + "。")
    fields.update(source=source, review_note=" ".join([REVIEW_NOTE] + notes), raw_text=raw_text)
    return fields
