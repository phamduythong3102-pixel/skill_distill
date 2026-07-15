#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验蒸馏生成的skill文件是否完整合规。

检查项：
  [ERROR] 缺少frontmatter、缺少name/description、代码块围栏未闭合（疑似截断）、
          残留```markdown包裹、markdown链接指向不存在的本地文件
  [WARN]  name不符合命名规范（英文小写+连字符）、缺少一级标题、正文过短、
          残留输入分隔标记（## 文档：）、疑似截断的结尾、
          残留"联系技术支持/提交工程师"类步骤、残留原始文档路径引用

用法：
  python3 validate_skills.py            # 校验今天的输出目录 skills_distilled/mm-dd
  python3 validate_skills.py <目录>     # 校验指定目录
存在ERROR时退出码为1。
"""
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
MD_LINK_PATTERN = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")

FORBIDDEN_PHRASES = [
    "联系技术支持",
    "联系华为工程师",
    "寻求技术支持",
    "提交给工程师",
    "报送工程师",
    "收集以下信息并联系",
]

# 合法markdown可能以 * (粗体/列表)、- (分隔线)、| (表格行) 结尾，
# 只把不可能正常收尾的标点视为截断特征。
TRUNCATION_TAIL_CHARS = ("，", "、", "：", ":", ",", "；", ";")


def check_skill_file(file_path: str, expected_name: str = None, known_paths: set = None) -> list:
    """返回 [(级别, 描述)] 列表，级别为 ERROR / WARN。

    expected_name: 该skill的指定name（即所在目录名，有映射时校验一致性）。
    known_paths: 全部已分配skill路径的集合（有映射时校验[路径]引用可解析）。
    """
    issues = []
    with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    stripped = content.strip()
    if not stripped:
        return [("ERROR", "文件为空")]

    # 1. 残留```markdown包裹（提取失败的痕迹）
    if stripped.startswith("```"):
        issues.append(("ERROR", "文件以```开头，残留了代码围栏包裹"))

    # 2. frontmatter
    fm_match = FRONTMATTER_PATTERN.match(content)
    if not fm_match:
        issues.append(("ERROR", "缺少frontmatter（--- name/description ---）"))
        body = content
    else:
        fm = fm_match.group(1)
        body = content[fm_match.end():]

        name_match = re.search(r"^name\s*:\s*(.+)$", fm, re.MULTILINE)
        if not name_match:
            issues.append(("ERROR", "frontmatter缺少name字段"))
        else:
            actual_name = name_match.group(1).strip()
            if not NAME_PATTERN.match(actual_name):
                issues.append(("WARN", f"name不符合命名规范（应为英文小写+连字符）: {actual_name!r}"))
            if expected_name and actual_name != expected_name:
                issues.append(("ERROR", f"name与skill_names.json指定不一致: 实际{actual_name!r}, 应为{expected_name!r}"))

        if not re.search(r"^description\s*:\s*\S+", fm, re.MULTILINE):
            issues.append(("ERROR", "frontmatter缺少description字段"))

    # 2.5 [路径]引用的可解析性：形如 [ip-routing/bgp-troubleshooting] 的引用
    #     必须是已分配的skill路径（排除markdown链接 [text](url) 的方括号）
    if known_paths:
        for ref in re.findall(r"\[([a-z0-9][a-z0-9/-]*)\](?!\()", body):
            if ref not in known_paths:
                issues.append(("ERROR", f"引用了不存在的skill路径: [{ref}]"))

    # 3. 一级标题
    if not re.search(r"^# \S", body, re.MULTILINE):
        issues.append(("WARN", "正文缺少一级标题（# xxx）"))

    # 4. 代码块围栏闭合（奇数个```为未闭合，典型的截断特征）
    fence_count = len(re.findall(r"^\s*```", content, re.MULTILINE))
    if fence_count % 2 != 0:
        issues.append(("ERROR", f"代码块围栏未闭合（共{fence_count}个```），疑似输出被截断"))

    # 5. 疑似截断的结尾
    last_char = stripped[-1]
    if last_char in TRUNCATION_TAIL_CHARS:
        issues.append(("WARN", f"结尾字符为{last_char!r}，疑似输出在句中被截断"))

    # 6. 正文过短
    if len(stripped) < 300:
        issues.append(("WARN", f"内容过短（{len(stripped)}字符），可能生成不完整"))

    # 7. 残留输入分隔标记
    if re.search(r"^#+\s*文档：", body, re.MULTILINE):
        issues.append(("WARN", "残留输入分隔标记'## 文档：'，模型可能照搬了输入结构"))

    # 8. 残留"联系技术支持"类步骤
    for phrase in FORBIDDEN_PHRASES:
        if phrase in body:
            line_no = content[:content.find(phrase)].count("\n") + 1
            issues.append(("WARN", f"第{line_no}行残留'{phrase}'，此类步骤应已删除"))

    # 9. markdown链接检查：skill中不应残留任何本地文档链接（应改写为文字指引），
    #    指向不存在文件的链接（含.html等原始文档遗留引用）为ERROR。
    reported_targets = set()
    for target in MD_LINK_PATTERN.findall(body):
        if target.startswith(("http://", "https://", "#")):
            continue
        target_path = target.split("#")[0]
        if not target_path:
            continue
        reported_targets.add(target_path)
        resolved = (Path(file_path).parent / target_path).resolve()
        if not resolved.exists():
            issues.append(("ERROR", f"链接指向不存在的文件: {target}"))
        else:
            issues.append(("WARN", f"残留本地文档链接（应改写为文字指引）: {target}"))

    # 10. 正文中残留的原始文档文件名（如 dc_vrp_xxx.html），即使不是链接形式
    for match in re.finditer(r"[\w./-]+\.html?\b", body):
        if match.group(0) not in reported_targets:
            issues.append(("WARN", f"残留原始文档文件名: {match.group(0)}"))

    return issues


