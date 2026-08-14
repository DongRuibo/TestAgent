use axum::{
    Json,
    extract::{State, rejection::JsonRejection},
    http::HeaderMap,
};

use crate::{
    AppState,
    error::AppError,
    models::{SearchRequest, SearchResponse},
    search::SearchContext,
};

pub async fn search(
    State(state): State<AppState>,
    headers: HeaderMap,
    payload: Result<Json<SearchRequest>, JsonRejection>,
) -> Result<Json<SearchResponse>, AppError> {
    let Json(request) = payload.map_err(|_| {
        AppError::invalid_request("请求体必须是包含 query 和可选 max_results 的 JSON 对象")
    })?;
    let context = SearchContext::from_headers(&headers)?;
    let outcome = state
        .search_service
        .search(&request.query, request.max_results, context)
        .await?;

    Ok(Json(SearchResponse {
        success: true,
        query: outcome.query,
        provider: outcome.provider,
        result_count: outcome.results.len(),
        request_id: outcome.request_id,
        cache_hit: outcome.cache_hit,
        attempts: outcome.attempts,
        elapsed_ms: outcome.elapsed_ms,
        results: outcome.results,
        trust: "untrusted",
        instructions_executable: false,
    }))
}

#[cfg(test)]
mod tests {
    use crate::search::SearchQuery;

    #[test]
    fn enforces_query_character_and_word_limits() {
        assert_eq!(
            SearchQuery::new(&"x".repeat(401), 5)
                .unwrap_err()
                .error_type(),
            "INVALID_REQUEST"
        );
        assert_eq!(
            SearchQuery::new(&(0..51).map(|_| "word").collect::<Vec<_>>().join(" "), 5)
                .unwrap_err()
                .error_type(),
            "INVALID_REQUEST"
        );
        assert!(
            SearchQuery::new(&(0..50).map(|_| "word").collect::<Vec<_>>().join(" "), 20).is_ok()
        );
    }
}
