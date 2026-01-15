# Explanation:

This is a multi-stage citation valigator designed to vaerify academic references from PDF documents (as of now ACM). THis is designed to handell journals, technical standards, books, and grey literature. 

Verifies citations against: OpenAlex (Primary Academic DB), Crossref (DOI Backstop), Semantic Scholar (Grey Literature & Datasets), WG21 (C++ Standards Committee Papers), IETF (Internet RFCs), OpenLibrary (Books/ISBNs)

Uses Grobid for structured extraction and local Ollama (Llama3) for parsing messy/unstructured citations.

# Prerequisites:
Python 3.9+

Docker Desktop (Required for Grobid)

Ollama (Required for AI fallback)

# Installation and Setup:

1. Fork the repository.

2. Clone the repository:

        git clone https://github.com/yourusername/reference_checker.git

        cd reference_checker.git

3. Install Python Dependencies:

        pip install -r requirements.txt

4. Start the Grobid Server (Docker):

        docker run --rm --init -p 8070:8070 -e "JAVA_OPTS=-Xmx4g" lfoppiano/grobid:0.8.1

Wait until you see INFO [org.eclipse.jetty.server.Server]: Started in the terminal.

5. Start Ollama:

Download and install Ollama.


        ollama pull llama3

Ensure Ollama is running (it usually sits in the system tray, or run ollama serve).

# Usage:

## Note that prior to running the flask app, you must add your email to the variable OPENALEX_EMAIL on line 20 of app_3.py.

1. Run the Flask App:

python app_3.py

2. Open your browser to: http://localhost:5000

3. Upload a PDF. The system will:

        Slice the PDF to find reference pages.

        Send the slice to Grobid.

        Parse and verify every reference found.

        Display results with color-coded sources.

# Frontend Color Key:

The results interface uses specific colors to indicate where a reference was found:

| Color | Source | Description |
| :--- | :--- | :--- |
| 🔵 **Blue** | **OpenAlex** | Standard academic journals and papers. |
| 🟣 **Purple** | **WG21** | C++ Standards Committee papers (via `wg21.link`). |
| 🟡 **Yellow** | **Crossref** | Found via DOI backup check. |
| 🟢 **Emerald** | **Semantic Scholar** | Datasets, grey literature, and AI/CS papers. |
| 🌸 **Pink** | **IETF** | Internet Standards (RFCs). |
| 💠 **Cyan** | **OpenLibrary** | Books verified via ISBN. |
# Troubleshooting:

Troubleshooting
1. Grobid is hanging / timing out.

Ensure you are using the -e "JAVA_OPTS=-Xmx4g" flag in your Docker command.

Ensure you are using version 0.8.1 (lfoppiano/grobid:0.8.1), NOT 0.8.0.

2. Ollama Connection Error (WinError 10061).

Ollama is not running. Launch the Ollama application or run ollama serve in a terminal.

3. "Document.insert_pdf" Error.

Ensure you are using the latest pdf_extractor.py included in this repo, which correctly loops through non-contiguous pages when slicing journals.
