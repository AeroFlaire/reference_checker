import os
import json
import requests
import re
import unicodedata  # <--- YOU NEED TO ADD THIS LINE
import difflib
from flask import Flask, jsonify, request, send_file, send_from_directory
from werkzeug.utils import secure_filename
import time
import concurrent.futures
import pdf_extractor 


# --- Configuration ---
OLLAMA_MODEL = "llama3"
OLLAMA_HOST = "http://localhost:11434"
OPENALEX_EMAIL = "" # TODO: Change this
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'pdf'}

MAX_WORKERS = 5

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024 # 500MB limit

# Ensure upload folder exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# --- Helper Functions ---

def normalize_text(text):
    """Converts fancy unicode (like 𝑘) to standard ASCII (like k)."""
    if not text: return ""
    return unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('utf-8')

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def call_ollama(prompt, output_format="json"):
    """
    Sends a prompt to the local Ollama server and gets a response.
    """
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
    """
    Calculates similarity between two strings based on Levenshtein Edit Distance.
    Returns a score between 0 and 100.
    """
    if not s1 or not s2: return 0
    s1, s2 = s1.lower(), s2.lower()
    
    if len(s1) < len(s2):
        return levenshtein_similarity(s2, s1)

    # len(s1) >= len(s2)
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
    
    # Convert distance to similarity percentage
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

# def get_title_similarity(title1, title2):
#     if not title1 or not title2: return 0
#     ratio = difflib.SequenceMatcher(None, title1.lower(), title2.lower()).ratio()
#     return int(ratio * 100)

# --- Core Logic (Refactored) ---

def process_references_list(reference_strings):
    """
    The core logic for checking a list of strings. 
    Includes limits and sleep timers to prevent crashing Ollama.
    """
    results_verified = []
    results_edition_mismatch = []
    results_flawed = []
    results_not_found = []

    # Regex to find DOIs (standard 10.xxxx format)
    doi_regex = re.compile(r'\b(10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+)\b')
    arxiv_regex = re.compile(r'arXiv:(\d{4}\.\d{4,5})', re.IGNORECASE)


    garbage_phrases = ["we propose", "in this paper", "section 3", "section 4", "instead of working"]

    # 1. FIX THE CURLY BRACES IN THE PROMPT (Double braces {{ }} for JSON)
    parsing_prompt_template = (
        "You are an expert citation parser. Extract: 1. Title 2. First Author Only (lastname only is fine). 3. Year. "
        "If arXiv ID exists (e.g. 1312.6114), year is 2013. "
        "Respond JSON: {{title, author, year(int)}}. "
        "Ref: {reference_string}"
    )
    
    # 2. UPDATE VERIFICATION LOGIC (Allow 1 year gap)
    verification_prompt_template = (
        "Compare Ref Year and Found Year. "
        "Rules: "
        "1. If years match exactly -> PASS. "
        "2. If years differ by only 1 year (e.g. 2018 vs 2019) -> YEAR_MISMATCH (allow preprint lag). "
        "3. Otherwise -> FAIL. "
        "Ref Year: {parsed_year}. Found Year: {found_year}. "
        "Respond: verdict: [PASS/FAIL]"
    )

    print(f"Total references found: {len(reference_strings)}")

    start_time = time.time()  # <--- ADD THIS

    # --- SAFETY LIMIT: Only check first 15 for now ---
    # remove [:15] later when you are ready for the full wait
    # reference_strings = reference_strings[:15] 

    for i, ref_string in enumerate(reference_strings):
        print(f"Processing {i+1}/{len(reference_strings)}...")

        # Sanity Check: Skip garbage strings
        if len(ref_string) < 10 or len(ref_string) > 600:
            print("Skipping invalid length reference.")
            continue

        # Remove LaTeX brackets and weird repetition
        ref_string = ref_string.replace("{", "").replace("}", "")

        # 0. Filter Noise (Headers/Footers)
        if len(ref_string) < 10 or "Publication date" in ref_string:
            print("Skipping likely header/footer noise.")
            continue

        ref_string = normalize_text(ref_string)


        doi_match = doi_regex.search(ref_string)
        match_found_via_doi = False


