"""Parse only registered policy documents into traceable section chunks."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

import yaml

from packages.domain.hf_download import file_sha256

ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC_DIR = ROOT / "data/synthetic/documents"
PUBLIC_DIR = ROOT / "data/raw/public"
SYNTHETIC_MANIFEST = ROOT / "data/synthetic/manifest.json"
PUBLIC_MANIFEST = ROOT / "data/manifests/public_docs.yaml"
SECTION = re.compile(r"^(?:第\s*[一二三四五六七八九十百千0-9]+\s*条|[0-9]+(?:\.[0-9]+)+|第[一二三四五六七八九十百]+章)")
MAX_CHARS = 900
OVERLAP = 120


@dataclass(frozen=True)
class RegisteredSource:
    doc_id: str
    path: Path
    title: str
    source_type: str
    source_name: str
    source_url: str
    product_code: str | None
    sha256: str


def registered_sources() -> list[RegisteredSource]:
    """Fixed allowlist; never walk the data tree or ingest provenance JSON."""
    synthetic = json.loads(SYNTHETIC_MANIFEST.read_text(encoding="utf-8"))
    public = yaml.safe_load(PUBLIC_MANIFEST.read_text(encoding="utf-8"))["documents"]
    result = []
    for filename, sha in sorted(synthetic["document_sha256"].items()):
        if not re.fullmatch(r"product_[0-9]{3}\.md", filename):
            raise ValueError(f"unexpected synthetic document name: {filename}")
        code = filename[:-3]
        path = SYNTHETIC_DIR / filename
        title = next((line[2:].strip() for line in path.read_text(encoding="utf-8").splitlines()
                      if line.startswith("# ")), code)
        result.append(RegisteredSource(code, path, title,
                                       "synthetic_product", "Synthetic demo product",
                                       "", code, sha))
    for item in public:
        if (item["status"] != "draft" or item["file_type"] not in ("pdf", "docx")
                or not re.fullmatch(r"iachina_[a-z0-9_]+", item["id"])):
            raise ValueError(f"unexpected public document: {item['id']}")
        result.append(RegisteredSource(item["id"],
                                       PUBLIC_DIR / f"{item['id']}.{item['file_type']}",
                                       item["title"], "public_consultation_draft",
                                       item["publisher"], item["source_page"], None,
                                       item["sha256"]))
    return result


def _pages(source: RegisteredSource):
    if source.path.suffix == ".md":
        yield None, source.path.read_text(encoding="utf-8").splitlines()
    elif source.path.suffix == ".docx":
        from docx import Document
        doc = Document(source.path)
        yield None, [paragraph.text for paragraph in doc.paragraphs]
    elif source.path.suffix == ".pdf":
        import pymupdf
        with pymupdf.open(source.path) as doc:
            for index, page in enumerate(doc, start=1):
                yield index, page.get_text().splitlines()
    else:
        raise ValueError(f"unsupported registered source: {source.path}")


def _windows(text: str):
    if len(text) <= MAX_CHARS:
        yield text
        return
    step = MAX_CHARS - OVERLAP
    for start in range(0, len(text), step):
        part = text[start:start + MAX_CHARS]
        if part:
            yield part
        if start + MAX_CHARS >= len(text):
            break


def parse_and_chunk(source: RegisteredSource) -> list[dict]:
    if not source.path.is_file() or file_sha256(source.path) != source.sha256:
        raise ValueError(f"source missing or SHA-256 mismatch: {source.doc_id}")
    chunks = []
    for page, lines in _pages(source):
        section = "前言"
        body = []

        def flush() -> None:
            content = "\n".join(body).strip()
            if not content:
                return
            for part_index, part in enumerate(_windows(content)):
                identity = f"{source.doc_id}|{page}|{section}|{part_index}|{part}"
                chunks.append({"doc_id": source.doc_id,
                               "chunk_id": hashlib.sha256(identity.encode()).hexdigest()[:24],
                               "title": source.title, "section": section, "page": page,
                               "text": part, "source_type": source.source_type,
                               "source_name": source.source_name,
                               "source_url": source.source_url,
                               "product_code": source.product_code,
                               "content_hash": hashlib.sha256(part.encode()).hexdigest()})

        for raw in lines:
            line = " ".join(raw.split())
            if not line:
                continue
            heading = line.startswith("#") or bool(SECTION.match(line))
            if heading:
                flush()
                body = []
                section = line.lstrip("# ")[:160]
            else:
                body.append(line)
        flush()
    if not chunks:
        raise ValueError(f"no parseable content: {source.doc_id}")
    return chunks


def build_knowledge_corpus(output: Path = ROOT / "data/processed/rag/knowledge.jsonl") -> dict:
    sources = registered_sources()
    if len(sources) != 17 or len({item.doc_id for item in sources}) != 17:
        raise ValueError("expected 12 synthetic and 5 public documents")
    output.parent.mkdir(parents=True, exist_ok=True)
    counts = {}
    with output.open("w", encoding="utf-8") as stream:
        for source in sources:
            chunks = parse_and_chunk(source)
            counts[source.doc_id] = len(chunks)
            for chunk in chunks:
                stream.write(json.dumps(chunk, ensure_ascii=False, sort_keys=True) + "\n")
    return {"source_count": len(sources), "chunk_counts": counts,
            "corpus_sha256": file_sha256(output), "corpus_path": str(output.relative_to(ROOT))}
