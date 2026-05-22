import os
import tempfile
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, UploadFile, File, Header, HTTPException, Query

API_KEY = os.getenv("API_KEY", "")
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "20"))

app = FastAPI(title="PDF Extract API")

_converter = None


def validate_api_key(x_api_key: str | None):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="API key inválida")


def get_converter():
    global _converter

    if _converter is None:
        # Importa e inicializa só no primeiro uso,
        # não durante o boot do container.
        from docling.document_converter import DocumentConverter
        _converter = DocumentConverter()

    return _converter


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
        converter = get_converter()
        result = converter.convert(tmp_path)
        doc = result.document

        if output == "markdown":
            extracted = doc.export_to_markdown()
        elif output == "text":
            extracted = doc.export_to_text()
        elif output == "html":
            extracted = doc.export_to_html()
        else:
            extracted = doc.export_to_dict()

        return {
            "filename": filename,
            "output": output,
            "content": extracted,
        }

    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    finally:
        Path(tmp_path).unlink(missing_ok=True)
