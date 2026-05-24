import html as html_mod
import logging
import os
import queue
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, UploadFile, File, Header, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)

API_KEY = os.getenv("API_KEY", "")
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "100"))
# Máximo de jobs concluídos/falhos mantidos em memória (os mais antigos
# são descartados automaticamente).
MAX_JOBS = int(os.getenv("MAX_JOBS", "20"))

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)

app = FastAPI(
    title="PDF Extract API",
    description=(
        "API para extração de texto de PDFs com suporte a OCR para "
        "documentos scaneados. O processamento é assíncrono: "
        "envie o PDF via POST `/extract` e acompanhe o resultado "
        "via GET `/job/{job_id}`."
    ),
    version="2.0.0",
)


# ═══════════════════════════════════════════════════════════════════════
# Modelos Pydantic (documentação OpenAPI automática)
# ═══════════════════════════════════════════════════════════════════════


class JobStatus(str, Enum):
    """Status de um job de extração."""

    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class ExtractResponse(BaseModel):
    """Resposta imediata ao criar um job de extração."""

    job_id: str
    status: JobStatus = JobStatus.pending
    message: str = (
        "Job criado. Use GET /job/{job_id} para acompanhar o progresso."
    )


class JobResult(BaseModel):
    """Resultado completo de um job de extração."""

    job_id: str
    status: JobStatus
    filename: str | None = None
    output: str | None = None
    content: str | list | None = None
    error: str | None = None
    created_at: str | None = None


# ═══════════════════════════════════════════════════════════════════════
# Fila de jobs em memória (thread única, ideal para 1 CPU / 512 MB RAM)
# ═══════════════════════════════════════════════════════════════════════

_jobs: dict[str, dict[str, Any]] = {}  # job_id -> job data
_jobs_lock = threading.Lock()
_job_queue: queue.Queue = queue.Queue()
_worker_started = False


def _cleanup_old_jobs() -> None:
    """
    Remove jobs antigos (concluídos ou falhos) quando o número
    acumulado ultrapassa MAX_JOBS. Mantém apenas os mais recentes.
    """
    with _jobs_lock:
        done = [
            (jid, j)
            for jid, j in _jobs.items()
            if j["status"] in (JobStatus.completed, JobStatus.failed)
        ]
        if len(done) > MAX_JOBS:
            done.sort(key=lambda x: x[1].get("created_at", ""), reverse=True)
            for jid, _ in done[MAX_JOBS:]:
                job = _jobs.pop(jid, None)
                # Limpa arquivo temporário se ainda existir
                if job and "file_path" in job:
                    Path(job["file_path"]).unlink(missing_ok=True)


def _job_worker() -> None:
    """
    Worker de background que processa um job por vez.

    Roda em uma thread daemon separada. Pega jobs da fila, processa
    e armazena o resultado no dicionário `_jobs`.
    """
    while True:
        job_id = _job_queue.get()

        try:
            with _jobs_lock:
                job = _jobs.get(job_id)
                if job is None:
                    _job_queue.task_done()
                    continue
                job["status"] = JobStatus.processing

            logger.info("[job %s] Iniciando processamento", job_id)
            _process_job(job_id, job)

        except Exception as exc:
            logger.error("[job %s] Falha interna: %s", job_id, exc)
            with _jobs_lock:
                if job_id in _jobs:
                    _jobs[job_id].update(
                        status=JobStatus.failed, error=f"Erro interno: {exc}"
                    )

        finally:
            _job_queue.task_done()
            _cleanup_old_jobs()


def _start_worker() -> None:
    """Inicia o worker de background uma única vez."""
    global _worker_started
    if not _worker_started:
        _worker_started = True
        t = threading.Thread(target=_job_worker, daemon=True)
        t.start()
        logger.info("Worker de background iniciado")


def _process_job(job_id: str, job: dict[str, Any]) -> None:
    """
    Executa a extração do PDF: OCR (se necessário) + extração de
    texto/tabelas no formato solicitado.

    O resultado é armazenado no dicionário `_jobs[job_id]`.
    O arquivo temporário de upload é removido ao final.
    """
    file_path = job.get("file_path", "")
    filename = job.get("filename", "")
    output_format = job.get("output", "markdown")

    if not file_path or not Path(file_path).exists():
        raise RuntimeError("Arquivo PDF não encontrado no disco")

    try:
        # OCR (aplica OCR apenas se PDF não tiver camada de texto)
        pdf_path = _ensure_ocr(file_path)

        # Extração
        import pdfplumber

        with pdfplumber.open(pdf_path) as pdf:
            pages_data = _extract_pages(pdf)

            if output_format == "markdown":
                extracted = _to_markdown(pages_data)
            elif output_format == "text":
                extracted = _to_text(pages_data)
            elif output_format == "html":
                extracted = _to_html(pages_data)
            else:
                extracted = pages_data

        with _jobs_lock:
            if job_id in _jobs:
                _jobs[job_id].update(
                    status=JobStatus.completed,
                    result_filename=filename,
                    result_output=output_format,
                    result_content=extracted,
                    error=None,
                )

        logger.info(
            "[job %s] Extração concluída (%s, %s caracteres)",
            job_id,
            output_format,
            len(str(extracted)) if extracted else 0,
        )

    except Exception as exc:
        logger.error("[job %s] Erro na extração: %s", job_id, exc)
        with _jobs_lock:
            if job_id in _jobs:
                _jobs[job_id].update(
                    status=JobStatus.failed, error=str(exc)
                )

    finally:
        # Remove arquivo temporário de upload
        Path(file_path).unlink(missing_ok=True)


