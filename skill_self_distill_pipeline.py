#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys

import re
import json
import requests
from pathlib import Path
from datetime import datetime
from multiprocessing import Pool

os.environ['NO_PROXY'] = '76.64.185.52'
os.environ['no_proxy'] = '76.64.185.52'


PROMPT_TEMPLATE = """你是一个ipran网络运维专家，需要在网管侧完成一份排障手册，将同一分类目录下的多篇输入文档合并转为一个完整的skill。

# 任务要求
- **输入文档集合**中每篇文档都以"## 文档：<标题>"作为分隔标记，均属于同一个二级目录分类下的故障处理案例，请将它们合并为**一个**完整的skill，同时要注意**用户要求**中的指示。
- 如果多篇文档描述的是相同或高度相似的故障场景、排查步骤，请合并去重，避免重复内容。
- 如果多篇文档描述的是不同的故障场景，请采用"公共前置检查 + 分场景追加步骤"的结构组织skill：
  1. 先提取所有场景共享的排查步骤（如检查接口状态、检查邻居状态、检查基础配置等），放在开头的 `## 公共前置检查` 小节中，只写一遍，并连续编号。
  2. 之后设 `## 分场景追加步骤` 小节，每个场景一个子小节（如 `### 场景A：xxx`），只写该场景在公共前置检查之后**追加**的特有步骤，不要重复公共步骤；步骤编号接续公共前置检查的编号。
  3. 在公共前置检查的相关步骤中，用判断结果指引进入哪个场景的追加步骤（如"若邻居未建立，转场景A继续排查"）。
  4. 若某场景与公共前置检查完全无交集，允许其步骤独立完整编写。
- 转换时，如果发现文档有缺漏，可以补充信息（比如需要进入某视图、需要commit）
- 由于这份skill是给网管agent使用，因此某个步骤如果涉及收集信息、联系技术支持、提交给工程师等动作，请删除整个步骤，并删除其它步骤对此步骤的引用。
- 特别注意：输出全文**禁止出现**"联系技术支持"、"寻求技术支持"、"提交给工程师"、"收集信息并联系"等表述。原文档结尾常见的"若以上步骤仍未解决，请收集信息并联系技术支持"这类兜底句，必须整句删除，不要以任何改写形式保留；排障步骤穷尽后直接结束即可。
- 引用其它文档时的处理规则（不要保留原始文档的路径或链接，它们在转换后已失效）：
  1. 如果被引用的文档就在本次**输入文档集合**中（已合并进本skill），改写为本skill内部小节的引用，如"参考场景A继续排查"。
  2. 如果被引用的内容属于**skill目录清单**中的其它分类，改写为skill引用标识：方括号包裹清单中的相对路径，如：参考[故障处理：IP路由/BGP故障案例.md]继续排查。
  3. 如果被引用的文档不在清单中（如外部产品手册、命令参考），保留为纯文字说明（写明文档名称即可），不要输出链接。
- 遇到源文档有大段回显时，不要照搬浪费token，在步骤中讲清要注意哪些回显内容即可。

# 输入文档集合
<input_docs>

# skill目录清单
本批次会将各分类目录分别生成为独立的skill，清单如下（每行一个skill的相对路径）。跨分类引用时必须使用清单中的路径，格式为[路径]：
<skill_catalog>

# 用户要求
<user_demand>

# Skill输出内容格式

```markdown
---
name: skill的英文名称（连字符使用-而不要使用_）
description: skill的一句话简介
---

# xxx
* 文档中完整的排障思路 + 对应排障所需工具
* 按照标准markdown格式输出skill，以#开始，每层增加一个#
......
```

# 输出格式参考
```markdown
---
name: bgp
description: 针对BGP邻居无法建立或频繁震荡场景的端到端排障指南。
---

# BGP邻居异常排障指南

## 1. 确认BGP邻居状态与全局错误日志
排障的第一步是明确当前BGP邻居停留在哪个状态（如Idle、Active、Connect等），并快速查看设备记录的BGP报错信息，这通常能直接指出问题所在。

* **检查BGP邻居状态**：
  使用 `display bgp peer` 查看邻居状态。如果状态不是 `Established`，则说明邻居关系异常。
* **查看日志缓冲区**：
  使用 `display logbuffer | include BGP` 检索近期是否有BGP状态变化的告警（Trap）信息，确认是刚初始化的建流失败还是已建连的邻居发生震荡（Flapping）。
......
```
"""


