# -*- coding: utf-8 -*-
"""年报章节「可读化」文本层：把 PDF 原始逐行文本重整为可读的中文结构化 Markdown。

对齐 `pdf-annual-report-md` skill 的两阶段精神（阶段二=结构化重整），针对本项目
「董事长致辞 + 管理层讨论与分析」的阅读需求做四件事：
  1) 段落重组：把 PDF 断行（如「公司实现营业 / 收入1809.99亿元」）并回整段；
  2) 表格还原：用 pdfplumber find_tables() 把表格转成 markdown 表格，并从正文流中剔除；
  3) 标题分层：识别 一、/（一）/1./（1）/第X节 等，转成 markdown 标题；
  4) 去英文：港股双栏双语按 x 坐标切栏，只留中文列；单栏则丢弃纯英文行。

设计要点（实测校准，见 2026-09-20 调试）：
- 港股双栏（如华润电力）中文列 x0≈351.5、英文列 42.5–337.5，栏间缝隙常仅 ~14pt →
  固定 gap 阈值不可靠，改用「扫描候选切割线，取跨线词数最少者」检测栏位。
- 表格用 bbox 从正文词流中排除，避免与 markdown 表格重复。
- 页家具（页眉/页码/单位/勾选框等）沿用 extract_chairman_mda 的 _is_furniture。
"""
import re
from collections import defaultdict, Counter

import pdfplumber

CJK_RE = re.compile(r'[\u4e00-\u9fff]')
# 标题行：第X节 / 一、 / （一） / (一) / 1. / 1、 / （1） / (1)
HEAD_RE = re.compile(
    r'^\s*(?:第[一二三四五六七八九十]+[节節]|[一二三四五六七八九十]+、|'
    r'[（(][一二三四五六七八九十]+[）)]|[0-9]{1,2}[.、]|[（(][0-9]{1,2}[）)])'
    r'\s*\S')
# 顶层标题（用于分级）：第X节 / 一、 / （一）
HEAD_L1_RE = re.compile(r'^\s*(?:第[一二三四五六七八九十]+[节節]|[一二三四五六七八九十]+、|[（(][一二三四五六七八九十]+[）)])\s*\S')


def _cjk(s):
    return len(CJK_RE.findall(s))


def _is_english(s):
    """英文行判据（两类）：
      ① 常规：≥2 个英文词且无中文（原判据放宽到 `_cjk<=1` 会漏单英文词行）；
      ② 短行残留：整行无中文、长度≤30、纯拉丁/数字/标点，且含 ≥1 个**长度≥4 的字母词**
         （覆盖双语表格被拆到正文的英文列，如 `Liaoning`/`Tianjin`/`Heilongjiang`）。
         长度≥4 门槛用于**保留** `MW`/`kWh`/`A4`/`ROE` 类短单位与代码，避免误删有信息量的标记。
    含任一中文字的行一律不判英文（如「资源量（Mt）」保留）。"""
    if _cjk(s) > 0:
        return False
    if len(re.findall(r'[A-Za-z]{2,}', s)) >= 2:
        return True
    if len(s) <= 30:
        words = [w for w in re.sub(r'[^A-Za-z ]', ' ', s).split() if w]
        if words and max(len(w) for w in words) >= 4:
            return True
    return False


