use std::time::Duration;

use async_trait::async_trait;
use reqwest::{
    Client, Method, Request,
    header::{ACCEPT, AUTHORIZATION, HeaderValue, RETRY_AFTER},
    redirect::Policy,
};
use serde::{Deserialize, Serialize};

use crate::{
    error::AppError,
    models::SearchResult,
    provider::SearchProvider,
    ranking::{RawSearchResult, normalize_and_deduplicate},
};

const TAVILY_SEARCH_ENDPOINT: &str = "https://api.tavily.com/search";
const MAX_PROVIDER_BODY_BYTES: usize = 1024 * 1024;

pub struct TavilySearchProvider {
    client: Client,
    authorization: Option<HeaderValue>,
}

impl TavilySearchProvider {
    pub fn new(api_key: Option<String>) -> Result<Self, AppError> {
        let client = Client::builder()
            .connect_timeout(Duration::from_secs(3))
            .timeout(Duration::from_secs(10))
            .redirect(Policy::none())
            // 固定官方端点直接连接，避免环境代理接触服务端密钥。
            .no_proxy()
            .user_agent("TestAgent-WebCapability/0.1")
            .pool_max_idle_per_host(10)
            .build()
            .map_err(|_| AppError::configuration("无法初始化 Tavily Search HTTP 客户端"))?;

        let authorization = api_key
            .map(|value| value.trim().to_owned())
            .filter(|value| !value.is_empty())
            .map(|value| {
                let mut header = HeaderValue::from_str(&format!("Bearer {value}"))
                    .map_err(|_| AppError::configuration("TAVILY_API_KEY 包含无效字符"))?;
                header.set_sensitive(true);
                Ok(header)
            })
            .transpose()?;

        Ok(Self {
            client,
            authorization,
        })
    }

    fn build_request(
        &self,
        authorization: &HeaderValue,
        query: &str,
        max_results: usize,
    ) -> Result<Request, AppError> {
        self.client
            .request(Method::POST, TAVILY_SEARCH_ENDPOINT)
            .header(AUTHORIZATION, authorization)
            .header(ACCEPT, "application/json")
            .json(&TavilyRequest::new(query, max_results))
            .build()
            .map_err(|_| AppError::configuration("无法构造 Tavily Search 请求"))
    }
}

#[derive(Debug, Serialize)]
struct TavilyRequest<'a> {
    query: &'a str,
    search_depth: &'static str,
    topic: &'static str,
    max_results: usize,
    include_answer: bool,
    include_raw_content: bool,
    include_images: bool,
    auto_parameters: bool,
}

impl<'a> TavilyRequest<'a> {
    fn new(query: &'a str, max_results: usize) -> Self {
        Self {
            query,
            search_depth: "basic",
            topic: "general",
            max_results,
            include_answer: false,
            include_raw_content: false,
            include_images: false,
            auto_parameters: false,
        }
    }
}

#[derive(Debug, Deserialize)]
struct TavilyResponse {
    results: Vec<TavilyResult>,
}

#[derive(Debug, Deserialize)]
struct TavilyResult {
    title: String,
    url: String,
    #[serde(default)]
    content: Option<String>,
    #[serde(default)]
    score: Option<f32>,
    #[serde(default)]
    published_date: Option<String>,
}

#[async_trait]
impl SearchProvider for TavilySearchProvider {
    fn name(&self) -> &'static str {
        "tavily"
    }

    fn configured(&self) -> bool {
        self.authorization.is_some()
    }

    async fn search(&self, query: &str, max_results: usize) -> Result<Vec<SearchResult>, AppError> {
        let authorization = self.authorization.as_ref().ok_or_else(|| {
            AppError::configuration("尚未配置 TAVILY_API_KEY；fetch 可用，但 search 暂不可用")
        })?;

        let request = self.build_request(authorization, query, max_results)?;
        let mut response = self
            .client
            .execute(request)
            .await
            .map_err(|_| AppError::search_provider("Tavily Search 请求失败", true))?;

        if !response.status().is_success() {
            let status = response.status();
            let retryable = is_retryable_status(status);
            let retry_after_seconds = (status.as_u16() == 429)
                .then(|| parse_retry_after(response.headers().get(RETRY_AFTER)))
                .flatten();
            return Err(AppError::search_provider(
                format!("Tavily Search 返回 HTTP {}", status.as_u16()),
                retryable,
            )
            .with_retry_after_seconds(retry_after_seconds));
        }

        if response
            .content_length()
            .is_some_and(|length| length > MAX_PROVIDER_BODY_BYTES as u64)
        {
            return Err(AppError::search_provider(
                "Tavily Search 响应超过 1 MiB 限制",
                false,
            ));
        }

        let mut body = Vec::new();
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|_| AppError::search_provider("读取 Tavily Search 响应失败", true))?
        {
            if body.len().saturating_add(chunk.len()) > MAX_PROVIDER_BODY_BYTES {
                return Err(AppError::search_provider(
                    "Tavily Search 响应超过 1 MiB 限制",
                    false,
                ));
            }
            body.extend_from_slice(&chunk);
        }

        let parsed: TavilyResponse = serde_json::from_slice(&body)
            .map_err(|_| AppError::search_provider("Tavily Search 响应格式无效", false))?;

        let raw = parsed.results.into_iter().map(|item| RawSearchResult {
            title: item.title,
            url: item.url,
            snippet: item.content,
            extra_snippets: vec![],
            published_at: item.published_date,
            provider_score: item.score,
        });

        // 仅在内存中规范化并返回，不持久化 Tavily 原始结果或响应。
        Ok(normalize_and_deduplicate(raw, self.name(), max_results))
    }
}

