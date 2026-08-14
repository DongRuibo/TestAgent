"""LazyTT RAG 只读检索工具的离线协议测试。"""

from __future__ import annotations

from io import BytesIO
from uuid import UUID

import httpx
import pytest

from tools.knowledge_base import KnowledgeBaseClient, KnowledgeBaseConfig


def _config() -> KnowledgeBaseConfig:
    return KnowledgeBaseConfig(
        base_url="http://rag.test/",
        knowledge_base_id="01d66b91-701a-4e33-9f66-877244fae693",
        bearer_token="synthetic-token",
    )


def test_search_knowledge_base_returns_citable_chunks() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/retrieval"
        assert request.headers["authorization"] == "Bearer synthetic-token"
        UUID(request.headers["x-request-id"])
        body = __import__("json").loads(request.content)
        assert body["knowledge_base_ids"] == [_config().knowledge_base_id]
        return httpx.Response(
            200,
            json={
                "data": {
                    "candidates": [
                        {
                            "chunk_id": "chunk-1",
                            "document_id": "document-1",
                            "document_name": "synthetic.md",
                            "content": "The launch code is ORBIT-4827.",
                            "positions": [{"page": 1, "bbox": [0, 0, 1, 1]}],
                            "scores": {"score": 0.9},
                        }
                    ],
                    "warnings": [],
                }
            },
        )

    http_client = httpx.Client(
        base_url="http://rag.test/",
        transport=httpx.MockTransport(handler),
    )
    result = KnowledgeBaseClient(_config(), http_client=http_client).search(
        "  synthetic   launch code  "
    )

    assert result["success"] is True
    assert result["query"] == "synthetic launch code"
    assert result["candidates"][0] == {
        "chunk_id": "chunk-1",
        "document_id": "document-1",
        "document_name": "synthetic.md",
        "pages": [1],
        "content": "The launch code is ORBIT-4827.",
        "score": 0.9,
    }
    assert result["trust"] == "untrusted_knowledge_data"
    assert result["instructions_executable"] is False
    http_client.close()


def test_search_knowledge_base_scopes_retrieval_to_conversation_documents() -> None:
    document_id = "71d66b91-701a-4e33-9f66-877244fae693"
    image_knowledge_base_id = "81d66b91-701a-4e33-9f66-877244fae693"

    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        assert body["document_ids"] == [document_id]
        assert body["knowledge_base_ids"] == [
            _config().knowledge_base_id,
            image_knowledge_base_id,
        ]
        return httpx.Response(200, json={"data": {"candidates": [], "warnings": []}})

    http_client = httpx.Client(
        base_url="http://rag.test/",
        transport=httpx.MockTransport(handler),
    )
    result = KnowledgeBaseClient(_config(), http_client=http_client).search(
        "当前附件",
        knowledge_base_ids=(_config().knowledge_base_id, image_knowledge_base_id),
        document_ids=(document_id,),
    )

    assert result["success"] is True
    http_client.close()


def test_attachment_configuration_requires_all_four_knowledge_bases(
    monkeypatch,
    tmp_path,
) -> None:
    token_file = tmp_path / "rag.jwt"
    token_file.write_text("synthetic-token", encoding="utf-8")
    monkeypatch.setenv("RAG_ENABLED", "true")
    monkeypatch.setenv("RAG_BASE_URL", "http://rag.test")
    monkeypatch.setenv(
        "RAG_KNOWLEDGE_BASE_ID", "01d66b91-701a-4e33-9f66-877244fae693"
    )
    monkeypatch.setenv("RAG_BEARER_TOKEN_FILE", str(token_file))
    for name in (
        "RAG_ATTACHMENT_PDF_KNOWLEDGE_BASE_ID",
        "RAG_ATTACHMENT_OFFICE_KNOWLEDGE_BASE_ID",
        "RAG_ATTACHMENT_IMAGE_KNOWLEDGE_BASE_ID",
        "RAG_ATTACHMENT_MEDIA_KNOWLEDGE_BASE_ID",
    ):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv(
        "RAG_ATTACHMENT_PDF_KNOWLEDGE_BASE_ID",
        "11d66b91-701a-4e33-9f66-877244fae693",
    )

    with pytest.raises(RuntimeError, match="RAG_ATTACHMENT_OFFICE"):
        KnowledgeBaseConfig.from_env()

    monkeypatch.setenv(
        "RAG_ATTACHMENT_OFFICE_KNOWLEDGE_BASE_ID",
        "21d66b91-701a-4e33-9f66-877244fae693",
    )
    monkeypatch.setenv(
        "RAG_ATTACHMENT_IMAGE_KNOWLEDGE_BASE_ID",
        "21d66b91-701a-4e33-9f66-877244fae693",
    )
    monkeypatch.setenv(
        "RAG_ATTACHMENT_MEDIA_KNOWLEDGE_BASE_ID",
        "41d66b91-701a-4e33-9f66-877244fae693",
    )

    with pytest.raises(RuntimeError, match="must be distinct"):
        KnowledgeBaseConfig.from_env()

    monkeypatch.setenv(
        "RAG_ATTACHMENT_IMAGE_KNOWLEDGE_BASE_ID",
        "31d66b91-701a-4e33-9f66-877244fae693",
    )
    configured = KnowledgeBaseConfig.from_env()
    assert configured.attachment_knowledge_bases is not None
    assert configured.attachment_knowledge_bases.for_kind("media") == (
        "41d66b91-701a-4e33-9f66-877244fae693"
    )

    monkeypatch.setenv(
        "RAG_ATTACHMENT_PDF_KNOWLEDGE_BASE_ID",
        "01d66b91-701a-4e33-9f66-877244fae693",
    )
    with pytest.raises(RuntimeError, match="must not reuse"):
        KnowledgeBaseConfig.from_env()


