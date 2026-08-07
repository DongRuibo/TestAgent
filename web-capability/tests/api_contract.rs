use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};

use async_trait::async_trait;
use axum::{
    body::{Body, to_bytes},
    http::{Request, StatusCode, header::CONTENT_TYPE},
};
use serde_json::Value;
use tower::ServiceExt;
use web_capability::{
    AppState, build_app,
    error::AppError,
    fetcher::HttpFetcher,
    models::SearchResult,
    production_state,
    provider::{SearchProvider, SearchProviderConfig},
};

struct MockSearchProvider {
    observed_max_results: Arc<AtomicUsize>,
}

#[async_trait]
impl SearchProvider for MockSearchProvider {
    fn name(&self) -> &'static str {
        "mock"
    }

    fn configured(&self) -> bool {
        true
    }

    async fn search(
        &self,
        _query: &str,
        max_results: usize,
    ) -> Result<Vec<SearchResult>, AppError> {
        self.observed_max_results
            .store(max_results, Ordering::Relaxed);
        Ok(vec![SearchResult {
            provider: "mock".into(),
            rank: 1,
            title: "Rust".into(),
            url: "https://www.rust-lang.org/".into(),
            snippet: Some("A language".into()),
            extra_snippets: vec![],
            published_at: None,
            domain: "www.rust-lang.org".into(),
            provider_score: None,
            rerank_score: None,
        }])
    }
}

fn unconfigured_state(allow_benchmark_dns_proxy: bool) -> AppState {
    production_state(
        SearchProviderConfig::Tavily { api_key: None },
        allow_benchmark_dns_proxy,
    )
    .unwrap()
}

async fn body_json(response: axum::response::Response) -> Value {
    let bytes = to_bytes(response.into_body(), 64 * 1024).await.unwrap();
    serde_json::from_slice(&bytes).unwrap()
}

#[tokio::test]
async fn health_works_without_search_key() {
    let app = build_app(unconfigured_state(false));
    let response = app
        .oneshot(Request::get("/health").body(Body::empty()).unwrap())
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        body_json(response).await,
        serde_json::json!({
            "status": "ok",
            "search_configured": false,
            "benchmark_dns_proxy_enabled": false
        })
    );
}

#[tokio::test]
async fn search_without_key_returns_structured_configuration_error() {
    let app = build_app(unconfigured_state(false));
    let response = app
        .oneshot(
            Request::post("/v1/search")
                .header(CONTENT_TYPE, "application/json")
                .body(Body::from(r#"{"query":"Rust async"}"#))
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    let body = body_json(response).await;
    assert_eq!(body["success"], false);
    assert_eq!(body["error_type"], "CONFIGURATION_ERROR");
    assert_eq!(body["retryable"], false);
}

#[tokio::test]
async fn search_success_is_top_level_and_defaults_to_five_results() {
    let observed = Arc::new(AtomicUsize::new(0));
    let state = AppState::new(
        Arc::new(MockSearchProvider {
            observed_max_results: observed.clone(),
        }),
        Arc::new(HttpFetcher::default()),
    );
    let response = build_app(state)
        .oneshot(
            Request::post("/v1/search")
                .header(CONTENT_TYPE, "application/json")
                .header("x-agent-id", "test-agent")
                .header("x-tenant-id", "test-tenant")
                .header("x-request-id", "contract-001")
                .body(Body::from(r#"{"query":"Rust"}"#))
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(observed.load(Ordering::Relaxed), 5);
    let body = body_json(response).await;
    assert_eq!(body["success"], true);
    assert_eq!(body["provider"], "mock");
    assert_eq!(body["result_count"], 1);
    assert_eq!(body["request_id"], "contract-001");
    assert_eq!(body["cache_hit"], false);
    assert_eq!(body["attempts"], 1);
    assert!(body["elapsed_ms"].is_number());
    assert_eq!(body["trust"], "untrusted");
    assert_eq!(body["instructions_executable"], false);
    assert!(body.get("data").is_none());
}

#[tokio::test]
async fn fetch_rejects_localhost_without_network_access() {
    let app = build_app(unconfigured_state(false));
    let response = app
        .oneshot(
            Request::post("/v1/fetch")
                .header(CONTENT_TYPE, "application/json")
                .body(Body::from(r#"{"url":"http://localhost/private"}"#))
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    let body = body_json(response).await;
    assert_eq!(body["success"], false);
    assert_eq!(body["error_type"], "URL_BLOCKED");
    assert_eq!(body["retryable"], false);
}

#[tokio::test]
async fn validates_max_results_before_provider_call() {
    let app = build_app(unconfigured_state(false));
    let response = app
        .oneshot(
            Request::post("/v1/search")
                .header(CONTENT_TYPE, "application/json")
                .body(Body::from(r#"{"query":"Rust","max_results":21}"#))
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    assert_eq!(body_json(response).await["error_type"], "INVALID_REQUEST");
}

#[tokio::test]
async fn unknown_routes_use_the_error_envelope() {
    let app = build_app(unconfigured_state(false));
    let response = app
        .oneshot(Request::get("/missing").body(Body::empty()).unwrap())
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::NOT_FOUND);
    assert_eq!(body_json(response).await["error_type"], "NOT_FOUND");
}

#[tokio::test]
async fn wrong_method_uses_the_error_envelope() {
    let app = build_app(unconfigured_state(false));
    let response = app
        .oneshot(Request::get("/v1/search").body(Body::empty()).unwrap())
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::METHOD_NOT_ALLOWED);
    assert_eq!(
        body_json(response).await["error_type"],
        "METHOD_NOT_ALLOWED"
    );
}

#[tokio::test]
async fn health_reports_benchmark_dns_proxy_opt_in() {
    let app = build_app(unconfigured_state(true));
    let response = app
        .oneshot(Request::get("/health").body(Body::empty()).unwrap())
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        body_json(response).await["benchmark_dns_proxy_enabled"],
        true
    );
}

#[tokio::test]
async fn benchmark_ip_literal_stays_blocked_when_proxy_opt_in_is_enabled() {
    let app = build_app(unconfigured_state(true));
    let response = app
        .oneshot(
            Request::post("/v1/fetch")
                .header(CONTENT_TYPE, "application/json")
                .body(Body::from(r#"{"url":"http://198.18.0.1/"}"#))
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    assert_eq!(body_json(response).await["error_type"], "URL_BLOCKED");
}
