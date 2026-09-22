# -*- coding: utf-8 -*-
"""
发电运营 15 家 · 年报「董事长致辞 / 管理层讨论与分析」抽取器
================================================================
对每家公司的 年报PDF/*.pdf，自动定位两个章节的起止页，抽取正文，
写入该公司目录下的专属文件：{公司名}_{年份}_董事长致辞与MD&A.md（每年度一个独立文件）

边界判定策略（对齐 pdf-annual-report-md 质控精神，针对性精简）：
- 文档级市场判定：扫前 30 页是否含「第X节」→ A股；否则按繁体大标题判港股。
- 严格标题判定：章节标题须为「短行 + 无目录引导点 + 以标题开头」，且所在页不是
  「标题丛集页」（目录/章节索引页，含 >=3 个顶级标题）→ 避免命中 TOC。
- 结束判定同时覆盖三种停止点：① 下一「第X节」编号；② A股章节名（去掉第X节前缀后）；
  ③ 港股顶级大标题（排除自身）。未找到的章节如实标注，绝不虚构（0 幻觉）。
依赖：pymupdf (fitz)。可选 --dry 仅打印检测范围不落盘。
"""
import os, re, glob, argparse, sys
from multiprocessing import Pool

try:
    import pymupdf as fitz
except ImportError:
    import fitz

# 可读化文本层（pdfplumber：段落重组 + 表格还原 + 标题分层 + 去英文）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tidy_extract

# 繁→简：老板要求所有产出默认简体（港股年报章节常是繁体，如「主席報告」「發電量」）。
# opencc t2s 幂等，对简体文本无副作用；缺依赖时安全降级不阻断。
try:
    import opencc
    _CC = opencc.OpenCC("t2s")
    FORCE_SIMPLIFIED = True
except Exception:
    _CC = None
    FORCE_SIMPLIFIED = False

