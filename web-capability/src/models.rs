use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

pub const DEFAULT_MAX_RESULTS: usize = 5;
pub const MAX_RESULTS: usize = 20;

#[derive(Debug, Clone, Deserialize)]
pub struct SearchRequest {
    pub query: String,
    #[serde(default = "default_max_results")]
    pub max_results: usize,
}

fn default_max_results() -> usize {
    DEFAULT_MAX_RESULTS
}

#[derive(Debug, Clone, Serialize)]
pub struct SearchResult {
    pub provider: String,
    pub rank: usize,
    pub title: String,
    pub url: String,
    pub snippet: Option<String>,
    pub extra_snippets: Vec<String>,
    pub published_at: Option<String>,
    pub domain: String,
    pub provider_score: Option<f32>,
    pub rerank_score: Option<f32>,
}

#[derive(Debug, Serialize)]
pub struct SearchResponse {
    pub success: bool,
    pub query: String,
    pub provider: String,
    pub result_count: usize,
    pub request_id: String,
    pub cache_hit: bool,
    pub attempts: usize,
    pub elapsed_ms: u64,
    pub results: Vec<SearchResult>,
    pub trust: &'static str,
    pub instructions_executable: bool,
}

#[derive(Debug, Clone, Deserialize)]
pub struct FetchRequest {
    pub url: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct FetchedPage {
    pub requested_url: String,
    pub final_url: String,
    pub status_code: u16,
    pub content_type: String,
    pub title: Option<String>,
    pub text: String,
    pub author: Option<String>,
    pub published_at: Option<String>,
    pub modified_at: Option<String>,
    pub fetch_method: &'static str,
    pub retrieved_at: DateTime<Utc>,
    pub body_hash: String,
    pub content_hash: String,
    pub text_truncated: bool,
    pub extraction_quality: f32,
    pub redirects: Vec<String>,
    pub robots_checked: bool,
    pub robots_urls: Vec<String>,
    pub robots_cache_hits: usize,
    pub resolved_addresses: Vec<String>,
    pub dns_rebinding_protection: &'static str,
    pub trust: &'static str,
    pub instructions_executable: bool,
}

#[derive(Debug, Serialize)]
pub struct FetchResponse {
    pub success: bool,
    #[serde(flatten)]
    pub page: FetchedPage,
}

#[derive(Debug, Serialize)]
pub struct HealthResponse {
    pub status: &'static str,
    pub search_configured: bool,
    pub benchmark_dns_proxy_enabled: bool,
}
