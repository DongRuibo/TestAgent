use axum::{Json, extract::State};

use crate::{AppState, models::HealthResponse};

pub async fn health(State(state): State<AppState>) -> Json<HealthResponse> {
    Json(HealthResponse {
        status: "ok",
        search_configured: state.search_service.configured(),
        benchmark_dns_proxy_enabled: state.fetcher.benchmark_dns_proxy_enabled(),
    })
}