BASE = "/Volumes/KIOXIA/上市公司研究/电力系统/01-发电运营（15家）"
# 生成的年份 MD 统一归置到公司目录下的专属子文件夹（不在根目录与 CSV/年报PDF 混放）
SUBDIR = "董事长致辞与MD&A"
CN_NUM = {'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}

# 董事长致辞 / 主席報告 同义词（长词在前；简繁同收）
CHAIR_TERMS = ["董事长报告书", "董事长致辞", "董事長致辭", "董事會致辭", "致股东的信", "致股东书", "致股東信",
               "主席致辭", "主席的話", "主席報告", "董事長報告書", "董事長報告",
               "致股东", "致股東"]

# 管理层讨论与分析 同义词（含简体「董事会报告」）
MDA_TERMS = ["管理层讨论与分析", "经营情况讨论与分析",
             "管理層討論與分析", "管理層討論及分析",
             "董事會工作報告", "董事會報告", "董事会报告", "董事会工作报告",
             "業務回顧", "业务回顾"]

# 规范 MD&A 标题优先（避免把「业务回顾」这类概述章误当作正式 MD&A，
# 导致与中国电力等「管理層討論及分析」章节重叠/错乱）。先搜规范名，未命中再回退概述名。
MDA_CANON = ["管理層討論與分析", "管理層討論及分析", "管理层讨论与分析", "经营情况讨论与分析",
             "董事會工作報告", "董事會報告", "董事会工作報告", "董事会报告"]
MDA_FALLBACK = ["業務回顧", "业务回顾", "業務回顧與分析"]

# 港股顶级章节标题（用于边界判定，排除自身）
HK_MAJOR = ["管理層討論與分析", "管理層討論及分析", "董事會工作報告", "董事會報告",
            "企業管治報告", "監事會工作報告", "獨立核數師報告", "綜合財務報表",
            "財務報表", "主席報告", "董事長報告", "環境社會及管治報告",
            "可持續發展報告", "業務回顧", "业务回顾"]

# A股章节名（去掉「第X节」前缀后用于结束判定）
A_SHARE_SECTION_NAMES = ["重要提示", "目录", "释义", "公司简介和主要财务指标",
                         "管理层讨论与分析", "经营情况讨论与分析", "董事会报告",
                         "监事会报告", "公司治理", "环境与社会责任", "重要事项",
                         "股份变动及股东情况", "优先股相关情况", "债券相关情况",
                         "财务报告", "董事长致辞", "致股东的信", "致股东书"]

DOTS = re.compile(r'\.{3,}')
SEC_HEAD = re.compile(r'^第([一二三四五六七八九十]+)[节節]')
SEC_HEAD_LINE = re.compile(r'^第[一二三四五六七八九十]+[节節](\s+[一-龥].*)?$')
SEC_STRIP = re.compile(r'^第[一二三四五六七八九十]+[节節]\s*')
COMBINED_TITLES = set(CHAIR_TERMS + MDA_TERMS + HK_MAJOR + A_SHARE_SECTION_NAMES)


# 已知章节标题集合（用于识别并丢弃正文内反复出现的『章节运行页眉』，
# 如港股每页「管理層討論與分析」「主席報告」、A股每页「第三节 管理层讨论与分析」）。
KNOWN_TITLE_SET = set(CHAIR_TERMS + MDA_TERMS + HK_MAJOR + A_SHARE_SECTION_NAMES)

# 港股每页『章节导航运行页眉』碎片（bare 形式，非完整标题）：年报每页顶部列出
# 当前/相邻章节名（如「管理層 / 討論及分析 / 企業管治 / 其他資料」），属页眉非内容。
# 这些碎片作为 standalone 行出现时为运行页眉；真实 MD&A 子标题均为完整长句，不会裸出现。
HK_HEADER_FRAG = {"管理層", "討論及分析", "企業管治", "其他資料", "業務回顧",
                 "環境社會及管治報告", "可持續發展報告", "綜合財務報表", "財務報表",
                 "監事會工作報告", "獨立核數師報告", "核數師報告", "企業管治報告"}

def _is_furniture(s):
    """判定一行是否为页眉/页脚/表格家具（A股简体 + 港股繁体 + 英文同收）。"""
    if not s:
        return False
    if re.fullmatch(r'\s*\d{1,4}\s*[/／]\s*\d{1,4}\s*', s):   # 页码 X / Y
        return True
    # 运行页眉含年份(简/繁/繁短/英)：2023 年年度报告 / 2024年報 / Annual Report 2020
    if re.search(r'年[度]?[報报]', s) and re.search(r'\d{4}', s):
        return True
    if re.search(r'Annual Report', s, re.I):
        return True
    if s.startswith('单位') or s.startswith('單位'):           # 单位：元 币种：人民币
        return True
    if re.search(r'[√□]\s*适用', s):                          # √适用/□适用 勾选框
        return True
    if '证券代码' in s or '证券简称' in s or '證券代碼' in s or '證券簡稱' in s:
        return True
    if 'http' in s or 'www.' in s:                            # 交易所/公司 URL 脚注
        return True
    if '上接第' in s or '下转第' in s:                        # 续页标记
        return True
    return False

def clean_line(s):
    s = s.rstrip()
    if not s.strip():
        return ""
    if _is_furniture(s):                          # 页眉/页脚/表格家具 → 丢弃
        return None
    if re.fullmatch(r'\d+', s.strip()):
        return None
    # 章节运行页眉：① 裸「第X节」(无后续标题文字)；② 去编号前缀后恰为已知章节标题；
    # ③ 港股导航页眉碎片（bare 形式）→ 均丢弃
    if SEC_HEAD.match(s) and not SEC_STRIP.sub('', s.strip()):
        return None
    norm = SEC_STRIP.sub('', s.strip())
    if norm in KNOWN_TITLE_SET or s.strip() in HK_HEADER_FRAG:
        return None
    return s


def find_section_start(doc, terms, max_scan, start_from=1):
    n = doc.page_count
    limit = min(n, max_scan) if max_scan else n
    for pno in range(start_from, limit + 1):
        text = doc[pno - 1].get_text("text")
        # TOC/索引页判定：单页含 >=2 个不同编号节，或 >=2 个『目录样式』主要标题 → 跳过
        if _is_toc_page(text):
            continue
        for line in text.splitlines():
            s = line.strip()
            if not s or len(s) > 30 or DOTS.search(s):
                continue
            if any(p in s for p in '，。、（）()：;；,.參閱詳見請頁见见'):
                continue
            norm = SEC_STRIP.sub('', s).strip()
            m_sec = SEC_HEAD.match(s)
            for term in terms:
                if norm == term or norm.startswith(term) or s.startswith(term):
                    own_num = CN_NUM.get(m_sec.group(1)) if m_sec else None
                    if own_num is None:
                        # 命中行无「第X节」前缀（如港股「董事會報告」以裸页眉被命中），
                        # 取本页首个编号节作为本节编号，避免后续同号页眉（第四節）被误判为新边界。
                        for ln in text.splitlines():
                            mm = SEC_HEAD.match(ln.strip())
                            if mm:
                                own_num = CN_NUM.get(mm.group(1))
                                break
                    return pno, term, own_num
    return None, None, None


def _tokens_on_page(text, own_term, own_num):
    """返回该页所有『停止点』令牌集合（排除自身章节）；拒绝内联引用行。"""
    CITE = '，。、（）()：;；,.參閱詳見請頁见见'
    toks = set()
    for line in text.splitlines():
        s = line.strip()
        if not s or len(s) > 30 or DOTS.search(s):
            continue
        if any(p in s for p in CITE):
            continue
        # ① 编号节（不同编号）→ 权威边界
        m = SEC_HEAD_LINE.match(s)
        if m:
            num = CN_NUM.get(SEC_HEAD.match(s).group(1))
            if num != own_num:
                toks.add(f"SEC{num}")
                continue
        # ② 去编号前缀后的 A股章节名（排除自身）
        norm2 = SEC_STRIP.sub('', s).strip()
        if norm2 and norm2 != own_term:
            for an in A_SHARE_SECTION_NAMES:
                if norm2 == an or norm2.startswith(an):
                    toks.add("A:" + an)
                    break
        # ③ 港股顶级大标题（排除自身；限干净短标题）
        for hk in HK_MAJOR:
            if hk != own_term and s.startswith(hk) and len(s) <= len(hk) + 3:
                toks.add("H:" + hk)
    return toks


def page_stopper_token(text, own_term, own_num):
    """返回该页首个『停止点』令牌（排除自身章节），无则 None。"""
    t = _tokens_on_page(text, own_term, own_num)
    return next(iter(t)) if t else None


def _toc_major_lines(text):
    """返回该页『目录样式』主要标题集合：标题行后紧跟引导点(...)或纯页码(1-4位)。
    用于精确区分目录页与真实章节起始页（真实章节页标题后紧跟正文而非页码）。
    注：编号节(第X节)不在此计，单独由 seen_sec 判定。"""
    CITE = '，。、（）()：;；,.參閱詳見請頁见见'
    out = set()
    lines = text.splitlines()
    n = len(lines)
    for idx, raw in enumerate(lines):
        s = raw.strip()
        if not s or len(s) > 30:
            continue
        if any(p in s for p in CITE):
            continue
        has_dots = bool(DOTS.search(s))
        if has_dots:
            core = DOTS.sub('', s).strip()
            if not core or len(core) > 30 or any(p in core for p in CITE):
                continue
            for t in COMBINED_TITLES:
                if core == t or core.startswith(t):
                    out.add(t)
                    break
            continue
        m_sec = SEC_HEAD.match(s)
        if m_sec:
            continue
        norm = SEC_STRIP.sub('', s).strip()
        term = None
        for t in COMBINED_TITLES:
            if norm == t or norm.startswith(t) or s.startswith(t):
                term = t
                break
        if not term:
            continue
        nxt = None
        for j in range(idx + 1, min(idx + 3, n)):
            if lines[j].strip():
                nxt = lines[j].strip()
                break
        if nxt is not None and re.fullmatch(r'\d{1,4}', nxt):
            out.add(term)
    return out


def _is_toc_page(text):
    """判断是否为目录/索引页。
    ① A股：>=2 个不同编号节；
    ② 通用：>=2 个『目录样式』标题（标题后跟引导点/纯页码）；
    ③ 港股目录（页码在前格式，如 `10主席報告`）：短行(<=25字)内含 >=4 个章节标题，
       或 >=3 个章节标题 + >=3 个独立页码行。
    用「短行」限定，避免把正文里偶然提及多个章节名的长句误判为目录。"""
    lines = [l.strip() for l in text.splitlines()]
    seen_sec = set()
    for s in lines:
        if SEC_HEAD.match(s) and len(s) <= 30 and not DOTS.search(s):
            seen_sec.add(SEC_HEAD.match(s).group(1))
    if len(seen_sec) >= 2 or len(_toc_major_lines(text)) >= 2:
        return True
    short = [l for l in lines if l and len(l) <= 25]
    titles_short = sum(1 for t in COMBINED_TITLES if any(t in l for l in short))
    nums = sum(1 for l in lines if re.fullmatch(r'\d{1,4}', l))
    return titles_short >= 4 or (titles_short >= 3 and nums >= 3)


def find_section_end(doc, start_pno, own_num, own_term):
    """扫描到下一个『全新』顶级章节即止。
    section_tokens 记录本段已出现的标题；重复出现的页眉或子章节视为同段内容，不触发边界。
    种子 = 起点之前『非目录』页中出现 >=2 次的标题（真正持续的子章节/页眉，如中国电力
    『業務回顧』p5-25 在 p44 重现；或华润每页『主席報告』）。仅出现 1 次的扉页提及
    （如下一章『董事會報告』在序言中被引用一次）不预植入种子，以免误吞真实边界。"""
    n = doc.page_count
    from collections import Counter
    pre = Counter()
    for sp in range(1, start_pno):
        t = doc[sp - 1].get_text("text")
        if _is_toc_page(t):
            continue
        for tk in _tokens_on_page(t, own_term, own_num):
            pre[tk] += 1
    section_tokens = {tk for tk, c in pre.items() if c >= 2}
    for pno in range(start_pno + 1, n + 1):
        toks = _tokens_on_page(doc[pno - 1].get_text("text"), own_term, own_num)
        if not toks:
            continue
        # 本页出现任一『本段从未见过』的标题 → 即进入新顶级章节（边界）。
        # 用 not issubset 而非 isdisjoint：某页可能同时带已知页眉(如業務回顧)
        # 与新顶级章节(如管理層討論及分析)，此时仍应判定为新边界。
        if not toks.issubset(section_tokens):
            return pno
        section_tokens |= toks
    return n


def extract_text(doc, start, end):
    out = []
    for pno in range(start, end + 1):
        t = doc[pno - 1].get_text("text")
        for line in t.splitlines():
            c = clean_line(line)
            out.append(c if c is not None else "")
    body = "\n".join(out)
    body = re.sub(r'\n{3,}', '\n\n', body).strip()
    return body


def process_company(comp_dir, dry=False):
    comp = os.path.basename(comp_dir.rstrip("/"))
    pdfs = sorted(glob.glob(os.path.join(comp_dir, "年报PDF", "*.pdf")))

    def yr(p):
        m = re.search(r"(\d{4})年", os.path.basename(p))
        return int(m.group(1)) if m else 0
    pdfs.sort(key=yr)
    results = []
    for p in pdfs:
        year = yr(p)
        fname = os.path.basename(p)
        try:
            doc = fitz.open(p)
        except Exception as e:
            results.append((year, fname, "OPEN_ERROR", str(e), None, None))
            continue
        cs, ct, cnum = find_section_start(doc, CHAIR_TERMS, 45)
        if cs:
            ce = find_section_end(doc, cs, cnum, ct)
        else:
            ce = None
        ms, mt, mnum = find_section_start(doc, MDA_CANON, None)
        if ms is None:
            ms, mt, mnum = find_section_start(doc, MDA_FALLBACK, None)
        # 防重叠：年报结构中「董事长致辞/主席報告」必在「管理层讨论与分析」之前。
        # 若 MD&A 起点【严格落在】董事长区间内（ms < ce，多见于港股双报告合并扉页，
        # 如华润 p4 同时标 主席報告+董事會報告），则从董事长结束页之后重搜，避免
        # 抢到误标起点。ms == ce 为章节紧邻（如神华 p15 致辞结束/报告即起），属正常边界，保留。
        if ms is not None and cs is not None and ms < ce:
            ms, mt, mnum = find_section_start(doc, MDA_CANON, None, start_from=ce + 1)
            if ms is None:
                ms, mt, mnum = find_section_start(doc, MDA_FALLBACK, None, start_from=ce + 1)
        if ms:
            me = find_section_end(doc, ms, mnum, mt)
        else:
            me = None
        doc.close()
        # 文本层：dry 模式不抽取正文（省时）；正式模式用 pdfplumber 重整为可读中文
        if dry:
            ctext = mtext = None
        else:
            ctext = tidy_extract.extract_range(p, cs, ce) if cs else None
            mtext = tidy_extract.extract_range(p, ms, me) if ms else None
        results.append((year, fname, (cs, ce, ct), (ms, me, mt), ctext, mtext))

    if dry:
        report = os.path.join("/tmp", "range_report.txt")
        with open(report, "a", encoding="utf-8") as rf:
            for r in results:
                y, f, c, m, _, _ = r
                cs, ce, ct = c
                ms, me, mt = m
                cstr = f"p{cs}-{ce}[{ct}]" if cs else "未找到"
                mstr = f"p{ms}-{me}[{mt}]" if ms else "未找到"
                line = f"  {comp} {y}: 董事长={cstr} | MD&A={mstr}"
                print(line, flush=True)
                rf.write(line + "\n")
        return comp, len(results), sum(1 for r in results if r[2][0]), sum(1 for r in results if r[3][0])

    # 公司名（去掉文件夹的 `NN_` 排序前缀），便于一眼识别
    short = re.sub(r'^\d+\s*[_\-\s]\s*', '', comp) or comp

    # 年份 MD 统一写入专属子文件夹 {公司目录}/董事长致辞与MD&A/
    out_dir = os.path.join(comp_dir, SUBDIR)
    os.makedirs(out_dir, exist_ok=True)

    # 按年份拆分成独立 MD 文件：{公司名}_{年份}_董事长致辞与MD&A.md
    n_written = 0
    for r in results:
        y, f, c, m, ctext, mtext = r
        cs, ce, ct = c
        ms, me, mt = m
        ytag = f"{y}" if y else "年份未知"
        out_path = os.path.join(out_dir, f"{short}_{ytag}_董事长致辞与MD&A.md")
        crange = f"第 {cs}–{ce} 页（命中标题「{ct}」）" if cs else "未找到"
        mrange = f"第 {ms}–{me} 页（命中标题「{mt}」）" if ms else "未找到"
        L = [f"# {short} · {ytag} 年年度报告 · 董事长致辞与管理层讨论与分析", ""]
        L.append(f"> **来源**：`年报PDF/{f}`（{short} {ytag} 年年度报告）")
        L.append(f"> **抽取范围**：董事长致辞 {crange}；管理层讨论与分析 {mrange}")
        L.append("> **已做可读化重整**：PDF 断行并回整段；章节标题分层（`####` / `#####`）；页码/页眉/勾选框等页家具剔除；表格尽量还原为 Markdown 表格。")
        L.append("> **只保留中文**：港股年报为中英双语，双语双栏按栏位切分后**去除英文栏**；A股本即中文。")
        L.append("> 说明：A股年报通常无独立「董事长致辞」章节（多在「第三节 管理层讨论与分析」）；港股多在「主席報告 / 致股東」中。未找到的章节如实标注，绝不虚构。")
        L.append("> 局限：① 极少数「数据概览 / 图版」页为多表拼版，文字顺序可能错乱；② 含装饰性 logo 水印的页面（如华润电力部分年份），水印字母可能残留少量孤立字母行。遇此请以源 PDF 页码回查。")
        L.append("")
        L.append("### 董事长致辞（主席報告 / 致股东）")
        if cs:
            L.append(f"> 抽取范围：第 {cs}–{ce} 页（命中标题「{ct}」）")
            L.append("")
            L.append(ctext or "（正文为空）")
        else:
            L.append("【未找到该章节：本年度年报未单列董事长致辞 / 主席報告】")
        L.append("")
        L.append("### 管理层讨论与分析")
        if ms:
            L.append(f"> 抽取范围：第 {ms}–{me} 页（命中标题「{mt}」）")
            L.append("")
            L.append(mtext or "（正文为空）")
        else:
            L.append("【未找到该章节】")
        L.append("")
        content = "\n".join(L)
        if FORCE_SIMPLIFIED and _CC is not None:
            content = _CC.convert(content)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        n_written += 1
    return comp, len(results), sum(1 for r in results if r[2][0]), sum(1 for r in results if r[3][0]), n_written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--company", default=None)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    if args.company:
        dirs = [os.path.join(args.base, args.company)]
    else:
        dirs = [os.path.join(args.base, d) for d in sorted(os.listdir(args.base))
                if os.path.isdir(os.path.join(args.base, d))]
    if args.dry:
        open("/tmp/range_report.txt", "w", encoding="utf-8").close()
        for d in dirs:
            print(f"### {os.path.basename(d.rstrip('/'))}", flush=True)
            process_company(d, dry=True)
        return
    with Pool(min(len(dirs), 8)) as pool:
        summaries = pool.map(process_company, dirs)
    print("\n===== 汇总 =====")
    tot_y = tot_c = tot_m = tot_f = 0
    for comp, ny, nc, nm, nw in summaries:
        tot_y += ny; tot_c += nc; tot_m += nm; tot_f += nw
        print(f"  {comp}: {ny} 份年报 | 董事长命中 {nc} | MD&A 命中 {nm} | 输出 {nw} 个 MD")
    print(f"  合计: {tot_y} 份年报 | 董事长命中 {tot_c} | MD&A 命中 {tot_m} | 输出 MD {tot_f} 个")


if __name__ == "__main__":
    main()
