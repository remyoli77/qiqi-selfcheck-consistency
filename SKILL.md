---
name: qiqi-selfcheck-consistency
version: 1.0.0
description: 对同一问题的 N 份回答做自一致性打分，输出两两矩阵、总体分数、少数派与矛盾断言对。触发词：自一致性、SelfCheckGPT、多份回答比对、一致性校验、矛盾检测。
permissions:
  paths: [skills/qiqi-selfcheck-consistency, <workspace>/data]
  write: []
---

# qiqi-selfcheck-consistency - 自一致性校验

## 💕 设计理念

SelfCheckGPT 的直觉很简单：**如果模型真的知道，那么换着法儿问几次，答案该差不多；如果是编的，多采几次就会打架。** 原框架里这套思路散在两处——`core/rendao/hallucination_detector.py`（290 行）的一致性打分，与 `core/self_consistency_checker.py`（511 行）的冲突检测。

本技能把它收敛成一个**纯离线、纯标准库**的比对器：不调 LLM、不联网，只用字符 n-gram 与词集 Jaccard 做相似度、用多数派聚合找出异类。它不判断"哪个答案是对的"，只回答"这些答案彼此有多不一致、谁被孤立、哪两句话互相打脸"。

## 🎯 核心能力

- **两两一致性矩阵**：N×N，对角线为 1.0。`sim = 0.5×数字/事实对齐 + 0.25×话题重叠 + 0.25×表面字面`（表面字面 = 字符二元/三元 + 词集 Jaccard）；数字按 `万/亿/千/百` 归一到同一量级，故 `24.6 万 ≈ 246009` 视为同一事实，数字冲突份（781 vs 320）显著更低。
- **总体一致性分数**：所有非对角两两相似度均值，映射为 `consistent / partial / conflict` 三档（阈值 0.8 / 0.5）。
- **被孤立的少数派**：以"与其他回答平均相似度最高者"为多数派中心，低于 `max(0.3, 0.5×中心均值)` 者为少数派；N<3 不做此判定（无法定义多数派）。
- **相互矛盾的断言对**：句级对齐后找出"话题重叠高、表述相左"的句子对；**否定词差异**（一有一无）单独标为「否定冲突」。
- **输入门控**：N<2 时输出 `{"status":"needs_input","required":"至少 2 份回答"}` 并 exit 2，绝不编造回答。

## 🔄 执行流程

```
N 份回答 JSON
   │
   ├─ 解析（["a","b"] 或 [{"answer":..}] 或 {"answers":[...]}）
   │     └─ N<2 → needs_input, exit 2
   │
   ├─ 归一化 → 字符二元/三元集合 + 词集
   ├─ ① 两两相似度 → N×N 矩阵
   ├─ ② 总体一致性 = 非对角均值 → level
   ├─ ③ 每份均值 → 多数派中心 → 少数派
   ├─ ④ 句级对齐 → 话题重叠 ≥0.4 且（否定冲突 或 字面差异大）→ 矛盾对
   │
   └─ stdout JSON（或 --format text）
```

## 📁 文件结构

```
qiqi-selfcheck-consistency/
├── SKILL.md
└── scripts/
    └── selfcheck_consistency.py     # 437 行
```

## 🔧 使用方式

### 命令行

```bash
# 完整报告
python3 scripts/selfcheck_consistency.py check --input answers.json

# 仅两两矩阵与逐对明细
python3 scripts/selfcheck_consistency.py --format text pairwise --input answers.json

# 自检（示例回答，全通过 exit 0）
python3 scripts/selfcheck_consistency.py selftest
```

输入格式：

```json
["回答一……", "回答二……", "回答三……"]
```

### Python API

```python
from selfcheck_consistency import similarity, analyze, split_sentences

similarity("今天下雨了", "今天没下雨")     # -> 0~1
report = analyze([{"index": 0, "text": "……"}, {"index": 1, "text": "……"}])
report["overall_consistency"]
report["minority_answers"]
report["contradictions"]
```

## ⚙️ 配置说明