def main(skill_dir: str):
    print("=" * 60)
    print("Skill自验校验")
    print("=" * 60)
    print(f"校验目录: {skill_dir}\n")

    if not os.path.isdir(skill_dir):
        print(f"错误: 目录不存在: {skill_dir}")
        sys.exit(1)

    md_files = []
    for root, dirs, files in os.walk(skill_dir):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for file in files:
            if file.endswith('.md'):
                md_files.append(os.path.join(root, file))
    md_files.sort()

    if not md_files:
        print("错误: 目录下没有找到.md文件")
        sys.exit(1)

    # 加载流水线生成的name映射，用于校验name一致性和[路径]引用可解析性
    name_mapping = {}
    names_path = os.path.join(skill_dir, "skill_names.json")
    if os.path.exists(names_path):
        with open(names_path, 'r', encoding='utf-8') as f:
            name_mapping = json.load(f)
        print(f"已加载name映射: {names_path} ({len(name_mapping.get('skills', {}))} 个skill)\n")
    known_paths = set(name_mapping.get("paths", {}).values()) or None

    error_count = 0
    warn_count = 0
    clean_count = 0

    for file_path in md_files:
        rel = os.path.relpath(file_path, skill_dir)
        # skill文件为 <skill路径>/SKILL.md，指定name即其所在目录名
        expected_name = None
        if os.path.basename(file_path) == "SKILL.md" and known_paths:
            skill_path = os.path.dirname(rel).replace(os.sep, "/")
            if skill_path in known_paths:
                expected_name = skill_path.rsplit("/", 1)[-1]
            else:
                print(f"● {rel}")
                print(f"    [WARN] 该skill路径不在skill_names.json映射中: {skill_path}\n")
                warn_count += 1
        issues = check_skill_file(file_path, expected_name=expected_name, known_paths=known_paths)
        if not issues:
            clean_count += 1
            continue
        print(f"● {rel}")
        for level, desc in issues:
            print(f"    [{level}] {desc}")
            if level == "ERROR":
                error_count += 1
            else:
                warn_count += 1
        print()

    print("=" * 60)
    print(f"共校验 {len(md_files)} 个skill: "
          f"{clean_count} 个通过, {error_count} 个ERROR, {warn_count} 个WARN")
    if error_count:
        print("存在ERROR，建议重新生成对应分组（可用GROUPS参数只跑失败的分组）")
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        skill_dir = sys.argv[1]
    else:
        skill_dir = f"skills_distilled/{datetime.now().strftime('%m-%d')}"
    main(skill_dir)
