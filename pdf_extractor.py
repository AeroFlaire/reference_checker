# import os
# import requests
# from lxml import etree
# import re

# GROBID_URL = "http://localhost:8070/api/processFulltextDocument"

# def run_grobid(pdf_path):
#     if not os.path.exists(pdf_path):
#         raise FileNotFoundError(f"PDF file not found: {pdf_path}")
    
#     try:
#         with open(pdf_path, 'rb') as f:
#             return requests.post(GROBID_URL, files={'input': f}, timeout=90).content
#     except Exception as e:
#         print(f"GROBID Connection Error: {e}")
#         return None

# def extract_references(pdf_path):
#     xml_content = run_grobid(pdf_path)
#     if not xml_content: return []

#     try:
#         root = etree.fromstring(xml_content)
#         ns = {"tei": "http://www.tei-c.org/ns/1.0"}
        
#         extracted_data = []

#         for bibl in root.xpath("//tei:listBibl/tei:biblStruct", namespaces=ns):
#             # 1. Get Raw String
#             raw_node = bibl.xpath("./tei:note[@type='raw_reference']/text()", namespaces=ns)
#             raw_text = raw_node[0].strip() if raw_node else ""
            
#             if not raw_text:
#                 texts = bibl.xpath(".//text()", namespaces=ns)
#                 raw_text = " ".join(t.strip() for t in texts)
            
#             # 2. Get Structured DOI
#             doi_nodes = bibl.xpath(".//tei:idno[@type='DOI']/text()", namespaces=ns)
#             if doi_nodes and doi_nodes[0] not in raw_text:
#                 raw_text += f" DOI:{doi_nodes[0]}"

#             # 3. GET STRUCTURED METADATA
#             grobid_title = ""
#             title_nodes = bibl.xpath(".//tei:analytic/tei:title/text()", namespaces=ns)
#             if not title_nodes:
#                 title_nodes = bibl.xpath(".//tei:monogr/tei:title/text()", namespaces=ns)
#             if title_nodes:
#                 grobid_title = title_nodes[0].strip()

#             grobid_author = ""
#             author_nodes = bibl.xpath(".//tei:author/tei:persName/tei:surname/text()", namespaces=ns)
#             if author_nodes:
#                 grobid_author = author_nodes[0].strip()

#             grobid_year = None
#             date_nodes = bibl.xpath(".//tei:date[@type='published']/@when", namespaces=ns)
#             if date_nodes:
#                 grobid_year = date_nodes[0].split("-")[0]

#           # --- UPDATED SUSPICION CHECK ---
#             is_suspicious = False
            
#             # A. Missing Core Data
#             if not grobid_title and not grobid_author:
#                 is_suspicious = True
            
#             # B. Prose / Sentence Detection (The fix for "Boost", "Conan", "CMake")
#             # Real titles rarely use these words in this frequency.
#             # We look for " word " with spaces to avoid matching inside words.
#             prose_triggers = [" is ", " are ", " we ", " you ", " that ", " which ", " to create ", " used to "]
#             lower_text = raw_text.lower()
            
#             if len(raw_text) > 200: # Only check long text
#                 # If it contains prose words, it's likely a paragraph, not a ref.
#                 if any(trigger in lower_text for trigger in prose_triggers):
#                     is_suspicious = True

#             extracted_data.append({
#                 "raw_text": raw_text,
#                 "grobid_title": grobid_title,
#                 "grobid_author": grobid_author,
#                 "grobid_year": grobid_year,
#                 "is_suspicious": is_suspicious
#             })

#         return extracted_data
        
#     except Exception as e:
#         print(f"XML Parsing Error: {e}")
#         return []

import os
import requests
from lxml import etree
import re
import fitz  # PyMuPDF

GROBID_URL = "http://localhost:8070/api/processFulltextDocument"

def create_reference_digest_pdf(original_path):
    """
    Scans EVERY page. Keeps only pages that look like they contain references.
    Works for single articles AND full journals.
    """
    try:
        doc = fitz.open(original_path)
        total_pages = len(doc) # <--- Capture this BEFORE closing the doc
        pages_to_keep = []
        
        # Keywords that strongly signal a reference section start
        header_regex = re.compile(r'(?m)^\s*(?:REFERENCES|BIBLIOGRAPHY|LITERATURE CITED|WORKS CITED)\s*$', re.IGNORECASE)

        for page_num in range(total_pages):
            page = doc[page_num]
            text = page.get_text("text")
            
            score = 0
            
            # 1. HEADER CHECK (+50 Points)
            if header_regex.search(text[:1000]):
                score += 50
                
            # 2. DENSITY CHECK
            markers = len(re.findall(r'(?:\[\d+\]|^\s*\d+\.)', text, re.MULTILINE))
            score += (markers * 2)

            years = len(re.findall(r'\b(19|20)\d{2}[a-z]?\b', text))
            score += (years * 1)
            
            tokens = len(re.findall(r'\b(?:vol|pp|doi|eds|proc|trans)\.', text, re.IGNORECASE))
            score += (tokens * 1)

            # 3. THRESHOLD
            if score > 15:
                pages_to_keep.append(page_num)

        # Safety Fallback
        if len(pages_to_keep) == 0:
            print("  -> Could not detect specific reference pages. Sending full PDF.")
            doc.close()
            return original_path
        
        if len(pages_to_keep) == total_pages:
            doc.close()
            return original_path

        # Create the "Digest" PDF by inserting pages one by one
        new_doc = fitz.open()
        for p_num in pages_to_keep:
            new_doc.insert_pdf(doc, from_page=p_num, to_page=p_num)
            
        digest_path = original_path.replace(".pdf", "_digest.pdf")
        new_doc.save(digest_path)
        new_doc.close()
        doc.close() # <--- Closing doc here is safe now
        
        print(f"  -> Created Reference Digest: {len(pages_to_keep)}/{total_pages} pages kept.")
        return digest_path

    except Exception as e:
        print(f"  -> Error creating digest PDF: {e}")
        return original_path

