use axum::{Json, extract::State, extract::rejection::JsonRejection};

use crate::{
    AppState,
    error::AppError,
    models::{FetchRequest, FetchResponse},
};

const MAX_URL_CHARS: usize = 8_192;

pub async fn fetch(
    State(state): State<AppState>,
    payload: Result<Json<FetchRequest>, JsonRejection>,
) -> Result<Json<FetchResponse>, AppError> {
    let Json(request) =
        payload.map_err(|_| AppError::invalid_request("请求体必须是包含 url 的 JSON 对象"))?;
    if request.url.trim().is_empty() {
        return Err(AppError::invalid_request("url 不能为空"));
    }
    if request.url.chars().count() > MAX_URL_CHARS {
        return Err(AppError::invalid_request("url 不能超过 8192 个字符"));
    }

    let page = state.fetcher.fetch(&request.url).await?;
    Ok(Json(FetchResponse {
        success: true,
        page,
    }))
}