def _is_reversed_or_garbled(s):
    """识别『倒序页眉』与『字符三连叠印』垃圾（部分港股年报页眉旋转180°绘制 →
    pdfplumber 逐字倒读，如 `報年5202`=2025年報、`司公限有…`=…有限公司；
    或同页眉/表头叠印致字符连续重复，如 `董董董事事事長長長`）。
    仅对短行生效，避免误伤正文。

    ⚠️ 叠字判据必须用**≥3 连续同字**（`(.)\\1{2,}`），不可用 ≥2：
    合法专名常含 2 连字（`国电电力`/`H股股東`/`國電東北`），用 2 连会把
    `国电电力山东新能发电电力源开发有限公司` 当垃圾丢弃（实测全库误伤 11 行）。
    实测分离度：≥3 判据精准命中 2 行真伪影（`是担担担保否`），0 误伤。"""
    if len(s) <= 40:
        r = s[::-1]
        if '有限公司' in r or '股份有限公司' in r:
            return True
        if re.search(r'年[度]?[報报]', r) and re.search(r'\d{3,4}', r):
            return True
    if len(s) <= 30 and re.search(r'([\u4e00-\u9fff])\1{2,}', s):
        return True
    # 装饰性 **logo 水印** / 叠印表头被逐字读出：短行内中文↔拉丁**高频交错**。
    # 如 `o華ur潤ce電s`（华润 logo 字母沿曲线排布，pdfplumber 按 x 序读成一串）、
    # `H股股東H股股東`（表头叠印）。实测全库命中 22 行、**精度 22/22（0 误伤）**；
    # 含数字者豁免（`2×1,000MW` 类真实混合文本），单字母行不在此判据内（`A` 等系真实列标签）。
    if len(s) <= 30 and not re.search(r'\d', s):
        kinds = [('C' if CJK_RE.match(ch) else ('L' if ch.isascii() and ch.isalpha() else 'O'))
                 for ch in s]
        trans = sum(1 for i in range(len(kinds) - 1)
                    if {kinds[i], kinds[i + 1]} == {'C', 'L'})
        if trans >= 3 and trans / len(s) >= 0.35:
            return True
    return False


def _is_furniture(s):
    """页眉/页脚/表格家具（与 extract_chairman_mda 同步）。"""
    if not s:
        return False
    if re.fullmatch(r'\s*\d{1,4}\s*[/／]\s*\d{1,4}\s*', s):
        return True
    # 裸页码（pdfplumber 常把页码拆成独立行，如 `9` / `0002` / `11`）
    if re.fullmatch(r'\s*\d{1,6}\s*', s):
        return True
    # 章节名重复行（正文页眉复述，MD 已有 ### 级别标题，重复行冗余）
    if re.fullmatch(r'\s*(?:主席報告|主席报告|董事長致辭|董事长致辞|董事長報告|致股東|致股东|'
                    r'管理層討論與?及?分析|管理层讨论与分析|經營?情況討論與分析|'
                    r'董事會報告|董事会报告|業務回顧|业务回顾)\s*', s):
        return True
    if re.search(r'年[度]?[報报]', s) and (re.search(r'\d{4}', s)
                                          or re.search(r'[〇零一二三四五六七八九]{4}', s)):
        return True
    # 港股「页码 + 公司名」页脚（如 `12中國神華能源股份有限公司`）/「公司名 + 页码」
    if re.fullmatch(r'\s*\d{1,4}\s*[\u4e00-\u9fff]{2,}?(?:股份)?有限公司\s*', s):
        return True
    if re.fullmatch(r'\s*[\u4e00-\u9fff]{2,}?(?:股份)?有限公司\s*\d{1,4}\s*', s):
        return True
    if re.search(r'Annual Report', s, re.I):
        return True
    if s.startswith('单位') or s.startswith('單位'):
        return True
    if re.search(r'[√□]\s*适用', s):
        return True
    if '证券代码' in s or '证券简称' in s or '證券代碼' in s or '證券簡稱' in s:
        return True
    if 'http' in s or 'www.' in s:
        return True
    if '上接第' in s or '下转第' in s:
        return True
    return False


def _table_bboxes(page):
    try:
        return page.find_tables()
    except Exception:
        return []


def _inside(w, bboxes, pad=2.0):
    for (x0, top, x1, bottom) in bboxes:
        if (w['x0'] >= x0 - pad and w['x1'] <= x1 + pad
                and w['top'] >= top - pad and w['bottom'] <= bottom + pad):
            return True
    return False


