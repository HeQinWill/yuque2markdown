# coding=utf-8
import json
import os
import random
import shutil
import sys
import argparse
import tarfile
from markdownify import markdownify as md
from bs4 import BeautifulSoup
from requests import get

import yaml
import tempfile
import re


TYPE_TITLE = "TITLE"
TYPE_DOC = "DOC"
META_JSON = "$meta.json"
TMP_DIR = tempfile.gettempdir()

DEFAULT_HEADING_STYLE = "ATX"

content_type_to_extension = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/svg+xml": ".svg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def sanitizer_file_name(name):
    name = name.replace("/", "_")
    name = name.replace("\\", "_")
    name = name.replace(" ", "_")
    name = name.replace("?", "_")
    name = name.replace("*", "_")
    name = name.replace("<", "_")
    name = name.replace(">", "_")
    name = name.replace("|", "_")
    name = name.replace('"', "_")
    name = name.replace(":", "_")
    return name


def read_toc(random_tmp_dir):
    # open meta json
    f = open(os.path.join(random_tmp_dir, META_JSON), "r", encoding="utf-8")
    meta_file_str = json.loads(f.read())
    meta_str = meta_file_str.get("meta", "")
    meta = json.loads(meta_str)
    toc_str = meta.get("book", {}).get("tocYml", "")
    toc = yaml.unsafe_load(toc_str)
    f.close()
    return toc


def extract_repos(repo_dir, output, toc, download_image):
    last_level = 0
    last_sanitized_title = ""
    path_prefixed = []
    for item in toc:
        t = item["type"]
        url = str(item.get("url", ""))
        current_level = item.get("level", 0)
        title = str(item.get("title", ""))
        sanitized_title = sanitizer_file_name(str(title))
        if not title:
            continue
        while True:
            if os.path.exists(os.path.join(output, sanitized_title)):
                sanitized_title = sanitizer_file_name(str(title)) + str(
                    random.randint(0, 1000)
                )
            break

        if current_level > last_level:
            path_prefixed = path_prefixed + [last_sanitized_title]
        elif current_level < last_level:
            diff = last_level - current_level
            path_prefixed = path_prefixed[0:-diff]

        # else:
        if t == TYPE_DOC:
            output_dir_path = os.path.join(output, *path_prefixed)
            if not os.path.exists(output_dir_path):
                os.makedirs(output_dir_path)
            raw_path = os.path.join(repo_dir, url + ".json")
            raw_file = open(raw_path, "r", encoding="utf-8")
            doc_str = json.loads(raw_file.read())
            html = doc_str["doc"]["body"] or doc_str["doc"]["body_asl"]
            draft = doc_str["doc"]["body_draft"] 
            if html != draft:   
                print("请检查草稿内容是否发布:"+sanitized_title)
            if html.startswith('{"format":"lakesheet"'):
                print("请手动处理Lakesheet表格:"+sanitized_title, url, sep='\n')
                continue

            if download_image:
                html = download_images_and_patch_html(
                    output_dir_path, sanitized_title, html
                )
            
            html = handle_highlight(html)
            html = convert_alerts_to_callout(html)
            html = handle_checkbox(html)
            
            output_path = os.path.join(output_dir_path, sanitized_title + ".md")
            f = open(output_path, "w", encoding="utf-8")
            md_out = md(html, heading_style=DEFAULT_HEADING_STYLE,
                     code_block_style='fenced', 
                     code_language_callback=code_lang_cb)
            f.write(pretty_md(md_out))

        last_sanitized_title = sanitized_title
        last_level = current_level

def code_lang_cb(el):
    """
    直接使用markdownify的code_language_callback来动态识别语言
    el 是一个 BeautifulSoup 的 <pre> 元素（注意：回调收到的是 <pre>，不是 <code>）
    返回语言字符串或 None
    优先级 data-language / data-lang > class中的 language-xxx|lang-xxx
    """
    # 1) data-language / data-lang
    for k in ('data-language', 'data-lang'):
        if el.has_attr(k) and el.get(k):
            return el.get(k).strip()

    # 2) class 里提取 language-xxx|lang-xxx
    classes = ' '.join(el.get('class', [])).strip()
    m = re.search(r'(?:language|lang)-([A-Za-z0-9_+-]+)', classes)
    if m:
        return m.group(1)

    # 3) 万一有些页面把语言挂在内层 <code> 上，尝试抓一下
    code = el.find('code')
    if code:
        classes = ' '.join(code.get('class', [])).strip()
        m = re.search(r'(?:language|lang)-([A-Za-z0-9_+-]+)', classes)
        if m:
            return m.group(1)

    return None  # 返回 None 则不加语言标签