def extract_markdown_content(text: str) -> str:
    pattern = r"```markdown\s*(.+)\s*```"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip()

    # 兜底：模型偶尔不带```markdown围栏，直接输出以frontmatter（---\nname:...）
    # 开头的skill正文，或用不带语言标记的```围栏包裹。
    fm_match = re.search(r"^-{3}\s*\n\s*name\s*:", text, re.MULTILINE)
    if fm_match:
        content = text[fm_match.start():].strip()
        # 仅当frontmatter之前存在外层围栏的开头```时，结尾的```才是包裹围栏，
        # 需要剥掉；否则结尾的```属于正文内代码块的闭合，剥掉会破坏内容。
        has_wrapper_fence = "```" in text[:fm_match.start()]
        if has_wrapper_fence and content.endswith("```"):
            content = content[:-3].strip()
        return content

    return ""


def call_model_with_retry(api_url: str, model_name: str, question: str, max_retries: int = 3, retry_delay: float = 1.0) -> str:
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": question}],
        "stream": False,
        "temperature": 0.4,
        "max_tokens": 16384,
        "chat_template_kwargs": {"enable_thinking": False, "thinking": False}
    }

    last_error = None
    for attempt in range(max_retries):
        try:
            response = requests.post(
                api_url,
                json=payload,
                timeout=300,
                verify=False
            )
            response.raise_for_status()
            res_json = response.json()
            choice = res_json['choices'][0]
            message = choice['message']
            content = message.get('content') or ""

            finish_reason = choice.get('finish_reason')
            if finish_reason == 'length':
                raise Exception(f"输出被截断(finish_reason=length)，内容长度{len(content)}字符，请增大max_tokens或拆分输入")

            extracted_content = extract_markdown_content(content)
            if not extracted_content:
                raise Exception(f"No markdown content found. finish_reason={finish_reason}, 内容长度{len(content)}字符, 开头内容: {content[:200]!r}")
            return extracted_content

        except requests.exceptions.Timeout:
            last_error = f"请求超时 (尝试 {attempt + 1}/{max_retries})"
        except requests.exceptions.ConnectionError:
            last_error = f"连接失败 (尝试 {attempt + 1}/{max_retries})"
        except requests.exceptions.HTTPError as e:
            last_error = f"HTTP错误: {e.response.status_code} (尝试 {attempt + 1}/{max_retries})"
        except Exception as e:
            last_error = f"未知错误: {str(e)} (尝试 {attempt + 1}/{max_retries})"

        if attempt < max_retries - 1:
            import time
            time.sleep(retry_delay * (attempt + 1))
            print(f"try failed: {last_error}")

    return f"错误：{last_error}"



def get_all_markdown_files(tree_dir: str) -> list:
    md_files = []
    for root, dirs, files in os.walk(tree_dir):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for file in files:
            if file.endswith('.md'):
                md_files.append(os.path.join(root, file))
    return sorted(md_files)


# 计算文档名与二级目录名的相似度前先剔除的通用词，避免"故障""案例"这类
# 字样在所有名称之间制造虚假重叠。
GENERIC_NAME_TOKENS = ("故障案例", "常见故障", "故障", "案例", "：", ":")


def _strip_generic_tokens(name: str) -> str:
    for token in GENERIC_NAME_TOKENS:
        name = name.replace(token, "")
    return name.strip()


def _longest_common_substring_len(a: str, b: str) -> int:
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for ch_a in a:
        cur = [0] * (len(b) + 1)
        for j, ch_b in enumerate(b, 1):
            if ch_a == ch_b:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def derive_case_group_name(level1_dir: str) -> str:
    """一级目录没有二级目录时，散文档合并组的名称：
    去掉"故障处理："类前缀后加"故障案例"，如 故障处理：QoS → QoS故障案例。"""
    base = re.split(r"[：:]", level1_dir)[-1].strip() or level1_dir
    return f"{base}故障案例"


