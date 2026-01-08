import os
import requests
from lxml import etree
import re

GROBID_URL = "http://localhost:8070/api/processFulltextDocument"

def run_grobid(pdf_path):
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")
    
    # Increase timeout for larger files
    try:
        with open(pdf_path, 'rb') as f:
            return requests.post(GROBID_URL, files={'input': f}, timeout=60).content
    except Exception as e:
        print(f"GROBID Connection Error: {e}")
        return None

def extract_references(pdf_path):
    xml_content = run_grobid(pdf_path)
    if not xml_content: return []

    try:
        root = etree.fromstring(xml_content)
        ns = {"tei": "http://www.tei-c.org/ns/1.0"}
        
        extracted_data = []

        for bibl in root.xpath("//tei:listBibl/tei:biblStruct", namespaces=ns):
            # 1. Get Raw String (Fallback)
            raw_node = bibl.xpath("./tei:note[@type='raw_reference']/text()", namespaces=ns)
            raw_text = raw_node[0].strip() if raw_node else ""
            
            if not raw_text:
                texts = bibl.xpath(".//text()", namespaces=ns)
                raw_text = " ".join(t.strip() for t in texts)
            
            # 2. Get Structured DOI (The Fix you already had)
            doi_nodes = bibl.xpath(".//tei:idno[@type='DOI']/text()", namespaces=ns)
            if doi_nodes and doi_nodes[0] not in raw_text:
                raw_text += f" DOI:{doi_nodes[0]}"

            # 3. GET STRUCTURED METADATA (The Speed Fix)
            # GROBID splits these for us. Let's grab them.
            
            # Title: Try analytic (article title) first, then monogr (book title)
            grobid_title = ""
            title_nodes = bibl.xpath(".//tei:analytic/tei:title/text()", namespaces=ns)
            if not title_nodes:
                title_nodes = bibl.xpath(".//tei:monogr/tei:title/text()", namespaces=ns)
            if title_nodes:
                grobid_title = title_nodes[0].strip()

            # Author: Grab the first surname
            grobid_author = ""
            author_nodes = bibl.xpath(".//tei:author/tei:persName/tei:surname/text()", namespaces=ns)
            if author_nodes:
                grobid_author = author_nodes[0].strip()

            # Year: Grab the date
            grobid_year = None
            date_nodes = bibl.xpath(".//tei:date[@type='published']/@when", namespaces=ns)
            if date_nodes:
                # usually returns "2019-07-12", we just want "2019"
                grobid_year = date_nodes[0].split("-")[0]

            # Return a rich object, not just a string
            extracted_data.append({
                "raw_text": raw_text,
                "grobid_title": grobid_title,
                "grobid_author": grobid_author,
                "grobid_year": grobid_year
            })

        return extracted_data
        
    except Exception as e:
        print(f"XML Parsing Error: {e}")
        return []