fn parse_retry_after(value: Option<&HeaderValue>) -> Option<u64> {
    value?.to_str().ok()?.trim().parse().ok()
}

fn is_retryable_status(status: reqwest::StatusCode) -> bool {
    status == reqwest::StatusCode::TOO_MANY_REQUESTS || status.is_server_error()
}

#[cfg(test)]
mod tests {
    use reqwest::header::CONTENT_TYPE;

    use super::*;

    #[test]
    fn request_uses_basic_search_without_generated_or_raw_content() {
        let request = serde_json::to_value(TavilyRequest::new("rust agent", 5)).unwrap();

        assert_eq!(request["query"], "rust agent");
        assert_eq!(request["search_depth"], "basic");
        assert_eq!(request["max_results"], 5);
        assert_eq!(request["include_answer"], false);
        assert_eq!(request["include_raw_content"], false);
        assert_eq!(request["include_images"], false);
        assert_eq!(request["auto_parameters"], false);
        assert!(request.get("api_key").is_none());
    }

    #[test]
    fn built_http_request_keeps_key_only_in_sensitive_authorization_header() {
        let provider = TavilySearchProvider::new(Some("test-key".into())).unwrap();
        let authorization = provider.authorization.as_ref().unwrap();
        let request = provider
            .build_request(authorization, "rust agent", 5)
            .unwrap();

        assert_eq!(request.method(), Method::POST);
        assert_eq!(request.url().as_str(), TAVILY_SEARCH_ENDPOINT);
        assert_eq!(request.headers()[AUTHORIZATION], "Bearer test-key");
        assert_eq!(request.headers()[ACCEPT], "application/json");
        assert_eq!(request.headers()[CONTENT_TYPE], "application/json");
        assert!(!request.url().as_str().contains("test-key"));

        let body: serde_json::Value =
            serde_json::from_slice(request.body().unwrap().as_bytes().unwrap()).unwrap();
        assert!(body.get("api_key").is_none());
        assert!(!body.to_string().contains("test-key"));
    }

    #[test]
    fn response_maps_content_score_and_published_date() {
        let parsed: TavilyResponse = serde_json::from_value(serde_json::json!({
            "results": [
                {
                    "title": "Rust releases",
                    "url": "https://blog.rust-lang.org/releases/?utm_source=test",
                    "content": "Release history",
                    "score": 0.91,
                    "published_date": "2026-07-01"
                }
            ]
        }))
        .unwrap();

        let results = normalize_and_deduplicate(
            parsed.results.into_iter().map(|item| RawSearchResult {
                title: item.title,
                url: item.url,
                snippet: item.content,
                extra_snippets: vec![],
                published_at: item.published_date,
                provider_score: item.score,
            }),
            "tavily",
            5,
        );

        assert_eq!(results.len(), 1);
        assert_eq!(results[0].provider, "tavily");
        assert_eq!(results[0].snippet.as_deref(), Some("Release history"));
        assert_eq!(results[0].published_at.as_deref(), Some("2026-07-01"));
        assert_eq!(results[0].provider_score, Some(0.91));
        assert_eq!(results[0].url, "https://blog.rust-lang.org/releases");
    }

    #[tokio::test]
    async fn missing_key_fails_before_network_access() {
        let provider = TavilySearchProvider::new(None).unwrap();

        let error = provider.search("rust", 5).await.unwrap_err();

        assert_eq!(error.error_type(), "CONFIGURATION_ERROR");
    }

    #[tokio::test]
    async fn whitespace_key_is_treated_as_missing() {
        let provider = TavilySearchProvider::new(Some("   ".into())).unwrap();

        assert!(!provider.configured());
        let error = provider.search("rust", 5).await.unwrap_err();
        assert_eq!(error.error_type(), "CONFIGURATION_ERROR");
    }

    #[test]
    fn retry_after_accepts_seconds_and_rejects_invalid_values() {
        assert_eq!(
            parse_retry_after(Some(&HeaderValue::from_static("17"))),
            Some(17)
        );
        assert_eq!(
            parse_retry_after(Some(&HeaderValue::from_static("later"))),
            None
        );
        assert_eq!(parse_retry_after(None), None);
    }

    #[test]
    fn only_rate_limits_and_server_errors_are_retryable() {
        for status in [400, 401, 432, 433] {
            assert!(!is_retryable_status(
                reqwest::StatusCode::from_u16(status).unwrap()
            ));
        }
        for status in [429, 500, 502, 503] {
            assert!(is_retryable_status(
                reqwest::StatusCode::from_u16(status).unwrap()
            ));
        }
    }
}
