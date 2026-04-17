#!/usr/bin/env python3
"""
Extract all patch content and create a consolidated file
"""

import zipfile
import xml.etree.ElementTree as ET
import re
import os

def extract_odt_content(odt_file):
    """Extract text from .odt file using multiple methods"""
    try:
        # Method 1: Standard XML parsing
        with zipfile.ZipFile(odt_file, 'r') as zip_file:
            if 'content.xml' in zip_file.namelist():
                content_xml = zip_file.read('content.xml')
                
                # Try to parse as XML
                try:
                    root = ET.fromstring(content_xml)
                    
                    # Extract text using namespace-aware parsing
                    namespaces = {
                        'office': 'urn:oasis:names:tc:opendocument:xmlns:office:1.0',
                        'text': 'urn:oasis:names:tc:opendocument:xmlns:text:1.0',
                        'style': 'urn:oasis:names:tc:opendocument:xmlns:style:1.0'
                    }
                    
                    text_parts = []
                    for paragraph in root.findall('.//text:p', namespaces):
                        para_text = ''
                        for elem in paragraph.iter():
                            if elem.text and elem.text.strip():
                                para_text += elem.text.strip() + ' '
                        if para_text.strip():
                            text_parts.append(para_text.strip())
                    
                    if text_parts:
                        return '\n'.join(text_parts)
                
                except ET.ParseError:
                    # Method 2: Regex extraction if XML parsing fails
                    content_str = content_xml.decode('utf-8', errors='ignore')
                    text_matches = re.findall(r'>([^<]+)<', content_str)
                    clean_texts = []
                    for match in text_matches:
                        clean = match.strip()
                        if clean and len(clean) > 1:
                            clean_texts.append(clean)
                    return '\n'.join(clean_texts)
            
            return "No content.xml found"
            
    except Exception as e:
        return f"Error: {str(e)}"

def main():
    """Extract all patches and create consolidated file"""
    
    # All patch files including the text one we already have
    patch_files = [
        ('Next-Patch-07.txt', None),  # Already text, no extraction needed
        ('Next-patch.odt', 'extract_odt'),
        ('Next-Parch-02.odt', 'extract_odt'),
        ('Next-Parch-03.odt', 'extract_odt'),
        ('Next-Parch-04.odt', 'extract_odt'),
        ('Next-Parch-05.odt', 'extract_odt'),
        ('Next-Parch-06.odt', 'extract_odt'),
    ]
    
    all_patches = []
    
    for filename, extract_method in patch_files:
        print(f"Processing: {filename}")
        
        if extract_method == 'extract_odt':
            content = extract_odt_content(filename)
        else:
            # Read text file directly
            try:
                with open(filename, 'r', encoding='utf-8') as f:
                    content = f.read()
            except Exception as e:
                content = f"Error reading {filename}: {str(e)}"
        
        all_patches.append(f"\n{'='*80}\n")
        all_patches.append(f"PATCH FILE: {filename}")
        all_patches.append(f"{'='*80}\n")
        all_patches.append(content)
        all_patches.append("\n")
    
    # Write consolidated patches
    with open('all_patches_consolidated.txt', 'w', encoding='utf-8') as f:
        f.write('\n'.join(all_patches))
    
    print("All patches extracted to: all_patches_consolidated.txt")

if __name__ == "__main__":
    main()