def test_attachment_lifecycle_uses_gateway_contract_without_exposing_token() -> None:
    attachment_id = "81d66b91-701a-4e33-9f66-877244fae693"
    conversation_id = "91d66b91-701a-4e33-9f66-877244fae693"
    document_id = "a1d66b91-701a-4e33-9f66-877244fae693"
    job_id = "b1d66b91-701a-4e33-9f66-877244fae693"
    delete_job_id = "c1d66b91-701a-4e33-9f66-877244fae693"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer synthetic-token"
        if request.method == "POST" and request.url.path.endswith("/documents"):
            assert request.headers["idempotency-key"] == f"attachment-upload-{attachment_id}"
            assert b"synthetic.md" in request.content
            assert b"testagent_chat_attachment" in request.content
            return httpx.Response(
                202,
                json={
                    "request_id": "d1d66b91-701a-4e33-9f66-877244fae693",
                    "data": {
                        "document_id": document_id,
                        "job_id": job_id,
                        "status": "RECEIVED",
                    },
                },
            )
        if request.method == "GET" and request.url.path.endswith(job_id):
            return httpx.Response(200, json={"data": {"status": "SUCCEEDED", "progress": 1}})
        if request.method == "GET" and request.url.path.endswith(document_id):
            return httpx.Response(200, json={"data": {"status": "READY", "progress": 1}})
        if request.method == "DELETE" and request.url.path.endswith(document_id):
            assert request.url.params["cancel_active"] == "true"
            assert request.headers["idempotency-key"] == f"attachment-delete-{attachment_id}"
            return httpx.Response(
                202,
                json={"data": {"job_id": delete_job_id, "status": "PENDING"}},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    http_client = httpx.Client(
        base_url="http://rag.test/",
        transport=httpx.MockTransport(handler),
    )
    client = KnowledgeBaseClient(_config(), http_client=http_client)

    upload = client.upload_document(
        BytesIO(b"# synthetic"),
        filename="synthetic.md",
        content_type="text/markdown",
        attachment_id=attachment_id,
        conversation_id=conversation_id,
    )

    assert upload["document_id"] == document_id
    assert client.get_job(job_id)["data"]["status"] == "SUCCEEDED"
    assert client.get_document(document_id)["data"]["status"] == "READY"
    assert client.delete_document(document_id, attachment_id=attachment_id)["success"] is True
    assert "synthetic-token" not in str(upload)
    http_client.close()


def test_search_knowledge_base_rejects_invalid_query_without_request() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid query must not reach RAG Gateway")

    http_client = httpx.Client(
        base_url="http://rag.test/",
        transport=httpx.MockTransport(handler),
    )
    result = KnowledgeBaseClient(_config(), http_client=http_client).search("   ")

    assert result["success"] is False
    assert result["error_type"] == "INVALID_ARGUMENTS"
    http_client.close()


def test_search_knowledge_base_does_not_expose_upstream_error_body() -> None:
    secret_body = "provider-secret-response"
    http_client = httpx.Client(
        base_url="http://rag.test/",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(401, text=secret_body)
        ),
    )
    result = KnowledgeBaseClient(_config(), http_client=http_client).search("query")

    assert result["error_type"] == "RAG_AUTHENTICATION_ERROR"
    assert secret_body not in result["message"]
    assert "synthetic-token" not in result["message"]
    http_client.close()
