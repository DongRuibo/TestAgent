use std::time::Duration;

use async_trait::async_trait;
use reqwest::{
    Client, Method, Request,
    header::{ACCEPT, HeaderValue, RETRY_AFTER},
    redirect::Policy,
};
use serde::{Deserialize, Serialize};

use crate::{
    error::AppError,
    models::SearchResult,
    provider::SearchProvider,
    ranking::{RawSearchResult, normalize_and_deduplicate},
};

const EXA_SEARCH_ENDPOINT: &str = "https://api.exa.ai/search";
const MAX_PROVIDER_BODY_BYTES: usize = 1024 * 1024;

pub struct ExaSearchProvider {
    client: Client,
    api_key: Option<HeaderValue>,
}

impl ExaSearchProvider {
    pub fn new(api_key: Option<String>) -> Result<Self, AppError> {
        let client = Client::builder()
            .connect_timeout(Duration::from_secs(3))
            .timeout(Duration::from_secs(10))
            .redirect(Policy::none())
            .no_proxy()
            .user_agent("TestAgent-WebCapability/0.1")
            .pool_max_idle_per_host(10)
            .build()
            .map_err(|_| AppError::configuration("无法初始化 Exa Search HTTP 客户端"))?;
        let api_key = api_key
            .map(|value| {
                let mut header = HeaderValue::from_str(value.trim())
                    .map_err(|_| AppError::configuration("EXA_API_KEY 包含无效字符"))?;
                header.set_sensitive(true);
                Ok(header)
            })
            .transpose()?;
        Ok(Self { client, api_key })
    }

    fn build_request(
        &self,
        api_key: &HeaderValue,
        query: &str,
        max_results: usize,
    ) -> Result<Request, AppError> {
        self.client
            .request(Method::POST, EXA_SEARCH_ENDPOINT)
            .header("x-api-key", api_key)
            .header(ACCEPT, "application/json")
            .json(&ExaRequest::new(query, max_results))
            .build()
            .map_err(|_| AppError::configuration("无法构造 Exa Search 请求"))
    }
}

#[derive(Debug, Serialize)]
struct ExaRequest<'a> {
    query: &'a str,
    #[serde(rename = "numResults")]
    num_results: usize,
    contents: ExaContents,
}

#[derive(Debug, Serialize)]
struct ExaContents {
    highlights: bool,
}

impl<'a> ExaRequest<'a> {
    fn new(query: &'a str, max_results: usize) -> Self {
        Self {
            query,
            num_results: max_results,
            contents: ExaContents { highlights: true },
        }
    }
}

#[derive(Debug, Deserialize)]
struct ExaResponse {
    #[serde(default)]
    results: Vec<ExaResult>,
}

#[derive(Debug, Deserialize)]
struct ExaResult {
    title: String,
    url: String,
    #[serde(rename = "publishedDate", default)]
    published_at: Option<String>,
    #[serde(default)]
    highlights: Vec<String>,
    #[serde(default)]
    score: Option<f32>,
}