def _table_ncol(t):
    """表列数（用于判定是否为「可用」多列表；复杂港股表常被识别成单列 → 退化）。"""
    try:
        rows = t.extract()
    except Exception:
        return 0
    if not rows:
        return 0
    return max((len(r) for r in rows), default=0)


def _table_ok(t, page):
    """判定「可用真表」，拒绝**幻影表**。

    🔴 幻影表成因（华润电力 2016 实测，289 张全部命中）：部分港股年报页面含**页外裁切线/
    装饰框**（线条坐标出现 x0=-575、x1=1170 等），pdfplumber 以线框建表 → 造出 bbox
    `(0,0,1170,893)` 的**整页 13~19 列假表**（越界幅度 575~609pt ≈ 一个页宽，真表 0/289 越界）。
    若不剔除，① 整页文字被吞进表格、列错位成乱码；② 该表 bbox 又从正文词流中排除 →
    正文整页丢失。故须在渲染前拒绝。

    判据（三道，任一命中即拒）：
      1) bbox 越出页面边界 >5pt（真表 bbox 必在 MediaBox 内；5pt 容差留给满版表格线）；
      2) 退化单列（ncol<2，复杂港股表常被识别成一列 → 交回正文重排）；
      3) 列数≥10 且空单元占比≥0.6（版面网格误判特征；真表实测 ncol≤7 或空比≤0.5）。
    """
    x0, top, x1, bottom = t.bbox
    if x0 < -5 or top < -5 or x1 > page.width + 5 or bottom > page.height + 5:
        return False
    try:
        rows = t.extract()
    except Exception:
        return False
    if not rows:
        return False
    ncol = max((len(r) for r in rows), default=0)
    if ncol < 2:
        return False
    if ncol >= 10:
        cells = [c for r in rows for c in r]
        if cells:
            empty = sum(1 for c in cells if not (c or '').strip())
            if empty / len(cells) >= 0.6:
                return False
    return True


def _row_is_foreign(r):
    """整行单元格拼起来无中文且判为英文 → 外文行（老板只读中文，英文表头/表体丢）。
    复用 `_is_english` 作为**单一判据源**，同时覆盖：①多词英文表头（`Number of options`）；
    ②单词英文表头（`Percentage`/`outstanding`，早期只判 ≥2 词会漏，实测华润残留 18 行）；
    ③表内 URL 行（`http://…`）。含任一中文字的单元格行一律保留。"""
    txt = ' '.join((c or '') for c in r).strip()
    return bool(txt) and _is_english(txt)


def _table_to_md(t, drop_en=True):
    try:
        rows = t.extract()
    except Exception:
        return None
    if not rows:
        return None
    rows = [[('' if c is None else str(c)) for c in r] for r in rows]
    if drop_en:                                   # 真表内也可能夹英文行/URL 行 → 逐行剔
        rows = [r for r in rows if not _row_is_foreign(r)]
    rows = [r for r in rows if any((c or '').strip() for c in r)]
    if not rows:
        return None
    ncol = max(len(r) for r in rows)
    rows = [list(r) + [''] * (ncol - len(r)) for r in rows]
    keep = [i for i in range(ncol) if any((r[i] or '').strip() for r in rows)]
    if not keep:
        return None
    rows = [[r[i] for i in keep] for r in rows]
    md = ['| ' + ' | '.join((c or '').replace('\n', ' ').replace('|', '／').strip()
                            for c in row) + ' |' for row in rows]
    md.insert(1, '| ' + ' | '.join(['---'] * len(keep)) + ' |')
    return '\n'.join(md)