# --- Step 1: The Sniper (DOI & ArXiv) ---
        match_found_via_sniper = False
        
        # A. Check ArXiv First (High Trust)
        arxiv_match = arxiv_regex.search(ref_string)
        if arxiv_match:
            arxiv_id = arxiv_match.group(1)
            print(f"  -> Found ArXiv ID: {arxiv_id}")
            try:
                # Search OpenAlex using the dedicated ArXiv filter
                url = f"https://api.openalex.org/works"
                params = {"filter": f"ids.arxiv:{arxiv_id}", "mailto": OPENALEX_EMAIL}
                resp = requests.get(url, params=params)
                
                if resp.status_code == 200 and resp.json()['results']:
                    item = resp.json()['results'][0]
                    results_verified.append({
                        "original_reference": ref_string,
                        "parsed_query": {"arxiv": arxiv_id},
                        "openalex_match": item
                    })
                    match_found_via_sniper = True
                    print("  -> ArXiv Verified!")
            except Exception:
                pass

        if match_found_via_sniper: continue

        # B. Check DOI (High Trust)
        doi_match = doi_regex.search(ref_string)
        if doi_match:
            raw_doi = doi_match.group(1).rstrip(".,)")
            # 10.5555 is a generic ACM DOI that often fails external lookup. Skip sniping for it.
            if not raw_doi.startswith("10.5555"): 
                print(f"  -> Found DOI: {raw_doi}")
                try:
                    # We use the filter param method which is more robust than the direct ID URL
                    url = f"https://api.openalex.org/works"
                    params = {"filter": f"doi:https://doi.org/{raw_doi}", "mailto": OPENALEX_EMAIL}
                    resp = requests.get(url, params=params)
                    
                    if resp.status_code == 200 and resp.json()['results']:
                        item = resp.json()['results'][0]
                        results_verified.append({
                            "original_reference": ref_string,
                            "parsed_query": {"doi": raw_doi},
                            "openalex_match": item
                        })
                        match_found_via_sniper = True
                        print("  -> DOI Verified!")
                except Exception:
                    pass

        if match_found_via_sniper: continue

        lower_ref = ref_string.lower()
        if any(phrase in lower_ref for phrase in garbage_phrases):
            print("  -> Skipping invalid reference (detected body text/garbage).")
            continue
        if len(ref_string) < 15: 
            continue

         # --- ADD THIS DEBUG PRINT ---
        print(f"DEBUG: Sending this to AI: {ref_string[:100]}...") 
        # ----------------------------


        try:
            # 1. Parse
            prompt = parsing_prompt_template.format(reference_string=ref_string)
            parsed_data = call_ollama(prompt, "json")
            
            # --- ROBUST EXTRACTION (Fixes the "list" crash) ---
            parsed_title = parsed_data.get("title")
            parsed_author = parsed_data.get("author")
            parsed_year = parsed_data.get("year")

            # Force Title to be a string
            if isinstance(parsed_title, list):
                parsed_title = " ".join([str(t) for t in parsed_title])
            elif parsed_title is None:
                parsed_title = ""
            else:
                parsed_title = str(parsed_title)

            # Force Author to be a string
            if isinstance(parsed_author, list):
                parsed_author = " ".join([str(a) for a in parsed_author])
            elif parsed_author is None:
                parsed_author = ""
            else:
                parsed_author = str(parsed_author)

            # 2. Remove BibTeX artifacts ({...})
            parsed_title = parsed_title.replace("{", "").replace("}", "")
            
            # 3. Remove "Proceedings of" noise from Title (Helps find the actual paper)
            # This splits the title and takes the part BEFORE "Proceedings" or "IEEE"
            clean_title_search = parsed_title.split("Proceedings of")[0].split("IEEE")[0].strip()
            clean_title_search = re.sub(r"[^a-zA-Z0-9\s]", "", clean_title_search)
            
            clean_author = re.sub(r"[^a-zA-Z0-9\s]", "", parsed_author)

            # Python arXiv override
            arxiv_match = re.search(r'arXiv:(\d{2})(\d{2})\.', ref_string)
            if arxiv_match:
                parsed_year = 2000 + int(arxiv_match.group(1))
                parsed_data["year"] = parsed_year

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
                    print(f"Trying General Search for: {parsed_title}")
                    oa_results = search_openalex(general_search=parsed_title)

                # C. Author Only
                if not oa_results and clean_author:
                    print(f"Trying Author Search for: {clean_author}")
                    oa_results = search_openalex(author=clean_author)

                # D. Strict Title Only
                if not oa_results and clean_title:
                    print(f"Trying Strict Title Search for: {clean_title}")
                    oa_results = search_openalex(title=clean_title)

            # 3. Verification
            status = "NOT_FOUND"
            best_flawed = None
            verified_match = None

            if oa_results:
                for match in oa_results[:20]: # Only check top 5 matches to save time
                    found_title = match.get("display_name")
                    found_year = match.get("publication_year")
                    
                    # Title Check
                    score = levenshtein_similarity(parsed_title, found_title)                
                    # Filter out bad matches immediately
                    if score < 60: continue 

                    # Calculate Year Gap
                    year_gap = 999
                    if parsed_year and found_year:
                        try:
                            year_gap = abs(int(parsed_year) - int(found_year))
                        except:
                            pass


                    
                    print(f"Title Match ({score}%): {found_title}")


                    if score >= 95:
                        if year_gap == 0:
                            status = "VERIFIED"
                            best_match = match
                            break # Found it, stop looking
                        elif year_gap <= 3:
                            status = "VERIFIED" # Allow preprint lag
                            match["note"] = f"Preprint lag ({year_gap} years)"
                            best_match = match
                            break
                        else:
                            # High Score, Big Gap = Edition Mismatch
                            if status != "VERIFIED": # Only set if we haven't found a better one yet
                                status = "YEAR_MISMATCH"
                                match["note"] = f"Edition Mismatch (Ref: {parsed_year}, Found: {found_year})"
                                best_match = match
                                # Don't break yet, a better year match might be next in the list

                    # 2. Flawed Match (Good title, but not perfect)
                    elif score >= 75 and status != "VERIFIED" and status != "YEAR_MISMATCH":
                        if status == "NOT_FOUND":
                            status = "FLAWED_REFERENCE"
                            best_flawed = match
            
            # 5. Sort into buckets
            payload = {"original_reference": ref_string, "parsed_query": parsed_data}
            
            if status == "VERIFIED":
                payload["openalex_match"] = best_match
                results_verified.append(payload)
            elif status == "YEAR_MISMATCH":
                payload["openalex_match (edition mismatch)"] = best_match
                results_edition_mismatch.append(payload)
            elif status == "FLAWED_REFERENCE":
                payload["openalex_match (mismatched)"] = best_flawed
                results_flawed.append(payload)
            else:
                results_not_found.append(payload)
                # --- END TRY BLOCK ---
        except Exception as e:
            print(f"⚠ Error processing reference {i}: {e}")
            # Optionally append to 'not_found' so it doesn't vanish
            results_not_found.append({"original_reference": ref_string, "error": str(e)})


        # --- CRITICAL SLEEP TIMER ---
        time.sleep(0.1) # 100ms between requests to Ollama

    end_time = time.time() # <--- ADD THIS
    duration = end_time - start_time # <--- ADD THIS
    print(f"⏱  DONE! Processed {len(reference_strings)} refs in {duration:.2f} seconds.") # <--- ADD THIS
    if len(reference_strings) > 0: # <--- ADD THIS
        print(f"⚡ Average speed: {duration / len(reference_strings):.2f} seconds/ref") # <--- ADD THIS

    return {
        "verified": results_verified, 
        "edition_mismatch": results_edition_mismatch,
        "flawed_reference": results_flawed, 
        "not_found": results_not_found
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
            print(f"Found {len(references)} references. Checking them now...")
            
            # 2. Process them
            results = process_references_list(references)
            
           # 3. Cleanup (optional)
            # Only delete if the file still exists (prevents crashes on double-requests)
            if os.path.exists(filepath):
                os.remove(filepath)
            
            return jsonify(results)
            
        except Exception as e:
            print(f"Error processing PDF: {e}")
            return jsonify({"error": str(e)}), 500
            
    return jsonify({"error": "Invalid file type"}), 400

if __name__ == '__main__':
    app.run(debug=True, port=5000)