#[async_trait]
impl SearchProvider for ExaSearchProvider {
    fn name(&self) -> &'static str {
        "exa"
    }

    fn configured(&self) -> bool {
        self.api_key.is_some()
    }

    async fn search(&self, query: &str, max_results: usize) -> Result<Vec<SearchResult>, AppError> {
        let api_key = self.api_key.as_ref().ok_or_else(|| {
            AppError::configuration("尚未配置 EXA_API_KEY；fetch 可用，但 search 暂不可用")
        })?;
        let request = self.build_request(api_key, query, max_results)?;
        let mut response = self
            .client
            .execute(request)
            .await
            .map_err(|_| AppError::search_provider("Exa Search 请求失败", true))?;
        if !response.status().is_success() {
            let status = response.status();
            let retry_after_seconds = (status.as_u16() == 429)
                .then(|| {
                    response
                        .headers()
                        .get(RETRY_AFTER)
                        .and_then(parse_retry_after)
                })
                .flatten();
            return Err(AppError::search_provider(
                format!("Exa Search 返回 HTTP {}", status.as_u16()),
                status.as_u16() == 429 || status.is_server_error(),
            )
            .with_retry_after_seconds(retry_after_seconds));
        }
        if response
            .content_length()
            .is_some_and(|length| length > MAX_PROVIDER_BODY_BYTES as u64)
        {
            return Err(AppError::search_provider(
                "Exa Search 响应超过 1 MiB 限制",
                false,
            ));
        }
        let mut body = Vec::new();
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|_| AppError::search_provider("读取 Exa Search 响应失败", true))?
        {
            if body.len().saturating_add(chunk.len()) > MAX_PROVIDER_BODY_BYTES {
                return Err(AppError::search_provider(
                    "Exa Search 响应超过 1 MiB 限制",
                    false,
                ));
            }
            body.extend_from_slice(&chunk);
        }
        let parsed: ExaResponse = serde_json::from_slice(&body)
            .map_err(|_| AppError::search_provider("Exa Search 响应格式无效", false))?;
        let raw = parsed.results.into_iter().map(|item| {
            let mut highlights = item.highlights.into_iter();
            let snippet = highlights.next();
            RawSearchResult {
                title: item.title,
                url: item.url,
                snippet,
                extra_snippets: highlights.take(5).collect(),
                published_at: item.published_at,
                provider_score: item.score,
            }
        });
        Ok(normalize_and_deduplicate(raw, self.name(), max_results))
    }
}

fn parse_retry_after(value: &HeaderValue) -> Option<u64> {
    value.to_str().ok()?.trim().parse().ok()
}

#[cfg(test)]
mod tests {
    use reqwest::header::CONTENT_TYPE;

    use super::*;

    #[test]
    fn request_uses_exa_search_shape_and_sensitive_key_header() {
        let provider = ExaSearchProvider::new(Some("test-key".into())).unwrap();
        let api_key = provider.api_key.as_ref().unwrap();
        let request = provider.build_request(api_key, "rust agent", 5).unwrap();
        assert_eq!(request.method(), Method::POST);
        assert_eq!(request.url().as_str(), EXA_SEARCH_ENDPOINT);
        assert_eq!(request.headers()["x-api-key"], "test-key");
        assert_eq!(request.headers()[CONTENT_TYPE], "application/json");
        let body: serde_json::Value =
            serde_json::from_slice(request.body().unwrap().as_bytes().unwrap()).unwrap();
        assert_eq!(body["query"], "rust agent");
        assert_eq!(body["numResults"], 5);
        assert_eq!(body["contents"]["highlights"], true);
        assert!(!request.url().as_str().contains("test-key"));
    }

    #[test]
    fn response_maps_highlights_and_published_date() {
        let parsed: ExaResponse = serde_json::from_value(serde_json::json!({
            "results": [{
                "title": "Rust releases",
                "url": "https://blog.rust-lang.org/releases/?utm_source=test",
                "publishedDate": "2026-07-01T00:00:00.000Z",
                "highlights": ["Release history", "More details"],
                "score": 0.8
            }]
        }))
        .unwrap();
        let raw = parsed.results.into_iter().map(|item| {
            let mut highlights = item.highlights.into_iter();
            RawSearchResult {
                title: item.title,
                url: item.url,
                snippet: highlights.next(),
                extra_snippets: highlights.collect(),
                published_at: item.published_at,
                provider_score: item.score,
            }
        });
        let results = normalize_and_deduplicate(raw, "exa", 5);
        assert_eq!(results[0].provider, "exa");
        assert_eq!(results[0].snippet.as_deref(), Some("Release history"));
        assert_eq!(
            results[0].published_at.as_deref(),
            Some("2026-07-01T00:00:00.000Z")
        );
    }
}
