import html as html_mod
import os
import tempfile
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, UploadFile, File, Header, HTTPException, Query

API_KEY = os.getenv("API_KEY", "")
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "20"))

app = FastAPI(title="PDF Extract API")


def validate_api_key(x_api_key: str | None):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="API key inválida")


@app.get("/")
def root():
    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/extract")
async def extract_pdf(
    file: UploadFile = File(...),
    output: Literal["markdown", "text", "html", "json"] = Query("markdown"),
    x_api_key: str | None = Header(default=None),
):
    validate_api_key(x_api_key)

    filename = file.filename or ""

    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Envie um arquivo PDF")

    max_bytes = MAX_FILE_MB * 1024 * 1024
    content = await file.read(max_bytes + 1)

    if len(content) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Arquivo maior que {MAX_FILE_MB} MB",
        )

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        import pdfplumber

        with pdfplumber.open(tmp_path) as pdf:
            pages_data = _extract_pages(pdf)

            if output == "markdown":
                extracted = _to_markdown(pages_data)
            elif output == "text":
                extracted = _to_text(pages_data)
            elif output == "html":
                extracted = _to_html(pages_data)
            else:
                extracted = pages_data

        return {
            "filename": filename,
            "output": output,
            "content": extracted,
        }

    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ── helpers ──────────────────────────────────────────────────────────


def _extract_pages(pdf):
    """Iterate over all pages and return a list of dicts with text + tables."""
    pages = []
    for page_num, page in enumerate(pdf.pages, start=1):
        text = page.extract_text() or ""

        raw_tables = page.extract_tables()
        tables = []
        for table in raw_tables:
            if table:
                # Convert None cells to empty string for clean serialization
                tables.append(
                    [[cell if cell is not None else "" for cell in row] for row in table]
                )

        pages.append(
            {
                "page": page_num,
                "text": text,
                "tables": tables,
            }
        )
    return pages


def _to_markdown(pages: list[dict]) -> str:
    parts = []
    for page in pages:
        parts.append(f"## Página {page['page']}\n\n{page['text']}")
        for table in page["tables"]:
            if table:
                parts.append(_table_to_markdown(table))
    return "\n\n".join(parts)


def _table_to_markdown(table: list[list[str]]) -> str:
    if not table:
        return ""
    header = table[0]
    sep = "| " + " | ".join("---" for _ in header) + " |"
    rows = []
    for row in table:
        rows.append("| " + " | ".join(row) + " |")
    return "\n".join(rows)


def _to_text(pages: list[dict]) -> str:
    parts = []
    for page in pages:
        parts.append(f"[Página {page['page']}]\n{page['text']}")
    return "\n\n".join(parts)


def _to_html(pages: list[dict]) -> str:
    parts = []
    for page in pages:
        escaped_text = html_mod.escape(page["text"])
        parts.append(f"<h2>Página {page['page']}</h2>\n<p>{escaped_text}</p>")
    return "<html><body>\n" + "\n".join(parts) + "\n</body></html>"