def handle_checkbox(html_content: str) -> str:
    """
    将 <input type="checkbox"> 转换为 Markdown 任务列表
    - [x] 代表已选中，- [ ] 代表未选中
    """
    bs = BeautifulSoup(html_content, "html.parser")

    # 查找所有的 <input type="checkbox">
    for input_tag in bs.find_all("input", {"type": "checkbox"}):
        # 查找其父元素，通常是 <li> 标签
        parent_li = input_tag.find_parent("li")

        # 检查是否选中
        if input_tag.get("checked") is not None:
            checkbox_mark = "- [x]"
        else:
            checkbox_mark = "- [ ]"

        # 获取复选框后面的文本
        text = ""
        span_tag = parent_li.find("span", class_="ne-text")
        if span_tag:
            text = "".join(span_tag.stripped_strings)

        # 将复选框和文本转换为 Markdown 任务列表
        if text:
            parent_li.insert_before(f"{checkbox_mark} {text}\n")

        # 删除原本的 <input> 和 <span> 标签
        input_tag.decompose()
        if span_tag:
            span_tag.decompose()

    # 返回处理后的 HTML 内容
    return str(bs)



def convert_alerts_to_callout(html_content: str) -> str:
    """
    将 ne-alert 转成 Obsidian Callout，但保留块级 HTML 结构，
    确保 markdownify 正确转换成 Markdown 引用块，避免粘连。
    """
    CALLOUT_MAP = {
        'info': 'INFO',
        'tips': 'TIP',
        'success': 'SUCCESS',
        'warning': 'WARNING',
        'danger': 'DANGER',
        'color1': 'NOTE',
        'color2': 'SUCCESS',
        'color3': 'WARNING',
        'color4': 'DANGER',
        'color5': 'ABSTRACT',
    }

    bs = BeautifulSoup(html_content, "html.parser")

    for div in bs.find_all("div", class_="ne-alert"):
        data_type = div.get("data-type")
        callout_type = CALLOUT_MAP.get(data_type, "NOTE")

        # 提取文本
        lines = []
        for p in div.find_all("p", recursive=False):
            text = "".join(p.stripped_strings)
            if text:
                lines.append(text)

        # 包装成 <blockquote> 结构，markdownify 会自动转为 `>`
        block = bs.new_tag("blockquote")
        block.append(bs.new_string(f"[!{callout_type}]"))
        block.append(bs.new_tag("br"))
        for line in lines:
            block.append(bs.new_string(line))
            block.append(bs.new_tag("br"))

        # 确保与周围内容分隔
        div.insert_before("\n")
        div.replace_with(block)
        block.insert_after("\n")

    return str(bs)


def handle_highlight(html):
    bs = BeautifulSoup(html, "html.parser")
    for span in bs.find_all("span", style=lambda value: 'background-color: #' in value if value else False):
        span.insert_before("==")
        span.insert_after("==")
        span.unwrap()
    return str(bs)

def download_images_and_patch_html(output_dir_path, sanitized_title, html):
    bs = BeautifulSoup(html, "html.parser")
    if len(bs.find_all("img")) > 0:
        attachments_dir_path = os.path.join(output_dir_path, "attachments")
        if not os.path.exists(attachments_dir_path):
            os.mkdir(attachments_dir_path)
        no = 1
        for image in bs.find_all("img"):
            print("Download %s" % image["src"])
            resp = get(image["src"])
            file_name = sanitized_title + "_%03d%s" % (
                no,
                content_type_to_extension.get(resp.headers["Content-Type"], ""),
            )
            attachments_file_path = os.path.join(attachments_dir_path, file_name)
            with open(attachments_file_path, "wb") as f:
                f.write(resp.content)
            no = no + 1
            image["src"] = "./attachments/" + file_name
        html = str(bs)
        return html
    else:
        return html


def pretty_md(text: str) -> str:
    output = text

    lines = output.split("\n")
    for i in range(len(lines)):
        lines[i] = lines[i].rstrip()
    output = "\n".join(lines)

    for i in range(50):
        output = output.replace("\n\n\n", "\n\n")
        if "\n\n\n" not in output:
            break

    return output


def main():
    parser = argparse.ArgumentParser(description="Convert Yuque doc to markdown")
    parser.add_argument("lakebook", help="Lakebook file")
    parser.add_argument("output", help="Output directory")
    parser.add_argument(
        "--download-image", help="Download images to local", action="store_true"
    )
    args = parser.parse_args()
    if not os.path.exists(args.lakebook):
        print("Lakebook file not found: " + args.lakebook)
        sys.exit(1)
    if not os.path.exists(args.output):
        os.mkdir(args.output)

    # extract lakebook file
    random_tmp_dir = os.path.join(TMP_DIR, "lakebook_" + str(os.getpid()))
    extract_tar(args.lakebook, random_tmp_dir)
    # detect only one directory in random_tmp_dir
    repo_dir = ""
    for root, dirs, files in os.walk(random_tmp_dir):
        for d in dirs:
            repo_dir = os.path.join(random_tmp_dir, d)
            break
    if not repo_dir:
        print(".lakebook file is invalid")
        sys.exit(1)

    toc = read_toc(repo_dir)
    # print len of toc
    print("Total " + str(len(toc)) + " files")

    extract_repos(repo_dir, args.output, toc, args.download_image)

    # remove tmp dir
    shutil.rmtree(random_tmp_dir)


# extract tar file in tar.gz
def extract_tar(tar_file, target_dir):
    if not os.path.exists(target_dir):
        os.mkdir(target_dir)
    tar = tarfile.open(tar_file)
    names = tar.getnames()
    for name in names:
        tar.extract(name, target_dir)
    tar.close()

#%%
if __name__ == "__main__":
    main()
