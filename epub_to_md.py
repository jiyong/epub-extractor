#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
EPUB内容提取器
此脚本用于提取EPUB电子书中的内容并转换为Markdown格式
"""

import sys
import os
import zipfile
import xml.etree.ElementTree as ET
import html2text
from bs4 import BeautifulSoup
import argparse
import re
import shutil
import uuid
import tempfile
from pathlib import Path
import requests
from io import BytesIO
import logging
import asyncio
from oss_uploader import OSSUploader
from task_manager import task_manager, TaskStatus

def get_product_id(filename):
    """
    从文件名中提取产品编号
    
    Args:
        filename: 文件名
        
    Returns:
        产品编号
    """
    match = re.match(r'(\d{6}-\d{2})', filename)
    if match:
        return match.group(1)
    return None

def get_first_line_content(markdown_path):
    """
    获取Markdown文件第一行的无格式内容
    
    Args:
        markdown_path: Markdown文件路径
        
    Returns:
        第一行的无格式内容
    """
    try:
        with open(markdown_path, 'r', encoding='utf-8') as f:
            first_line = f.readline().strip()
            
        # 移除Markdown标记
        content = re.sub(r'^#+\s*', '', first_line)  # 移除标题标记
        content = re.sub(r'[`*_]', '', content)      # 移除其他格式标记
        
        return content if content else None
        
    except Exception as e:
        print(f"读取Markdown文件时出错: {str(e)}")
        return None

async def upload_to_oss(product_code: str, data_dir: str) -> bool:
    """
    异步上传文件到OSS
    
    Args:
        product_code: 产品代码
        data_dir: 数据目录
        
    Returns:
        bool: 上传是否成功
    """
    try:
        # 更新任务状态为进行中
        await task_manager.update_task_status(product_code, "epub-to-md", TaskStatus.DOING, "正在上传文件到OSS")
        
        uploader = OSSUploader()
        product_dir = os.path.join(data_dir, product_code)
        
        if not os.path.exists(product_dir):
            logging.error(f"产品目录不存在: {product_dir}")
            await task_manager.update_task_status(product_code, "epub-to-md", TaskStatus.FAIL, "产品目录不存在")
            return False
            
        # 获取文件锁
        if not await task_manager.acquire_file_lock(product_code):
            logging.info(f"等待其他任务完成: {product_code}")
            # 等待所有相关任务完成
            if not await task_manager.wait_for_tasks_completion(product_code, ["epub-to-md", "md-to-json-structure"]):
                await task_manager.update_task_status(product_code, "epub-to-md", TaskStatus.FAIL, "等待任务超时")
                return False
                
        try:
            # 上传整个产品目录
            success = uploader.upload_directory(product_dir)
            
            if success:
                # 上传成功后删除本地文件
                uploader.delete_local_files(product_dir)
                await task_manager.update_task_status(product_code, "epub-to-md", TaskStatus.SUCCESS, "文件上传成功")
            else:
                await task_manager.update_task_status(product_code, "epub-to-md", TaskStatus.FAIL, "文件上传失败")
                
            return success
            
        finally:
            # 释放文件锁
            await task_manager.release_file_lock(product_code)
            
    except Exception as e:
        logging.error(f"上传到OSS失败: {str(e)}")
        await task_manager.update_task_status(product_code, "epub-to-md", TaskStatus.FAIL, f"上传失败: {str(e)}")
        return False

def extract_content_from_epub(epub_path, product_code, md_img_dir=None, save=False):
    """
    从EPUB文件中提取内容并转换为Markdown格式
    
    Args:
        epub_path: EPUB文件的路径
        product_code: 产品编号
        md_img_dir: Markdown文件中图片引用的基础路径
        save: 是否保存文件
    
    Returns:
        提取的Markdown文本内容
    """
    # 转换为Path对象
    epub_path = Path(epub_path)
    
    # 设置输出路径和图片目录
    if save:
        output_path = Path(f"./data/{product_code}/epub/{product_code}.epub.md")
        image_dir = Path(f"./data/{product_code}/images/")
    else:
        # 如果不保存，则使用临时目录
        temp_dir = Path(tempfile.mkdtemp())
        output_path = temp_dir / f"{product_code}.epub.md"
        image_dir = temp_dir / "images"
    
    # 确保输出目录存在
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image_dir.mkdir(parents=True, exist_ok=True)
    
    # 如果没有指定md_img_dir，则使用默认值
    if not md_img_dir:
        md_img_dir = f"/books/{product_code}/images"
    
    print(f"extract_content_from_epub 输入参数:")
    print(f"  epub_path: {epub_path}")
    print(f"  output_path: {output_path}")
    print(f"  image_dir: {image_dir}")
    print(f"  md_img_dir: {md_img_dir}")
    print(f"  product_code: {product_code}")
    
    if not epub_path.exists():
        print(f"错误: 文件 '{epub_path}' 不存在")
        return None
    
    # 创建html2text转换器实例
    h2t = html2text.HTML2Text()
    h2t.ignore_links = False
    h2t.ignore_images = False
    h2t.escape_snob = False
    h2t.ignore_tables = False
    h2t.body_width = 0  # 不自动断行
    h2t.unicode_snob = True  # 使用Unicode
    h2t.mark_code = True
    h2t.wrap_links = False
    h2t.wrap_lists = False
    h2t.single_line_break = True  # 单个换行符不被忽略
    
    try:
        # 打开EPUB文件(实际是ZIP文件)
        with zipfile.ZipFile(epub_path, 'r') as epub:
            # 首先查找OPF文件位置
            container = epub.read('META-INF/container.xml')
            container_root = ET.fromstring(container)
            opf_path = container_root.find('.//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile').get('full-path')
            
            # 读取OPF文件，获取内容文件列表
            opf_content = epub.read(opf_path)
            opf_root = ET.fromstring(opf_content)
            
            # 获取基础路径
            opf_dir = os.path.dirname(opf_path)
            if opf_dir and not opf_dir.endswith('/'):
                opf_dir += '/'
            
            # 提取内容文件列表
            manifest = opf_root.find('.//{http://www.idpf.org/2007/opf}manifest')
            spine = opf_root.find('.//{http://www.idpf.org/2007/opf}spine')
            
            # 获取标题和作者信息
            metadata = opf_root.find('.//{http://www.idpf.org/2007/opf}metadata')
            title = ""
            author = ""
            if metadata is not None:
                title_elem = metadata.find('.//{http://purl.org/dc/elements/1.1/}title')
                if title_elem is not None and title_elem.text:
                    title = title_elem.text
                
                creator_elem = metadata.find('.//{http://purl.org/dc/elements/1.1/}creator')
                if creator_elem is not None and creator_elem.text:
                    author = creator_elem.text
            
            # 获取itemrefs的顺序
            itemrefs = []
            if spine is not None:
                itemrefs = [item.get('idref') for item in spine.findall('.//{http://www.idpf.org/2007/opf}itemref')]
            
            # 收集所有内容项目
            content_items = {}
            image_items = {}
            
            for item in manifest.findall('.//{http://www.idpf.org/2007/opf}item'):
                item_id = item.get('id')
                href = item.get('href')
                media_type = item.get('media-type')
                
                if media_type in ['application/xhtml+xml', 'text/html']:
                    content_items[item_id] = href
                elif media_type.startswith('image/'):
                    image_items[item_id] = href
            
            # 保存所有图片，并创建图片ID到保存路径的映射
            image_map = {}
            
            for image_id, image_href in image_items.items():
                try:
                    # 构建完整的图片路径
                    image_path = os.path.join(opf_dir, image_href)
                    # 提取图片文件扩展名
                    _, ext = os.path.splitext(image_href)
                    # 生成唯一的图片文件名
                    new_image_name = f"{image_id}{ext}"
                    save_path = os.path.join(image_dir, new_image_name)
                    
                    # 读取并保存图片
                    with open(save_path, 'wb') as img_file:
                        img_file.write(epub.read(image_path))
                    
                    # 构建Markdown中引用的图片路径（使用md_img_dir）
                    md_image_path = f"{md_img_dir}/{new_image_name}"
                    
                    # 记录图片ID到保存路径的映射
                    image_map[os.path.basename(image_href)] = md_image_path
                    
                    # 如果href中包含了路径信息，也创建一个映射，因为HTML中的引用可能是相对路径
                    image_map[image_href] = md_image_path
                except Exception as e:
                    print(f"保存图片 {image_href} 时出错: {str(e)}")
            
            # 开始创建Markdown文件内容
            markdown_content = []
            
            # 添加书籍标题和作者信息
            if title:
                markdown_content.append(f"# {title}\n")
            if author:
                markdown_content.append(f"**作者：{author}**\n")
            
            # 按照spine中的顺序提取内容
            if itemrefs:
                for idref in itemrefs:
                    if idref in content_items:
                        file_path = content_items[idref]
                        convert_html_to_markdown(epub, opf_dir, file_path, markdown_content, image_map, h2t)
            else:
                # 如果没有spine，则直接按顺序提取所有HTML文件
                for _, file_path in content_items.items():
                    convert_html_to_markdown(epub, opf_dir, file_path, markdown_content, image_map, h2t)
            
            # 写入输出文件
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(markdown_content))
            
            print(f"内容已成功提取到Markdown文件: {output_path}")
            print(f"图片已保存到目录: {image_dir}")
            print(f"Markdown中的图片引用路径: {md_img_dir}")
            
            # 读取生成的Markdown内容
            with open(output_path, 'r', encoding='utf-8') as f:
                markdown_text = f.read()
            
            # 如果不保存，则删除临时文件
            if not save:
                shutil.rmtree(temp_dir)
            
            # 在保存文件后，启动异步上传任务
            if save:
                asyncio.create_task(upload_to_oss(product_code, os.path.join(os.getcwd(), "data")))
            
            return markdown_text
            
    except Exception as e:
        print(f"提取过程中出错: {str(e)}")
        return None

def convert_html_to_markdown(epub, opf_dir, file_path, markdown_content, image_map, h2t):
    """将HTML内容转换为Markdown格式"""
    try:
        full_path = os.path.join(opf_dir, file_path)
        file_content = epub.read(full_path)
        
        # 使用Beautiful Soup解析HTML
        soup = BeautifulSoup(file_content, 'html.parser')
        
        # 处理图片路径，将其替换为本地保存的图片路径
        for img in soup.find_all('img'):
            src = img.get('src')
            if src:
                # 尝试在映射中查找图片
                if src in image_map:
                    img['src'] = image_map[src]
                else:
                    # 尝试通过文件名匹配
                    img_name = os.path.basename(src)
                    if img_name in image_map:
                        img['src'] = image_map[img_name]
        
        # 优化标题处理
        for i, heading in enumerate(soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])):
            # 确保标题元素前有一个空行
            if i > 0:  # 跳过第一个标题，因为可能是章节标题
                new_tag = soup.new_tag('p')
                new_tag.string = ""
                heading.insert_before(new_tag)
        
        # 转换为Markdown
        html_content = str(soup)
        md_content = h2t.handle(html_content)
        
        # 后处理Markdown内容
        # 1. 修复可能的格式问题
        md_content = re.sub(r'\n{3,}', '\n\n', md_content)  # 删除多余空行
        
        # 2. 优化图片引用格式
        md_content = re.sub(r'!\[\]\(([^)]+)\)', r'![图片](\1)', md_content)
        
        # 3. 确保代码块格式正确
        md_content = re.sub(r'```\s+```', '', md_content)  # 删除空代码块
        
        markdown_content.append(md_content)
        
    except KeyError:
        print(f"无法找到文件: {full_path}")
    except Exception as e:
        print(f"处理文件 {file_path} 时出错: {str(e)}")

def process_epub_file(file_content, product_code, md_img_dir=None, save=False):
    """
    处理上传的EPUB文件内容
    
    Args:
        file_content: 上传的EPUB文件内容
        product_code: 产品编号
        md_img_dir: Markdown文件中图片引用的基础路径
        save: 是否保存文件
    
    Returns:
        提取的内容写入Markdown文件，图片保存到指定目录，并返回输出文件的路径
    """
    # 创建临时文件
    with tempfile.NamedTemporaryFile(delete=False, suffix='.epub') as temp_file:
        temp_file.write(file_content)
        temp_file_path = temp_file.name
    
    try:
        # 处理EPUB文件
        result = extract_content_from_epub(temp_file_path, product_code, md_img_dir, save)
        return result
    finally:
        # 删除临时文件
        os.unlink(temp_file_path)

def process_epub_url(url, product_code, md_img_dir=None, save=False):
    """
    处理网络上的EPUB文件
    
    Args:
        url: EPUB文件的URL
        product_code: 产品编号
        md_img_dir: Markdown文件中图片引用的基础路径
        save: 是否保存文件
    
    Returns:
        提取的内容写入Markdown文件，图片保存到指定目录，并返回输出文件的路径
    """
    try:
        # 下载EPUB文件
        response = requests.get(url, stream=True)
        response.raise_for_status()
        
        # 创建临时文件
        with tempfile.NamedTemporaryFile(delete=False, suffix='.epub') as temp_file:
            for chunk in response.iter_content(chunk_size=8192):
                temp_file.write(chunk)
            temp_file_path = temp_file.name
        
        try:
            # 处理EPUB文件
            result = extract_content_from_epub(temp_file_path, product_code, md_img_dir, save)
            return result
        finally:
            # 删除临时文件
            os.unlink(temp_file_path)
    except Exception as e:
        print(f"下载或处理URL时出错: {str(e)}")
        return None

def main():
    parser = argparse.ArgumentParser(description='提取EPUB文件中的内容并转换为Markdown格式')
    parser.add_argument('--src', help='输入的EPUB文件路径或URL')
    parser.add_argument('--file', help='上传的EPUB文件')
    parser.add_argument('--product_code', required=True, help='产品编号（例如：100227-01）')
    parser.add_argument('--md_img_dir', help='Markdown文件中图片引用的基础路径')
    parser.add_argument('--save', action='store_true', help='是否保存文件')
    
    args = parser.parse_args()
    
    print(f"处理参数:")
    print(f"  产品编号: {args.product_code}")
    print(f"  输入文件: {args.src}")
    print(f"  上传文件: {args.file}")
    print(f"  Markdown图片引用路径: {args.md_img_dir}")
    print(f"  保存文件: {args.save}")
    
    # 检查输入参数
    if not args.src and not args.file:
        print("错误: 必须提供--src或--file参数")
        sys.exit(1)
    
    # 处理EPUB文件
    if args.src:
        # 检查是否为URL
        if args.src.startswith('http://') or args.src.startswith('https://'):
            result = process_epub_url(args.src, args.product_code, args.md_img_dir, args.save)
        else:
            # 检查输入文件是否存在
            if not os.path.exists(args.src):
                print(f"错误: 输入文件不存在: {args.src}")
                sys.exit(1)
            
            # 处理本地文件
            result = extract_content_from_epub(args.src, args.product_code, args.md_img_dir, args.save)
    elif args.file:
        # 处理上传的文件
        with open(args.file, 'rb') as f:
            file_content = f.read()
        
        result = process_epub_file(file_content, args.product_code, args.md_img_dir, args.save)
    
    if result:
        print(f"成功处理文件")
        print("\n提取的Markdown内容:")
        print(result)
    else:
        print(f"处理文件失败")
        sys.exit(1)

if __name__ == "__main__":
    main() 