import os
import json
import requests
import re
import difflib # Our objective title checker
from flask import Flask, jsonify, request, send_file, send_from_directory

# --- Configuration ---
OLLAMA_MODEL = "llama3"
OLLAMA_HOST = "http://localhost:11434"
# TODO: Change this to your email
OPENALEX_EMAIL = "arjo456@gmail.com"
# ---------------------

app = Flask(__name__)

# --- Helper Functions ---

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

def search_openalex(title=None, author=None, year=None, general_search=None):
    """
    Searches the OpenAlex API using either specific filters OR a general search query.
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36',
        'Referer': 'https://www.google.com/'
    }

    base_url = "https://api.openalex.org/works"

    # Base parameters
    params = {
        "select": "id,display_name,authorships,publication_year",
        "mailto": OPENALEX_EMAIL
    }

    if general_search:
        params["search"] = general_search
    else:
        filters = []
        if title:
            filters.append(f"title.search:{title}")
        if author:
            filters.append(f"raw_author_name.search:{author}")
        if year:
            filters.append(f"publication_year:{year}")

        if not filters:
            print("Search failed: No valid filters provided.")
            return []

        params["filter"] = ",".join(filters)

    try:
        response = requests.get(base_url, params=params, headers=headers)
        response.raise_for_status()
        data = response.json()
        return data.get("results", [])

    except requests.exceptions.RequestException as e:
        print(f"Error calling OpenAlex: {e}")
        try:
            print(f"API Error Response: {e.response.json()}")
        except:
            pass
        return []

def get_title_similarity(title1, title2):
    """
    Compares two titles and returns a similarity score from 0-100.
    """
    if not title1 or not title2:
        return 0

    # Use SequenceMatcher to get a ratio
    ratio = difflib.SequenceMatcher(None, title1.lower(), title2.lower()).ratio()
    return int(ratio * 100) # Return as a percentage

# --- API Endpoints ---

@app.route('/')
def home():
    return "Reference Checker API is running. POST to /api/check-references"

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory('.', path)

@app.route("/api/check-references", methods=["POST"])
def check_references():
    """
    The main API endpoint for checking references.
    """
    try:
        data = request.get_json()
        reference_strings = data.get("references")

        if not reference_strings or not isinstance(reference_strings, list):
            return jsonify({"error": "Invalid input. Expected a JSON object with a 'references' list."}), 400

        # Create lists for the 3 categories
        results_verified = []
        results_flawed = []
        results_not_found = []

        parsing_prompt_template = (
            "You are an expert citation parser. "
            "Extract the title, first author, and publication year from the following reference. "
            "If an arXiv ID is present (e.g., 'arXiv:1312.6114'), the year is 2013. 'arXiv:1810.04805' is 2018. "
            "**The arXiv year takes priority over any other year in parentheses.** "
            "Respond *only* with a JSON object with 'title', 'author', and 'year' (as an integer) keys. "
            "If a field is not found, set its value to null.\n\n"
            "Reference: {reference_string}"
        )

        verification_prompt_template = (
            "You are a strict year verifier. You must follow these rules:\n"
            "1. Do the 'Original Year' and 'Found Year' match?\n"
            "2. If 'Original Year' is 'N/A' or null, this check automatically **PASSES**.\n"
            "3. If the years are different (e.g., 2016 vs 1995), this check **FAILS**.\n\n"
            "---Original Reference Info---\n"
            "Year: {parsed_year}\n\n"
            "---Found Paper Info---\n"
            "Year: {found_year}\n\n"
            "Respond *only* in this format:\n"
            "year_check: [PASS or FAIL] [Reasoning for year match/mismatch]\n"
            "verdict: [PASS or FAIL]"
        )


        for ref_string in reference_strings:

            # --- AI STEP 1: PARSE REFERENCE ---
            parse_prompt = parsing_prompt_template.format(reference_string=ref_string)
            parsed_data = call_ollama(parse_prompt, output_format="json")

            parsed_title = parsed_data.get("title")
            parsed_author = parsed_data.get("author")
            parsed_year = parsed_data.get("year")

            # --- PYTHON-BASED PARSER OVERRIDE ---
            arxiv_match = re.search(r'arXiv:(\d{2})(\d{2})\.', ref_string)
            if arxiv_match:
                year_digits = int(arxiv_match.group(1)) # e.g., 13 or 18
                arxiv_year = 2000 + year_digits
                if parsed_year != arxiv_year:
                    print(f"--- PARSER OVERRIDE: Found arXiv year {arxiv_year}, overriding AI's parsed year ({parsed_year}) ---")
                    parsed_year = arxiv_year
                    parsed_data["year"] = arxiv_year

            # --- API STEP: SEARCH OPENALEX ---
            oa_results = []
            if "error" in parsed_data or (not parsed_title and not parsed_author):
                print(f"LLM failed to parse, skipping OpenAlex: {ref_string}")
            else:

                author_string = parsed_author
                if isinstance(parsed_author, list) and parsed_author:
                    author_string = parsed_author[0]
                elif not isinstance(parsed_author, str):
                    author_string = ""

                clean_title = re.sub(r"[^a-zA-Z0-9\s]", "", parsed_title or "")
                clean_author = re.sub(r"[^a-zA-Z0-9\s]", "", author_string or "")

                # --- WATERFALL LOGIC (Correct Order) ---

                # Attempt 1: Strict (Title + Author)
                if clean_title and clean_author:
                    oa_results = search_openalex(title=clean_title, author=clean_author)

                # Attempt 2: "Dumb" Search (General `search` with title ONLY)
                if not oa_results and parsed_title:
                    search_query = f"{parsed_title}" # ONLY the title
                    print(f"Strict search failed. Trying fallback (General Search) for '{search_query}'...")
                    oa_results = search_openalex(general_search=search_query)

                # Attempt 3: Fallback (Author Only)
                if not oa_results and clean_author:
                    print(f"General search failed. Trying fallback (Author Only) for '{clean_author}'...")
                    oa_results = search_openalex(author=clean_author)

                # Attempt 4: Fallback (Title Only)
                if not oa_results and clean_title:
                    print(f"Author Only search failed. Trying fallback (Title Only) for '{clean_title}'...")
                    oa_results = search_openalex(title=clean_title)

            # --- VERIFICATION STEP (NEW LOOP LOGIC) ---

            # Initialize status and candidate storage
            final_status = "NOT_FOUND"
            first_flawed_match_candidate = None
            verified_match_candidate = None

            if oa_results:
                # <-- THE FIX IS HERE: Check top 15 results -->
                for match_candidate in oa_results[:15]:
                    found_title = match_candidate.get("display_name")
                    found_year = match_candidate.get("publication_year")

                    # --- PYTHON TITLE CHECK ---
                    title_score = get_title_similarity(parsed_title, found_title)

                    if title_score < 90:
                        print(f"Python Title Check: FAIL (Score: {title_score}) for '{found_title}'")
                        continue # Skip to the next candidate

                    # Title PASSES. It's at least flawed.
                    print(f"Python Title Check: PASS (Score: {title_score}) for '{found_title}'")

                    # Store the *first* flawed match we encounter, in case no verified match is found
                    if final_status == "NOT_FOUND":
                         final_status = "FLAWED_REFERENCE"
                         first_flawed_match_candidate = match_candidate

                    # --- AI YEAR CHECK ---
                    verify_prompt = verification_prompt_template.format(
                        parsed_year=parsed_year or "N/A",
                        found_year=found_year or "N/A"
                    )

                    verification_response = call_ollama(verify_prompt, output_format="text")
                    print(f"AI Year Check:\n{verification_response}\n")

                    year_verdict = "FAIL"
                    match = re.search(r"^\s*verdict:\s*(PASS|FAIL)", verification_response, re.MULTILINE | re.IGNORECASE)

                    if match:
                        year_verdict = match.group(1).upper()

                    if year_verdict == "PASS":
                        # We found a verified match! Store it and stop looping.
                        final_status = "VERIFIED"
                        verified_match_candidate = match_candidate
                        break # Exit the loop early

            # --- APPEND TO CORRECT LIST (Based on final status after loop) ---
            result_payload = {
                "original_reference": ref_string,
                "parsed_query": parsed_data
            }

            if final_status == "VERIFIED":
                result_payload["openalex_match"] = verified_match_candidate
                results_verified.append(result_payload)

            elif final_status == "FLAWED_REFERENCE":
                result_payload["openalex_match (mismatched)"] = first_flawed_match_candidate
                results_flawed.append(result_payload)

            elif final_status == "NOT_FOUND":
                results_not_found.append(result_payload)

        # Return the new grouped JSON object
        return jsonify({
            "verified": results_verified,
            "flawed_reference": results_flawed,
            "not_found": results_not_found
        })

    except Exception as e:
        print(f"An error occurred: {e}")
        import traceback
        traceback.print_exc()

        return jsonify({ "error": str(e) }), 500

@app.route('/health')
def health():
    return jsonify({ "status": "ok" })

if __name__ == '__main__':
    app.run(debug=True, port=5000)