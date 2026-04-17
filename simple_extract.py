#!/usr/bin/env python3
import zipfile
import xml.etree.ElementTree as ET
import re
import os

def extract_text_from_odt(odt_file):
    try:
        with zipfile.ZipFile(odt_file, 'r') as zip_file:
            # Read content.xml
            content_xml = zip_file.read('content.xml')
            
            # Parse XML
            root = ET.fromstring(content_xml)
            
            # Extract all text content
            text_content = []
            
            # Iterate through all elements and extract text
            for elem in root.iter():
                if elem.text and elem.text.strip():
                    text_content.append(elem.text.strip())
            
            return '\n'.join(text_content)
    except Exception as e:
        return f"Error: {str(e)}"

# Test with one file
file_to_test = "Next-patch.odt"
if os.path.exists(file_to_test):
    print(f"Extracting from {file_to_test}...")
    content = extract_text_from_odt(file_to_test)
    print(f"Content length: {len(content)}")
    print("Content:")
    print(content[:2000])  # First 2000 characters
else:
    print(f"File {file_to_test} not found")
