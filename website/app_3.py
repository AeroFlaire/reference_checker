import os
import json
import requests
import re
import unicodedata 
import difflib
import time
from flask import Flask, jsonify, request, send_file, send_from_directory
from werkzeug.utils import secure_filename

# --- NEW: Import for Parallel Processing ---
import concurrent.futures

# Import the scraper module 
import pdf_extractor

# --- Configuration ---
OLLAMA_MODEL = "llama3"
OLLAMA_HOST = "http://localhost:11434"
OPENALEX_EMAIL = "" #TODO: Make open_alex email and add here.
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'pdf'}

# --- THREADING CONFIG ---
# 3-5 is safe. Higher might crash Ollama or get you blocked by OpenAlex.
MAX_WORKERS = 5 

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024 

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# --- Helper Functions (KEPT EXACTLY AS IS) ---

def normalize_text(text):
    """Converts fancy unicode (like 𝑘) to standard ASCII (like k)."""
    if not text: return ""
    return unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('utf-8')

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def call_ollama(prompt, output_format="json"):
    api_url = f"{OLLAMA_HOST}/api/generate"
    data = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False
    }

    if output_format == "json":
        data["format"] = "json"
    
    try:
        response = requests.post(api_url, json=data)
        response.raise_for_status()
        
        response_data = response.json()
        raw_response = response_data.get("response", "{}").strip()

        if output_format == "json":
            return json.loads(raw_response)
        else:
            return raw_response
        
    except requests.exceptions.RequestException as e:
        print(f"Error calling Ollama: {e}")
        return {"error": f"Ollama request failed: {e}"} if output_format == "json" else "Error"
    except json.JSONDecodeError as e:
        print(f"Ollama returned invalid JSON: {raw_response}")
        return {"error": f"Ollama returned invalid JSON: {e}"}
    
def levenshtein_similarity(s1, s2):
    if not s1 or not s2: return 0
    s1, s2 = s1.lower(), s2.lower()
    
    if len(s1) < len(s2):
        return levenshtein_similarity(s2, s1)

    if len(s2) == 0:
        return 0

    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    
    distance = previous_row[-1]
    max_len = max(len(s1), len(s2))
    return int((1 - distance / max_len) * 100)

def search_openalex(title=None, author=None, year=None, general_search=None):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36'}
    base_url = "https://api.openalex.org/works"
    params = {"select": "id,display_name,authorships,publication_year", "mailto": OPENALEX_EMAIL}

    if general_search:
        params["search"] = general_search
    else:
        filters = []
        if title: filters.append(f"title.search:{title}")
        if author: filters.append(f"raw_author_name.search:{author}")
        if year: filters.append(f"publication_year:{year}")
        if not filters: return []
        params["filter"] = ",".join(filters)

    try:
        response = requests.get(base_url, params=params, headers=headers)
        response.raise_for_status()
        return response.json().get("results", [])
    except:
        return []
    
