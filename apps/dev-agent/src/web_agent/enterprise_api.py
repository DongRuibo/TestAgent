"""企业档案的用户管理入口；文档上传继续使用现有知识库 API。"""
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from uuid import UUID

from web_agent.enterprise import EnterpriseUpdate


class DocumentDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_ids: list[UUID] = Field(min_length=1, max_length=10)


def register_enterprise_api(app, get_enterprise, open_gateway, require_result):
    def ready_documents(gateway, ids):
        requested = {str(value) for value in ids}
        # 与页面同样只列首 100 份文档；不接受任意路径或其他知识库的 ID。
        documents = require_result(gateway.list_documents(page=1, page_size=100))["documents"]
        selected = [item for item in documents if item["document_id"] in requested and item["status"] == "READY"]
        if {item["document_id"] for item in selected} != requested:
            raise HTTPException(409, detail={"message": "所选文档不存在或尚未解析完成，请刷新文档列表。"})
        return [{"document_id": item["document_id"], "filename": item["filename"]} for item in selected]

    @app.get("/api/enterprise")
    def get_profile():
        return {"profile": get_enterprise().profile()}

    @app.put("/api/enterprise")
    def save_profile(request: EnterpriseUpdate):
        documents = []
        if request.document_ids:
            gateway = open_gateway()
            try:
                documents = ready_documents(gateway, request.document_ids)
            finally:
                gateway.close()
        try:
            return {"profile": get_enterprise().save(request, documents)}
        except ValueError as exc:
            raise HTTPException(409, detail={"message": str(exc)}) from exc

    @app.post("/api/enterprise/document-draft")
    def document_draft(request: DocumentDraftRequest):
        gateway = open_gateway()
        try:
            documents = ready_documents(gateway, request.document_ids)
            result = require_result(gateway.search("企业名称、主营产品服务、服务区域、目标客户、核心优势、品牌定位与业务边界",
                document_ids=tuple(item["document_id"] for item in documents)))
        finally:
            gateway.close()
        ids = {item["document_id"] for item in documents}
        chunks = [item for item in result.get("candidates", []) if item.get("document_id") in ids]
        if not chunks:
            raise HTTPException(409, detail={"message": "没有检索到所选文档的可用片段，请手动填写，或检查解析结果。"})
        # 仅提取有来源的片段供用户整理，不将局部检索伪装成完整文档总结。
        draft = "\n\n".join(f"【{item['document_name']} · 待核实片段】\n{item['content']}" for item in chunks)
        return {"draft": draft[:6000], "partial": True,
            "message": "这是检索片段，不是全文总结。请整理、核实后保存；尚未修改企业档案。"}