# ═══════════════════════════════════════════════════════════════════════
# Endpoints
# ═══════════════════════════════════════════════════════════════════════


@app.get("/", include_in_schema=False)
def root():
    return {"ok": True}


@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True, "jobs_pending": _job_queue.qsize()}


@app.post(
    "/extract",
    response_model=ExtractResponse,
    status_code=202,
    summary="Iniciar extração de PDF",
    description=(
        "Envia um arquivo PDF para extração. O processamento é "
        "assíncrono — a resposta contém um `job_id` que deve ser "
        "usado no endpoint `GET /job/{job_id}` para obter o resultado.\n\n"
        "**Formatos de saída:**\n"
        "- `markdown` — texto com cabeçalhos e tabelas formatadas\n"
        "- `text` — apenas texto plano, páginas separadas por `[Página N]`\n"
        "- `html` — HTML básico com tags `<h2>` e `<p>`\n"
        "- `json` — estrutura completa com páginas, texto e tabelas\n\n"
        "PDFs scaneados (sem camada de texto) passam por OCR "
        "automaticamente com Tesseract (idioma: português)."
    ),
    responses={
        202: {
            "description": "Job criado com sucesso",
            "model": ExtractResponse,
        },
        400: {"description": "Arquivo enviado não é um PDF"},
        401: {"description": "API key inválida"},
        413: {"description": "Arquivo excede o tamanho máximo permitido"},
    },
    openapi_extra={
        "requestBody": {
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "file": {
                                "type": "string",
                                "format": "binary",
                                "description": "Arquivo PDF para extração",
                            },
                            "output": {
                                "type": "string",
                                "enum": ["markdown", "text", "html", "json"],
                                "default": "markdown",
                                "description": "Formato de saída desejado",
                            },
                        },
                        "required": ["file"],
                    }
                }
            }
        }
    },
)
async def extract_pdf(
    file: UploadFile = File(..., description="Arquivo PDF para extração"),
    output: Literal["markdown", "text", "html", "json"] = Query(
        "markdown", description="Formato de saída desejado"
    ),
    x_api_key: str | None = Header(
        default=None,
        description="Chave de API (obrigatória se API_KEY estiver configurada)",
    ),
):
    """
    Cria um job de extração de PDF e retorna um `job_id` para
    acompanhamento do processamento.
    """
    # API key
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="API key inválida")

    # Valida extensão
    filename = file.filename or ""
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Envie um arquivo PDF")

    # Valida tamanho
    max_bytes = MAX_FILE_MB * 1024 * 1024
    content = await file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Arquivo maior que {MAX_FILE_MB} MB",
        )

    # Salva em arquivo temporário (evita manter 100 MB na memória do job)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(content)
        file_path = tmp.name

    # Libera o conteúdo da memória o quanto antes
    del content

    # Cria o job
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    with _jobs_lock:
        _jobs[job_id] = {
            "status": JobStatus.pending,
            "filename": filename,
            "output": output,
            "file_path": file_path,
            "created_at": now,
            "result_filename": None,
            "result_output": None,
            "result_content": None,
            "error": None,
        }

    _start_worker()
    _job_queue.put(job_id)

    logger.info("[job %s] Criado — %s, formato=%s", job_id, filename, output)

    return ExtractResponse(job_id=job_id, status=JobStatus.pending)


@app.get(
    "/job/{job_id}",
    response_model=JobResult,
    summary="Consultar resultado da extração",
    description=(
        "Retorna o status e, se concluído, o conteúdo extraído do PDF. "
        "Use o `job_id` retornado pelo endpoint `POST /extract`.\n\n"
        "**Possíveis status:**\n"
        "- `pending` — aguardando processamento\n"
        "- `processing` — sendo processado agora\n"
        "- `completed` — extração concluída, `content` disponível\n"
        "- `failed` — ocorreu um erro, detalhes em `error`"
    ),
    responses={
        200: {"description": "Job encontrado", "model": JobResult},
        404: {"description": "Job não encontrado"},
    },
)
async def get_job(
    job_id: str,
    x_api_key: str | None = Header(
        default=None,
        description="Chave de API (obrigatória se API_KEY estiver configurada)",
    ),
):
    """
    Consulta o status e resultado de um job de extração pelo seu ID.
    """
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="API key inválida")

    with _jobs_lock:
        job = _jobs.get(job_id)

    if job is None:
        raise HTTPException(
            status_code=404,
            detail="Job não encontrado. Jobs concluídos podem ter sido removidos para liberar memória.",
        )

    return JobResult(
        job_id=job_id,
        status=job["status"],
        filename=job.get("result_filename") or job.get("filename"),
        output=job.get("result_output") or job.get("output"),
        content=job.get("result_content"),
        error=job.get("error"),
        created_at=job.get("created_at"),
    )