def group_files_by_second_level(md_files: list, source_tree_dir: str) -> dict:
    """按源目录树的二级子目录对文档分组：<source_tree_dir>/一级目录/二级目录/*.md 归为同一组。

    - 若文档位于二级目录及更深层级，按 (一级目录, 二级目录) 分组（更深层级一并并入该组）。
    - 若文档散落在一级目录下（没套二级目录）：
      * 该一级目录下存在二级分组时，并入名称最相似（剔除通用词后最长公共子串>=2）
        的既有二级分组；
      * 无法匹配或该一级目录没有任何二级目录时，全部合并为
        (一级目录, derive_case_group_name(一级目录)) 一个新分组。
      每篇散文档的归并决策都会打印出来，便于核对归属。
    - 若文档直接位于 source_tree_dir 根目录下，归入 () 空分组。
    分组结果按 key 排序，组内文件保持原有排序。
    """
    groups = {}
    loose_by_level1 = {}
    for doc_path in md_files:
        rel_path = os.path.relpath(doc_path, source_tree_dir)
        parts = Path(rel_path).parts
        depth = len(parts) - 1
        if depth >= 2:
            groups.setdefault((parts[0], parts[1]), []).append(doc_path)
        elif depth == 1:
            loose_by_level1.setdefault(parts[0], []).append(doc_path)
        else:
            groups.setdefault((), []).append(doc_path)

    merge_notes = []
    for level1, doc_paths in loose_by_level1.items():
        existing_level2 = [key[1] for key in groups if len(key) == 2 and key[0] == level1]
        for doc_path in doc_paths:
            doc_token = _strip_generic_tokens(Path(doc_path).stem)
            best_name, best_score = None, 0
            for level2 in existing_level2:
                score = _longest_common_substring_len(doc_token, _strip_generic_tokens(level2))
                if score > best_score:
                    best_name, best_score = level2, score
            if best_name is not None and best_score >= 2:
                target, reason = best_name, f"按名称并入既有分组"
            else:
                target, reason = derive_case_group_name(level1), "无匹配的二级目录，归入新分组"
            groups.setdefault((level1, target), []).append(doc_path)
            merge_notes.append(
                f"{os.path.relpath(doc_path, source_tree_dir)} → {level1}/{target}.md ({reason})")

    if merge_notes:
        print(f"\n[INFO] {len(merge_notes)} 篇一级目录下的散文档已并入二级分组，请核对归属:")
        for note in merge_notes:
            print(f"  - {note}")

    return dict(sorted(groups.items()))