阈值与权重内嵌于脚本（单文件分发）；如需外置可抽为：

```json
{
  "weights": { "ngram2": 0.4, "ngram3": 0.35, "tokenset": 0.25 },
  "levels": [ { "name": "consistent", "min": 0.8 },
              { "name": "partial", "min": 0.5 },
              { "name": "conflict", "min": 0.0 } ],
  "minority": { "cutoff_floor": 0.3, "center_ratio": 0.5, "min_answers": 3 },
  "contradiction": { "topic_overlap_min": 0.4, "literal_sim_max": 0.55 }
}
```

## 🔥 五行归属

金行·校验——「一次可能是巧合，多次不一致就是信号」。

## 🔗 与框架的对应关系

| 本技能文件 | 来源 core 模块 | 改造动作 |
|---|---|---|
| scripts/selfcheck_consistency.py `analyze` / 相似度 | `core/rendao/hallucination_detector.py`（290 行）| 沿用 SelfCheckGPT"低一致性=可疑"思路；相似度由"按空格切词重叠"改为**字符 n-gram + 词集 Jaccard**（适配中文）|
| scripts/selfcheck_consistency.py 一致性等级 | `core/self_consistency_checker.py`（511 行）| 沿用 `CONSISTENCY_THRESHOLDS`（0.8/0.5）三档；冲突检测改为句级对齐 |
| scripts/selfcheck_consistency.py 数量门控 | `self_consistency_checker.check_consistency` 的 `len<2` 分支 | 显式化为 `needs_input` + exit 2 |
| —（未迁移）| `SelfConsistencyChecker.verify_from_memory/_conversation/_knowledge_graph` | 见下 |

## ⚠️ 迁移注意

- **路径参数化**：原 `self_consistency_checker` 默认 `~/.openclaw/workspace`；本技能改为 `--workspace > QIQI_WORKSPACE > cwd`，且不写盘。
- **依赖**：仅标准库（argparse/json/os/re/sys/pathlib）。
- **未实现 / 降级**：
  - `self_consistency_checker.py` 的**多源验证**（记忆库 / 知识图谱 / 对话历史）与 `check_results.json` 持久化、`get_stats()` **未迁移**——它们依赖框架的其它子系统，剥离后无意义。
  - **不做"哪个是对的"判断**：只给一致性信号。多数派中心是"最像大家的那个"，不保证正确（大家一起错时它会错）。
  - 相似度**无句子嵌入、无中文分词**，对"语义相同但换了大量措辞"会低估一致性；对关键词堆砌会高估。
  - 矛盾检测是**启发式对齐**：可能漏掉含蓄矛盾，也可能把正常的不同侧面当成矛盾。
  - N<3 时不判定少数派（`minority_note` 会说明）。

## 📝 更新日志

- 2026-09-16：由 core 模块改造为独立 Skill。
- 2026-09-16（修复）：相似度方向修正——原纯字符 n-gram 会把「语义一致、数字相同」的两份压到比「数字矛盾」的两份还低；现引入**数字/量级对齐**（万/亿归一），使 `#0 与 #1（781/24.6万）` 显著高于 `#0 与 #2（320/12万）`，少数派排序随之纠正。

## 前置条件（Precondition）

- 已安装 Python 3.12（仅标准库）。
- 提供同一问题的 **N≥2** 份回答，JSON 数组（字符串数组或 `{"answer"/"text"/"content"}` 对象数组，或 `{"answers":[...]}`）。

## 后置效果（Effect）

- eff_add: 一致性报告 `{pairwise_matrix, overall_consistency, minority_answers[], contradictions[]}` 进入调用方上下文。
- eff_event: `overall_consistency` 落入 `conflict` 档、或检出「否定冲突」断言对时，发出「结果存疑、建议交叉验证」事件。

## 副作用说明

- **无副作用**：不写文件、不改状态、不联网、不调用任何 LLM/嵌入模型。
- 一致性分数是**相对指标**：只在同批回答内部有意义，跨批次不可直接比较。
- `N=2` 时一致性只有一个数，参考价值有限；少数派判定不生效。