def _detect_cut(page, words):
    """检测双栏切割 x：取『跨线词数最少、且尽量居中』的缝隙。
    平衡判据用**词数各≥8**（不用比例）：中英双栏里英文被拆成大量短词、中文词少而长，
    比例会失衡（华润 p39 英 313 / 中 29），按比例会漏判；跨线词数为 0 即已强指示真实栏缝。"""
    if not words:
        return None
    W = page.width
    best = None
    for cut in range(int(0.28 * W), int(0.72 * W) + 1, 2):
        spans = sum(1 for w in words if w['x0'] < cut < w['x1'])
        left = sum(1 for w in words if w['x1'] <= cut)
        right = sum(1 for w in words if w['x0'] >= cut)
        if left < 8 or right < 8:
            continue
        score = (spans, abs(cut - W / 2))
        if best is None or score < best[0]:
            best = (score, cut, spans)
    if best and best[2] <= 2:
        return best[1]
    return None


def _cjk_ratio(s):
    s = re.sub(r'\s', '', s)
    return _cjk(s) / max(1, len(s))


def _split_columns(page, words):
    """返回**栏块列表**：每元素是一栏的段落列表 `[(lvl, text)]`。
    双栏且两栏皆中文 → `[左栏段落, 右栏段落]`（阅读序：左栏读完再读右栏）；
    仅一栏中文 → `[保留栏段落]`；单栏 → `[全页段落]`。
    返回分栏是为了让调用方能识别「栏边界」，从而把被栏/页边界截断的段落并回。"""
    cut = _detect_cut(page, words)
    if cut is None:
        return [_reflow(_lines_from_words(words), True)]
    left = [w for w in words if w['x1'] <= cut]
    right = [w for w in words if w['x0'] >= cut]
    lt = ''.join(t for _, t, _x in _lines_from_words(left))
    rt = ''.join(t for _, t, _x in _lines_from_words(right))
    lr, rr = _cjk_ratio(lt), _cjk_ratio(rt)
    if lr >= 0.3 and rr >= 0.3:          # 两栏皆中文 → 都留（左栏在前）
        return [_reflow(_lines_from_words(left), True), _reflow(_lines_from_words(right), True)]
    if rr >= lr:                          # 右栏中文占优 → 只留右栏
        return [_reflow(_lines_from_words(right), True)]
    return [_reflow(_lines_from_words(left), True)]   # 左栏中文占优 → 只留左栏


def _lines_from_words(words):
    """词 → 行（按 top 分箱），每行按 x0 拼接。返回 [(topbin, text, x0_min)]。"""
    bins = defaultdict(list)
    for w in words:
        bins[round(w['top'] / 3)].append(w)
    out = []
    for k in sorted(bins):
        ws = sorted(bins[k], key=lambda w: w['x0'])
        t = ws[0]['text']
        for w in ws[1:]:
            if (t[-1:].isascii() and t[-1:].isalnum()
                    and w['text'][:1].isascii() and w['text'][:1].isalnum()):
                t += ' ' + w['text']
            else:
                t += w['text']
        out.append((k, t, ws[0]['x0']))
    return out


# 首行缩进判据：行首 x0 比本栏左边距缩进 ≥12pt 即视为**新段首行**
# （中文年报段落靠「首行缩进 2 字符 ≈ 21pt」分隔，且**行距往往均匀无突变**）
INDENT_PT = 12.0