def run_grobid(pdf_path):
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")
    
    print(f"  -> Sending to GROBID ({os.path.getsize(pdf_path)/1024:.1f} KB)...")
    
    # Retry loop for stability
    for attempt in range(3):
        try:
            with open(pdf_path, 'rb') as f:
                # timeout=(connect_timeout, read_timeout)
                # 5 seconds to connect, 60 seconds to process
                response = requests.post(GROBID_URL, files={'input': f}, timeout=(5, 60))
            
            if response.status_code == 200:
                return response.content
            else:
                print(f"  -> GROBID Error {response.status_code}: {response.text[:100]}")
                return None

        except requests.exceptions.Timeout:
            print(f"  -> GROBID Timed out (Attempt {attempt+1}/3)")
        except requests.exceptions.ConnectionError:
            print(f"  -> GROBID Connection Failed. Is the server running on port 8070?")
            return None # If we can't connect, no point retrying immediately
        except Exception as e:
            print(f"  -> GROBID Request Error: {e}")
            return None
            
    return None

def extract_references(pdf_path):
    # --- SMART INPUT FILTER ---
    # Create a PDF containing ONLY the reference pages
    target_pdf = create_reference_digest_pdf(pdf_path)
    
    xml_content = run_grobid(target_pdf)
    
    # Cleanup temporary file
    if target_pdf != pdf_path and os.path.exists(target_pdf):
        os.remove(target_pdf)
        
    if not xml_content: return []

    try:
        root = etree.fromstring(xml_content)
        ns = {"tei": "http://www.tei-c.org/ns/1.0"}
        
        extracted_data = []

        for bibl in root.xpath("//tei:listBibl/tei:biblStruct", namespaces=ns):
            # 1. Get Raw String
            raw_node = bibl.xpath("./tei:note[@type='raw_reference']/text()", namespaces=ns)
            raw_text = raw_node[0].strip() if raw_node else ""
            
            if not raw_text:
                texts = bibl.xpath(".//text()", namespaces=ns)
                raw_text = " ".join(t.strip() for t in texts)
            
            # 2. Get Structured DOI
            doi_nodes = bibl.xpath(".//tei:idno[@type='DOI']/text()", namespaces=ns)
            if doi_nodes and doi_nodes[0] not in raw_text:
                raw_text += f" DOI:{doi_nodes[0]}"

            # 3. Get Metadata
            grobid_title = ""
            title_nodes = bibl.xpath(".//tei:analytic/tei:title/text()", namespaces=ns)
            if not title_nodes:
                title_nodes = bibl.xpath(".//tei:monogr/tei:title/text()", namespaces=ns)
            if title_nodes:
                grobid_title = title_nodes[0].strip()

            grobid_author = ""
            author_nodes = bibl.xpath(".//tei:author/tei:persName/tei:surname/text()", namespaces=ns)
            if author_nodes:
                grobid_author = author_nodes[0].strip()

            grobid_year = None
            date_nodes = bibl.xpath(".//tei:date[@type='published']/@when", namespaces=ns)
            if date_nodes:
                grobid_year = date_nodes[0].split("-")[0]

            # --- SUSPICION CHECK ---
            is_suspicious = False
            
            # A. Missing Core Data
            if not grobid_title and not grobid_author:
                is_suspicious = True
            
            # B. Prose / Sentence Detection
            prose_triggers = [
                " is ", " are ", " we ", " you ", " that ", " which ", 
                " to create ", " used to ", " its purpose ", " the goal ", 
                " features ", " provides ", " allows ", " designed to ",
                " its advantages "
            ]
            lower_text = raw_text.lower()
            
            if len(raw_text) > 150: 
                if any(trigger in lower_text for trigger in prose_triggers):
                    is_suspicious = True

            # C. Massive text blob check
            if len(raw_text) > 600:
                is_suspicious = True

            extracted_data.append({
                "raw_text": raw_text,
                "grobid_title": grobid_title,
                "grobid_author": grobid_author,
                "grobid_year": grobid_year,
                "is_suspicious": is_suspicious
            })

        return extracted_data
        
    except Exception as e:
        print(f"XML Parsing Error: {e}")
        return []