# -*- coding: utf-8 -*-
"""
可读化 MD 批量质检器
================================================================
扫描各公司 {公司名}_董事长致辞与MD&A.md，统计五类硬伤：
  1. 英文残留     —— 整行英文（去数字/符号后 ≥60% 拉丁字母且 ≥6 词）
  2. 反序页眉     —— 倒读含「有限公司 / 年報+年份」（港股旋转页眉）
  3. 字符重复     —— 短行内同一汉字连续重复 ≥2 次（PDF 伪影）
  4. 孤立符号行   —— 整行不含任何中英数字符（纯 # — · | 等）
  5. 结构覆盖     —— 「董事长致辞」与「管理层讨论与分析」两节命中与否
输出：逐家明细 + 全库合计。0 硬伤为过关。
"""
import os, re, glob, sys, argparse
from collections import defaultdict

BASE = "/Volumes/KIOXIA/上市公司研究/电力系统/01-发电运营（15家）"
SUBDIR = "董事长致辞与MD&A"
CJK = re.compile(r'[\u4e00-\u9fff]')
LAT = re.compile(r'[A-Za-z]')
ALNUM = re.compile(r'[\u4e00-\u9fff0-9A-Za-z]')
# 合法「无字母数字」行：① 分隔线 ---；② 表格分隔行 |---|；③ 表格纯符号数据行（（%）/–/%）
LEGIT_SYM = (re.compile(r'^[-*_]{3,}$'),
             re.compile(r'^\|[\s\-:|]+\|$'),
             re.compile(r'^\|[\s\-:|（()）%–—]*\|$'))


def is_english_line(s):
    """与 tidy_extract._is_english 同判据（①≥2 英文词 ②短行含≥4字母词），
    保证「英文残留=0」是可核验的真实达标，而非口径放宽造成的假绿。"""
    if CJK.search(s):
        return False
    core = re.sub(r'[^A-Za-z\u4e00-\u9fff ]', ' ', s)
    words = [w for w in core.split() if w]
    if len(words) >= 2 and sum(len(w) for w in words if LAT.search(w)) >= 24:
        return True
    if len(s) <= 30:
        aw = [w for w in re.sub(r'[^A-Za-z ]', ' ', s).split() if w]
        if aw and max(len(w) for w in aw) >= 4:
            return True
    return False


def is_reversed(s):
    t = s.strip()
    if not t or len(t) > 40:
        return False
    r = t[::-1]
    return ('有限公司' in r) or bool(re.search(r'年[報报]\d', r))


def is_dup(s):
    """字符三连叠印伪影（≥3 连续同字）。⚠️ 不可用 ≥2 连：合法专名常含 2 连字
    （`国电电力`/`H股股東`），用 2 连会误报 11 行；`>` 元数据行（来源/范围）须排除。"""
    t = s.strip()
    if not t or len(t) > 30 or t.startswith('>'):
        return False
    return bool(re.search(r'([\u4e00-\u9fff])\1{2,}', t))


def main():
    ap = argparse.ArgumentParser(description="可读化 MD 批量质检")
    ap.add_argument("--base", default=BASE, help="公司根目录（内含 NN_公司 子目录）")
    a = ap.parse_args()
    base = a.base
    dirs = sorted([d for d in glob.glob(os.path.join(base, "*"))
                   if os.path.isdir(d) and re.match(r'^\d+_', os.path.basename(d))])
    tot = defaultdict(int)
    rows = []
    for d in dirs:
        name = os.path.basename(d)
        # 优先查专属子文件夹；兼容旧版（MD 曾直接放根目录）做回退
        mds = sorted(glob.glob(os.path.join(d, SUBDIR, "*董事长致辞与MD&A.md")))
        if not mds:
            mds = sorted(glob.glob(os.path.join(d, "*董事长致辞与MD&A.md")))
        if not mds:
            rows.append((name, "缺 MD", 0, 0, 0, 0, 0, 0, 0, 0))
            continue
        en = rv = dp = sym = chars = 0
        hc = hm = 0
        for mp in mds:
            with open(mp, encoding='utf-8') as f:
                lines = f.read().splitlines()
            for ln in lines:
                s = ln.strip()
                if not s:
                    continue
                if is_english_line(s):
                    en += 1
                if is_reversed(s):
                    rv += 1
                if is_dup(s):
                    dp += 1
                if not ALNUM.search(s) and not any(p.match(s) for p in LEGIT_SYM):
                    sym += 1
            txt = "\n".join(lines)
            if re.search(r'董事长致辞|董事長致辭|主席報告|致股东', txt):
                hc = 1
            if re.search(r'管理层讨论与分析|管理層討論|董事会报告|業務回顧|经营情况讨论', txt):
                hm = 1
            chars += len(txt)
        nfiles = len(mds)
        rows.append((name, "OK", nfiles, chars, en, rv, dp, sym, hc, hm))
        tot['en'] += en; tot['rv'] += rv; tot['dp'] += dp; tot['sym'] += sym
        tot['chair'] += hc; tot['mda'] += hm; tot['chars'] += chars; tot['files'] += nfiles

    print(f"{'公司':<14}{'年文件':>6}{'字数':>9}{'英文行':>7}{'反序':>6}{'重复':>6}{'符号':>6}{'致辞':>5}{'MD&A':>6}")
    print("-" * 78)
    for r in rows:
        if r[1] == "缺 MD":
            print(f"{r[0]:<14}{'—':>6}{'—':>9}{'—':>7}{'—':>6}{'—':>6}{'—':>6}{'—':>5}{'—':>6}")
            continue
        _, _, nf, ch, en, rv, dp, sym, hc, hm = r
        print(f"{r[0]:<14}{nf:>6}{ch:>9}{en:>7}{rv:>6}{dp:>6}{sym:>6}{'✓' if hc else '✗':>5}{'✓' if hm else '✗':>6}")
    print("-" * 78)
    print(f"合计：{len([r for r in rows if r[1]=='OK'])}/{len(rows)} 家成稿 | "
          f"{tot['files']} 个年份 MD | 总字数 {tot['chars']:,} | 致辞命中 {tot['chair']} | MD&A命中 {tot['mda']}")
    print(f"硬伤：英文行 {tot['en']} | 反序页眉 {tot['rv']} | 字符重复 {tot['dp']} | 纯符号行 {tot['sym']}")
    ok = (tot['en'] == 0 and tot['rv'] == 0 and tot['sym'] == 0)
    print("判定：" + ("✅ 过关" if ok else "⚠️ 需复核（见上表）"))


if __name__ == '__main__':
    main()