def build_merged_doc_content(file_paths: list) -> str:
    doc_blocks = []
    for doc_path in file_paths:
        title = Path(doc_path).stem
        with open(doc_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read().strip()
        doc_blocks.append(f"## 文档：{title}\n\n{content}")
    return "\n\n---\n\n".join(doc_blocks)


def group_key_to_rel_path(group_key: tuple) -> str:
    """分组key对应的skill相对路径（即引用[路径]和输出文件路径）。"""
    if len(group_key) == 2:
        return f"{group_key[0]}/{group_key[1]}.md"
    if len(group_key) == 1:
        return f"{group_key[0]}.md"
    return "root.md"


def convert_group_to_skill(args: tuple) -> dict:
    (group_key, file_paths, output_dir, api_url, model_name,
     prompt_template, skill_catalog) = args

    # skill输出为 <一级中文>/<二级中文>.md，agent按 [相对路径] 引用并直接Read
    skill_rel_path = group_key_to_rel_path(group_key)
    output_path = os.path.join(output_dir, skill_rel_path)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    group_label = "/".join(group_key) if group_key else "(root)"
    print(f"[PID {os.getpid()}] 处理分组: {group_label} ({len(file_paths)} 个文档)")

    try:
        merged_doc_content = build_merged_doc_content(file_paths)

        full_prompt = (
            prompt_template
            .replace("<user_demand>", "")
            .replace("<skill_catalog>", skill_catalog)
            .replace("<input_docs>", merged_doc_content)
        )

        result = call_model_with_retry(api_url, model_name, full_prompt)

        result_dict = {
            "group": group_label,
            "skill_path": skill_rel_path,
            "source_paths": file_paths,
            "output_path": output_path,
            "content": result,
            "success": not result.startswith("错误：")
        }

        if result_dict["success"]:
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(result)
            print(f"[PID {os.getpid()}] 已保存: {output_path}")
        else:
            print(f"[PID {os.getpid()}] 转换失败: {result}")

        return result_dict
    except Exception as e:
        print(f"[PID {os.getpid()}] 处理异常: {e}")
        return {
            "group": group_label,
            "skill_path": skill_rel_path,
            "source_paths": file_paths,
            "output_path": None,
            "content": f"错误：{str(e)}",
            "success": False
        }



def main(SOURCE_TREE_DIR, OUTPUT_DIR, API_URL, MODEL_NAME, WORKERS, GROUPS=None):
    print("=" * 60)
    print("Skill自蒸馏流水线")
    print("=" * 60)
    print(f"\n源文档树: {SOURCE_TREE_DIR}")
    print(f"输出目录: {OUTPUT_DIR}")
    print(f"API地址: {API_URL}")
    print(f"模型: {MODEL_NAME}")
    print(f"并行Worker数: {WORKERS}")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    md_files = get_all_markdown_files(SOURCE_TREE_DIR)
    print(f"\n找到 {len(md_files)} 个Markdown文档")

    groups = group_files_by_second_level(md_files, SOURCE_TREE_DIR)
    print(f"按二级目录合并为 {len(groups)} 个skill分组")

    # 一级目录下的散文档已在分组时自动并入二级分组（见group_files_by_second_level
    # 打印的归并明细）；直接位于源目录根下的文档无法归入任何一级分类，仍需人工整理。
    root_docs = groups.get(())
    if root_docs:
        print(f"\n[WARN] 发现 {len(root_docs)} 篇直接位于源目录根下的文档，"
              f"无法归入任何一级分类，将合并输出为 root.md:")
        for p in root_docs:
            print(f"  - {os.path.relpath(p, SOURCE_TREE_DIR)}")
        print()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 用全量分组构建skill目录清单（在GROUPS过滤之前），保证部分重跑时
    # 跨分类引用的skill路径依然完整。路径即 <一级中文>/<二级中文>.md
    all_keys = list(groups.keys())
    skill_catalog = "\n".join(
        f"- [{group_key_to_rel_path(key)}]" for key in all_keys
    )

    if GROUPS:
        groups = {
            key: paths
            for key, paths in groups.items()
            if any(pattern in ("/".join(key) if key else "(root)") for pattern in GROUPS)
        }
        print(f"按 --groups 参数过滤后剩余 {len(groups)} 个分组:")
        for key in groups:
            print(f"  - {'/'.join(key) if key else '(root)'}")
        if not groups:
            print("警告: 没有分组匹配指定的 --groups 参数，请检查名称（按'一级目录/二级目录'子串匹配）")
            return

    total_docs = sum(len(paths) for paths in groups.values())

    task_args = [
        (group_key, file_paths, OUTPUT_DIR, API_URL, MODEL_NAME, PROMPT_TEMPLATE, skill_catalog)
        for group_key, file_paths in groups.items()
    ]

    results = []
    with Pool(processes=WORKERS) as pool:
        results = pool.map(convert_group_to_skill, task_args)

    print("\n" + "=" * 60)
    print("转换完成!")
    print("=" * 60)

    success_count = sum(1 for r in results if r["success"])
    print(f"\n成功: {success_count}/{len(results)} 个分组，共 {total_docs} 篇源文档")

    failed_results = [r for r in results if not r["success"]]
    if failed_results:
        print("\n失败分组明细:")
        for r in failed_results:
            print(f"  - {r['group']} ({len(r['source_paths'])} 个文档): {r['content']}")

    # 生成references风格的skill树结构（可直接挂到总SKILL.md的# references下），
    # 引用skill时用 [相对路径]，agent直接Read该路径。
    listing_lines = ["# references", f"- {OUTPUT_DIR}"]
    seen_cats = set()
    for key in all_keys:
        if len(key) == 2:
            if key[0] not in seen_cats:
                listing_lines.append(f"  - {key[0]}")
                seen_cats.add(key[0])
            listing_lines.append(f"    - {key[1]}.md")
        elif len(key) == 1:
            listing_lines.append(f"  - {key[0]}.md")
        else:
            listing_lines.append("  - root.md")
    skill_tree_text = "\n".join(listing_lines)

    tree_output_path = os.path.join(OUTPUT_DIR, "skill_tree_structure.txt")
    with open(tree_output_path, 'w', encoding='utf-8') as f:
        f.write(f"Skill树结构图\n")
        f.write(f"输出目录: {OUTPUT_DIR}\n")
        f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("引用格式: [一级目录/二级目录.md]，agent直接Read该相对路径\n")
        f.write("=" * 60 + "\n\n")
        f.write(skill_tree_text)

    print(f"\n树结构图已保存到: {tree_output_path}")
    print("\n" + skill_tree_text)

    # 报告增量合并：部分重跑(GROUPS)时，本次跑过的分组按skill_path覆盖已有
    # 报告中的对应条目，未跑的分组保留上次记录；同时清理源树中已不存在的
    # 过期条目（如分组规则变化后遗留的旧路径），报告始终反映目录的最新全貌。
    json_report_path = os.path.join(OUTPUT_DIR, "conversion_report.json")
    expected_paths = {group_key_to_rel_path(key) for key in all_keys}
    merged_results = {}
    if os.path.isfile(json_report_path):
        try:
            with open(json_report_path, 'r', encoding='utf-8') as f:
                old_report = json.load(f)
            merged_results = {
                r["skill_path"]: r
                for r in old_report.get("results", [])
                if r.get("skill_path") in expected_paths
            }
        except (json.JSONDecodeError, OSError) as e:
            print(f"[WARN] 已有转换报告无法解析，将重新生成: {e}")
    for r in results:
        merged_results[r["skill_path"]] = r
    all_results = [merged_results[path] for path in sorted(merged_results)]

    with open(json_report_path, 'w', encoding='utf-8') as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "source_dir": SOURCE_TREE_DIR,
            "output_dir": OUTPUT_DIR,
            "api_url": API_URL,
            "model_name": MODEL_NAME,
            "groups_filter": GROUPS,
            "total_source_files": sum(len(r["source_paths"]) for r in all_results),
            "total_groups": len(all_results),
            "success_count": sum(1 for r in all_results if r["success"]),
            "workers": WORKERS,
            "results": all_results
        }, f, ensure_ascii=False, indent=2)
    print(f"\n转换报告已保存到: {json_report_path}"
          f"（本次 {len(results)} 个分组，合并后共 {len(all_results)} 个分组）")


if __name__ == "__main__":
    # main(
    #     SOURCE_TREE_DIR="result/v01/tree",
    #     OUTPUT_DIR="skills_distilled/v01",
    #     API_URL="http://141.73.1.167:7412/v1/chat/completions",
    #     MODEL_NAME="GLM-5.1-W4A8",
    #     WORKERS=4
    # )
    main(
        SOURCE_TREE_DIR="result/v01/tree",
        OUTPUT_DIR=f"skills_distilled/{datetime.now().strftime('%m-%d')}",
        API_URL="http://76.64.185.52:2207/v1/chat/completions",
        MODEL_NAME="qwen3.6-27b",
        WORKERS=3,
        # GROUPS=None 表示处理全部分组；只想跑部分分组时，
        # 传入按'一级目录/二级目录'子串匹配的名称列表，例如：
        # GROUPS=["IP组播/IP组播故障案例", "IP路由/BGP故障案例", "IP路由/IS-IS故障案例"]
        GROUPS=None
    )
