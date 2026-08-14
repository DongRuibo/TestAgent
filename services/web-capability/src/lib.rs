pub mod api;
pub mod config;
pub mod error;
pub mod extractor;
pub mod fetcher;
pub mod models;
pub mod provider;
pub mod ranking;
pub mod search;

use std::sync::Arc;

use axum::{
    Router,
    extract::DefaultBodyLimit,
    routing::{get, post},
};

use api::{fetch::fetch, health::health, method_not_allowed, not_found, search::search};
use fetcher::{HttpFetcher, robots::RobotsConfig};
use provider::{SearchProvider, SearchProviderConfig, build_search_providers};
use search::{SearchPolicy, SearchService};

pub const SERVICE_NAME: &str = "web-capability";

#[derive(Clone)]
pub struct AppState {
    pub search_service: Arc<SearchService>,
    pub fetcher: Arc<HttpFetcher>,
}

impl AppState {
    pub fn new(search_provider: Arc<dyn SearchProvider>, fetcher: Arc<HttpFetcher>) -> Self {
        Self::with_search_service(
            Arc::new(SearchService::from_provider(search_provider)),
            fetcher,
        )
    }

    pub fn with_search_service(
        search_service: Arc<SearchService>,
        fetcher: Arc<HttpFetcher>,
    ) -> Self {
        Self {
            search_service,
            fetcher,
        }
    }
}

pub fn build_app(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/v1/search", post(search))
        .route("/v1/fetch", post(fetch))
        .fallback(not_found)
        .method_not_allowed_fallback(method_not_allowed)
        .layer(DefaultBodyLimit::max(16 * 1024))
        .with_state(state)
}

pub fn production_state(
    search_provider: SearchProviderConfig,
    allow_benchmark_dns_proxy: bool,
) -> Result<AppState, error::AppError> {
    production_state_with_providers(
        [search_provider],
        allow_benchmark_dns_proxy,
        SearchPolicy::default(),
    )
}

pub fn production_state_with_providers(
    providers: impl IntoIterator<Item = SearchProviderConfig>,
    allow_benchmark_dns_proxy: bool,
    policy: SearchPolicy,
) -> Result<AppState, error::AppError> {
    production_state_with_providers_and_fetch_config(
        providers,
        allow_benchmark_dns_proxy,
        policy,
        RobotsConfig::default(),
    )
}

pub fn production_state_with_providers_and_fetch_config(
    providers: impl IntoIterator<Item = SearchProviderConfig>,
    allow_benchmark_dns_proxy: bool,
    policy: SearchPolicy,
    robots_config: RobotsConfig,
) -> Result<AppState, error::AppError> {
    robots_config.validate()?;
    let providers = build_search_providers(providers)?;
    let search_service = SearchService::new(providers, policy, None, None)?;
    Ok(AppState::with_search_service(
        Arc::new(search_service),
        Arc::new(HttpFetcher::new_with_robots_config(
            allow_benchmark_dns_proxy,
            robots_config,
        )),
    ))
}