# ═══════════════════════════════════════════════════════════════════════
# Helpers — OCR e extração
# ═══════════════════════════════════════════════════════════════════════


def _ensure_ocr(path_in: str) -> str:
    """
    Verifica se o PDF tem camada de texto. Se não tiver, aplica OCR.

    Retorna o caminho do PDF com texto (original ou com OCR).

    Parâmetros de performance (configuráveis via env vars):
    - ``OCR_LANG``: idioma do OCR (padrão ``por``)
    - ``TESSERACT_TIMEOUT``: timeout por página em segundos (padrão 120)
    """
    # --- Passo 1: verificar se já tem texto ---
    try:
        import pdfplumber

        with pdfplumber.open(path_in) as pdf:
            total_chars = sum(
                len((page.extract_text() or "")) for page in pdf.pages
            )

        if total_chars > 50:
            logger.info(
                "PDF já possui camada de texto (%d caracteres) — OCR ignorado",
                total_chars,
            )
            return path_in

        logger.info(
            "PDF sem texto (%d caracteres) — aplicando OCR", total_chars
        )

    except Exception as exc:
        logger.warning("Não foi possível verificar texto no PDF: %s", exc)

    # --- Passo 2: aplicar OCR ---
    try:
        import ocrmypdf
    except ImportError:
        logger.warning("ocrmypdf não instalado — OCR indisponível")
        return path_in

    fd_out, path_out = tempfile.mkstemp(suffix=".pdf")
    os.close(fd_out)

    try:
        ocrmypdf.ocr(
            path_in,
            path_out,
            skip_text=True,
            force_ocr=False,
            language=os.getenv("OCR_LANG", "por"),
            progress_bar=False,
            tesseract_timeout=float(os.getenv("TESSERACT_TIMEOUT", "120")),
            max_image_mpixels=int(os.getenv("OCR_MAX_MPIXELS", "10")),
        )
        logger.info("OCR concluído com sucesso")
        return path_out

    except Exception as exc:
        logger.error("OCR falhou: %s", exc, exc_info=True)
        Path(path_out).unlink(missing_ok=True)
        raise HTTPException(
            status_code=422,
            detail=f"Não foi possível extrair texto do PDF. OCR falhou: {exc}",
        )


def _extract_pages(pdf) -> list[dict]:
    """Itera sobre todas as páginas e retorna lista de dicts com texto e tabelas."""
    pages: list[dict] = []
    for page_num, page in enumerate(pdf.pages, start=1):
        text = page.extract_text() or ""

        raw_tables = page.extract_tables()
        tables: list[list[list[str]]] = []
        for table in raw_tables:
            if table:
                tables.append(
                    [
                        [cell if cell is not None else "" for cell in row]
                        for row in table
                    ]
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
    """Converte páginas para Markdown com cabeçalhos e tabelas."""
    parts: list[str] = []
    for page in pages:
        parts.append(f"## Página {page['page']}\n\n{page['text']}")
        for table in page["tables"]:
            if table:
                parts.append(_table_to_markdown(table))
    return "\n\n".join(parts)


def _table_to_markdown(table: list[list[str]]) -> str:
    """Converte uma tabela para formato Markdown."""
    if not table:
        return ""
    sep = "| " + " | ".join("---" for _ in table[0]) + " |"
    rows: list[str] = []
    for row in table:
        rows.append("| " + " | ".join(row) + " |")
    return "\n".join(rows)


def _to_text(pages: list[dict]) -> str:
    """Converte páginas para texto plano."""
    parts: list[str] = []
    for page in pages:
        parts.append(f"[Página {page['page']}]\n{page['text']}")
    return "\n\n".join(parts)


def _to_html(pages: list[dict]) -> str:
    """Converte páginas para HTML básico."""
    parts: list[str] = []
    for page in pages:
        escaped_text = html_mod.escape(page["text"])
        parts.append(f"<h2>Página {page['page']}</h2>\n<p>{escaped_text}</p>")
    return "<html><body>\n" + "\n".join(parts) + "\n</body></html>"


# ═══════════════════════════════════════════════════════════════════════
# Inicialização do servidor (quando executado diretamente)
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        workers=1,
        limit_concurrency=2,
        backlog=4,
        reload=False,
    )