def search_crossref(query):
    """
    Fallback search using Crossref API.
    """
    # Crossref asks for a 'mailto' to give you faster/polite access
    base_url = "https://api.crossref.org/works"
    params = {
        "query.bibliographic": query,
        "rows": 1,
        "mailto": OPENALEX_EMAIL # Reuse your email here
    }
    
    try:
        resp = requests.get(base_url, params=params, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("message", {}).get("items", [])
            if items:
                return items[0] # Return the top match
    except Exception as e:
        print(f"Crossref Error: {e}")
    return None

def check_wg21_link(ref_string):
    """
    Specialized check for C++ Standards Committee papers.
    Now handles spaces like "P 0380R0".
    """
    # Regex: Matches N1234, P1234, but also P 1234 (common typo in PDFs)
    wg21_regex = re.compile(r'\b([NP])\s*(\d{4}(?:R\d+)?)\b', re.IGNORECASE)
    
    match = wg21_regex.search(ref_string)
    if match:
        # Reconstruct clean ID (remove space if it existed)
        paper_id = f"{match.group(1)}{match.group(2)}".upper()
        link = f"https://wg21.link/{paper_id}"
        
        try:
            resp = requests.head(link, allow_redirects=True, timeout=5)
            if resp.status_code == 200:
                return {
                    "display_name": f"C++ Standard Paper {paper_id}",
                    "publication_year": None, 
                    "id": link,
                    "note": "Verified via WG21.link"
                }
        except: pass
    return None

def check_ietf_rfc(ref_string):
    """
    Checks for IETF Request For Comments (Internet Standards).
    Matches: "RFC 793", "RFC-1234"
    """
    rfc_regex = re.compile(r'\bRFC[\s-]?(\d{1,5})\b', re.IGNORECASE)
    match = rfc_regex.search(ref_string)
    
    if match:
        rfc_id = match.group(1)
        # The official IETF Datatracker URL
        url = f"https://datatracker.ietf.org/doc/rfc{rfc_id}/"
        
        try:
            # HEAD request to check existence
            resp = requests.head(url, timeout=5)
            if resp.status_code == 200:
                return {
                    "display_name": f"IETF RFC {rfc_id}",
                    "publication_year": None, 
                    "id": url,
                    "note": "Verified via IETF Datatracker (Internet Standard)"
                }
        except: pass
    return None

def check_isbn(ref_string):
    """
    Checks for Books.
    Now matches raw ISBN-13 (starting with 978/979) even if 'ISBN' word is missing.
    """
    # Group 1: Optional "ISBN" prefix
    # Group 2: The actual number (starting with 978 or 979)
    isbn_regex = re.compile(r'\b(?:ISBN(?:[:\s]+))?((?:978|979)[0-9-]{10,17})\b', re.IGNORECASE)
    match = isbn_regex.search(ref_string)
    
    if match:
        raw_isbn = match.group(1).replace("-", "").replace(" ", "")
        
        if len(raw_isbn) == 13:
            url = f"https://openlibrary.org/isbn/{raw_isbn}.json"
            try:
                resp = requests.get(url, timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    return {
                        "display_name": data.get("title", f"Book (ISBN {raw_isbn})"),
                        "publication_year": data.get("publish_date", "N/A"),
                        "id": f"https://openlibrary.org/isbn/{raw_isbn}",
                        "note": "Verified via OpenLibrary (Book)"
                    }
            except: pass
    return None

def get_semantic_scholar_paper(paper_id):
    """
    Fetches paper details by ID (DOI, ArXiv) directly from Semantic Scholar.
    paper_id example: "DOI:10.1145/..." or "ARXIV:1705.103"
    """
    url = f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}"
    params = {"fields": "title,authors,year,url,externalIds"}
    try:
        # Respect rate limits
        time.sleep(0.5) 
        resp = requests.get(url, params=params, timeout=5)
        if resp.status_code == 200:
            item = resp.json()
            if not item.get("title"): return None # Safety check
            
            return {
                "display_name": item.get("title"),
                "publication_year": item.get("year"),
                "id": item.get("url"),
                "note": "Verified via Semantic Scholar (ID Match)"
            }
    except: pass
    return None

def search_semantic_scholar(query):
    """
    Searches Semantic Scholar (Good for datasets & grey literature).
    """
    base_url = "https://api.semanticscholar.org/graph/v1/paper/search"
    
    # Semantic Scholar creates keywords from the query, so we keep it simple
    params = {
        "query": query,
        "limit": 1,
        "fields": "title,authors,year,url,externalIds"
    }
    
    try:
        # 1 second sleep to respect free tier rate limits (100 req/5min)
        time.sleep(1.0) 
        resp = requests.get(base_url, params=params, timeout=5)
        
        if resp.status_code == 200:
            data = resp.json()
            if data.get("data"):
                item = data["data"][0]
                
                # Format into an OpenAlex-like structure for consistency
                author_name = "Unknown"
                if item.get("authors"):
                    author_name = item["authors"][0].get("name", "Unknown")
                
                return {
                    "display_name": item.get("title"),
                    "publication_year": item.get("year"),
                    "id": item.get("url"),
                    "note": "Verified via Semantic Scholar"
                }
    except Exception as e:
        print(f"S2 Error: {e}")
        pass
    return None


def check_single_reference(ref_data):
    """
    Worker function: Double-Check Logic.
    1. Try DOI/ArXiv (Instant)
    2. Try GROBID parse (Fast) -> Return if VERIFIED
    3. Try OLLAMA parse (Slow, High Quality) -> Return final result
    """

    # 1. Handle Input
    if isinstance(ref_data, str):
        ref_string = ref_data
        g_title, g_author, g_year = "", "", None
    else:
        ref_string = ref_data.get("raw_text", "")
        g_title = ref_data.get("grobid_title", "")
        g_author = ref_data.get("grobid_author", "")
        g_year = ref_data.get("grobid_year")

    # Define Regex/Prompts
    doi_regex = re.compile(r'\b(10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+)\b')
    arxiv_regex = re.compile(r'arxiv\s*[:\s]\s*(\d{4}\.\d{4,5})', re.IGNORECASE)
    garbage_phrases = ["we propose", "in this paper", "section 3", "section 4"]

    parsing_prompt_template = (
        "You are an expert citation parser. Extract: 1. Title 2. First Author Only. 3. Year. "
        "Respond JSON: {{title, author, year(int)}}. "
        "Ref: {reference_string}"
    )

    ref_string = ref_string.replace("{", "").replace("}", "")
    
    # <--- FIX: Clean header/footer noise instead of rejecting the whole string
    # Remove the specific header phrases found in your PDF
    ref_string = ref_string.replace("Publication date", "")
    
    # Clean up double spaces created by the removal
    ref_string = re.sub(r'\s+', ' ', ref_string).strip()


    if len(ref_string) < 10:
        return {"status": "NOT_REFERENCE", "payload": {"original_reference": ref_string, "note": "Invalid length"}}
    
    # Normalize for fuzzy matching AND regex checks
    norm_ref = ref_string.replace("{", "").replace("}", "")
    norm_ref = normalize_text(norm_ref)
    

    # =========================================================
    # PHASE 0: STANDARDS SNIPER (WG21 / C++)
    # =========================================================
    # We check this FIRST because it is fast and handles drafts 
    # that usually fail the DOI/OpenAlex checks.
    wg21_match = check_wg21_link(norm_ref)
    if wg21_match:
        return {
            "status": "VERIFIED",
            "payload": {
                "original_reference": norm_ref,
                "parsed_query": {"source": "WG21_LINK"},
                "openalex_match": wg21_match
            }
        }
    
    # 2. Check IETF RFCs (Internet Standards)
    rfc_match = check_ietf_rfc(norm_ref)
    if rfc_match:
        return {
            "status": "VERIFIED",
            "payload": {
                "original_reference": norm_ref,
                "parsed_query": {"source": "IETF_RFC"},
                "openalex_match": rfc_match
            }
        }

    # 3. Check ISBNs (Books)
    isbn_match = check_isbn(norm_ref) # Use normalized string for ISBN check
    if isbn_match:
        return {
            "status": "VERIFIED",
            "payload": {
                "original_reference": norm_ref,
                "parsed_query": {"source": "ISBN"},
                "openalex_match": isbn_match
            }
        }
    

    # =========================================================
    # PHASE 1: THE SNIPER (DOI & ArXiv) - WITH S2 BACKUP
    # =========================================================
    
    # 1. Check ArXiv
    arxiv_match = arxiv_regex.search(norm_ref)
    if arxiv_match:
        arxiv_id = arxiv_match.group(1)
        found_match = None  # <--- FIX: Initialize variable here
        
        # A. Try OpenAlex
        try:
            url = f"https://api.openalex.org/works"
            params = {"filter": f"ids.arxiv:{arxiv_id}", "mailto": OPENALEX_EMAIL}
            resp = requests.get(url, params=params)
            if resp.status_code == 200 and resp.json()['results']:
                found_match = resp.json()['results'][0]
        except: pass
        
        # B. Try Semantic Scholar (Backup)
        if not found_match:
            found_match = get_semantic_scholar_paper(f"ARXIV:{arxiv_id}")

        if found_match:
            return {
                "status": "VERIFIED",
                "payload": {
                    "original_reference": ref_string,
                    "parsed_query": {"arxiv": arxiv_id},
                    "openalex_match": found_match
                }
            }

    # 2. Check DOI
    doi_match = doi_regex.search(norm_ref)
    if doi_match:
        raw_doi = doi_match.group(1).rstrip(".,)")
        found_match = None  # <--- FIX: Initialize variable here
        
        # A. Try OpenAlex
        try:
            url = f"https://api.openalex.org/works"
            params = {"filter": f"doi:https://doi.org/{raw_doi}", "mailto": OPENALEX_EMAIL}
            resp = requests.get(url, params=params)
            if resp.status_code == 200 and resp.json()['results']:
                found_match = resp.json()['results'][0]
        except: pass
        
        # B. Try Semantic Scholar (Backup)
        if not found_match:
            found_match = get_semantic_scholar_paper(f"DOI:{raw_doi}")

        if found_match:
            return {
                "status": "VERIFIED",
                "payload": {
                    "original_reference": ref_string,
                    "parsed_query": {"doi": raw_doi},
                    "openalex_match": found_match
                }
            }

    # =========================================================
    # HELPER: The Verification Logic (Reused for both attempts)
    # =========================================================
    def perform_search_and_verify(parsed_title, parsed_author, parsed_year):
        """
        Takes parsed data, searches OpenAlex, and calculates score.
        Returns: (status, best_match, best_flawed)
        """
        # Cleaning
        parsed_title = parsed_title.replace("{", "").replace("}", "")
        parsed_title = normalize_text(parsed_title)
        
    # 3. Remove "Proceedings of" noise from Title 
        clean_title_search = parsed_title.split("Proceedings of")[0].split("IEEE")[0].strip()
        clean_title_search = re.sub(r"[^a-zA-Z0-9\s]", "", clean_title_search)
        
        clean_author = re.sub(r"[^a-zA-Z0-9\s]", "", parsed_author)

        # Python arXiv override
        arxiv_match = re.search(r'arXiv:(\d{2})(\d{2})\.', ref_string)
        if arxiv_match:
            parsed_year = 2000 + int(arxiv_match.group(1))

        # 2. Search Waterfall
        oa_results = []
        if parsed_title or parsed_author:
            # Clean inputs
            clean_title = re.sub(r"[^a-zA-Z0-9\s]", "", parsed_title or "")
            clean_author = re.sub(r"[^a-zA-Z0-9\s]", "", str(parsed_author or "") if not isinstance(parsed_author, list) else parsed_author[0])

            # A. Strict Search
            if clean_title and clean_author:
                oa_results = search_openalex(title=clean_title, author=clean_author)
            
            # B. General (Title Only) - Best Fuzzy
            if not oa_results and parsed_title:
                oa_results = search_openalex(general_search=parsed_title)

            # C. Author Only
            if not oa_results and clean_author:
                oa_results = search_openalex(author=clean_author)

            # D. Strict Title Only
            if not oa_results and clean_title:
                oa_results = search_openalex(title=clean_title)

        # 3. Verification
        status = "NOT_FOUND"
        best_flawed = None
        best_match = None

        if oa_results:
            for match in oa_results[:20]: 
                found_title = match.get("display_name")
                found_year = match.get("publication_year")
                
                # Title Check
                score = levenshtein_similarity(parsed_title, found_title)                                
                # --- RESCUE LOGIC: Check for Substring Match (Fixes Title+Author mash) ---
                if score < 85: # If not a perfect match, check deeper
                    # Normalize both to simple alphanumeric strings
                    pt_flat = re.sub(r'[^a-z0-9]', '', parsed_title.lower())
                    ft_flat = re.sub(r'[^a-z0-9]', '', found_title.lower())
                    
                    # If the found title is fully inside the parsed title (and isn't tiny)
                    if len(ft_flat) > 20 and ft_flat in pt_flat:
                        score = 90 # Boost the score to PASS
                        match["note"] = "Substring Match (Title merged with Authors)"
                # -------------------------------------------------------------------------
                
                if score < 60: continue
                # Calculate Year Gap
                year_gap = 999
                if parsed_year and found_year:
                    try:
                        year_gap = abs(int(parsed_year) - int(found_year))
                    except:
                        pass

                if score >= 95:
                    if year_gap == 0:
                        status = "VERIFIED"
                        best_match = match
                        break 
                    elif year_gap <= 3:
                        status = "VERIFIED" # Allow preprint lag
                        match["note"] = f"Preprint lag ({year_gap} years)"
                        best_match = match
                        break
                    else:
                        # High Score, Big Gap = Edition Mismatch
                        if status != "VERIFIED": 
                            status = "YEAR_MISMATCH"
                            match["note"] = f"Edition Mismatch (Ref: {parsed_year}, Found: {found_year})"
                            best_match = match
                            
                # 2. Flawed Match 
                elif score >= 75 and status != "VERIFIED" and status != "YEAR_MISMATCH":
                    if status == "NOT_FOUND":
                        status = "FLAWED_REFERENCE"
                        best_flawed = match
        
        return status, best_match, best_flawed

    # =========================================================
    # PHASE 2: ATTEMPT 1 - GROBID (Fast Lane)
    # =========================================================
    
    # We only try this if Grobid actually found a title
    if g_title and len(g_title) > 5:
        status, match, flawed = perform_search_and_verify(g_title, g_author, g_year)
        
        # IF IT WORKED -> RETURN IMMEDIATELY (Speed Win!)
        if status == "VERIFIED":
            payload = {
                "original_reference": ref_string,
                "parsed_query": {"title": g_title, "source": "GROBID"},
                "openalex_match": match
            }
            return {"status": "VERIFIED", "payload": payload}
        
        # If it failed, we just silently fall through to PHASE 3...
        # (We don't return "NOT_FOUND" yet, we give Ollama a chance)

    # =========================================================
    # PHASE 3: ATTEMPT 2 - OLLAMA (The Backup / High Quality)
    # =========================================================
    
    lower_ref = ref_string.lower()
    if any(phrase in lower_ref for phrase in garbage_phrases): return None

    try:
        prompt = parsing_prompt_template.format(reference_string=ref_string)
        parsed_data = call_ollama(prompt, "json")
        
        parsed_title = str(parsed_data.get("title", ""))
        parsed_author = str(parsed_data.get("author", ""))
        parsed_year = parsed_data.get("year")
        
        # Fix List outputs from Ollama
        if isinstance(parsed_title, list): parsed_title = " ".join(map(str, parsed_title))
        if isinstance(parsed_author, list): parsed_author = " ".join(map(str, parsed_author))
        
        # Run search logic again with Ollama data
        status, match, flawed = perform_search_and_verify(parsed_title, parsed_author, parsed_year)
        
        payload = {
            "original_reference": ref_string, 
            "parsed_query": {"title": parsed_title, "source": "OLLAMA"}
        }
        
        if status == "VERIFIED":
            payload["openalex_match"] = match
            return {"status": "VERIFIED", "payload": payload}
        elif status == "YEAR_MISMATCH":
            payload["openalex_match (edition mismatch)"] = match
            return {"status": "YEAR_MISMATCH", "payload": payload}
        elif status == "FLAWED_REFERENCE":
            payload["openalex_match (mismatched)"] = flawed
            return {"status": "FLAWED_REFERENCE", "payload": payload}
        else:
            # =========================================================
            # PHASE 3.5: THE FINAL BACKSTOP (Crossref)
            # =========================================================
            # If OpenAlex failed, let's ask Crossref one last time.
            crossref_match = None
            
            # Construct a clean query string
            crossref_query = f"{parsed_title} {parsed_author}"
            
            # Don't search if the query is empty/garbage
            if len(crossref_query) > 10:
                raw_crossref = search_crossref(crossref_query)
                
                if raw_crossref:
                    # Check if the Crossref title matches reasonably well
                    cr_title = raw_crossref.get("title", [""])[0]
                    cr_year = None
                    try:
                        cr_year = raw_crossref.get("published", {}).get("date-parts", [[None]])[0][0]
                    except: pass

                    # Simple validation (Score > 70)
                    score = levenshtein_similarity(parsed_title, cr_title)
                    
                    if score > 95:
                        # Convert Crossref format to match your OpenAlex format for the frontend
                        formatted_match = {
                            "display_name": cr_title,
                            "publication_year": cr_year,
                            "id": raw_crossref.get("URL", "No URL"),
                            "note": "Found via Crossref (Backstop)"
                        }
                        
                        payload["openalex_match"] = formatted_match
                        return {"status": "VERIFIED", "payload": payload}
                    
        # =========================================================
        # PHASE 4: SEMANTIC SCHOLAR (Datasets & Grey Lit)
        # =========================================================
            # 1. Try S2 with the Title + Author (Specific)
            s2_query = f"{parsed_title} {parsed_author}"
            
            # 2. If that's too short, try the raw clean ref (Fuzzy)
            if len(s2_query) < 15:
                s2_query = ref_string[:200] # Limit length for API
            
            s2_match = search_semantic_scholar(s2_query)
            
            if s2_match:
                # Calculate score to ensure it's not a hallucination
                s2_title = normalize_text(s2_match["display_name"])
                score = levenshtein_similarity(parsed_title, s2_title)
                
                # S2 is good, so we trust it with a lower threshold (e.g., 70)
                # OR if the original ref contains the S2 title (good for datasets)
                title_in_ref = s2_title.lower() in ref_string.lower()
                
                if score > 95 or title_in_ref:
                    payload["openalex_match"] = s2_match
                    return {"status": "VERIFIED", "payload": payload}

            # If Crossref also fails, THEN return NOT_FOUND

            # =========================================================
            # PHASE 6: FINAL SUSPICION CHECK
            # =========================================================
            
            # If we are here, we found NO match in any database.
            # Now we check if the extractor thought this looked like garbage.
            is_suspicious = False
            if isinstance(ref_data, dict):
                is_suspicious = ref_data.get("is_suspicious", False)

            if is_suspicious:
                # It failed all DB checks AND looks like noise -> It is likely not a reference.
                return {
                    "status": "NOT_REFERENCE", 
                    "payload": {
                        "original_reference": ref_string, 
                        "note": "Ignored: Unstructured text with no database matches"
                    }
                }

            return {"status": "NOT_FOUND", "payload": payload}

    except Exception as e:
        # <--- CHANGED: Return NOT_REFERENCE if parsing fails critically, or keep as NOT_FOUND with error
        return {"status": "NOT_FOUND", "payload": {"original_reference": ref_string, "error": str(e)}}

def process_references_list(reference_strings):
    """
    Main orchestration function using ThreadPoolExecutor.
    """
    results_verified = []
    results_edition_mismatch = []
    results_flawed = []
    results_not_found = []
    results_not_reference = [] # <--- ADD THIS

    print(f"Total references to check: {len(reference_strings)}")

    start_time = time.time()  # <--- ADD THIS
    
    # --- PARALLEL EXECUTION ---
    # We use ThreadPoolExecutor to run check_single_reference multiple times at once
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Submit all tasks
        future_to_ref = {executor.submit(check_single_reference, ref): ref for ref in reference_strings}
        
        completed_count = 0
        total = len(reference_strings)

        # Gather results as they finish
        for future in concurrent.futures.as_completed(future_to_ref):
            completed_count += 1
            if completed_count % 5 == 0:
                print(f"Progress: {completed_count}/{total} references checked...")
            
            try:
                result = future.result()
                
                # If result is None, it was a skipped reference (noise/garbage)
                if result is None: 
                    continue
                
                status = result["status"]
                payload = result["payload"]

                if status == "VERIFIED":
                    results_verified.append(payload)
                elif status == "YEAR_MISMATCH":
                    results_edition_mismatch.append(payload)
                elif status == "FLAWED_REFERENCE":
                    results_flawed.append(payload)
                elif status == "NOT_REFERENCE": # <--- ADD THIS
                    results_not_reference.append(payload)
                else:
                    results_not_found.append(payload)

            except Exception as exc:
                print(f'Generated an exception in main thread: {exc}')

    end_time = time.time() # <--- ADD THIS
    duration = end_time - start_time # <--- ADD THIS
    print(f"⏱  DONE! Processed {len(reference_strings)} refs in {duration:.2f} seconds.") # <--- ADD THIS
    if len(reference_strings) > 0: # <--- ADD THIS
        print(f"⚡ Average speed: {duration / len(reference_strings):.2f} seconds/ref") # <--- ADD THIS

    return {
        "verified": results_verified, 
        "edition_mismatch": results_edition_mismatch,
        "flawed_reference": results_flawed, 
        "not_found": results_not_found,
        "not_reference": results_not_reference # <--- ADD THIS
    }

# --- Routes ---

@app.route('/')
def home():
    return send_file('index.html')

@app.route("/api/check-references", methods=["POST"])
def api_check_references():
    """Legacy route for raw JSON input"""
    try:
        data = request.get_json()
        results = process_references_list(data.get("references", []))
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/upload-pdf", methods=["POST"])
def upload_pdf():
    """New route for PDF uploads"""
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
        
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        try:
            # 1. Extract references using the separate module
            print(f"Extracting references from {filename}...")
            references = pdf_extractor.extract_references(filepath)
            print(f"Found {len(references)} references. Checking them now (Parallel)...")
            
            # 2. Process them
            results = process_references_list(references)
            
           # 3. Cleanup (optional)
            if os.path.exists(filepath):
                os.remove(filepath)
            
            return jsonify(results)
            
        except Exception as e:
            print(f"Error processing PDF: {e}")
            return jsonify({"error": str(e)}), 500
            
    return jsonify({"error": "Invalid file type"}), 400

if __name__ == '__main__':
    # Threaded=True is important for Flask to handle requests while processing
    app.run(debug=True, port=5000, threaded=True)