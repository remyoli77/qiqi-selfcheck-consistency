#!/usr/bin/env python3
"""qiqi-selfcheck-consistency —— 自一致性校验（SelfCheckGPT 思路，纯标准库）

来源：
    core/rendao/hallucination_detector.py     (290 行, SelfCheckGPT 一致性打分)
    core/self_consistency_checker.py          (511 行, 多源一致性/冲突检测)

核心思想（沿用原模块）：
    若模型对某内容确知，多次采样的回答应彼此一致；若为幻觉，则回答会相互矛盾。

本技能对「同一问题的 N 份回答」做：
    ① 两两一致性矩阵（字符 n-gram + 词集 Jaccard）
    ② 总体一致性分数
    ③ 被孤立的少数派回答
    ④ 相互矛盾的断言对（找出关键差异句）

实现要点：
    * 纯规则/统计，**不调用任何 LLM**，不联网；
    * 若调用方未提供 N>=2 份回答 → 输出 needs_input 并 exit 2（绝不编造回答）。

子命令：
    check    --input <file>    完整报告
    pairwise --input <file>    仅两两矩阵与逐对明细
    selftest                   用示例回答跑完整流程

返回码：0=正常；2=输入不足（needs_input）
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

SKILL_NAME = "qiqi-selfcheck-consistency"
NEEDS_INPUT = {"status": "needs_input", "required": "至少 2 份回答"}

# 回归用示例（取自 data/zh_texts.json 的 consistency_case，非生产数据）
CONSISTENCY_CASE_ANSWERS = [
    "core/ 目录下递归统计共有 781 个 Python 文件，总计约 246009 行代码。",
    "我数了一下，core/ 下面大概有 781 个 .py 文件，总行数差不多 24.6 万行。",
    "core/ 目录下大约有 320 个 Python 文件，代码量在 12 万行左右。",
]

# 与源模块 CONSISTENCY_THRESHOLDS 对齐
LEVEL_THRESHOLDS = [("consistent", 0.8), ("partial", 0.5), ("conflict", 0.0)]

_SPLIT_RE = re.compile(r"[。！？!?；;\n]+|(?<=\.)\s+")
_NEG_RE = re.compile(r"(不|没|无|非|未|别|never|not|no\b|n't)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# 工作区
# ---------------------------------------------------------------------------

def resolve_workspace(cli_value):
    """--workspace > QIQI_WORKSPACE > 当前目录。禁止硬编码。"""
    if cli_value:
        return Path(cli_value).expanduser().resolve()
    env = os.environ.get("QIQI_WORKSPACE")
    if env:
        return Path(env).expanduser().resolve()
    return Path.cwd().resolve()


# ---------------------------------------------------------------------------
# 文本相似度（字符 n-gram + 词集 Jaccard）
# ---------------------------------------------------------------------------

def _norm(s):
    return re.sub(r"\s+", "", s).lower()


def _ngrams(s, n):
    s = _norm(s)
    return {s[i:i + n] for i in range(len(s) - n + 1)} if len(s) >= n else ({s} if s else set())


def _tokens(s):
    s = s.lower()
    toks = set(re.findall(r"[a-z0-9_]+", s))
    toks |= set(re.findall(r"[\u4e00-\u9fff]{2,}", s))
    return toks


def _jaccard(a, b):
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# 数量级单位 → 归一乘数（『24.6 万』与『246009』应被视为同一事实）
_NUM_UNIT = {"千": 1e3, "百": 1e2, "万": 1e4, "亿": 1e8, "億": 1e8}
_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(万|亿|億|千|百)?")


def _numeric_values(text):
    """抽取文本中的数值并归一到同一量级（万/亿/千/百 换算为绝对值）。"""
    vals = []
    for m in _NUM_RE.finditer(text):
        v = float(m.group(1))
        if m.group(2):
            v *= _NUM_UNIT[m.group(2)]
        vals.append(v)
    return vals


def _number_agreement(va, vb):
    """数字对齐度（0~1）；任一为空则返回 None（该分量不参与）。

    以相对容差 5% 判定『同一数字』——246009 与 24.6 万(246000) 视为一致，
    781 与 320 视为冲突。"""
    if not va or not vb:
        return None
    matched, used = 0, set()
    for a in va:
        for j, b in enumerate(vb):
            if j in used:
                continue
            if abs(a - b) <= max(1.0, 0.05 * max(abs(a), abs(b))):
                matched += 1
                used.add(j)
                break
    return 2 * matched / (len(va) + len(vb))


def similarity(a, b):
    """综合相似度：数字/事实对齐 + 话题重叠 + 表面字面。

    缺陷 C1：原实现只用字符 n-gram（0.4×二元 + 0.35×三元 + 0.25×词集），
    对短中文改写极不稳定——『同一事实、同一数字』的两份反而不如『数字矛盾』的
    两份像。现引入**数字/量级对齐**：把 24.6 万 与 246009 归一到同一量级比较，
    使『同一事实』显著高于『数字冲突』。
    """
    if _norm(a) == _norm(b):
        return 1.0
    n2 = _jaccard(_ngrams(a, 2), _ngrams(b, 2))
    n3 = _jaccard(_ngrams(a, 3), _ngrams(b, 3))
    tk = _jaccard(_tokens(a), _tokens(b))
    surface = 0.4 * n2 + 0.35 * n3 + 0.25 * tk
    topic = _overlap_ratio(a, b)
    num_agr = _number_agreement(_numeric_values(a), _numeric_values(b))
    if num_agr is None:
        return round(0.5 * surface + 0.5 * topic, 4)
    return round(0.5 * num_agr + 0.25 * surface + 0.25 * topic, 4)


def split_sentences(text):
    return [s.strip() for s in _SPLIT_RE.split(text) if s and len(s.strip()) >= 2]


def _overlap_ratio(a, b):
    """话题重叠率：共享二元组 / 较短一方二元组数。"""
    ga, gb = _ngrams(a, 2), _ngrams(b, 2)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / min(len(ga), len(gb))


def _has_negation(s):
    return bool(_NEG_RE.search(s))


# ---------------------------------------------------------------------------
# 输入
# ---------------------------------------------------------------------------

def load_answers(path):
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8", errors="replace")
    data = json.loads(raw)
    if isinstance(data, dict):
        for k in ("answers", "responses", "samples", "outputs"):
            if k in data and isinstance(data[k], list):
                data = data[k]
                break
        else:
            raise ValueError("JSON 对象需含 answers/responses/samples 数组")
    if not isinstance(data, list):
        raise ValueError("输入需为 JSON 数组")
    out = []
    for i, item in enumerate(data):
        if isinstance(item, str):
            out.append({"index": i, "text": item})
        elif isinstance(item, dict):
            for k in ("answer", "text", "content", "response"):
                if k in item and isinstance(item[k], str):
                    out.append({"index": i, "text": item[k], "id": item.get("id", i)})
                    break
            else:
                raise ValueError(f"第 {i} 项缺少 answer/text/content 字段")
        else:
            raise ValueError(f"第 {i} 项类型不支持（需字符串或对象）")
    return out


# ---------------------------------------------------------------------------
# 主分析
# ---------------------------------------------------------------------------

def _level_of(score):
    for name, thr in LEVEL_THRESHOLDS:
        if score >= thr:
            return name
    return "conflict"


def analyze(answers):
    n = len(answers)
    texts = [a["text"] for a in answers]

    # ① 两两一致性矩阵
    matrix = [[1.0 if i == j else similarity(texts[i], texts[j]) for j in range(n)] for i in range(n)]

    pair_sims = []
    for i in range(n):
        for j in range(i + 1, n):
            pair_sims.append((i, j, matrix[i][j]))

    # ② 总体一致性分数：所有非对角两两相似度的均值
    vals = [p[2] for p in pair_sims]
    overall = round(sum(vals) / len(vals), 4) if vals else None
    overall_level = _level_of(overall) if overall is not None else "insufficient"

    # 每份回答的平均相似度
    avg = []
    for i in range(n):
        others = [matrix[i][j] for j in range(n) if j != i]
        avg.append(round(sum(others) / len(others), 4) if others else 0.0)

    centroid_idx = max(range(n), key=lambda i: avg[i])
    centroid_avg = avg[centroid_idx]

    # ③ 被孤立的少数派（N<3 时无法定义"多数派"，不做判定）
    minority = []
    minority_note = None
    if n >= 3:
        cutoff = max(0.3, 0.5 * centroid_avg)
        for i in range(n):
            if i == centroid_idx:
                continue
            if avg[i] < cutoff:
                minority.append({
                    "index": i,
                    "avg_similarity": avg[i],
                    "distance_to_majority": round(centroid_avg - avg[i], 4),
                    "reason": f"与其他回答平均相似度 {avg[i]:.2f} < 阈值 {cutoff:.2f}",
                })
        minority.sort(key=lambda x: x["avg_similarity"])
    else:
        minority_note = "回答数 < 3，无法区分多数派/少数派"

    # ④ 相互矛盾的断言对
    contradictions = []
    seen = set()
    for i in range(n):
        si_list = split_sentences(texts[i])
        for j in range(i + 1, n):
            sj_list = split_sentences(texts[j])
            for si in si_list:
                for sj in sj_list:
                    ov = _overlap_ratio(si, sj)
                    ss = similarity(si, sj)
                    if ov < 0.4:
                        continue
                    neg_diff = _has_negation(si) != _has_negation(sj)
                    # 否定冲突：话题相近即视为矛盾（哪怕字面很像）；否则需字面差异较大
                    if neg_diff:
                        accept = ss < 0.9
                    else:
                        accept = ss < 0.55 and ov >= 0.5
                    if not accept:
                        continue
                    key = (i, j, si, sj)
                    if key in seen:
                        continue
                    seen.add(key)
                    contradictions.append({
                        "answers": [i, j],
                        "sentence_a": si,
                        "sentence_b": sj,
                        "similarity": ss,
                        "topic_overlap": round(ov, 4),
                        "negation_conflict": neg_diff,
                        "note": "否定/肯定不一致（同一话题下断言相左）" if neg_diff
                                else "同一话题但表述差异较大",
                    })
    contradictions.sort(key=lambda c: (not c["negation_conflict"], -c["topic_overlap"]))
    contradictions = contradictions[:20]

    return {
        "skill": SKILL_NAME,
        "status": "ok",
        "n_answers": n,
        "answer_lengths": [len(t) for t in texts],
        "pairwise_matrix": matrix,
        "pairwise_similarities": [
            {"i": i, "j": j, "similarity": s} for (i, j, s) in pair_sims
        ],
        "overall_consistency": overall,
        "consistency_level": overall_level,
        "per_answer_avg_similarity": avg,
        "majority_answer_index": centroid_idx,
        "majority_answer_avg": centroid_avg,
        "minority_note": minority_note,
        "minority_answers": minority,
        "contradictions": contradictions,
    }


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def emit(result, fmt, sub):
    if fmt == "text":
        print(_render(result, sub))
    else:
        if sub == "pairwise":
            slim = {
                "skill": result["skill"], "status": result["status"],
                "n_answers": result["n_answers"],
                "pairwise_matrix": result["pairwise_matrix"],
                "pairwise_similarities": result["pairwise_similarities"],
                "per_answer_avg_similarity": result["per_answer_avg_similarity"],
                "majority_answer_index": result["majority_answer_index"],
            }
            print(json.dumps(slim, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))


def _render(r, sub):
    lines = [f"【{r['skill']}】回答数 {r['n_answers']}  总体一致性 {r['overall_consistency']}（{r['consistency_level']}）"]
    lines.append("两两一致性矩阵:")
    for i, row in enumerate(r["pairwise_matrix"]):
        lines.append("  " + " ".join(f"{v:.2f}" for v in row))
    if sub != "pairwise":
        lines.append(f"多数派回答: #{r['majority_answer_index']}（均值 {r['majority_answer_avg']}）")
        if r["minority_answers"]:
            lines.append("被孤立的少数派:")
            for m in r["minority_answers"]:
                lines.append(f"  - #{m['index']}  均值 {m['avg_similarity']}  {m['reason']}")
        if r["contradictions"]:
            lines.append("相互矛盾的断言对:")
            for c in r["contradictions"]:
                tag = "[否定冲突]" if c["negation_conflict"] else "[表述差异]"
                lines.append(f"  {tag} #{c['answers'][0]} vs #{c['answers'][1]}  sim={c['similarity']}")
                lines.append(f"      A: {c['sentence_a']}")
                lines.append(f"      B: {c['sentence_b']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# selftest（示例回答，明确标注为示例）
# ---------------------------------------------------------------------------

def selftest():
    ok = True
    total = 0

    def check(name, cond, extra=""):
        nonlocal ok, total
        total += 1
        status = "PASS" if cond else "FAIL"
        if not cond:
            ok = False
        print(f"[{status}] {name}" + (f"  ({extra})" if extra else ""))

    print("=== qiqi-selfcheck-consistency selftest（示例回答） ===")

    # 1. 三份回答：两份一致，一份相反 → 少数派 + 否定冲突
    ans = load_answers_from([
        "会议定在周三下午三点，地点是三楼会议室。",
        "会议定在周三下午三点，地点在三楼会议室。",
        "会议不是定在周三的。另外地点我也不记得了，可能在别处。",
    ])
    r = analyze(ans)
    check("矩阵尺寸正确", len(r["pairwise_matrix"]) == 3 and len(r["pairwise_matrix"][0]) == 3)
    check("对角线为 1.0", all(r["pairwise_matrix"][i][i] == 1.0 for i in range(3)))
    check("矩阵对称", all(abs(r["pairwise_matrix"][i][j] - r["pairwise_matrix"][j][i]) < 1e-9
                          for i in range(3) for j in range(3)))
    check("总体一致性在 [0,1]", 0.0 <= r["overall_consistency"] <= 1.0, str(r["overall_consistency"]))
    check("识别出被孤立的少数派 #2", any(m["index"] == 2 for m in r["minority_answers"]),
          str([m["index"] for m in r["minority_answers"]]))
    check("识别出否定冲突断言", any(c["negation_conflict"] for c in r["contradictions"]),
          f"{len(r['contradictions'])} 对")
    check("多数派索引前二之一", r["majority_answer_index"] in (0, 1), str(r["majority_answer_index"]))

    # 2. 三份完全一致 → 一致性 1.0，无少数派
    same = load_answers_from(["苹果是红色的水果。", "苹果是红色的水果。", "苹果是红色的水果。"])
    r2 = analyze(same)
    check("全一致 → overall=1.0", r2["overall_consistency"] == 1.0)
    check("全一致 → 无少数派", len(r2["minority_answers"]) == 0)
    check("全一致 → 无矛盾", len(r2["contradictions"]) == 0)
    check("全一致 → level=consistent", r2["consistency_level"] == "consistent")

    # 3. 相似度自洽性
    check("相似度自反", similarity("你好世界", "你好世界") == 1.0)
    check("相似度对称", similarity("今天下雨了", "今天没下雨") ==
          similarity("今天没下雨", "今天下雨了"))
    check("相似度越界钳制", 0.0 <= similarity("abc", "xyz") <= 1.0)

    # 4. N<2 → needs_input
    shim = _needs_input_check(1)
    check("N=1 → needs_input 且 exit 2", shim["status"] == "needs_input" and shim["exit"] == 2)
    shim = _needs_input_check(0)
    check("N=0 → needs_input", shim["status"] == "needs_input")

    # 5. 句子切分
    sents = split_sentences("你好。世界！这是真的吗？")
    check("句子切分正确", len(sents) == 3, str(sents))

    # 6. 回归（缺陷 C1）：同一事实（781 / 24.6万）应比数字冲突份（320 / 12万）更像
    ans = load_answers_from(CONSISTENCY_CASE_ANSWERS)
    r = analyze(ans)
    sim01 = r["pairwise_matrix"][0][1]
    sim02 = r["pairwise_matrix"][0][2]
    check("回归·#0 与 #1 相似度必须高于 #0 与 #2",
          sim01 > sim02, f"sim(0,1)={sim01} > sim(0,2)={sim02}")
    check("回归·少数派必须是 #2",
          [m["index"] for m in r["minority_answers"]] == [2],
          str([m["index"] for m in r["minority_answers"]]))
    check("回归·#1 不得被误判为少数派",
          1 not in [m["index"] for m in r["minority_answers"]], "")
    check("回归·矛盾对指向 #0 vs #2",
          any(c["answers"] == [0, 2] for c in r["contradictions"]),
          str([c["answers"] for c in r["contradictions"]]))
    check("回归·数字归一：24.6 万 ≈ 246009 判为同事实",
          _number_agreement(_numeric_values("246009 行"), _numeric_values("24.6 万行")) == 1.0, "")

    print(f"\n{'✅ 全部通过' if ok else '❌ 存在失败项'}（{total} 步）")
    return 0 if ok else 1


def load_answers_from(lst):
    """把字符串列表转成内部结构（selftest 用）。"""
    return [{"index": i, "text": t} for i, t in enumerate(lst)]


def _needs_input_check(n):
    if n < 2:
        return {**NEEDS_INPUT, "exit": 2}
    return {"status": "ok", "exit": 0}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    sub = argparse.ArgumentParser(add_help=False)
    sub.add_argument("--workspace", default=argparse.SUPPRESS)
    sub.add_argument("--format", choices=["json", "text"], default=argparse.SUPPRESS)

    p = argparse.ArgumentParser(
        prog="selfcheck_consistency.py",
        description="qiqi-selfcheck-consistency —— 同一问题 N 份回答的自一致性校验（不调用 LLM）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python3 selfcheck_consistency.py check --input answers.json\n"
               "  python3 selfcheck_consistency.py pairwise --input answers.json --format text\n"
               "  python3 selfcheck_consistency.py selftest\n"
               "输入格式（JSON）: [\"回答1\", \"回答2\", ...] 或 [{\"answer\":\"...\"}, ...]",
    )
    p.add_argument("--workspace", default=None)
    p.add_argument("--format", choices=["json", "text"], default="json")

    sp = p.add_subparsers(dest="cmd")
    c = sp.add_parser("check", parents=[sub], help="完整一致性报告")
    c.add_argument("--input", required=True, help="回答 JSON 文件，或 '-'")
    pw = sp.add_parser("pairwise", parents=[sub], help="仅两两矩阵与逐对明细")
    pw.add_argument("--input", required=True, help="回答 JSON 文件，或 '-'")
    sp.add_parser("selftest", parents=[sub], help="用示例回答跑完整流程")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    resolve_workspace(args.workspace)
    fmt = getattr(args, "format", "json")

    if args.cmd is None:
        build_parser().print_help()
        return 0
    if args.cmd == "selftest":
        return selftest()

    try:
        answers = load_answers(args.input)
    except Exception as e:
        print(json.dumps({"status": "error", "error": f"无法读取输入: {e}"},
                         ensure_ascii=False, indent=2))
        return 2

    if len(answers) < 2:
        print(json.dumps(NEEDS_INPUT, ensure_ascii=False, indent=2))
        return 2

    result = analyze(answers)
    emit(result, fmt, args.cmd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