def _reflow(lines, drop_en=True):
    """行 → 段落。返回 [(level, text)] level∈{0,1,2}。

    🔴 **分段信号（2026-09-20 实测校正，勿轻改）**：早期只用「行距突变」分段是**错的**——
    中文年报（尤其 A股单栏满宽）段落靠**首行缩进**分隔，行距在 15/18pt 间自然波动，
    用 `med+1` 阈值会①把真实段断漏掉（gap 恰好相等）②在段内误断（gap 恰好多 1 bin）。
    实测华能国际 2023：med=5 bin、阈值 6，每遇 gap=6 就断 → 每 ~200 字假断一次，
    且把两个真实段并为一段。故改为**缩进为主、行距为辅**：
      ① 该行 x0 比本栏左边距缩进 ≥INDENT_PT → 新段首行（主判据）；
      ② 行距 ≥ med+3 且 ≥ med*1.5（保守）→ 新段（辅判据，兜住无缩进的版式）；
      ③ 标题单独成段（level 1/2）。
    同栏内其余情况串接为同段。"""
    clean = []
    for k, t, x0 in lines:
        t = t.strip()
        if not t or _is_furniture(t) or _is_reversed_or_garbled(t):
            continue
        if not re.search(r'[\u4e00-\u9fff0-9A-Za-z]', t):   # 纯符号噪声（# — · | 等）
            continue
        if drop_en and _is_english(t):
            continue
        clean.append((k, t, x0))
    if not clean:
        return []
    # 本栏左边距 = 正文行 x0 的**众数**（不能取 min！）。
    # 🔴 实测坑：港股页脚/页眉（如 `12中國神華能源股份有限公司`，x0=34）比正文（x0=58）
    # 更靠左，取 min 会把 margin 拉到 34 → 所有正文行「缩进」都算成 24pt ≥ 12 →
    # **每行都断段**（神华 p13 由 3 段炸成 19 段）。众数代表主导正文左边距，抗离群。
    xs = [round(x) for _, _, x in clean]
    margin = Counter(xs).most_common(1)[0][0]
    # 行距统计（相邻 top 差的中位数）→ 保守阈值
    deltas = sorted(clean[i + 1][0] - clean[i][0] for i in range(len(clean) - 1))
    med = deltas[len(deltas) // 2] if deltas else 1
    thr = max(med + 3, med * 1.5, 4)
    paras = []
    buf = []
    prev_k = None
    for k, t, x0 in clean:
        # 标题：匹配标题前缀 + 长度受限 + 不以续行/句末标点结尾（排除「一、本…真实、」类正文续行）
        is_head = (bool(HEAD_RE.match(t)) and len(t) <= 40
                   and not t.endswith(('。', '，', ',', '、', '；', ';', '：', ':')))
        gap = (k - prev_k) if prev_k is not None else 0
        if is_head:
            if buf:
                paras.append((0, ''.join(buf)))
                buf = []
            lvl = 1 if HEAD_L1_RE.match(t) else 2
            paras.append((lvl, t))
        else:
            new_para = (prev_k is not None
                        and ((x0 - margin) >= INDENT_PT or gap >= thr))
            if new_para and buf:
                paras.append((0, ''.join(buf)))
                buf = []
            buf.append(t)
        prev_k = k
    if buf:
        paras.append((0, ''.join(buf)))
    # 🔴 **段级二次过滤（必须）**：行级过滤挡不住「并段拼接出的英文」——
    # 相邻两行分别以 `eti` 结尾、以 `Re dso` 开头，各自都不算英文行而幸存，
    # join 后却拼成 `etiRe dso`（华润 logo 水印碎片实测）。故并段后再按同一判据筛一次。
    if drop_en:
        paras = [(lvl, t) for lvl, t in paras
                 if lvl != 0 or not (_is_english(t) or _is_reversed_or_garbled(t))]
        paras = _drop_logo_runs(paras)
    return paras


def _drop_logo_runs(paras, min_run=4):
    """剔除**装饰性 logo 水印**：公司 logo 字母沿曲线排布，pdfplumber 逐字读出 →
    每个字母自成一段，形成一长串「孤立单字符段落」（如华润电力的
    `g s C o m p a n y L i m i t e d` 碎片）。
    仅当**连续 ≥min_run 个单字符段**同时出现才判为装饰 —— 单发/两连的单字符可能是
    真实列标签（如 `A`），故不删。实测全库：长度≥4 的 run 共 25 个、**全部**是华润 logo
    （长度≤2 的 168 个 run 为散落残留，不在此判据内），故 min_run=4 精准且零误伤。"""
    if len(paras) < min_run:
        return paras
    drop, run = set(), []
    for i, (lvl, t) in enumerate(paras):
        s = t.strip()
        if lvl == 0 and len(s) == 1 and (CJK_RE.match(s)
                                        or (s.isascii() and s.isalpha())):
            run.append(i)
        else:
            if len(run) >= min_run:
                drop.update(run)
            run = []
    if len(run) >= min_run:
        drop.update(run)
    if not drop:
        return paras
    return [p for i, p in enumerate(paras) if i not in drop]


def extract_range(pdf_path, start, end, drop_en=True):
    """抽取 [start,end]（1-indexed 闭区间）页 → 可读 markdown 文本。"""
    items = []            # ('p', lvl, text, page) | ('t', md, page)
    with pdfplumber.open(pdf_path) as pdf:
        n = len(pdf.pages)
        for pno in range(max(1, start), min(end, n) + 1):
            page = pdf.pages[pno - 1]
            all_tabs = _table_bboxes(page)
            # 仅「可用真表」渲染为 markdown 并从正文剔除；幻影表/退化单列不剔，
            # 让文字回落正文重排（避免「整页文字被吞进假表」或「整表挤一格」不可读）。
            # ⚠️ 先渲染后剔除：整表被去英文过滤成空（如纯英文双语表）→ 不剔 bbox，
            #    文字回落正文重排，避免该页内容整体丢失。
            tabs, tab_md = [], []
            for t in all_tabs:
                if not _table_ok(t, page):
                    continue
                tm = _table_to_md(t, drop_en)
                if not tm:
                    continue
                tabs.append(t)
                tab_md.append(tm)
            bboxes = [t.bbox for t in tabs]
            words = [w for w in (page.extract_words(keep_blank_chars=False) or [])
                     if not _inside(w, bboxes)]
            # 分栏 + 去英文（保留中文栏）；栏块列表用于识别栏边界
            blocks = _split_columns(page, words) if drop_en else [_reflow(_lines_from_words(words), False)]
            for ci, blk in enumerate(blocks):
                bid = (pno, ci)                   # 栏块标识（页, 栏序）
                for lvl, t in blk:
                    t = t.strip()
                    if t:
                        items.append(('p', lvl, t, bid))
            for tm in tab_md:
                items.append(('t', tm, (pno, -1)))
    text = _render(_merge_across_pages(items))
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text


# 句末标点（段落未以此收尾 → 可能是被页边界截断，需并回）
_TERM = '。！？；：…”」』）)'
_HEAD_ANY = re.compile(r'^\s*(?:第[一二三四五六七八九十]+[节節]|[一二三四五六七八九十]+、|'
                       r'[（(][一二三四五六七八九十]+[）)]|[0-9]{1,2}[.、]|[（(][0-9]{1,2}[）)])')


def _merge_across_pages(items):
    """跨「栏/页」边界合并段落：`_reflow` 按页（且按栏）独立分段，边界落在句子中间时
    会产生假分段（如「…大范围持续高温」∥「和寒潮天气等因素影响…」；A股双栏、港股跨页均常见）。
    仅当 ①相邻块都是正文段（lvl=0）②**栏块标识不同**（不同页或同页不同栏）
    ③上段未以句末标点收尾 ④下段不是标题 时并回。
    同一栏块内的分段是行距算法判定的真实段断，不动。"""
    out = []
    for it in items:
        if (it[0] == 'p' and it[1] == 0 and out and out[-1][0] == 'p' and out[-1][1] == 0
                and out[-1][3] != it[3]):
            prev = out[-1][2]
            if prev and prev[-1] not in _TERM and not _HEAD_ANY.match(it[2]):
                out[-1] = ('p', 0, prev + it[2], it[3])
                continue
        out.append(it)
    return out


def _render(items):
    md = []
    for it in items:
        if it[0] == 't':
            md.append("\n" + it[1] + "\n")
        elif it[1] == 1:
            md.append(f"\n#### {it[2]}\n")
        elif it[1] == 2:
            md.append(f"\n##### {it[2]}\n")
        else:
            md.append(f"{it[2]}\n")
    return "\n".join(md)
