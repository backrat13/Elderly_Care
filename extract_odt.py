#!/usr/bin/env python3
"""
Extract text content from OpenDocument (.odt) files
"""

import zipfile
import xml.etree.ElementTree as ET
import re
import sys
import os

def extract_odt_text(odt_file):
    """Extract text content from an .odt file"""
    try:
        with zipfile.ZipFile(odt_file, 'r') as zf:
            # Read content.xml
            if 'content.xml' not in zf.namelist():
                return "No content.xml found in the archive"
            
            content_xml = zf.read('content.xml')
            
            # Parse XML
            root = ET.fromstring(content_xml)
            
            # Define namespaces for OpenDocument
            namespaces = {
                'office': 'urn:oasis:names:tc:opendocument:xmlns:office:1.0',
                'text': 'urn:oasis:names:tc:opendocument:xmlns:text:1.0',
                'style': 'urn:oasis:names:tc:opendocument:xmlns:style:1.0'
            }
            
            # Extract all text paragraphs
            text_parts = []
            
            # Find all text paragraphs
            for paragraph in root.findall('.//text:p', namespaces):
                # Get all text content in the paragraph
                text_content = ''
                
                # Extract text from the paragraph and its children
                for elem in paragraph.iter():
                    if elem.text and elem.text.strip():
                        text_content += elem.text.strip() + ' '
                
                if text_content.strip():
                    text_parts.append(text_content.strip())
            
            return '\n'.join(text_parts)
            
    except Exception as e:
        return f"Error extracting from {odt_file}: {str(e)}"

def main():
    """Extract content from all patch files"""
    patch_files = [
        'Next-patch.odt',
        'Next-Parch-02.odt', 
        'Next-Parch-03.odt',
        'Next-Parch-04.odt',
        'Next-Parch-05.odt',
        'Next-Parch-06.odt'
    ]
    
    for patch_file in patch_files:
        print(f"Checking file: {patch_file}")
        if os.path.exists(patch_file):
            print(f"File exists, size: {os.path.getsize(patch_file)} bytes")
            
            print(f"\n{'='*60}")
            print(f"Extracting from: {patch_file}")
            print(f"{'='*60}")
            
            try:
                content = extract_odt_text(patch_file)
                print(f"Extracted content length: {len(content)}")
                print(content)
            except Exception as e:
                print(f"Error during extraction: {e}")
            
            print(f"\n{'='*60}")
            print(f"End of {patch_file}")
            print(f"{'='*60}\n")
        else:
            print(f"File not found: {patch_file}")

if __name__ == "__main__":
    main()
