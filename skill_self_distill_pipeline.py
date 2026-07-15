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
- 引用其它文档时的处理规则（不要保留原始文档的路径或链接，它们在转换后已失效）：
  1. 如果被引用的文档就在本次**输入文档集合**中（已合并进本skill），改写为本skill内部小节的引用，如"参考场景A继续排查"。
  2. 如果被引用的内容属于**skill目录清单**中的其它分类，改写为文字指引，如：参考skill《对应的skill名》。
  3. 如果被引用的文档不在清单中（如外部产品手册、命令参考），保留为纯文字说明（写明文档名称即可），不要输出链接。
- 遇到源文档有大段回显时，不要照搬浪费token，在步骤中讲清要注意哪些回显内容即可。

# 输入文档集合
<input_docs>

# skill目录清单
本批次会将各分类目录分别生成为独立的skill，清单如下（格式：一级分类/skill名）。跨分类引用时请使用此清单中的skill名：
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
        if content.endswith("```"):
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


def group_files_by_second_level(md_files: list, source_tree_dir: str) -> dict:
    """按源目录树的二级子目录对文档分组：<source_tree_dir>/一级目录/二级目录/*.md 归为同一组。

    - 若文档位于二级目录及更深层级，按 (一级目录, 二级目录) 分组（更深层级一并并入该组）。
    - 若文档仅位于一级目录下（没有二级目录），按 (一级目录,) 单独分组。
    - 若文档直接位于 source_tree_dir 根目录下，归入 () 空分组。
    分组结果按 key 排序，组内文件保持原有排序。
    """
    groups = {}
    for doc_path in md_files:
        rel_path = os.path.relpath(doc_path, source_tree_dir)
        parts = Path(rel_path).parts
        depth = len(parts) - 1
        if depth >= 2:
            key = (parts[0], parts[1])
        elif depth == 1:
            key = (parts[0],)
        else:
            key = ()
        groups.setdefault(key, []).append(doc_path)
    return dict(sorted(groups.items()))


def build_merged_doc_content(file_paths: list) -> str:
    doc_blocks = []
    for doc_path in file_paths:
        title = Path(doc_path).stem
        with open(doc_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read().strip()
        doc_blocks.append(f"## 文档：{title}\n\n{content}")
    return "\n\n---\n\n".join(doc_blocks)


def convert_group_to_skill(args: tuple) -> dict:
    group_key, file_paths, output_dir, api_url, model_name, prompt_template, skill_catalog = args

    if len(group_key) == 2:
        level1, level2 = group_key
        output_subdir_full = os.path.join(output_dir, level1)
        output_filename = f"{level2}.md"
    elif len(group_key) == 1:
        (level1,) = group_key
        output_subdir_full = output_dir
        output_filename = f"{level1}.md"
    else:
        output_subdir_full = output_dir
        output_filename = "root.md"

    os.makedirs(output_subdir_full, exist_ok=True)
    output_path = os.path.join(output_subdir_full, output_filename)

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
            "source_paths": file_paths,
            "output_path": None,
            "content": f"错误：{str(e)}",
            "success": False
        }


def build_file_tree(path: str, max_depth: int = 10) -> list:
    tree = []
    try:
        if not os.path.exists(path):
            return tree

        items = os.listdir(path)
        items = [
            item
            for item in items
            if not item.startswith('.') and item not in ['node_modules', '__pycache__', '.git']
        ]

        base_path = Path(path)
        items_with_type = [(item, (base_path / item).is_dir()) for item in items]
        items_with_type.sort(key=lambda x: (not x[1], x[0].lower()))

        for name, is_dir in items_with_type:
            full_path = os.path.join(path, name)
            node = {"name": name, "path": full_path, "is_dir": is_dir}
            if is_dir and max_depth > 0:
                node["children"] = build_file_tree(full_path, max_depth - 1)
            elif is_dir:
                node["children"] = []
            tree.append(node)
    except Exception as e:
        print(f"读取目录 {path} 失败: {e}")
    return tree


def tree_to_ascii(tree: list, prefix: str = "", is_last: bool = True) -> str:
    lines = []
    for i, node in enumerate(tree):
        is_last_node = (i == len(tree) - 1)
        connector = "└── " if is_last_node else "├── "
        extension = "│   " if not is_last_node else "    "

        if node["is_dir"]:
            lines.append(f"{prefix}{connector}{node['name']}/")
            lines.append(tree_to_ascii(node.get("children", []), prefix + extension, is_last_node))
        else:
            lines.append(f"{prefix}{connector}{node['name']}")
    return "\n".join(lines)


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

    # 用全量分组构建skill目录清单（在GROUPS过滤之前），保证部分重跑时
    # 跨分类引用的skill名依然完整。
    skill_catalog = "\n".join(
        f"- {'/'.join(key) if key else '(root)'}" for key in groups
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

    os.makedirs(OUTPUT_DIR, exist_ok=True)

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

    tree = build_file_tree(OUTPUT_DIR)
    ascii_tree = tree_to_ascii(tree)

    tree_output_path = os.path.join(OUTPUT_DIR, "..", "skill_tree_structure.txt")
    with open(tree_output_path, 'w', encoding='utf-8') as f:
        f.write(f"Skill树结构图\n")
        f.write(f"输出目录: {OUTPUT_DIR}\n")
        f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 60 + "\n\n")
        f.write(ascii_tree)

    print(f"\n树结构图已保存到: {tree_output_path}")
    print("\n" + ascii_tree)

    json_report_path = os.path.join(OUTPUT_DIR, "..", "conversion_report.json")
    with open(json_report_path, 'w', encoding='utf-8') as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "source_dir": SOURCE_TREE_DIR,
            "output_dir": OUTPUT_DIR,
            "api_url": API_URL,
            "model_name": MODEL_NAME,
            "groups_filter": GROUPS,
            "total_source_files": total_docs,
            "total_groups": len(results),
            "success_count": success_count,
            "workers": WORKERS,
            "results": results
        }, f, ensure_ascii=False, indent=2)
    print(f"\n转换报告已保存到: {json_report_path}")


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
