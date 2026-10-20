# qiqi-selfcheck-consistency

> **标签**：`ai-agent` `selfcheckgpt` `consistency-check` `hallucination-detection` `nlp` `python`

> 单智能体技能（Skill） · v1.0.0 · Python 3.10+ · 仅标准库，零第三方依赖

## 功能简介

**对同一问题的 N 份回答做自一致性打分，输出两两矩阵、总体分数、少数派与矛盾断言对**

对同一问题的 N 份回答做自一致性打分，输出两两矩阵、总体分数、少数派与矛盾断言对。触发词：自一致性、SelfCheckGPT、多份回答比对、一致性校验、矛盾检测。


**触发词**：自一致性、SelfCheckGPT、多份回答比对、一致性校验、矛盾检测


## 特点

- ✅ 自带 `selftest` 自检命令，开箱即验
- ✅ 仅用 Python 标准库，`pip install` 零依赖
- ✅ 工作区可参数化（`--workspace` / 环境变量 `QIQI_WORKSPACE`），无硬编码路径
- ✅ 默认输出 JSON，便于被其他智能体/流程编排调用

## 主要命令

```bash
check / pairwise / selftest
```

## 快速开始

```bash
python3 scripts/<入口脚本>.py selftest
```

## 来源与改动

- **来源模块**：`rendao/hallucination_detector.py`(290) + `core/self_consistency_checker.py`(511)
- **改造动作**：合并两个来源，输入改为 N 份回答

### 诚实标注（未移植/边界）

## ⚠️ 迁移注意
- **路径参数化**：原 `self_consistency_checker` 默认 `~/.openclaw/workspace`；本技能改为 `--workspace > QIQI_WORKSPACE > cwd`，且不写盘。
- **依赖**：仅标准库（argparse/json/os/re/sys/pathlib）。
- **未实现 / 降级**：
  - `self_consistency_checker.py` 的**多源验证**（记忆库 / 知识图谱 / 对话历史）与 `check_results.json` 持久化、`get_stats()` **未迁移**——它们依赖框架的其它子系统，剥离后无意义。
  - **不做"哪个是对的"判断**：只给一致性信号。多数派中心是"最像大家的那个"，不保证正确（大家一起错时它会错）。
  - 相似度**无句子嵌入、无中文分词**，对"语义相同但换了大量措辞"会低估一致性；对关键词堆砌会高估。
  - 矛盾检测是**启发式对齐**：可能漏掉含蓄矛盾，也可能把正常的不同侧面当成矛盾。
  - N<3 时不判定少数派（`minority_note` 会说明）。


## 目录结构

```
SKILL.md
LICENSE
scripts/selfcheck_consistency.py
```

## 许可证

BSD-3-Clause（见 [LICENSE](LICENSE)）